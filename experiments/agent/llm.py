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
    source. Raw Lua passes through untouched."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # drop the opening fence line (``` or ```lua) and any closing fence
        lines = [ln for ln in lines[1:] if not ln.strip() == "```"]
        stripped = "\n".join(lines).strip()
    return stripped


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
        return r.json()["choices"][0]["message"]["content"]


def model_from_env(env: dict[str, str] | None = None) -> Model:
    """Pick the model from the environment (see README):

      ECON_AGENT_SCRIPTED_FILE  offline scripted run (checked first)
      ANTHROPIC_API_KEY         Anthropic (ECON_AGENT_MODEL overrides slug)
      OPENAI_API_KEY            OpenAI (ECON_AGENT_MODEL overrides slug)
    """
    env = dict(env if env is not None else os.environ)
    if path := env.get("ECON_AGENT_SCRIPTED_FILE"):
        return ScriptedModel.from_file(path)
    slug = env.get("ECON_AGENT_MODEL")
    if key := env.get("ANTHROPIC_API_KEY"):
        return AnthropicModel(key, model=slug or "claude-sonnet-4-5")
    if key := env.get("OPENAI_API_KEY"):
        return OpenAIModel(key, model=slug or "gpt-4o")
    raise RuntimeError(
        "no model configured: set ECON_AGENT_SCRIPTED_FILE (offline), "
        "ANTHROPIC_API_KEY, or OPENAI_API_KEY (ECON_AGENT_MODEL overrides "
        "the default slug)"
    )
