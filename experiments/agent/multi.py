"""The multi-agent run: N dynasties, M rounds, snapshots every round.

The NIM run's mechanics, separated from the CLI the way the loop is
separated from run.py: everything here works over *transports* (the same
`(method, params) -> result` adapter `McpClient` takes), so tests drive a
whole three-dynasty world through the FastAPI TestClient with
ScriptedModels and no network, while the live runner injects httpx
transports pointed at a spawned server. Same doctrine, one level up.

The world is the content pack's substrate (experiments/world/scenario.py:
goods, tech, recipes, needs, markets, the tiered libs) with one twist —
the seats are SYMMETRIC: every dynasty gets scenario.make_house's
identical bundle (same money, FARM + FORGE + ORE seam, both unlocks)
and the plain survival starter, which runs only until its first cycle
replaces it. From then on every mind in the world is a model rewriting
Lua on its OWN cadence: one authoring thread per dynasty — read the
world, submit for the next round, signal consent, repeat — with no
communal round barrier (run 52's killer: a two-hour llama ladder held
every seat's next submission hostage while the world clock played
five rounds underneath; the healthy seats died of stale behaviours).
A seat slower than its round simply misses the window — its behaviour
keeps running (a keep) and its late submission rolls forward — and
the rounds keep closing: on the world's own ticks under a clock
(§9.2, `clock_wait`), or on the final consent in a readiness-gated
world (§9.1). No admin in the pacing loop — the operator built the
world, then stepped back.

Snapshots are taken from each dynasty's OWN MCP surface (the §13 parity
set plus leaderboard/prices): the dashboard's data is exactly what the
players could see, never an omniscient dump.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .loop import AgentLoop, McpClient, McpError
from econengine.catalog import catalog_state, catalog_text

# ---------------------------------------------------------------------------
# The dynasties and their world
# ---------------------------------------------------------------------------


@dataclass
class Dynasty:
    """One player seat: a user, a house name, and the model that will
    mind it. `entity_id` fills in at world build."""
    user_id: str
    name: str
    model_name: str
    token: str
    entity_id: str | None = None


def build_agent_world(session, dynasties: list[Dynasty], scenario: str = "frontier",
                       seeds: dict[str, str] | None = None) -> dict:
    """Content-pack substrate + symmetric, agent-owned seats.

    `scenario` names the pack (SCENARIOS below): it provides goods/tech/
    recipes/needs/markets, installs the tiered libs through the gate (with
    manifest pinning), and defines the symmetric seat constructor plus the
    starter behaviour every dynasty inherits. Then each dynasty gets that
    pack's identical endowment — no role priming, no seat-specific starter
    — and the readiness gate is switched to `readiness` mode: from here the
    world paces itself.

    `seeds` (the champions program, run 24+): house name -> lua source,
    installed INSTEAD of the stock starter for that house. A seed is a
    banked winner from a finished run (script_archive), gated by the
    same smoke-run the pack's own lua passes — API drift between eras
    dies at launch, not at the seat's first tick. Unlisted houses keep
    the starter; from round 1 the owner remains free to rewrite or KEEP
    (edit-mode): seeding is world config, not steering.
    """
    from econengine.models import Script, ScriptType, WorldSetting

    from experiments.world import scenario as _frontier
    from experiments.world import stone_age

    scenarios = {"frontier": _frontier, "stone_age": stone_age}
    if scenario not in scenarios:
        raise ValueError(f"unknown scenario {scenario!r}; "
                         f"known: {sorted(scenarios)}")
    pack = scenarios[scenario]

    if not dynasties:
        raise ValueError("need at least one dynasty")

    pack.create_content(session)

    for d in dynasties:
        entity = pack.make_house(session, d.name)
        entity.owner_id = d.user_id          # the join-tool pattern: the
        d.entity_id = entity.id               # seat becomes a player's own
        seed = (seeds or {}).get(d.name)
        source = pack._read_lua(pack.STARTER)
        if seed is not None:
            from econengine import scripting
            libs = {"world": pack._read_lua("world_lib.lua"),
                    "pack": pack._read_lua("pack.lua")}
            problems = scripting.validate_script_source(seed, libs)
            if problems:
                raise ValueError(
                    f"seed script for {d.name} rejected: {problems}")
            source = seed
        session.add(Script(
            name=f"house-behaviour-{entity.id}",
            script_type=ScriptType.BEHAVIOUR,
            source=source,
            entity_id=entity.id,
            timeout_ms=200,
            state={},
        ))

    # The players' clock (game.md §9.1): rounds close when every dynasty
    # consents. Operator fiat at world creation — mode is data, not code.
    session.add(WorldSetting(key="round.gate", value={"mode": "readiness"}))
    session.commit()

    # The pack's MANUAL (world.manual WorldSetting), if it ships one: the
    # authored notes -- strategy and seams -- that ride under the
    # generated catalog (the 3a prompt fold: tables derive, meaning
    # stays authored). Rival privacy (multi's own read below) is a pack
    # decision, not an agent-client one.
    manual_row = session.get(WorldSetting, getattr(pack, "MANUAL_KEY", "world.manual"))
    return read_world_meta(session)


def read_world_meta(session) -> dict:
    """The manual + rendered catalog from a BUILT world — the same read
    build_agent_world returns, for a process that attaches to an
    existing world (nim_run --resume) instead of building one."""
    from econengine.models import WorldSetting

    manual_row = session.get(WorldSetting, "world.manual")
    return {
        "manual": (manual_row.value or {}).get("text") if manual_row else None,
        # The readable world, rendered at read time from the same shared
        # read the REST catalog and MCP surface serve (never stored, so
        # it can never drift from the physics it renders).
        "catalog": catalog_text(catalog_state(session)),
    }


# ---------------------------------------------------------------------------
# The run: one authoring thread per dynasty, rounds close on the world's
# clock (or its consent), snapshots every closure
# ---------------------------------------------------------------------------

# The worker's cond-wait tick: how often a parked seat rechecks the
# world's closed-round counter (the main thread notifies on every
# closure, so this is a belt, not the mechanism).
_WORKER_POLL_S = 0.05

# Clock mode's grace: how long the main thread waits for an in-request
# resolution (a mixed launch whose gate still closes rounds on consent)
# before parking on the world's clock. Long enough for real consent
# round-trips in tests; noise against a 60s round.
_CONSENT_GRACE_S = 2.0

# The final join: a seat mid-ladder at the run's end gets this long to
# land its last submission, then is left as a daemon — the run's
# artifacts are already on disk.
_JOIN_S = 2.0

@dataclass
class RoundSnapshot:
    """Everything the dashboard eats, for one resolved round."""
    round: int
    ticks: list[int]
    resolved: dict
    market: list[dict]
    dynasties: dict[str, dict]            # house name -> view (below)
    events_by_type: dict[str, int] = field(default_factory=dict)
    taken_at: str = ""
    # The world's condition goods (world_catalog's machine-readable
    # flag): HUNGER, WARMTH, ... held like any good but read as a state
    # of the holder, not inventory. The dashboard splits these out of
    # the holdings breakdown.
    conditions: list[str] = field(default_factory=list)
    # Each condition's kill line (world_catalog's incapacitates_at,
    # #213): the level at which the holder is incapacitated. The
    # dashboard draws its condition meters against these — a climb
    # reads as a health bar draining. Conditions without one
    # (or a world that predates the field) simply render no meter.
    kill_lines: dict[str, str] = field(default_factory=dict)
    # The rendered audit-trail tail (§15.3): this round's readable world
    # log — the unattributed public facts, plus each dynasty's own events
    # as prose. Bounded by the round's own ticks; the dashboard renders
    # it with a per-dynasty filter, so the artifact carries the round's
    # whole story offline.
    activity: dict = field(default_factory=dict)
    # The map (docs/spatial.md): places, roads, and every entity's
    # location at the round's end — public facts off the world_map tool.
    # The dashboard draws the map and a per-round location strip from
    # it; a resumed pre-map run's old snapshots simply lack the key.
    world_map: dict = field(default_factory=dict)
    # The non-house cast, same source as the map (run 50's lesson: the
    # merchant post IS the lever, but the dashboard only ever showed
    # the dynasties — answering "how is the post doing" meant reading
    # the world database by hand). Businesses get the full state read
    # (accounts, holdings — the SHOP_OPEN lamp among them — processes);
    # wildlife is counted off the map's statuses. Anything a world
    # without businesses simply leaves empty.
    cast: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "round": self.round, "ticks": self.ticks,
            "resolved": self.resolved, "market": self.market,
            "events_by_type": self.events_by_type,
            "taken_at": self.taken_at,
            "dynasties": self.dynasties,
            "activity": self.activity,
            "conditions": self.conditions,
            "kill_lines": self.kill_lines,
            "world_map": self.world_map,
            "cast": self.cast,
        }


def _dynasty_view(mcp: McpClient, d: Dynasty, entry: dict | None) -> dict:
    """One dynasty's slice of the round, from its own MCP surface."""
    eid = d.entity_id
    state = mcp.call("entity_state", {"entity_id": eid})
    try:
        behaviour = mcp.call("get_behaviour", {"entity_id": eid})
    except McpError:
        behaviour = {}
    board = mcp.call("leaderboard")
    row = next((r for r in board.get("rows", []) if r["user_id"] == d.user_id), None)
    return {
        "model": d.model_name,
        "leaderboard": row,
        "accounts": state.get("accounts", []),
        "holdings": state.get("holdings", []),
        "needs": state.get("needs", []),
        "processes": state.get("processes", []),
        "parcels": len(state.get("parcels", [])),
        "unlocks": state.get("unlocks", []),
        "behaviour": {
            "sha": hashlib.sha256(
                (behaviour.get("source") or "").encode()).hexdigest()[:16],
            "description": behaviour.get("description"),
            "state": behaviour.get("state"),
            "source": behaviour.get("source"),
        },
        "entry": entry,               # this round's journal entry (or failure)
    }


