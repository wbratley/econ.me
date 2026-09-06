"""The round scheduler -- the platform's batched-tick clock (game.md §9).

The engine advances one tick at a time on demand (``run_tick``). The
**round** is the platform's grouping: one round = a submit window (players
edit behaviour / queue votes) followed by resolving K ticks in a batch. This
module owns that clock -- it calls ``run_tick`` K times per advance and
tracks which round we are in. The engine is untouched by all of this; this
is pure platform over ``run_tick``.

Two things that are deliberately *not* the same kind of thing:

  * **Pace (K)** is **deployment config** (``ECON_TICKS_PER_ROUND``), not a
    WorldSetting: it is a property of "how fast the server runs," not "what
    kind of world this is" -- exactly like the server caps (game.md §9).
  * **Round state** (the round counter) is **runtime state** -- it lives in
    the ``round.state`` WorldSetting, the same store the platform uses for
    council registers and the join config. Reads are pure derivation (no
    side effects: a GET never persists); only an advance persists.

The round counter is authoritative and independent of raw ticks: one
advance = one round = K ticks. Ticks run via the low-level ``POST
/admin/ticks`` escape hatch move the tick clock but not the round counter --
a round is a scheduler batch, not a tick quotient.
"""
from __future__ import annotations

import os
import time
from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from econ.api import epochs as epochs_mod
from econengine.models import Entity, EntityStatus, Tick, WorldSetting
from econengine.tick import run_tick

ROUND_STATE_KEY = "round.state"
DEFAULT_TICKS_PER_ROUND = 10

#: The deadline backstop (game.md §9.1): how many seconds a round may
#: stay open after its last consent-reset before the server closes it
#: itself. Deployment pace like ECON_TICKS_PER_ROUND (env, not a
#: WorldSetting): 0/absent/invalid = off, and existing worlds never see
#: a server-closed round -- consent or the operator still does it.
DEADLINE_ENV = "ECON_ROUND_DEADLINE_S"

#: The readiness gate (game.md §9.1): rounds close by player consent in
#: ``readiness`` mode; ``operator`` mode (the default) keeps the clock
#: purely in the operator's hands, so existing worlds are unchanged.
READINESS_KEY = "round.readiness"
GATE_KEY = "round.gate"
GATE_MODES = ("readiness", "operator")


class NotEligibleError(Exception):
    """The user owns no ACTIVE entity -- they have no agency in the round
    (spectator, or an eliminated dynasty) and cannot signal readiness."""


def ticks_per_round() -> int:
    """K -- ticks resolved per round. Deployment config (env), default 10.

    Not a WorldSetting: pace is an operator/deployment knob, not world
    content (game.md §9). Falls back to the default if unset, blank, or
    non-positive.
    """
    raw = os.environ.get("ECON_TICKS_PER_ROUND")
    if not raw:
        return DEFAULT_TICKS_PER_ROUND
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TICKS_PER_ROUND
    return n if n > 0 else DEFAULT_TICKS_PER_ROUND


def round_deadline_s() -> float:
    """Seconds a round may stay open before the server closes it (0 =
    the backstop is off; negative/invalid reads as off too — a bad env
    value must not turn into a zero-second world)."""
    raw = os.environ.get(DEADLINE_ENV)
    if not raw:
        return 0.0
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return n if n > 0 else 0.0


def _rounds_completed(session: Session) -> int:
    """How many rounds the scheduler has resolved (0 at genesis).

    Read-only derivation from the ``round.state`` WorldSetting; never
    persists. Absent => 0.
    """
    row = session.get(WorldSetting, ROUND_STATE_KEY)
    if row is None:
        return 0
    return int(dict(row.value).get("round_number", 0))


def _ticks_run(session: Session) -> int:
    """Total committed ticks, from the Tick table (0 before tick 1)."""
    n = session.execute(select(func.max(Tick.number))).scalar_one_or_none()
    return int(n) if n is not None else 0


# ---------------------------------------------------------------------------
# The readiness gate (game.md §9.1)
# ---------------------------------------------------------------------------

