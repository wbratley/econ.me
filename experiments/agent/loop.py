"""The real agent loop: observe -> think -> submit, with lint feedback.

A player-shaped client that plays the game the way docs/actors.md fork A
describes: the agent owns its entity's BEHAVIOUR script and rewrites it
between rounds from what it can see. Everything the agent does goes
through the MCP endpoint — exactly the bytes a real agent client sends —
and its observation set is the §13 parity set: `entity_state`,
`entity_events` (the same filtered feed the script itself receives),
`get_behaviour`, `market_prices`, `round_state`, `leaderboard`,
`epoch_state`. No omniscience, no operator surfaces; ticks are advanced
by whoever drives the loop (run.py, with a separate admin client, or a
live operator — the loop itself never advances anything).

The loop is the payoff of the scripting arc (docs/scripting.md):

  - `get_script_libraries` puts the exact tier vocabulary in the prompt,
    so a model authoring from scratch reads the world's API instead of
    hallucinating it;
  - Phase 3's submit-time lint turns a hallucinated helper from a
    per-tick zombie (the first live demo's founder) into a refused
    submission whose finding is fed straight back to the model —
    the fix costs one round-trip, and the entity keeps its working
    behaviour throughout.

One cycle = observe, think, submit (retried on lint refusal, bounded),
journal. Warnings and per-tick script errors ride into the next cycle's
prompt: the model sees the consequences of its last rewrite.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json

from .llm import strip_fences

# ---------------------------------------------------------------------------
# MCP client (transport-free: tests inject a TestClient adapter, run.py an
# httpx adapter — the loop never knows which)
# ---------------------------------------------------------------------------


class McpError(Exception):
    """A tool call the server refused (isError), with its text verbatim —
    the lint finding, when the refused tool is set_behaviour."""


class McpClient:
    def __init__(self, transport):
        """`transport(method, params) -> result dict` — one MCP JSON-RPC
        tools/call round-trip, raising on protocol-level errors."""
        self._transport = transport

    def call(self, tool: str, args: dict | None = None) -> dict:
        result = self._transport("tools/call", {"name": tool, "arguments": args or {}})
        if result.get("isError"):
            raise McpError(result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def system_prompt(libraries: dict, entity_id: str) -> str:
    """Identity + the tier vocabulary, verbatim. The std/world/pack sources
    go in whole: a model that has read them cannot honestly hallucinate a
    helper, and the lint backstops the ones that do anyway."""
    std = libraries["std"]["source"]
    world = libraries.get("world") or "-- (no world lib installed)"
    pack = libraries.get("pack") or "-- (no content pack installed)"
    return f"""\
You are the mind of entity {entity_id} in a batched simulated economy. You
do NOT act tick by tick: your whole agency is one Lua BEHAVIOUR script
that the engine runs for your entity every tick. Between rounds you may
rewrite that script from what you observed. Survival first (needs eat
holdings every tick), then wealth.

Your script runs in a sandbox with exactly this vocabulary injected:

- ctx.tick, ctx.entity, ctx.accounts, ctx.holdings, ctx.processes,
  ctx.parcels, ctx.needs, ctx.unlocks, ctx.events (your own events, last
  few ticks), ctx.state (a dict that PERSISTS across your script's runs —
  keep counters/marks there, in `local` variables they reset)
- ctx.action.* — the intent surface: transfer(from,to,amount,ref),
  place_order(symbol,side,quantity,limit_price,account_id),
  cancel_order(order_id), start_process(recipe,parcel_id),
  cancel_process(process_id), transfer_parcel(parcel_id,to_entity_id).
  Intents are validated and applied by the engine after your script
  returns; you can only spend YOUR entity's money and stock.
- ctx.query.* — read-only: balance(account_id), total_supply(currency),
  market_price(symbol), holding(entity_id,symbol), has_unlock(entity_id,code),
  holders(symbol). Results are strings (exact decimals).
- std.* — the engine stdlib (source below): pure helpers over ctx.
- world.* — this world's library (source below).
- pack.* — the content pack's play opinions (source below).

There is NO require, no io/os/debug, and NO other global vocabulary: a
call to an undefined global is a script error every tick (your entity
stops acting — the worst outcome). Strict rules enforced at submit:
undeclared globals (read OR write) are refused; `local` is always the
fix. Money quantities are exact decimal strings — convert with tonumber
for arithmetic, pass strings to intents.

Reply with ONLY the complete Lua source of your next behaviour script.
No prose, no markdown fences.

----- std.* (engine stdlib, pinned) -----
{std}

