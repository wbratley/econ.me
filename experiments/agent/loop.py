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
prompt: the model sees the consequences of its last rewrite. The model
has three ways to answer: the complete script, edit blocks (edit_mode),
or KEEP to carry the current behaviour forward verbatim — a player
whose script is right readies up without gambling on a rewrite.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re

from .llm import ScriptedModelEmpty, strip_fences

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


def system_prompt(libraries: dict, entity_id: str, edit_mode: bool = False,
                 manual: str | None = None) -> str:
    """Identity + the tier vocabulary, verbatim. The std/world/pack sources
    go in whole: a model that has read them cannot honestly hallucinate a
    helper, and the lint backstops the ones that do anyway. `manual` -- the
    pack's legible rules (tech tree, conditions, effects) when it ships
    one -- rides in whole too: the numbers of the world, stated plainly."""
    std = libraries["std"]["source"]
    world = libraries.get("world") or "-- (no world lib installed)"
    pack = libraries.get("pack") or "-- (no content pack installed)"
    reply_rules = (
        "Reply with ONLY the complete Lua source of your next behaviour script.\n"
        "No prose, no markdown fences. If the current behaviour is already\n"
        "right, reply with the single line KEEP to carry it forward unchanged.")
    if edit_mode:
        reply_rules = (
            "Reply in one of three ways:\n"
            "- the complete Lua source of your next behaviour script (no prose,\n"
            "  no markdown fences), or\n"
            "- the single line KEEP, to carry the current behaviour forward\n"
            "  unchanged, or\n"
            "- a list of edit blocks, to change only part of the current\n"
            "  behaviour — nothing outside the blocks:\n\n"
            "  <<<<<<< SEARCH\n"
            "  exact lines copied from the current behaviour\n"
            "  =======\n"
            "  replacement lines\n"
            "  >>>>>>> REPLACE\n\n"
            "  Each SEARCH must match the current behaviour exactly, whitespace\n"
            "  included; blocks apply in order.")
    text = f"""\
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

{reply_rules}

----- std.* (engine stdlib, pinned) -----
{std}

----- world.* (this world's library) -----
{world}

----- pack.* (content pack opinions) -----
{pack}
"""
    if manual:
        text += f"""
----- THE WORLD MANUAL (this world's rules, in numbers) -----
{manual}
"""
    return text


def user_prompt(observation: dict, current: dict, feedback: list[str],
                edit_mode: bool = False) -> str:
    """One turn: the parity digest, the current behaviour, and every
    finding the platform handed back since (lint refusals, warnings,
    script errors) — the model argues with the world, not from memory."""
    parts = ["OBSERVATION (exactly what your entity can see):",
             json.dumps(observation, indent=1, sort_keys=True)]
    parts += ["", "CURRENT BEHAVIOUR (runs every tick):", current.get("source", "(none)")]
    if feedback:
        parts += ["", "FINDINGS since your last submission (address these):"]
        parts += [f"- {f}" for f in feedback]
    parts += ["", ("Write the next behaviour: complete Lua source, edit "
                    "blocks, or KEEP." if edit_mode
                    else "Write the next behaviour. Lua source only.")]
    return "\n".join(parts)


def diary_prompt(system_text: str, transcript: list[dict],
                 action: str, refusal: str | None) -> tuple[str, str]:
    """The strategy diary: after the decision stands, one short call
    carrying the COMPLETE round record — the rules played under, every
    prompt (with accumulated findings), the model's own replies
    verbatim (the code itself), and every platform response between
    attempts. Anything less asks the model to describe a decision it
    can no longer see, and it will hallucinate a plausible one."""
    system = (
        "You are the strategist of an entity in a batched simulated "
        "economy. A round just concluded; below is the complete verbatim "
        "record of your own decision-making chat for it. Write your "
        "strategy diary for this round: 1-3 plain sentences — no "
        "markdown, no code, no quotes — on what you changed or kept and "
        "why, your read of the world (markets, food, rivals), and your "
        "intent for next round. Ground it strictly in the record: it is "
        "the authority on what you actually did.")
    parts = ["THE COMPLETE RECORD OF THIS ROUND (your own chat, verbatim):",
             "", "===== RULES YOU PLAYED UNDER (system prompt) =====",
             system_text]
    for step in transcript:
        if "user" in step:
            parts += ["", "===== PROMPT TO YOU =====", step["user"],
                      "", "===== YOUR REPLY (verbatim) =====", step["reply"]]
        else:
            parts += ["", "===== PLATFORM RESPONSE =====", step["platform"]]
    outcome = f"\nFINAL OUTCOME: action taken: {action}"
    if refusal:
        outcome += f" (last refusal: {refusal})"
    parts += [outcome, "", "Write the diary entry now. Plain sentences only."]
    return system, "\n".join(parts)


