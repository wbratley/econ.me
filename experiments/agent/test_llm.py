"""The NIM rate limiter: one shared per-minute budget for the whole run.

Not a mock-heavy test — the limiter is 20 lines of monotonic-clock
arithmetic, so we test exactly that: bursts pass until the window is
full, the next call waits for the oldest entry to age out, and the
429-retry path consumes budget too (it is, after all, another request).
"""

import json
import time

import pytest

from experiments.agent.llm import (
    _RateLimiter, _final_content, NimModel, OpenAIModel,
)


def test_bursts_pass_then_wait():
    lim = _RateLimiter(3, window=0.4)
    t0 = time.monotonic()
    for _ in range(3):
        lim.wait()                          # a burst of 3 goes straight out
    assert time.monotonic() - t0 < 0.2
    lim.wait()                              # the 4th waits for the window
    assert time.monotonic() - t0 >= 0.35    # oldest entry aged out first


def test_window_slides():
    lim = _RateLimiter(1, window=0.3)
    lim.wait()
    time.sleep(0.35)
    t0 = time.monotonic()
    lim.wait()                              # window already drained — no wait
    assert time.monotonic() - t0 < 0.2
    assert len(lim._times) == 1             # only the fresh call recorded


# ===========================================================================
# The reasoning-model seam: thinking on, thinking ignored
# ===========================================================================

class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Stream:
    """Stands in for httpx.stream(...)'s context manager: status code,
    SSE body lines, and the read() the 429/5xx branch does."""

    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = list(lines)

    def read(self):
        return b""

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_final_content_takes_the_answer_not_the_reasoning():
    body = {"choices": [{"finish_reason": "stop", "message": {
        "content": "ctx.state.plan = std.amount_str(1)",
        "reasoning_content": "the user wants a Lua behaviour; let me plan",
        "reasoning": {"content": "nested variant some providers send"},
    }}]}
    assert _final_content(body) == "ctx.state.plan = std.amount_str(1)"


def test_budget_starved_reasoning_raises_with_the_cause():
    # gpt-oss-20b against a real prompt: 4096 completion tokens, all spent
    # thinking, content comes back null with finish_reason=length
    body = {"choices": [{"finish_reason": "length", "message": {
        "content": None, "reasoning_content": "16k chars of thinking",
    }}]}
    with pytest.raises(RuntimeError, match="finish_reason=length"):
        _final_content(body)
    with pytest.raises(RuntimeError, match="raise max_tokens"):
        _final_content(body)


def test_openai_complete_ignores_reasoning_channel(monkeypatch):
    import httpx
    body = {"choices": [{"finish_reason": "stop", "message": {
        "content": "final", "reasoning_content": "ignored",
    }}]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(body))
    assert OpenAIModel("k").complete("s", "u") == "final"


def test_nim_default_budget_covers_reasoning():
    # the regression: 4096 shared by thinking + answer starved gpt-oss —
    # the cap is not a target, so doubling it costs nothing when unused
    assert NimModel("k", "openai/gpt-oss-20b")._max_tokens >= 8192


def test_nim_reasoning_models_get_a_bigger_default():
    # stone-run3: gpt-oss finished `length` with empty content 7 rounds
    # in — thinking ate the whole 8192 before the answer started. The
    # per-family default budgets reasoning models generously; plain
    # instruct models keep the 8192 that has always sufficed.
    assert NimModel("k", "openai/gpt-oss-20b")._max_tokens == 65536
    assert NimModel("k", "meta/llama-3.3-70b-instruct")._max_tokens == 8192
    # explicit argument still wins over any family default
    assert NimModel("k", "openai/gpt-oss-20b",
                    max_tokens=1234)._max_tokens == 1234