def _snapshot(mcps: list[tuple[Dynasty, McpClient]], resolved: dict,
              entries: dict[str, dict]) -> RoundSnapshot:
    market = mcps[0][1].call("market_prices")
    try:
        catalog = mcps[0][1].call("world_catalog").get("goods", [])
        conditions = sorted(g["symbol"] for g in catalog if g.get("condition"))
        kill_lines = {g["symbol"]: g["incapacitates_at"] for g in catalog
                      if g.get("condition") and g.get("incapacitates_at")}
    except McpError:
        conditions = []
        kill_lines = {}
    # The audit-trail tail (§15.3): this round's rendered world log, read
    # back through the very surfaces that serve it (GET /activity and
    # entity_activity are the same render). Bounded by the round's own
    # tick span, capped at the tool's 50-tick maximum.
    tail = min(max(1, len(resolved.get("ticks") or [1])), 50)
    try:
        world = mcps[0][1].call(
            "world_activity", {"last_ticks": tail}).get("activity", [])
    except McpError:
        world = []
    dyn_activity: dict[str, list] = {}
    # The map (docs/spatial.md): public facts — places, roads, every
    # entity's place at the round's end. Same try/McpError tolerance as
    # the other optional sections: an older world keeps rendering.
    try:
        world_map = mcps[0][1].call("world_map")
    except McpError:
        world_map = {}
    for d, mcp in mcps:
        try:
            # witnessed=True (game.md 15.6): each house's log also carries
            # what was DELIVERED to it -- speech and loud facts -- so the
            # dashboard's world log shows the conversation, each side
            # hearing what that side heard.
            dyn_activity[d.name] = mcp.call(
                "entity_activity",
                {"entity_id": d.entity_id, "last_ticks": tail,
                 "witnessed": True},
            ).get("activity", [])
        except McpError:
            dyn_activity[d.name] = []
    # The non-house cast (run 50): every business on the map gets a
    # state read (accounts, holdings, processes) off the same MCP
    # surface the houses use; wildlife is a status count off the map.
    # Optional like the map itself: an older world keeps rendering.
    cast: dict = {"businesses": [], "wildlife": {}}
    for ent in (world_map.get("entities") or []):
        name, etype = ent.get("name", ""), ent.get("entity_type", "")
        if etype == "business":
            try:
                st = mcps[0][1].call("entity_state", {"entity_id": ent["id"]})
                cast["businesses"].append({
                    "name": name, "place": ent.get("place"),
                    "status": ent.get("status"),
                    "accounts": st.get("accounts", []),
                    "holdings": st.get("holdings", []),
                    "processes": st.get("processes", []),
                })
            except McpError:
                pass
        for beast in ("Wolf", "Boar"):
            if beast.lower() in name.lower():
                cur = cast["wildlife"].setdefault(beast, {"active": 0, "down": 0})
                key = "active" if ent.get("status") == "active" else "down"
                cur[key] += 1
    snap = RoundSnapshot(
        round=resolved["round_number"], ticks=resolved.get("ticks", []),
        resolved=resolved, market=market,
        events_by_type=resolved.get("events_by_type", {}),
        taken_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        conditions=conditions,
        kill_lines=kill_lines,
        dynasties={d.name: _dynasty_view(mcp, d, entries.get(d.name))
                   for d, mcp in mcps},
        activity={"world": world, "dynasties": dyn_activity},
        world_map=world_map,
        cast=cast,
    )
    return snap