def gate_mode(session: Session) -> str:
    """Who closes rounds: ``"readiness"`` (player consent) or ``"operator"``
    (admin advance only; the default). A WorldSetting, not env -- *who must
    consent* is world policy; the readiness *machinery* is mechanism and
    ships once either way."""
    row = session.get(WorldSetting, GATE_KEY)
    if row is None:
        return "operator"
    mode = str(dict(row.value).get("mode", "operator"))
    return mode if mode in GATE_MODES else "operator"


def set_gate_mode(session: Session, mode: str) -> None:
    """Operator-set world policy (§9.1). Raises ``ValueError`` on a mode
    outside ``GATE_MODES``; the caller maps that to 422."""
    if mode not in GATE_MODES:
        raise ValueError(f"mode must be one of {GATE_MODES}, not {mode!r}")
    row = session.get(WorldSetting, GATE_KEY, with_for_update=True)
    if row is None:
        session.add(WorldSetting(key=GATE_KEY, value={"mode": mode}))
    else:
        row.value = {"mode": mode}
    session.flush()


def eligible_users(session: Session) -> set[str]:
    """Users who own at least one ACTIVE entity -- the consent set for the
    gate. One test excludes both eliminated dynasties (no ACTIVE entities:
    they cannot block a world they no longer play) and spectators (no
    agency in the round)."""
    rows = session.execute(
        select(Entity.owner_id).where(
            Entity.status == EntityStatus.ACTIVE,
            Entity.owner_id.is_not(None),
        )
    ).scalars()
    return set(rows)


def _readiness_register(session: Session) -> tuple[int, list[str]]:
    """The raw persisted register: ``(round, ready_user_ids)``.
    ``(0, [])`` before the first ready ever recorded."""
    return _read_register(session)[:2]


def _read_register(session: Session) -> tuple[int, list[str], float | None]:
    """The register with its clock anchor: ``(round, ready_user_ids,
    opened_at_epoch)``. ``opened_at`` is when the round's submit window
    opened — written at every legitimate reset (advance, stale-register
    normalization, deadline anchoring) so the deadline backstop always
    has a wall-clock anchor. ``None`` on registers from before this
    field existed (a world mid-migration: the next reset anchors it)."""
    row = session.get(WorldSetting, READINESS_KEY)
    if row is None:
        return 0, [], None
    value = dict(row.value)
    opened = value.get("opened_at")
    return (int(value.get("round", 0)), [str(u) for u in value.get("ready", [])],
            float(opened) if isinstance(opened, (int, float)) else None)


def _write_readiness_register(session: Session, round_no: int, ready: list[str],
                              opened_at: float | None = None) -> None:
    """Persist the register, locking its row so two simultaneous final-ready
    POSTs serialize on it (SQLite's single writer does this for free; the
    ``with_for_update`` is for a future Postgres)."""
    row = session.get(WorldSetting, READINESS_KEY, with_for_update=True)
    value = {"round": round_no, "ready": list(ready)}
    if opened_at is not None:
        value["opened_at"] = opened_at
    elif row is not None and row.value is not None:
        prior = dict(row.value).get("opened_at")  # preserve across rewrites
        if isinstance(prior, (int, float)):
            value["opened_at"] = prior
    if row is None:
        session.add(WorldSetting(key=READINESS_KEY, value=value))
    else:
        row.value = value
    session.flush()


def readiness_state(session: Session) -> dict[str, Any]:
    """The gate's public face (a pure read; never persists). Readiness is
    per-round consent, so a register left over from an older round reads as
    empty (normalization is in-memory only -- GETs never persist)."""
    current = _rounds_completed(session) + 1
    reg_round, ready_raw = _readiness_register(session)
    if reg_round != current:
        ready_raw = []
    eligible = eligible_users(session)
    ready = sorted(u for u in set(ready_raw) if u in eligible)
    return {
        "mode": gate_mode(session),
        "round": current,
        "ready": len(ready),
        "eligible": len(eligible),
        "ready_users": ready,
    }