def test_nim_complete_raises_on_null_content(monkeypatch):
    # the wiring gap, seen live twice (nim-run2 r4, nim-run3 r2 under a
    # machine suspend): a null final channel leaked into the loop and
    # died far from the cause as `'NoneType' object has no attribute
    # .strip'`. The empty-final guard must raise with the
    # finish_reason in the message — and must NOT burn transport
    # retries on an empty answer.
    import httpx
    posts = []

    def fake_stream(*a, **k):
        posts.append(k)
        return _Stream(200, [
            'data: {"choices": [{"delta": '
            '{"reasoning_content": "thinking..."}}]}',
            'data: {"choices": [{"finish_reason": "length", "delta": {}}]}',
            'data: [DONE]',
        ])

    monkeypatch.setattr(httpx, "stream", fake_stream)
    with pytest.raises(RuntimeError, match="finish_reason=length"):
        NimModel("k", "openai/gpt-oss-20b").complete("s", "u")
    assert len(posts) == 1          # raised out, not retried in-loop


def test_nim_complete_streams_and_joins_deltas(monkeypatch):
    # run 21: non-streaming + the 32k budget + a 120s read timeout =
    # every long authoring call timed out before a byte arrived, and
    # the houses ran their round-1 scripts forever. complete() must
    # stream, and assemble the final channel from the deltas.
    import httpx
    posts = []

    def fake_stream(*a, **k):
        posts.append(k)
        return _Stream(200, [
            'data: {"choices": [{"delta": {"reasoning_content": "hm"}}]}',
            'data: {"choices": [{"delta": {"content": "return "}}]}',
            'data: {"choices": [{"delta": {"content": "7"}}]}',
            'data: {"choices": [{"finish_reason": "stop", "delta": {}}]}',
            'data: [DONE]',
        ])

    monkeypatch.setattr(httpx, "stream", fake_stream)
    out = NimModel("k", "openai/gpt-oss-20b").complete("s", "u")
    assert out == "return 7"
    assert posts[0]["json"]["stream"] is True   # the actual fix


def test_nim_complete_retries_429_then_succeeds(monkeypatch):
    import httpx
    posts = []
    responses = [
        _Stream(429, []),
        _Stream(200, [
            'data: {"choices": [{"delta": {"content": "ok"}}]}',
            'data: [DONE]',
        ]),
    ]

    def fake_stream(*a, **k):
        posts.append(k)
        return responses[len(posts) - 1]

    monkeypatch.setattr(httpx, "stream", fake_stream)
    out = NimModel("k", "openai/gpt-oss-20b").complete("s", "u")
    assert out == "ok" and len(posts) == 2


# --- FileModel: the live fourth seat (exhibition run 25) ---------------------

def test_file_model_roundtrip(tmp_path):
    """The rendezvous contract: prompt file appears, player answers
    atomically, the model returns the answer and cleans up both files."""
    import threading
    from experiments.agent.llm import FileModel

    m = FileModel("House Excalibur", tmp_path, poll_s=0.02, timeout_s=10)
    prompt = tmp_path / "seat-house-excalibur.prompt.md"
    answer = tmp_path / "seat-house-excalibur.response.txt"

    def player():
        import time
        for _ in range(500):
            if prompt.exists():
                break
            time.sleep(0.02)
        assert prompt.exists(), "player never saw the prompt"
        tmp = answer.with_suffix(".tmp")
        tmp.write_text("KEEP")
        tmp.replace(answer)

    t = threading.Thread(target=player)
    t.start()
    assert m.complete("be the house", "the world, round 1") == "KEEP"
    t.join()
    assert not answer.exists() and not prompt.exists()


def test_file_model_prompt_carries_both_halves(tmp_path):
    from experiments.agent.llm import FileModel

    m = FileModel("House Excalibur", tmp_path, poll_s=0.02, timeout_s=0.2)
    import pytest
    from experiments.agent.llm import LiveSeatTimeout
    with pytest.raises(LiveSeatTimeout):
        m.complete("SYSTEM TEXT", "USER TEXT")
    body = (tmp_path / "seat-house-excalibur.prompt.md")
    # timed out — but the NEXT call re-issues its own prompt
    with pytest.raises(LiveSeatTimeout):
        m.complete("SYSTEM TEXT", "USER TEXT")
    text = body.read_text()
    assert "===== SYSTEM =====" in text and "SYSTEM TEXT" in text
    assert "===== USER =====" in text and "USER TEXT" in text
    assert "call 2" in text


