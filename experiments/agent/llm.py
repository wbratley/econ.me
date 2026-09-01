"""The LLM seam for the agent loop — one call shape, any provider.

The loop needs exactly one capability from a model: given a system
context (identity + the tier vocabulary) and a user turn (the observation
digest, the current behaviour, and any feedback), return the next Lua
behaviour source. Everything else — transport, auth, provider quirks —
lives behind this seam, so the loop (`loop.py`) and its tests never
import a vendor SDK.

`ScriptedModel` is the honest offline implementation: a FIFO of canned
responses (or a JSONL file via `ECON_AGENT_SCRIPTED_FILE`), recording
every prompt it was shown so tests can assert what the model *saw*.
`AnthropicModel` / `OpenAIModel` are plain `httpx` calls — no SDK, same
reason the MCP client is plain JSON-RPC: the interesting bugs in an
agent loop are in the loop, not the transport.

    from experiments.agent.llm import model_from_env
    model = model_from_env()   # raises with guidance if nothing configured
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Protocol


class ScriptedModelEmpty(RuntimeError):
    """The canned queue ran dry — the loop asked for a response nobody
    scripted. In tests this is a missing fixture; in an offline run it
    means the scenario needed more cycles than were scripted."""


class Model(Protocol):
    """What the loop needs from a model: a name (for the journal) and one
    completion call returning raw text."""

    name: str

    def complete(self, system: str, user: str) -> str: ...


class ScriptedModel:
    """Offline model: pops canned responses FIFO, records every prompt.

    `calls` is the assertion surface for tests — the prompts the model was
    shown, in order, exactly as a real provider would receive them.
    """

    name = "scripted"

    def __init__(self, responses: list[str]):
        self._queue = list(responses)
        self.calls: list[dict[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if not self._queue:
            raise ScriptedModelEmpty(
                f"scripted model queue is empty after {len(self.calls)} calls; "
                "script more responses or shorten the run"
            )
        return self._queue.pop(0)

    @classmethod
    def from_file(cls, path: str | Path) -> "ScriptedModel":
        """A JSONL file of responses (one JSON string per line) — a
        reproducible offline scenario for `run.py`."""
        responses = [
            json.loads(line)
            for line in Path(path).read_text().splitlines()
            if line.strip()
        ]
        return cls(responses)


def strip_fences(text: str) -> str:
    """Defensively unwrap a ```lua fence a model may add around the
    source. Raw Lua passes through untouched.

    The stone-run2 lesson: models (Nemotron, every round) prefix the
    fence with prose -- "Here are the changes:" -- and a leading-prose
    response used to pass through whole, so the submitted "script"
    began with an English sentence and died in lint as `syntax error
    near 'are'`. Now: if the text does not START with a fence, take the
    first fenced block anywhere in it (models put the code in one),
    falling back to the raw text only when there is no fence at all.
    Prose after the closing fence is dropped the same way.
    """
    stripped = text.strip()
    if not stripped:
        return stripped
    if not stripped.startswith("```"):
        idx = stripped.find("```")
        if idx != -1:
            stripped = stripped[idx:]
        # no fence anywhere: assume it is raw Lua and let lint judge
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        body = []
        for ln in lines[1:]:          # drop the ```lua / ``` header
            if ln.strip() == "```":
                break                 # closing fence: prose after it is prose
            body.append(ln)
        stripped = "\n".join(body).strip()
    return stripped


