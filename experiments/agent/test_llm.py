"""The NIM rate limiter: one shared per-minute budget for the whole run.

Not a mock-heavy test — the limiter is 20 lines of monotonic-clock
arithmetic, so we test exactly that: bursts pass until the window is
full, the next call waits for the oldest entry to age out, and the
429-retry path consumes budget too (it is, after all, another request).
"""

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


def test_nim_complete_raises_on_null_content(monkeypatch):
    # the wiring gap, seen live twice (nim-run2 r4, nim-run3 r2 under a
    # machine suspend): NimModel returned the raw content field, so a
    # null final channel leaked into the loop and died far from the
    # cause as `'NoneType' object has no attribute 'strip'`. Now the
    # same seam as OpenAIModel: _final_content raises with the
    # finish_reason in the message — and does NOT burn transport
    # retries on an empty answer.
    import httpx
    body = {"choices": [{"finish_reason": "length", "message": {
        "content": None, "reasoning_content": "thinking...",
    }}]}
    posts = []

    def fake_post(*a, **k):
        posts.append(k)
        return _Resp(body)

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(RuntimeError, match="finish_reason=length"):
        NimModel("k", "openai/gpt-oss-20b").complete("s", "u")
    assert len(posts) == 1          # raised out, not retried in-loop