def set_user_ready(session: Session, user_id: str) -> dict[str, Any]:
    """Record the caller as ready for the round open at call time -- the
    server derives the round, so a straggler POST that races an advance
    simply lands in the new round instead of corrupting anything. The
    final ready resolves the round in-request and the response carries the
    round summary. Idempotent; raises ``NotEligibleError`` for a user with
    no ACTIVE entity."""
    if user_id not in eligible_users(session):
        raise NotEligibleError(
            "No ACTIVE entity owned -- spectators and eliminated dynasties "
            "cannot signal readiness (own an active entity to gain a voice)"
        )
    current = _rounds_completed(session) + 1
    reg_round, ready = _readiness_register(session)
    if reg_round != current:          # stale (post-advance) -- start clean
        ready = []
    if user_id not in ready:
        ready.append(user_id)
    # A first consent on a round the register predates opens the window
    # officially now (the deadline anchor the backstop will count from).
    opened_at = time.time() if reg_round != current else None
    _write_readiness_register(session, current, ready, opened_at=opened_at)

    state = readiness_state(session)
    resolved = None
    if (
        state["mode"] == "readiness"
        and state["eligible"] > 0          # empty set never blocks (genesis)
        and state["ready"] == state["eligible"]
    ):
        resolved = advance_round(session)  # resets the register (below)
        state = readiness_state(session)
    return {"user_id": user_id, "round": current,
            "readiness": state, "resolved": resolved}


def unset_user_ready(session: Session, user_id: str) -> dict[str, Any]:
    """Withdraw readiness for the round open at call time. Idempotent, and
    a no-op once the round has resolved -- readiness is historical then
    (§9.1: un-ready is allowed *until the advance fires*)."""
    current = _rounds_completed(session) + 1
    reg_round, ready = _readiness_register(session)
    if reg_round == current and user_id in ready:
        ready.remove(user_id)
        _write_readiness_register(session, current, ready)
    return {"user_id": user_id, "round": current,
            "readiness": readiness_state(session)}


def current_round_state(session: Session) -> dict[str, Any]:
    """The round clock's current state (a pure read; never persists).

      * ``round_number``     -- rounds completed so far (0 at genesis)
      * ``current_round``    -- the round open for submission = round_number + 1
      * ``status``           -- "submit" between advances (v0; resolving is
                                instant within an advance request). Ready for
                                time-based windows later.
      * ``ticks_run``        -- total ticks committed (from the Tick table)
      * ``ticks_per_round``  -- K (deployment config)
      * ``ticks_into_round`` -- ticks already run in the current round's batch
                                (negative if raw ticks ran past the last round
                                via the single-tick escape hatch)
      * ``readiness``       -- the gate (§9.1): mode, how many of the eligible
                                players have readied, and who (public facts,
                                like prices and standings)
    """
    k = ticks_per_round()
    rounds_done = _rounds_completed(session)
    ticks_run = _ticks_run(session)
    return {
        "round_number": rounds_done,
        "current_round": rounds_done + 1,
        "status": "submit",
        "ticks_run": ticks_run,
        "ticks_per_round": k,
        "ticks_into_round": ticks_run - rounds_done * k,
        "readiness": readiness_state(session),
    }