# ===========================================================================
# Separating reasoning from script intent (the stone-run5 lesson)
# ===========================================================================
# Nemotron's run-5 refusals were almost all ONE failure wearing two masks:
# the model reasons in prose around its code, and the extractor is
# one-shot -- first fence or raw text, no deliberation stripped, no
# sanity check. 9 rounds died as `syntax error near 'are'` (line 1 =
# prose: no usable fence anywhere in the reply), 6 as `<eof> expected
# near 'end'` (code and trailing commentary merged, or genuinely
# unbalanced). The fix is structural, not prompt-shaped -- prompts
# cannot stop a model that opens its DIARY with "We are given":
#
#   1. drop <think>...</think> deliberation blocks (reasoning models
#      that embed CoT in the content channel);
#   2. candidates -- the classic first-fence body, the LAST fence body
#      (a model that fences its reasoning first and its answer second),
#      and the longest contiguous run of Lua-looking lines (no fences
#      at all);
#   3. let the Lua parser arbitrate: the first candidate that COMPILES
#      is the script. If nothing compiles, fall back to today's
#      behavior unchanged -- lint reports, the retry loop hints.
#   4. identity guard (the stone-run6 lesson): a compiling candidate
#      that is the CURRENT script re-indented is a quote, not intent --
#      when the model's reply also offers a textually different
#      candidate, that one wins. Nemotron's round-3 "fix" differed from
#      the crashing round-2 script by one leading space per line: the
#      diary said "changed ctx.accounts[0] to [1]", the submission
#      still had [0], because the reply quoted the old script in an
#      indented fence BEFORE the corrected code.

_THINK_RE = re.compile(r"(?is)<think>.*?</think>")
_FENCE_RE = re.compile(r"(?s)```[a-zA-Z0-9_-]*[ \t]*\n(.*?)```")

# A line that looks like Lua, not English. Strong openers and strong
# tokens; an occasional prose line slipping in is fine -- the compile
# check is the arbiter, this only has to find the island.
_LUA_LINE_RE = re.compile(
    r"""^\s*(?:
        local\s|function\b|for\s|while\s|if\s|repeat\b|until\b|
        return\b|end\b|else\b|elseif\b|then\b|do\b|--|\[\[|\]\]|
        ctx\.|std\.|world\.|pack\.|\)|\}|
        [\w.\"'\[]+\s*[,=\(
]
    )""",
    re.X,
)


def _is_lua_line(ln: str) -> bool:
    if _LUA_LINE_RE.match(ln):
        return True
    stripped = ln.strip()
    return " = " in ln or stripped.endswith((")", "}", ",", "then"))


def strip_think(text: str) -> str:
    """Drop <think>...</think> deliberation a reasoning model embedded in
    the content channel (the separate reasoning_content channel is already
    ignored by _final_content). No tags: text unchanged."""
    return _THINK_RE.sub("", text).strip()


def _lua_compiles(source: str) -> bool:
    """Syntax-only arbitration: lupa's compile runs nothing, it parses.
    The check is the extractor's ground truth for 'this is the script'."""
    try:
        from lupa import LuaRuntime
        LuaRuntime().compile(source)
        return True
    except Exception:
        return False


def _last_fence(text: str) -> str | None:
    fences = _FENCE_RE.findall(text)
    if not fences:
        return None
    return fences[-1].strip()


def _code_island(text: str) -> str | None:
    """The longest contiguous run of Lua-looking lines -- the no-fence
    reply: prose, code, trailing prose. Blank lines may sit inside a run
    (code breathes); they never anchor one."""
    best: list[str] = []
    run: list[str] = []
    for ln in text.splitlines():
        if not ln.strip():
            if run:
                run.append(ln)          # a breath inside the island
            continue
        if _is_lua_line(ln):
            run.append(ln)
        else:
            if len(run) > len(best):
                best = run
            run = []
    if len(run) > len(best):
        best = run
    while best and not best[0].strip():
        best.pop(0)
    while best and not best[-1].strip():
        best.pop()
    return "\n".join(best) or None


def _ws_same(a: str, b: str) -> bool:
    """Same program modulo whitespace: line indentation and blank
    lines are formatting, not semantics (Lua does not care). Used to
    spot a candidate that is merely the current script re-quoted."""
    ka = [ln.strip() for ln in a.splitlines() if ln.strip()]
    kb = [ln.strip() for ln in b.splitlines() if ln.strip()]
    return ka == kb


def _candidates(raw: str) -> list[str]:
    """The extraction candidates in arbitration order: the first fenced
    block (yesterday's strip_fences, still the workhorse), the last
    fenced block, the longest Lua-looking island. Deduped, empties
    dropped — each is a script the reply might have meant."""
    body = strip_think(raw)
    out = [strip_fences(body)]
    tail = _last_fence(body)
    if tail:
        out.append(tail)
    island = _code_island(body)
    if island:
        out.append(island)
    seen: set[str] = set()
    uniq: list[str] = []
    for cand in out:
        if cand and cand not in seen:
            seen.add(cand)
            uniq.append(cand)
    return uniq