def test_file_model_drops_stale_response(tmp_path):
    """A response left by a dead cycle must never leak into the next
    call — the seat waits for a FRESH answer, not a ghost."""
    import threading, time
    from experiments.agent.llm import FileModel

    m = FileModel("House Excalibur", tmp_path, poll_s=0.02, timeout_s=10)
    prompt = tmp_path / "seat-house-excalibur.prompt.md"
    stale = tmp_path / "seat-house-excalibur.response.txt"
    stale.write_text("GHOST FROM A DEAD CYCLE")

    def player():
        while not prompt.exists():
            time.sleep(0.01)
        time.sleep(0.1)              # let complete() enter its wait loop
        tmp = stale.with_suffix(".tmp")
        tmp.write_text("FRESH ANSWER")
        tmp.replace(stale)

    t = threading.Thread(target=player)
    t.start()
    assert m.complete("s", "u") == "FRESH ANSWER"
    t.join()


# --- DeepSeek: the prepaid seat and its billing gate -------------------------

def test_deepseek_key_env_first_then_home_file(monkeypatch, tmp_path):
    import experiments.agent.llm as llm
    assert llm.deepseek_key({}) is None                 # hermetic: nothing
    assert llm.deepseek_key({"DEEPSEEK_API_KEY": " k \n"}) == "k"
    monkeypatch.setattr(llm.Path, "home", lambda: tmp_path)
    (tmp_path / ".deepseek_api_key").write_text("sk-file\nmore\n")
    assert llm.deepseek_key() == "sk-file"              # first line only


def test_deepseek_offpeak_is_the_beijing_billing_clock():
    """Weekdays 00:30–08:30 Beijing inclusive; weekends all day (the
    2026-08-23 rule change). Beijing is UTC+8, so the tests pin UTC
    instants on the boundary and let the conversion do the talking."""
    from datetime import datetime, timezone
    from experiments.agent.llm import deepseek_offpeak
    utc = timezone.utc
    # Sat + Sun 2026-09-05/06: off-peak around the clock (Sunday
    # midnight Beijing = Saturday 16:00 UTC)
    assert deepseek_offpeak(datetime(2026, 9, 5, 4, 0, tzinfo=utc))   # Sat noon
    assert deepseek_offpeak(datetime(2026, 9, 5, 16, 0, tzinfo=utc))  # Sun 00:00
    # Monday 2026-09-07: 00:29 shut, 00:30 open, 08:29 open, 08:30 shut
    assert not deepseek_offpeak(datetime(2026, 9, 6, 16, 29, tzinfo=utc))
    assert deepseek_offpeak(datetime(2026, 9, 6, 16, 30, tzinfo=utc))
    assert deepseek_offpeak(datetime(2026, 9, 7, 0, 29, tzinfo=utc))
    assert not deepseek_offpeak(datetime(2026, 9, 7, 0, 30, tzinfo=utc))


def test_deepseek_minutes_to_window_finds_the_next_open():
    from datetime import datetime, timezone
    from experiments.agent.llm import _deepseek_minutes_to_window
    utc = timezone.utc
    # Friday 19:00 Beijing (peak): tomorrow is Saturday, so the window
    # opens at MIDNIGHT, not 00:30 — 5h exactly
    assert _deepseek_minutes_to_window(
        datetime(2026, 9, 4, 11, 0, tzinfo=utc)) == 300.0
    # Monday 09:00 Beijing: a plain weekday, so 00:30 tomorrow — 15.5h
    assert _deepseek_minutes_to_window(
        datetime(2026, 9, 7, 1, 0, tzinfo=utc)) == 930.0
    # Monday 00:10 Beijing: today's 00:30, 20 minutes out
    assert _deepseek_minutes_to_window(
        datetime(2026, 9, 6, 16, 10, tzinfo=utc)) == 20.0


