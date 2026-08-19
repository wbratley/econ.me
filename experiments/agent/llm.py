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

import json
import os
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
                        "temperature": self._temperature,
                        "max_tokens": self._max_tokens,
                        "top_p": 0.95,
                    },
                )
                if r.status_code in (429,) or r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                    continue
                r.raise_for_status()
                return _final_content(r.json())
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