def _extinct_entry(d: Dynasty, round_no: int) -> dict:
    """The turn an extinct dynasty gets: none. No model call, no
    observation, no submission — the dead keep their last behaviour
    (kept_old) and the journal says what happened. From run 3 on this
    matters twice over: dead houses were burning provider calls for
    rounds they could not act in, and their 'keep' entries read as
    strategy in the dashboard when they were tombstones."""
    return {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "entity": d.entity_id, "model": d.model_name, "round": round_no,
        "attempts": 0, "accepted": False, "kept_old": True,
        "action": "extinct", "refusal": None, "warnings": [],
        "source_sha": None, "thoughts": "", "prompt_bytes": 0,
    }


def _decide(d: Dynasty, lp: AgentLoop, round_no: int) -> dict:
    """One dynasty's decision turn, ready to run on its own thread: the
    dynasties' cycles are independent (each reads only its own entity's
    surface), so their LLM latencies can overlap instead of summing.
    An extinct dynasty (status != active) is skipped before any model
    call: the dead get no turn."""
    try:
        state = lp.mcp.call("entity_state", {"entity_id": d.entity_id})
        if (state.get("entity") or {}).get("status") != "active":
            return _extinct_entry(d, round_no)
        return lp.cycle()
    except Exception as exc:                # provider death: keep playing
        return {
            "entity": d.entity_id, "model": d.model_name,
            "round": round_no, "attempts": 0, "accepted": False,
            "kept_old": True, "refusal": f"model failure: {exc}",
            "warnings": [], "source_sha": None, "prompt_bytes": 0,
        }