# ---------------------------------------------------------------------------
# Edit blocks (SEARCH/REPLACE): patch the current behaviour instead of
# rewriting it whole. Aider's format — the one LLMs transcribe most
# faithfully; strict exact match, because the retry loop is the fuzzing.
# ---------------------------------------------------------------------------

_PATCH_RE = re.compile(
    r"<<<<<<< SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n>>>>>>> REPLACE",
    re.DOTALL)


def _parse_patches(text: str) -> list[tuple[str, str]]:
    """All (search, replace) blocks in a raw completion; [] when the reply
    isn't in block form (a full rewrite or KEEP — the other two actions)."""
    return [(m.group(1), m.group(2)) for m in _PATCH_RE.finditer(text)]


def _apply_edits(source: str,
                 patches: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """Apply blocks in order, first occurrence each. Returns the patched
    source, or (None, why) — `why` is feedback the model can act on."""
    for i, (search, replace) in enumerate(patches, 1):
        if not search.strip():
            return None, f"block {i}: SEARCH is empty"
        if search not in source:
            head = " ".join(search.split())[:60]
            return None, f"block {i}: SEARCH not found in the current " \
                         f"behaviour: {head!r}"
        source = source.replace(search, replace, 1)
    return source, None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class AgentLoop:
    def __init__(self, mcp: McpClient, model, entity_id: str | None = None,
                 max_attempts: int = 3, journal_path: str | None = None,
                 last_ticks: int = 8, edit_mode: bool = False,
                 diary: bool = False, manual: str | None = None):
        self.mcp = mcp
        self.model = model
        self.entity_id = entity_id
        self.max_attempts = max_attempts
        self.journal_path = journal_path
        self.last_ticks = last_ticks
        self.edit_mode = edit_mode
        self.diary = diary
        self.manual = manual              # the pack's legible rules, if any
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
        # Rival privacy: the standings keep every PUBLIC fact (status,
        # entities, unlocks, wins) but drop `money` -- a fortune is not a
        # public fact in this loop's worlds (the leaderboard's money column
        # is a standings tie-breaker the platform sorts by, not something a
        # player can read off a rival; holdings privacy is the pack's
        # PRIVATE_HOLDINGS flag, the script-surface twin of this rule).
        board = self.mcp.call("leaderboard")
        for row in board.get("rows", []):
            row.pop("money", None)
        obs = {
            "round": self.mcp.call("round_state"),
            "entity": self.mcp.call("entity_state", {"entity_id": eid}),
            "events": self.mcp.call(
                "entity_events", {"entity_id": eid, "last_ticks": self.last_ticks}),
            "prices": self.mcp.call("market_prices"),
            "leaderboard": board,
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
        has_current = current.get("id") is not None

        attempts, accepted, warnings, last_error = 0, False, [], None
        source, action = "", "rewrite"
        sys_text = system_prompt(libraries, eid, edit_mode=self.edit_mode,
                                manual=self.manual)
        transcript: list[dict] = []      # prompts + replies + platform
        # responses: the diary's ground truth, captured as it happens
        while attempts < self.max_attempts:
            attempts += 1
            try:
                usr_text = user_prompt(obs, current, feedback,
                                       edit_mode=self.edit_mode)
                raw = self.model.complete(sys_text, usr_text)
            except ScriptedModelEmpty:
                raise                  # a missing fixture, not a provider hiccup
            except Exception as exc:  # provider/model failure: an attempt,
                last_error = f"model failure: {exc}"   # not a dead round
                feedback.append(f"the previous model call failed: {exc}")
                transcript.append(
                    {"platform": f"the model call failed: {exc}"})
                continue
            transcript.append({"user": usr_text, "reply": raw})

            # action 1: KEEP — carry the behaviour forward verbatim, no
            # submission at all. Readying up without gambling a rewrite.
            # Bare KEEP only: prose around it ("I would KEEP") is the
            # model talking, and a talking response is a rewrite attempt.
            if raw.strip().upper() == "KEEP":
                if has_current:
                    source, action = current["source"], "keep"
                    break
                last_error = "KEEP refused: no previous behaviour to keep"
                feedback.append("you replied KEEP, but there is no previous "
                                "behaviour to keep — write the complete script")
                transcript.append(
                    {"platform": "KEEP refused: no previous behaviour "
                                "to keep — write the complete script"})
                continue

            # action 2: edit blocks — patch the current behaviour, then
            # through the same lint gate as a full rewrite
            patches = _parse_patches(raw)
            if patches:
                if not has_current:
                    last_error = "patch refused: no previous behaviour to patch"
                    feedback.append("you sent edit blocks, but there is no "
                                    "previous behaviour — write the complete script")
                    transcript.append(
                        {"platform": "edit blocks refused: no previous "
                                    "behaviour to patch"})
                    continue
                patched, err = _apply_edits(current["source"], patches)
                if err:
                    last_error = err
                    feedback.append(f"patch did not apply: {err}. SEARCH must "
                                    "match the current behaviour exactly")
                    transcript.append(
                        {"platform": f"patch did not apply: {err}"})
                    continue
                source, action = patched, "edit"
            else:                     # action 3: the full rewrite
                source, action = strip_fences(raw), "rewrite"

            try:
                result = self.mcp.call(
                    "set_behaviour",
                    {"entity_id": eid, "source": source,
                     "description": f"agent cycle ({self.model.name})"})
                accepted, warnings = True, result.get("lint_warnings", [])
                transcript.append({"platform": "submission accepted"
                                   + (f" (warnings: {warnings})" if warnings
                                      else "")})
                if warnings:  # accepted, but the model should still see them
                    self._feedback += [f"lint warning: {w}" for w in warnings]
                break
            except McpError as exc:
                last_error = str(exc)
                feedback.append(f"submission refused by lint: {last_error}")
                transcript.append(
                    {"platform": f"submission refused by lint: {last_error}"})

        # The strategy diary: one extra short call, same mind, after the
        # decision stands. Failure degrades to silence — the diary must
        # never be the reason a round dies.
        thoughts = ""
        if self.diary:
            try:
                thoughts = self.model.complete(
                    *diary_prompt(sys_text, transcript,
                                  action, last_error)).strip()
            except ScriptedModelEmpty:
                raise                 # a missing fixture, not a provider hiccup
            except Exception:
                thoughts = ""

        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "entity": eid,
            "model": self.model.name,
            "round": obs["round"].get("current_round"),
            "attempts": attempts,
            "accepted": accepted,
            "kept_old": not accepted,
            "action": action,
            "refusal": last_error,
            "warnings": warnings,
            "source_sha": hashlib.sha256(source.encode()).hexdigest()[:16],
            "thoughts": thoughts,
            "prompt_bytes": next((len(s["user"]) for s in reversed(transcript)
                                  if "user" in s), 0),
        }
        self.journal_lines.append(entry)
        if self.journal_path:
            with open(self.journal_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        return entry

    def set_ready(self, ready: bool = True) -> dict:
        """Signal (or withdraw) readiness for the open round (game.md §9.1).
        In a readiness-gated world the final ready closes the round — the
        player's clock vote, still pure MCP: no admin client, no operator,
        the world paces itself."""
        return self.mcp.call("set_ready", {"ready": ready})

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