def test_deepseek_model_gates_peak_and_skips_the_nim_budget(monkeypatch):
    import httpx
    import experiments.agent.llm as llm
    monkeypatch.setattr(llm, "deepseek_offpeak", lambda now=None: False)
    monkeypatch.setattr(llm, "_deepseek_minutes_to_window",
                        lambda now=None: 240.0)
    m = llm.DeepSeekModel("k", "deepseek-chat")
    assert m.name == "deepseek:deepseek-chat"
    assert m._base_url == "https://api.deepseek.com"
    assert m._limiter_factory is None        # prepaid credit: no RPM budget
    assert llm.NimModel("k", "x")._limiter_factory is llm.nim_limiter
    # peak hours: a readable refusal, and not one byte leaves the box
    with pytest.raises(RuntimeError, match="peak hours.*4.0h"):
        m.complete("s", "u")
    # the bypass is explicit, and the thinking slugs get the big budget
    assert llm.DeepSeekModel("k", "deepseek-reasoner")._max_tokens == 32768
    assert llm.DeepSeekModel("k", "deepseek-v4-flash")._max_tokens == 32768
    assert llm.DeepSeekModel("k", "deepseek-chat")._max_tokens == 8192

    def fake_stream(*a, **k):
        return _Stream(200, [
            'data: {"choices": [{"delta": {"content": "ok"}}]}',
            'data: [DONE]',
        ])
    monkeypatch.setattr(httpx, "stream", fake_stream)
    m2 = llm.DeepSeekModel("k", "deepseek-chat", window="any")
    assert m2.complete("s", "u") == "ok"     # any-hour override spends
    m3 = llm.DeepSeekModel("k", "deepseek-chat")
    monkeypatch.setattr(llm, "deepseek_offpeak", lambda now=None: True)
    assert m3.complete("s", "u") == "ok"     # window open: gate passes


def test_deepseek_reasoning_effort_default_low_env_overridable(monkeypatch):
    import httpx
    import experiments.agent.llm as llm
    posts = []

    def fake_stream(*a, **k):
        posts.append(k)
        return _Stream(200, [
            'data: {"choices": [{"delta": {"content": "ok"}}]}',
            'data: [DONE]',
        ])

    monkeypatch.setattr(httpx, "stream", fake_stream)
    # window=any so the billing gate doesn't refuse off-schedule runs
    m = llm.DeepSeekModel("k", "deepseek-v4-flash", window="any")
    m.complete("s", "u")
    assert posts[-1]["json"]["reasoning_effort"] == "low"
    # plain NIM never sends the field
    n = llm.NimModel("k", "x")
    monkeypatch.setattr(llm, "nim_limiter", lambda: None)
    n.complete("s", "u")
    assert "reasoning_effort" not in posts[-1]["json"]
    # env override: medium; empty string omits the field entirely
    monkeypatch.setenv("ECON_DEEPSEEK_REASONING", "medium")
    llm.DeepSeekModel("k", "deepseek-v4-flash", window="any").complete("s", "u")
    assert posts[-1]["json"]["reasoning_effort"] == "medium"
    monkeypatch.setenv("ECON_DEEPSEEK_REASONING", "")
    llm.DeepSeekModel("k", "deepseek-v4-flash", window="any").complete("s", "u")
    assert "reasoning_effort" not in posts[-1]["json"]
    # explicit ctor arg beats env
    llm.DeepSeekModel("k", "deepseek-v4-flash", window="any",
                      reasoning_effort="high").complete("s", "u")
    assert posts[-1]["json"]["reasoning_effort"] == "high"


# ===========================================================================
# Call traces: the reasoning-burn forensics hook (run 31: Harald burned
# ~85 min per attempt thinking and the text was discarded client-side)
# ===========================================================================