def extract_script_detailed(
        raw: str, current: str | None = None) -> tuple[str, dict]:
    """The script inside a raw reply, plus the extractor's forensics.

    First candidate that COMPILES wins; nothing compiles -> the first
    candidate verbatim, so behavior degrades to exactly the one-shot
    extractor's (lint refuses, the loop hints, the model retries).

    `current` is the behaviour the entity runs right now. When given,
    a whitespace-identical resubmission is bypassed if any compiling
    candidate textually differs: after a failing round, re-sending the
    same program is never intent — the model that "fixed" nothing is
    the one that quoted its old script before the corrected code. When
    every compiling candidate is the current script (or there is only
    one), first-that-compiles stands: a genuine verbatim resubmit, or
    a reply with nothing better to say, is the model's answer.

    Returns `(source, info)`; `info` names the choice — candidate
    count, winner index, per-candidate sha[:8], and the indices
    bypassed for whitespace-identity — so accepted rounds are no
    longer a forensic blind spot.
    """
    cands = _candidates(raw)
    ok = [i for i, c in enumerate(cands) if _lua_compiles(c)]
    winner = ok[0] if ok else 0
    ws_skip: list[int] | None = None
    if current and current.strip() and ok:
        differing = [i for i in ok if not _ws_same(cands[i], current)]
        if differing:
            ws_skip = [i for i in ok if _ws_same(cands[i], current)]
            winner = differing[0]
    info = {
        "n": len(cands),
        "winner": winner,
        "shas": [hashlib.sha256(c.encode()).hexdigest()[:8] for c in cands],
        "ws_skip": ws_skip,
    }
    return cands[winner], info


def extract_script(raw: str, current: str | None = None) -> str:
    """The script inside a raw reply, whatever the model wrapped it in
    (see extract_script_detailed for the full arbitration and the
    `current` identity guard)."""
    return extract_script_detailed(raw, current)[0]