----- world.* (this world's library) -----
{world}

----- pack.* (content pack opinions) -----
{pack}
"""


def user_prompt(observation: dict, current: dict, feedback: list[str]) -> str:
    """One turn: the parity digest, the current behaviour, and every
    finding the platform handed back since (lint refusals, warnings,
    script errors) — the model argues with the world, not from memory."""
    parts = ["OBSERVATION (exactly what your entity can see):",
             json.dumps(observation, indent=1, sort_keys=True)]
    parts += ["", "CURRENT BEHAVIOUR (runs every tick):", current.get("source", "(none)")]
    if feedback:
        parts += ["", "FINDINGS since your last submission (address these):"]
        parts += [f"- {f}" for f in feedback]
    parts += ["", "Write the next behaviour. Lua source only."]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class AgentLoop:
    def __init__(self, mcp: McpClient, model, entity_id: str | None = None,
                 max_attempts: int = 3, journal_path: str | None = None,
                 last_ticks: int = 8):
        self.mcp = mcp
        self.model = model
        self.entity_id = entity_id
        self.max_attempts = max_attempts
        self.journal_path = journal_path
        self.last_ticks = last_ticks
        self._feedback: list[str] = []          # rides into the next prompt
        self.journal_lines: list[dict] = []

    # -- identity ----------------------------------------------------------

    def ensure_entity(self) -> str:
        """The entity the loop plays: the one given, else the first the
        user owns, else a fresh join (endowed + starter per world config)."""
        if self.entity_id:
            return self.entity_id
        mine = self.mcp.call("my_entities")
        if mine:
            self.entity_id = mine[0]["id"]
        else:
            self.entity_id = self.mcp.call("join")["entity"]["id"]
        return self.entity_id

    # -- one cycle ---------------------------------------------------------

    def observe(self) -> dict:
        """The parity set: everything the script itself could see, plus the
        public facts (prices, standings, round clock) any player reads."""
        eid = self.entity_id
        obs = {
            "round": self.mcp.call("round_state"),
            "entity": self.mcp.call("entity_state", {"entity_id": eid}),
            "events": self.mcp.call(
                "entity_events", {"entity_id": eid, "last_ticks": self.last_ticks}),
            "prices": self.mcp.call("market_prices"),
            "leaderboard": self.mcp.call("leaderboard"),
            "epoch": self.mcp.call("epoch_state"),
        }
        # Per-tick script errors are the loudest finding there is: the
        # behaviour is broken in play (lint cannot catch state-dependent
        # failures). Feed them forward like warnings.
        for tick in obs["events"].get("ticks", []):
            for ev in tick.get("events", []):
                if ev.get("type") == "script_error":
                    self._feedback.append(
                        f"script_error at tick {tick['tick']}: {ev.get('error')}")
        return obs

    def cycle(self) -> dict:
        """observe -> think -> submit; lint refusals re-prompt, bounded.
        A refusal never destroys anything: the entity keeps its current
        behaviour (Phase 3), so exhaustion just means 'no change this
        round' — the loop journals it and continues."""
        eid = self.ensure_entity()
        obs = self.observe()
        libraries = self.mcp.call("get_script_libraries")
        try:
            current = self.mcp.call("get_behaviour", {"entity_id": eid})
        except McpError:
            # a world without a join starter: the player's first submission
            # creates the behaviour — there is nothing to keep, and nothing
            # to show the model except that fact.
            current = {"source": "(no behaviour yet — this submission is your first)"}
        feedback, self._feedback = self._feedback, []

        attempts, accepted, warnings, last_error = 0, False, [], None
        source = ""
        while attempts < self.max_attempts:
            attempts += 1
            raw = self.model.complete(
                system_prompt(libraries, eid), user_prompt(obs, current, feedback))
            source = strip_fences(raw)
            try:
                result = self.mcp.call(
                    "set_behaviour",
                    {"entity_id": eid, "source": source,
                     "description": f"agent cycle ({self.model.name})"})
                accepted, warnings = True, result.get("lint_warnings", [])
                if warnings:  # accepted, but the model should still see them
                    self._feedback += [f"lint warning: {w}" for w in warnings]
                break
            except McpError as exc:
                last_error = str(exc)
                feedback.append(f"submission refused by lint: {last_error}")

        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "entity": eid,
            "model": self.model.name,
            "round": obs["round"].get("current_round"),
            "attempts": attempts,
            "accepted": accepted,
            "kept_old": not accepted,
            "refusal": last_error,
            "warnings": warnings,
            "source_sha": hashlib.sha256(source.encode()).hexdigest()[:16],
            "prompt_bytes": 0 if not getattr(self.model, "calls", None)
                            else len(self.model.calls[-1]["user"]),
        }
        self.journal_lines.append(entry)
        if self.journal_path:
            with open(self.journal_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        return entry

    def run(self, cycles: int, between=None) -> list[dict]:
        """N cycles; `between(cycle_no)` runs after each (e.g. advance one
        round — the operator step, supplied by the driver, never by the
        loop). The loop tolerates elimination: epoch_state says so, and the
        next cycle's MCP errors end the run cleanly."""
        entries = []
        for i in range(cycles):
            entries.append(self.cycle())
            if between and i < cycles - 1:
                between(i)
        return entries