def _missed_entry(d: Dynasty, round_no: int) -> dict:
    """The honest journal line for a seat that was still mid-authoring
    when the round closed: it missed the window, its behaviour rolled
    forward unchanged (a keep), and its submission — when it lands —
    applies to the rounds still to come. Run 52 made these invisible
    (a communal barrier meant rounds only closed with every seat's
    entry in hand, or — once the barrier wedged — not at all); now the
    snapshot says so instead of going quiet."""
    return {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "entity": d.entity_id, "model": d.model_name, "round": round_no,
        "attempts": 0, "accepted": False, "kept_old": True,
        "action": "missed_window", "refusal": None, "warnings": [],
        "source_sha": None, "thoughts": "", "prompt_bytes": 0,
    }


# Seat worker phases, as the main thread reads them:
#   deciding       — a call is in flight; its consent may still close a
#                    round (readiness mode waits for exactly this)
#   readying       — submission landed, consent call in flight (the
#                    final ready may resolve the round any moment)
#   ready_waiting  — submitted and consented for its target; waiting
#                    for the world to close a round
#   extinct        — the dynasty is dead; the worker has parked for good
#   done           — the worker exited (run target reached / stopped)
_SETTLED = {"ready_waiting", "extinct", "done"}


@dataclass
class _RunState:
    """The shared state between the main thread (round bookkeeping) and
    the per-dynasty authoring workers: the world's closed-round counter
    the workers chase, each seat's phase and last-submitted round, and
    the resolutions a worker's final ready produced (readiness mode's
    in-request round closures)."""
    dynasties: dict[str, Dynasty]          # name -> seat
    rounds: int
    closed: int
    cond: threading.Condition = field(default_factory=threading.Condition)
    stop: bool = False
    phases: dict[str, str] = field(default_factory=dict)
    submitted: dict[str, int] = field(default_factory=dict)
    entries: dict[str, list[dict]] = field(default_factory=dict)
    resolutions: list[dict] = field(default_factory=list)
    ready_refusals: dict[str, str] = field(default_factory=dict)
    # Consent calls serialize: the server's readiness register is a
    # read-modify-write (two parallel set_readies can drop one consent,
    # and the round would never close). Consent ORDER is free — the
    # gate resolves in whichever ready lands last — so this costs a
    # round trip, never an authoring barrier.
    ready_lock: threading.Lock = field(default_factory=threading.Lock)

    def everyone_settled(self) -> bool:
        return bool(self.phases) and all(p in _SETTLED for p in
                                         self.phases.values())

    def all_extinct(self) -> bool:
        return bool(self.phases) and all(p == "extinct"
                                         for p in self.phases.values())

    def snapshot_entries(self, round_no: int) -> dict[str, dict]:
        """Each dynasty's journal entry for a closing round: the entry
        it authored FOR this round if one landed in time; else — a
        tombstone for the dead (the dashboard counts them as neither
        refusals nor keeps) and an honest missed-window keep for the
        living (run 52's rounds 6-10 closed with no entry at all; the
        snapshot now says what happened instead of going quiet)."""
        with self.cond:
            out = {}
            for name, d in self.dynasties.items():
                if self.phases.get(name) == "extinct":
                    out[name] = _extinct_entry(d, round_no)
                    continue
                landed = [e for e in self.entries[name]
                          if e.get("round", 0) <= round_no]
                out[name] = landed[-1] if landed else _missed_entry(d,
                                                                    round_no)
            return out