def test_nim_complete_emits_trace_on_success(monkeypatch):
    import httpx
    traces = []
    lines = [
        'data: {"choices": [{"delta": {"reasoning_content": "plan "}}]}',
        'data: {"choices": [{"delta": {"reasoning_content": "more"}}]}',
        'data: {"choices": [{"delta": {"content": "return "}}]}',
        'data: {"choices": [{"delta": {"content": "1"}}]}',
        'data: {"choices": [{"finish_reason": "stop", "delta": {}}]}',
        'data: {"usage": {"completion_tokens": 42}}',
        'data: [DONE]',
    ]
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream(200, lines))
    m = NimModel("k", "openai/gpt-oss-20b", on_trace=traces.append)
    assert m.complete("s", "u") == "return 1"
    assert len(traces) == 1
    t = traces[0]
    assert t["ok"] is True and t["attempt"] == 1
    assert t["finish_reason"] == "stop"
    assert t["content_chars"] == 8 and t["reasoning_chars"] == 9
    assert t["content_deltas"] == 2 and t["reasoning_deltas"] == 2
    assert t["usage"]["completion_tokens"] == 42
    assert t["max_tokens"] == 65536 and t["elapsed_s"] >= 0
    assert "reasoning_text" not in t      # success ships no reasoning blob


def test_nim_complete_emits_reasoning_text_on_length_exhaustion(monkeypatch):
    import httpx
    traces = []
    lines = [
        'data: {"choices": [{"delta": {"reasoning_content": "loop "}}]}',
        'data: {"choices": [{"delta": {"reasoning_content": "loop"}}]}',
        'data: {"choices": [{"finish_reason": "length", "delta": {}}]}',
        'data: [DONE]',
    ]
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream(200, lines))
    m = NimModel("k", "openai/gpt-oss-20b", on_trace=traces.append)
    with pytest.raises(RuntimeError, match="finish_reason=length"):
        m.complete("s", "u")
    t = traces[0]
    assert t["ok"] is False
    assert t["reasoning_text"] == "loop loop"
    assert t["finish_reason"] == "length"
    assert t["reasoning_deltas"] == 2 and t["content_deltas"] == 0
    assert "empty final content" in t["error"]


def test_nim_trace_hook_failure_never_kills_the_call(monkeypatch):
    import httpx

    def boom(trace):
        raise OSError("disk full")

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream(200, [
        'data: {"choices": [{"delta": {"content": "fine"}}]}',
        'data: {"choices": [{"finish_reason": "stop", "delta": {}}]}',
        'data: [DONE]',
    ]))
    m = NimModel("k", "openai/gpt-oss-20b", on_trace=boom)
    assert m.complete("s", "u") == "fine"


# --- RepetitionBreaker: the run-32 marathon cure ----------------------------
#
# Four burns = 57% of run 32's wall-clock, all one shape: within any
# 150-line window a handful of identical lines dominate (Harald r1:
# "We can..." x7,120 / "We need..." x7,096; Lagertha r11: one warmth
# argument sentence x330). The breaker detects that in-stream and aborts
# the attempt; every captured burn's follow-up attempt succeeded with
# 15-40x less thinking, so abort-then-retry is the cure.

def _rb_lines(n, line):
    return [f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': line + chr(10)}}]})}"
            for _ in range(n)]


def test_repetition_breaker_fires_on_token_loop():
    # Mode A: two alternating degenerate lines (Harald r1's shape)
    from experiments.agent.llm import RepetitionBreaker
    rb = RepetitionBreaker()
    assert rb.push("We can..." + "\nWe can...\n" * 54) is False
    # a line split across deltas must assemble, not double-count
    assert rb.push("We ne") is False
    assert rb.push("ed...\n") is False
    assert rb.push("We need...\n" * 54) is True
    assert rb.fired and rb.reason
    assert "top line" in rb.reason and "..." in rb.reason