def _final_content(data: dict) -> str:
    """The final answer from a chat-completions body: `message.content`,
    ignoring the reasoning channel reasoning models also return
    (`reasoning_content` / `reasoning`). Thinking stays enabled — the
    budget just has to cover it (see NimModel's max_tokens). A null or
    empty final channel raises with `finish_reason` in the message:
    the classic case is `length` with reasoning present, i.e. thinking
    consumed the whole completion budget and the answer never started.
    Raising beats leaking None into the loop, where it dies far from
    the cause as `'NoneType' object has no attribute 'strip'`."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content")
    if content is None or not str(content).strip():
        finish = choice.get("finish_reason") or "?"
        why = ("; reasoning consumed the token budget — raise max_tokens"
               if message.get("reasoning_content") else "")
        raise RuntimeError(
            f"empty final content (finish_reason={finish}{why})")
    return str(content)


class AnthropicModel:
    """Anthropic Messages API over plain httpx."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5",
                 timeout: float = 120.0,
                 base_url: str = "https://api.anthropic.com"):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self.name = f"anthropic:{model}"

    def complete(self, system: str, user: str) -> str:
        import httpx  # deferred: tests never hit the network

        r = httpx.post(
            f"{self._base_url}/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=self._timeout,
            json={
                "model": self._model,
                "max_tokens": 8192,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        r.raise_for_status()
        body = r.json()
        # content blocks: text blocks join; thinking blocks (extended
        # mind) carry "thinking", not "text", so they vanish here —
        # reasoning on, reasoning ignored
        return "".join(
            block.get("text", "") for block in body.get("content", [])
        )


class OpenAIModel:
    """OpenAI Chat Completions over plain httpx."""

    def __init__(self, api_key: str, model: str = "gpt-4o",
                 timeout: float = 120.0,
                 base_url: str = "https://api.openai.com"):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self.name = f"openai:{model}"

    def complete(self, system: str, user: str) -> str:
        import httpx

        r = httpx.post(
            f"{self._base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        r.raise_for_status()
        return _final_content(r.json())


NIM_DEFAULT_BASE = "https://integrate.api.nvidia.com"

# Reasoning models whose thinking channel reliably eats a default-sized
# completion budget before the answer starts (observed: GPT-OSS,
# finish_reason=length with empty content, 7 stone-run3 rounds).
_REASONING_TOKEN_DEFAULTS: dict[str, int] = {
    "gpt-oss": 32768,
    # nemotron-3 reasons before content (observed ~110 thinking tokens
    # for a trivial ask; a full script rewrite thinks more). max_tokens
    # is a cap, not a target -- the headroom costs nothing unspent.
    "nemotron-3": 16384,
}


def _default_max_tokens(model: str) -> int:
    """Completion budget by slug family; plain instruct default 8192."""
    for family, budget in _REASONING_TOKEN_DEFAULTS.items():
        if family in model:
            return budget
    return 8192


def nim_key(env: dict[str, str] | None = None) -> str | None:
    """The NVIDIA NIM key: NVIDIA_API_KEY / NIM_API_KEY from the env, or
    (only when reading the real process env) the first line of
    ~/.nim_api_key — kept out of the repo and shells. An explicit env
    dict is hermetic: tests pass {} and mean nothing is configured,
    even on a machine that happens to hold a key file."""
    file_fallback = env is None
    env = dict(env if env is not None else os.environ)
    if key := (env.get("NVIDIA_API_KEY") or env.get("NIM_API_KEY")):
        return key.strip()
    if file_fallback:
        path = Path.home() / ".nim_api_key"
        if path.exists():
            first = path.read_text().splitlines()
            if first and first[0].strip():
                return first[0].strip()
    return None


class _RateLimiter:
    """Sliding-window limiter, process-wide, thread-safe. The hosted NIM
    key is metered per minute and shared by every dynasty client in the
    run — so the budget is shared too, and a wait happens BEFORE the
    request (belt), while the 429-retry stays as suspenders."""

    def __init__(self, max_calls: int, window: float = 60.0):
        self._n = max_calls
        self._window = window
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block until one call fits the window, then record it."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self._window:
                    self._times.popleft()
                if len(self._times) < self._n:
                    self._times.append(now)
                    return
                sleep_for = self._window - (now - self._times[0]) + 0.05
            time.sleep(max(sleep_for, 0.05))


_nim_limiter: _RateLimiter | None = None
_nim_limiter_lock = threading.Lock()


def nim_limiter() -> _RateLimiter:
    """The process-wide NIM budget: ECON_AGENT_NIM_RPM calls per minute,
    default 36 — deliberately under the hosted tier's 40 so a burst of
    lint retries can't trip it."""
    global _nim_limiter
    with _nim_limiter_lock:
        if _nim_limiter is None:
            try:
                rpm = int(os.environ.get("ECON_AGENT_NIM_RPM") or 36)
            except ValueError:
                rpm = 36
            _nim_limiter = _RateLimiter(max(rpm, 1))
        return _nim_limiter


class NimModel(OpenAIModel):
    """NVIDIA NIM (OpenAI-compatible hosted inference): same chat
    completions call, NIM base URL, plus the two courtesies a shared
    metered endpoint wants — explicit max_tokens/temperature and a small
    retry on 429/5xx (backoff, bounded; never on 4xx, which retrying
    cannot fix)."""

    def __init__(self, api_key: str, model: str,
                 timeout: float = 120.0,
                 base_url: str = NIM_DEFAULT_BASE,
                 temperature: float = 0.3, max_tokens: int | None = None):
        super().__init__(api_key, model=model, timeout=timeout,
                         base_url=base_url)
        self.name = f"nim:{model}"
        self._temperature = temperature
        # Per-model defaults, overridable by argument: reasoning models
        # (the stone-run3 GPT-OSS evidence) spend the completion budget
        # thinking and finish_reason=length with an empty final channel --
        # `raise max_tokens` -- so they get a bigger default than the
        # 8192 a plain instruct model is fine with.
        self._max_tokens = max_tokens or _default_max_tokens(model)

    def complete(self, system: str, user: str) -> str:
        import httpx

        last_error = None
        for attempt in range(3):
            nim_limiter().wait()                  # shared per-minute budget
            if attempt:
                time.sleep(2.0 * attempt)          # 2s, 4s — polite, bounded
            try:
                # Streamed, always. With the 32k completion budget a
                # reasoning model thinks for many minutes, and a NON-
                # streaming NIM sends no bytes until generation ends —
                # so the 120s read timeout kills any long authoring
                # call before a byte can arrive (run 21: every round-2+
                # authoring died exactly there; the houses ran on their
                # round-1 scripts for the whole run while 19-minute
                # rounds burned on timeout ladders). Streamed, the read
                # timeout is a BETWEEN-chunks budget: a slow generation
                # keeps both the bytes and the deadline alive.
                chunks: list[str] = []
                reasoning = False
                finish: str | None = None
                with httpx.stream(
                    "POST",
                    f"{self._base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self._timeout,
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": self._temperature,
                        "max_tokens": self._max_tokens,
                        "top_p": 0.95,
                        "stream": True,
                    },
                ) as r:
                    if r.status_code == 429 or r.status_code >= 500:
                        last_error = (f"HTTP {r.status_code}: "
                                      f"{r.read()[:200]!r}")
                        continue
                    r.raise_for_status()
                    for line in r.iter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue   # keep-alive comments, blanks
                        if isinstance(obj.get("error"), dict):
                            raise RuntimeError(
                                "NIM stream error: "
                                + str(obj["error"].get("message"))[:200])
                        for choice in obj.get("choices") or []:
                            finish = choice.get("finish_reason") or finish
                            delta = choice.get("delta") or {}
                            if delta.get("reasoning_content"):
                                reasoning = True
                            piece = delta.get("content")
                            if piece:
                                chunks.append(str(piece))
                text = "".join(chunks)
                if not text.strip():
                    why = ("; reasoning consumed the token budget — "
                           "raise max_tokens") if reasoning else ""
                    raise RuntimeError(
                        f"empty final content (finish_reason="
                        f"{finish or '?'}{why})")
                return text
            except httpx.HTTPError as exc:
                last_error = str(exc)
        raise RuntimeError(f"NIM {self._model} failed after 3 attempts: "
                           f"{last_error}")


def model_from_env(env: dict[str, str] | None = None) -> Model:
    """Pick the model from the environment (see README):

      ECON_AGENT_SCRIPTED_FILE  offline scripted run (checked first)
      ANTHROPIC_API_KEY         Anthropic (ECON_AGENT_MODEL overrides slug)
      OPENAI_API_KEY            OpenAI (ECON_AGENT_MODEL overrides slug)
      NVIDIA_API_KEY/NIM_API_KEY  NVIDIA NIM, ECON_AGENT_MODEL = the slug
                                 (or ~/.nim_api_key; ECON_AGENT_NIM_BASE
                                 overrides the hosted endpoint — self-hosted
                                 NIM containers speak the same protocol)
    """
    env = dict(env if env is not None else os.environ)
    if path := env.get("ECON_AGENT_SCRIPTED_FILE"):
        return ScriptedModel.from_file(path)
    slug = env.get("ECON_AGENT_MODEL")
    if key := env.get("ANTHROPIC_API_KEY"):
        return AnthropicModel(key, model=slug or "claude-sonnet-4-5")
    if key := env.get("OPENAI_API_KEY"):
        return OpenAIModel(key, model=slug or "gpt-4o")
    if key := nim_key(env):
        if not slug:
            raise RuntimeError(
                "NIM key found but no model: set ECON_AGENT_MODEL to a NIM "
                "catalog slug (see `python -m experiments.agent.nim_pick --list`)"
            )
        return NimModel(key, model=slug,
                        base_url=env.get("ECON_AGENT_NIM_BASE", NIM_DEFAULT_BASE))
    raise RuntimeError(
        "no model configured: set ECON_AGENT_SCRIPTED_FILE (offline), "
        "ANTHROPIC_API_KEY, OPENAI_API_KEY, or NVIDIA_API_KEY/NIM_API_KEY "
        "plus ECON_AGENT_MODEL (ECON_AGENT_MODEL overrides the slug elsewhere)"
    )