def _seat_worker(d: Dynasty, lp: AgentLoop, state: _RunState) -> None:
    """One dynasty's authoring cadence, free of every other seat: read
    the world, author and submit for the next round it hasn't closed,
    signal consent, repeat — at the seat's own pace. A submission that
    lands after its round closed applies going forward (a keep for the
    rounds slept through); a slow seat costs its own house those
    rounds, never another seat's. The dead park for good."""
    name = d.name
    try:
        while True:
            with state.cond:
                while True:
                    if state.stop:
                        state.phases[name] = "done"
                        return
                    target = state.closed + 1
                    if target > state.rounds:
                        state.phases[name] = "done"
                        return
                    if state.submitted.get(name, 0) < target:
                        break           # we owe the world a submission
                    state.cond.wait(_WORKER_POLL_S)
                state.phases[name] = "deciding"
            entry = _decide(d, lp, target)   # the long part: no lock held
            with state.cond:
                state.entries[name].append(entry)
                state.submitted[name] = target
                dead = entry.get("action") == "extinct"
                state.phases[name] = ("extinct" if dead else "readying")
                state.cond.notify_all()
            if dead:
                return                      # the dead get no turn
            try:
                with state.ready_lock:
                    out_ready = lp.set_ready()
                with state.cond:
                    if out_ready.get("resolved"):
                        state.resolutions.append(out_ready["resolved"])
                    state.phases[name] = "ready_waiting"
                    state.cond.notify_all()
            except Exception as exc:
                # consent-channel death rides like a model death: the
                # entry says so, the seat keeps playing
                with state.cond:
                    entry.setdefault("refusal", f"set_ready refused: {exc}")
                    state.ready_refusals[name] = f"set_ready refused: {exc}"
                    state.phases[name] = "ready_waiting"
                    state.cond.notify_all()
    except Exception:
        # transport death mid-flight: this seat is out of the game, but
        # it must not take the run with it — mark it settled and go
        with state.cond:
            state.phases[name] = "done"
            state.cond.notify_all()