def test_repetition_breaker_fires_on_impasse_circling():
    # Mode B (Lagertha r11): unique intro, then the same signature
    # sentence re-walked between one-off filler lines
    from experiments.agent.llm import RepetitionBreaker
    rb = RepetitionBreaker()
    sig = ("Thus we need to find a way to get warmth at night. "
           "The only way is to start TEND_FIRE at night.")
    fired = False
    for i in range(70):
        fired = rb.push(f"filler consideration number {i} about the "
                        f"hearth and the wolves, distinct each time\n")
        if fired:
            break
        fired = rb.push(sig + "\n")
        if fired:
            break
    assert fired, "circling signature sentence must trip the breaker"


def test_repetition_breaker_ignores_healthy_streams():
    # varied deliberation prose, and a Lua-ish tail whose `end` lines
    # repeat the way real scripts do — neither may fire
    from experiments.agent.llm import RepetitionBreaker
    rb = RepetitionBreaker()
    for i in range(150):
        assert rb.push(f"-- step {i}: rotate stock, eat when satiety "
                       f"of {i * 7} runs low\n") is False
    for i in range(50):
        assert rb.push(f"local x{i} = stock[{i}] + {i}\n") is False
        assert rb.push("end\n") is False
    assert not rb.fired


def test_nim_breaker_aborts_degenerate_reasoning_and_retries(monkeypatch):
    # attempt 1 degenerates (Mode A): the stream is aborted mid-burn,
    # the trace says why with the partial reasoning aboard, and the
    # existing attempt loop re-fires — attempt 2 answers normally
    import httpx
    import experiments.agent.llm as llm_mod
    assert NimModel("k", "openai/gpt-oss-20b")._breaker is True

    healthy = _Stream(200, [
        'data: {"choices": [{"delta": {"reasoning_content": "hm"}}]}',
        'data: {"choices": [{"delta": {"content": "ok"}}]}',
        'data: {"choices": [{"finish_reason": "stop", "delta": {}}]}',
        'data: [DONE]',
    ])
    responses = [
        _Stream(200, _rb_lines(110, "We can...")
                + ['data: {"choices": [{"delta": {"content": "never"}}]}',
                   'data: [DONE]']),
        healthy,
    ]
    posts = []

    def fake_stream(*a, **k):
        posts.append(k)
        return responses[len(posts) - 1]

    monkeypatch.setattr(httpx, "stream", fake_stream)
    traces = []
    m = NimModel("k", "openai/gpt-oss-20b", on_trace=traces.append)
    assert m.complete("s", "u") == "ok"
    assert len(posts) == 2                       # aborted, retried, done
    t = traces[0]
    assert t["ok"] is False and t["breaker"] is True
    assert t["breaker_channel"] == "reasoning"
    assert "repetition breaker" in t["error"]
    assert "We can..." in t["reasoning_text"]    # forensics survive
    assert traces[1]["ok"] is True and "breaker" not in traces[1]


def test_nim_breaker_all_attempts_degenerate_raises(monkeypatch):
    # three degenerate attempts exhaust the model-level retry; the loop
    # then treats it like any failed call (keep-old-script round)
    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream(
        200, _rb_lines(120, "Ok.")))
    with pytest.raises(RuntimeError, match="repetition breaker"):
        NimModel("k", "openai/gpt-oss-20b").complete("s", "u")


def test_nim_breaker_covers_content_channel(monkeypatch):
    # degeneration is not reasoning-only: a content stream of one line
    # aborts the attempt too (the garbage script never gets submitted)
    import httpx
    content = [f"data: {json.dumps({'choices': [{'delta': {'content': line}}]})}"
               for line in ["x = 1\n"] * 120]
    responses = [
        _Stream(200, content + ['data: [DONE]']),
        _Stream(200, [
            'data: {"choices": [{"delta": {"content": "ok"}}]}',
            'data: [DONE]']),
    ]
    posts = []

    def fake_stream(*a, **k):
        posts.append(k)
        return responses[len(posts) - 1]

    monkeypatch.setattr(httpx, "stream", fake_stream)
    m = NimModel("k", "openai/gpt-oss-20b")
    assert m.complete("s", "u") == "ok"
    assert len(posts) == 2