def advance_round(session: Session) -> dict[str, Any]:
    """Resolve one round: run K ticks in a batch, mark a round complete.

    Atomic with respect to the caller's commit: all K ticks and the round
    counter flush together, so the endpoint's single ``commit()`` makes the
    whole round durable (or rolls it all back on failure).

    Returns a summary of the round just completed: its number, the tick
    numbers it ran, its events broken down by type, and any victory
    stamps / elimination records the observer made while resolving it.
    """
    k = ticks_per_round()
    rounds_before = _rounds_completed(session)

    tick_numbers: list[int] = []
    events_by_type: Counter[str] = Counter()
    total_events = 0
    stamps_made: list[dict[str, Any]] = []
    for _ in range(k):
        tick = run_tick(session)
        tick_numbers.append(tick.number)
        for event in tick.events or []:
            total_events += 1
            events_by_type[str(event.get("type", "unknown"))] += 1
        # The victory observer runs after EACH tick, not once per round
        # (game.md §14.2): a crossing that dips back below before the batch
        # ends still counts. It stops itself the moment the epoch ends.
        stamps_made.extend(epochs_mod.observe_tick(session, tick.number))

    # Population renews at the round boundary (spawns.py): what the
    # world's SPAWN_RULES call for, after the round's ticks committed.
    from econengine import spawns as spawns_mod
    spawns_mod.apply_on_round(session, rounds_before + 1)

    # Upsert the round counter. WorldSetting.value is plain JSON (not a
    # mutable wrapper), so reassign the whole dict to persist. The payload
    # carries N (rounds per window) so scripts -- the content-pack clerk
    # (§14.4) -- derive the governance calendar from the same channel they
    # already read; env stays the single source, re-projected each advance.
    from econ.api import governance as governance_mod  # deferred: imports this module
    value = {
        "round_number": rounds_before + 1,
        "status": "submit",
        "rounds_per_window": governance_mod.rounds_per_window(),
    }
    row = session.get(WorldSetting, ROUND_STATE_KEY)
    if row is None:
        session.add(WorldSetting(key=ROUND_STATE_KEY, value=value))
    else:
        row.value = value
    session.flush()

    # Reset the readiness register for the now-open round (§9.1): readiness
    # is per-round consent, and a resolved round's consents are historical.
    # Runs on every advance -- operator override and final-ready alike.
    # The new round's submit window opens NOW; the summary carries the
    # epoch so the post-commit event broadcast can state the deadline.
    opened_at = time.time()
    _write_readiness_register(session, rounds_before + 2, [], opened_at=opened_at)

    # Dynasty-extinction scan: once per round, after the batch (§14.2).
    eliminations = epochs_mod.scan_eliminations(session, tick_numbers[-1])

    return {
        "round_number": rounds_before + 1,   # the round just completed
        "ticks": tick_numbers,
        "events": total_events,
        "events_by_type": dict(events_by_type),
        "next_round": rounds_before + 2,
        "ticks_per_round": k,
        "next_opened_at": opened_at,
        "victory_stamps": stamps_made,
        "eliminations": eliminations,
    }


# ---------------------------------------------------------------------------
# The deadline backstop (§9.1) -- the server closes rounds nobody closes
# ---------------------------------------------------------------------------

def maybe_auto_advance(session: Session, now: float | None = None) -> dict[str, Any] | None:
    """Close the open round by deadline, if the backstop is armed and the
    clock has run out. Consent stays king: a full-consent resolve happens
    in-request at the final ready and never needs this; the backstop exists
    for the seat that never shows (M2a: external/human seats).

    Returns the advance summary when it closed a round, ``None`` otherwise
    -- including every "not yet" case: backstop off, gate in operator
    mode, deadline not elapsed, or **no eligible users** (the extinction
    brake: a world whose last dynasty died stops, it does not tick a
    corpse forever).

    May persist without advancing: a round whose register predates the
    deadline machinery (or was reset without an anchor) gets anchored
    ``opened_at`` on the first look -- the caller should commit either
    way. Callers should treat check+advance as one transaction (SQLite's
    single writer makes the flush serialize; a busy-timeout loss rolls
    back and the next scheduler poll retries).
    """
    deadline = round_deadline_s()
    if deadline <= 0 or gate_mode(session) != "readiness":
        return None
    if not eligible_users(session):
        return None                      # nobody left to play for
    now = time.time() if now is None else now
    current = _rounds_completed(session) + 1
    reg_round, ready, opened_at = _read_register(session)
    if reg_round != current or opened_at is None:
        # First sight of this round's window: anchor it, don't punish it.
        _write_readiness_register(session, current, ready, opened_at=now)
        session.flush()
        return None
    if now < opened_at + deadline:
        return None
    return advance_round(session)