def _extinction_brake(snapshots: list[dict]) -> None:
    """Run 16's ending, still the rule: a world nobody is left to act
    in stops cleanly at its last resolved round instead of parking on
    a gate (or a clock) that will never close another round."""
    last = snapshots[-1]["round"] if snapshots else 0
    print(f"  total extinction — no dynasty left to act; "
          f"stopping after round {last}")


def _no_consent(state: _RunState, admin_advance, round_no: int) -> dict:
    """Everyone settled without a closure: the referee's moment. With
    `admin_advance` the world is pushed; without it the run says what
    the gate refused to do (the run-16 shape) and dies loudly."""
    if admin_advance is not None:
        return admin_advance(round_no)
    refusals = "; ".join(
        f"{name}: {msg}" for name, msg in sorted(state.ready_refusals.items()))
    raise RuntimeError(
        f"round {round_no} did not resolve after every dynasty "
        "readied (gate not in readiness mode?)"
        + (f" -- readies refused: {refusals}" if refusals else ""))


def _await_next_round(state: _RunState, clock_wait, admin_advance,
                      after_round: int, snapshots: list[dict]):
    """The main thread's whole job between rounds: take the next round
    closure from whichever clock produces it — a seat's final ready
    (in-request resolution; mixed launches), the world's own ticks
    (clock mode's park), or the referee (everyone consented, nothing
    closed). The extinction brake fires before any park. Resolutions
    at or behind `after_round` are stale races — dropped, not replayed."""
    deadline = (None if clock_wait is None
                else time.monotonic() + _CONSENT_GRACE_S)
    while True:
        expired = False
        with state.cond:
            while state.resolutions:
                r = state.resolutions.pop(0)
                if r.get("round_number", 0) > after_round:
                    return r
            if state.all_extinct():
                _extinction_brake(snapshots)
                return None
            if deadline is None:
                # Readiness mode: only consent moves the world — wait
                # for a ready to resolve, or discover nobody ever will.
                if state.everyone_settled():
                    return _no_consent(state, admin_advance,
                                       after_round + 1)
                state.cond.wait(_WORKER_POLL_S)
            else:
                # Clock mode: in-request resolution preempts the park
                # for a grace (a mixed launch whose gate still
                # resolves), then the world's clock owns advancement.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    expired = True
                else:
                    state.cond.wait(min(_WORKER_POLL_S, remaining))
        if expired:
            return clock_wait(after_round)