def test_nim_breaker_env_off_disables(monkeypatch):
    # the escape hatch: ECON_AGENT_REPETITION_BREAKER=off lets a
    # degenerate stream run to its natural end (old marathon behavior)
    import httpx
    monkeypatch.setenv("ECON_AGENT_REPETITION_BREAKER", "off")
    m = NimModel("k", "openai/gpt-oss-20b")
    assert m._breaker is False
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream(
        200, _rb_lines(200, "We can...")
        + ['data: {"choices": [{"delta": {"content": "ok"}}]}',
           'data: {"choices": [{"finish_reason": "stop", "delta": {}}]}',
           'data: [DONE]']))
    assert m.complete("s", "u") == "ok"


# ===========================================================================
# ApprovalModel — the assisted-autonomy dial stop (M2b seat kit)
# ===========================================================================

def _wait_for(path, timeout=2.0):
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if path.exists():
            return
        _t.sleep(0.005)
    raise AssertionError(f"{path} never appeared")


def _answer(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def test_approval_ok_submits_draft_verbatim(tmp_path):
    from experiments.agent.llm import ApprovalModel, ScriptedModel
    m = ApprovalModel(ScriptedModel(["-- the draft"]), "House X", tmp_path,
                      poll_s=0.005, timeout_s=5.0)
    import threading

    out = {}

    def approve():
        resp = tmp_path / "seat-house-x.response.txt"
        _wait_for(tmp_path / "seat-house-x.prompt.md")
        assert "===== DRAFT" in (tmp_path / "seat-house-x.prompt.md").read_text()
        _answer(resp, "OK")

    t = threading.Thread(target=approve)
    t.start()
    out["r"] = m.complete("sys", "user")
    t.join()
    assert out["r"] == "-- the draft"
    # the rendezvous is consumed: both files gone
    assert not (tmp_path / "seat-house-x.prompt.md").exists()
    assert not (tmp_path / "seat-house-x.response.txt").exists()


def test_approval_empty_reply_approves(tmp_path):
    from experiments.agent.llm import ApprovalModel, ScriptedModel
    import threading
    m = ApprovalModel(ScriptedModel(["draft()"]), "House X", tmp_path,
                      poll_s=0.005, timeout_s=5.0)

    def approve():
        resp = tmp_path / "seat-house-x.response.txt"
        _wait_for(tmp_path / "seat-house-x.prompt.md")
        _answer(resp, "   \n")

    t = threading.Thread(target=approve)
    t.start()
    assert m.complete("s", "u") == "draft()"
    t.join()


def test_approval_override_replaces_draft(tmp_path):
    from experiments.agent.llm import ApprovalModel, ScriptedModel
    import threading
    m = ApprovalModel(ScriptedModel(["-- model's idea"]), "House X", tmp_path,
                      poll_s=0.005, timeout_s=5.0)

    def override():
        resp = tmp_path / "seat-house-x.response.txt"
        _wait_for(tmp_path / "seat-house-x.prompt.md")
        _answer(resp, "ctx.state.human = true   -- the human's edit")

    t = threading.Thread(target=override)
    t.start()
    assert m.complete("s", "u") == "ctx.state.human = true   -- the human's edit"
    t.join()
    assert m.name == "assisted:scripted"


def test_approval_timeout_keeps_the_prompt(tmp_path):
    from experiments.agent.llm import ApprovalModel, LiveSeatTimeout, ScriptedModel
    m = ApprovalModel(ScriptedModel(["-- draft"]), "House X", tmp_path,
                      poll_s=0.005, timeout_s=0.05)
    with pytest.raises(LiveSeatTimeout):
        m.complete("s", "u")
    # the ask stays on disk — a timed-out seat's last prompt is the
    # postmortem artifact (FileModel's doctrine, kept)
    prompt = tmp_path / "seat-house-x.prompt.md"
    assert prompt.exists() and "-- draft" in prompt.read_text()
