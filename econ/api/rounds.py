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
from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from econ.api import epochs as epochs_mod
from econengine.models import Tick, WorldSetting
from econengine.tick import run_tick

ROUND_STATE_KEY = "round.state"
DEFAULT_TICKS_PER_ROUND = 10


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

    # Upsert the round counter. WorldSetting.value is plain JSON (not a
    # mutable wrapper), so reassign the whole dict to persist.
    value = {"round_number": rounds_before + 1, "status": "submit"}
    row = session.get(WorldSetting, ROUND_STATE_KEY)
    if row is None:
        session.add(WorldSetting(key=ROUND_STATE_KEY, value=value))
    else:
        row.value = value
    session.flush()

    # Dynasty-extinction scan: once per round, after the batch (§14.2).
    eliminations = epochs_mod.scan_eliminations(session, tick_numbers[-1])

    return {
        "round_number": rounds_before + 1,   # the round just completed
        "ticks": tick_numbers,
        "events": total_events,
        "events_by_type": dict(events_by_type),
        "next_round": rounds_before + 2,
        "ticks_per_round": k,
        "victory_stamps": stamps_made,
        "eliminations": eliminations,
    }