def run_rounds(loops: list[tuple[Dynasty, AgentLoop]], rounds: int,
               out_dir: str | Path,
               admin_advance=None, on_round=None,
               start_round: int = 1,
               snapshots: list[dict] | None = None,
               clock_wait=None) -> list[dict]:
    """Rounds until the WORLD has completed N (default 1..N; a resumed
    run continues at start_round and passes the rounds already on disk
    as `snapshots` so `on_round` and the return value carry the WHOLE
    run — the dashboard never forgets round 1 because the process
    restarted). One authoring thread per dynasty (§ the worker): each
    seat reads the world, submits for the next round it hasn't closed,
    and consents — at its own pace, with no communal barrier. A seat
    slower than its round misses the window: its behaviour keeps
    running (journaled as `missed_window`, kept_old) and its late
    submission rolls forward to the rounds still open. Rounds close on
    the world's own ticks in clock mode (`clock_wait`, §9.2 — the
    closure may be for a LATER round than any seat played; snapshots
    are labeled with the round that actually closed) or on the final
    consent in a readiness-gated world (§9.1); `admin_advance` stays
    the referee fallback for a round everyone consented to that still
    didn't close. A dynasty whose model hard-fails (network, provider)
    keeps its behaviour, journals the failure, and STILL readies — one
    dead model must not stop the world, and now one SLOW one must not
    stop the others either. `on_round(snapshots)` fires after each
    resolved round with the full list so far — the live dashboard is
    rewritten from it. Without clock_wait the consent path is
    bit-identical to before."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mcps = [(d, lp.mcp) for d, lp in loops]
    snapshots = snapshots if snapshots is not None else []

    state = _RunState(
        dynasties={d.name: d for d, _ in loops}, rounds=rounds,
        closed=start_round - 1,
        phases={d.name: "deciding" for d, _ in loops},
        submitted={d.name: start_round - 1 for d, _ in loops},
        entries={d.name: [] for d, _ in loops},
    )
    workers = [threading.Thread(target=_seat_worker, args=(d, lp, state),
                                daemon=True, name=f"seat-{d.name}")
               for d, lp in loops]
    for w in workers:
        w.start()

    rounds_done = start_round - 1
    try:
        while rounds_done < rounds:
            resolved = _await_next_round(state, clock_wait, admin_advance,
                                         rounds_done, snapshots)
            if resolved is None:
                break               # the extinction brake fired
            if resolved.get("round_number", 0) <= rounds_done:
                continue            # a stale race — already bookkept
            # The world's counter, not the iteration count, drives the
            # run: consent resolves exactly one round, but a clocked
            # world (or an admin advance into a new gate) may close
            # LATER rounds. Publishing it first lets the seats start
            # their next submission while this round's snapshot is
            # still being read.
            rounds_done = resolved["round_number"]
            with state.cond:
                state.closed = rounds_done
                state.cond.notify_all()
            snap = _snapshot(mcps, resolved,
                             state.snapshot_entries(rounds_done))
            (out / f"round-{resolved['round_number']:02d}.json").write_text(
                json.dumps(snap.to_json(), indent=1))
            snapshots.append(snap.to_json())
            kinds = ", ".join(f"{k}×{v}" for k, v in
                              sorted(snap.events_by_type.items())) or "quiet"
            first_t = snap.ticks[0] if snap.ticks else "?"
            print(f"  round {snap.round} resolved (ticks {first_t}.."
                  f"{snap.ticks[-1] if snap.ticks else '?'}): {kinds}")
            if on_round is not None:
                on_round(snapshots)
    finally:
        with state.cond:
            state.stop = True
            state.cond.notify_all()
        for w in workers:
            w.join(timeout=_JOIN_S)   # a straggler mid-ladder is left as a
        # daemon: the run's artifacts are on disk, and the old code's
        # alternative — blocking the whole run on the slowest call — is
        # exactly the behavior this loop replaced
    return snapshots


# ---------------------------------------------------------------------------
# Wealth arithmetic for the dashboard (money + holdings at last prices)
# ---------------------------------------------------------------------------

def dynasty_money(view: dict) -> Decimal:
    return sum((Decimal(a["balance"]) for a in view.get("accounts", [])),
               Decimal("0"))


def dynasty_assets(view: dict, prices: dict[str, Decimal]) -> Decimal:
    total = Decimal("0")
    for h in view.get("holdings", []):
        qty = Decimal(h["quantity"])
        price = prices.get(h["symbol"])
        if price is not None and qty > 0:
            total += qty * price
    return total


def price_table(market: list[dict]) -> dict[str, Decimal]:
    return {m["symbol"]: Decimal(m["last_price"])
            for m in market if m.get("last_price") is not None}
