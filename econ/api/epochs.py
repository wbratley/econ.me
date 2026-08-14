"""Epochs and the victory observer (docs/game.md §7, §14; Phase 2a).

A win is an **engine-witnessed, immutable historical fact, never a vote**
(game.md §7). This module is the observer that makes that real: it
evaluates the epoch's achievement condition against each dynasty after
every tick the round scheduler resolves, and *stamps* genuine crossings.

Three WorldSetting registers, none of them writable by any script:

  * ``epoch.state``         -- the running (or last) epoch:
       {number, condition, started_tick, ended_tick, winner_user_ids}
  * ``victory.stamps``      -- append-only list of
       {epoch, user_id, tick, code, value}  -- **the stamp *is* the win**
  * ``epoch.eliminations``  -- append-only list of {epoch, user_id, tick}

**Immutability by surface absence** (§14 preamble): no Lua action writes
arbitrary WorldSettings -- the only script-writable keys ride dedicated
intents (``set_fiscal_policy``, ``set_constitution``, ...). A register
under a key no intent can reach is immutable the same way "capabilities
don't breed" is: there is no path, not a promise. The observer is the only
writer, and it only appends.

The condition is **data, set once**: the operator starts an epoch with a
``{code, params}`` spec from the §7 victory menu; it is frozen for the
epoch's life. Mid-epoch amendment is explicitly post-v0 (§14.1) --
not-amendable-at-all is the stronger lock-in.

Evaluation is pure reads over engine tables, scoped to a dynasty
(``Entity.owner_id``, which already propagates through both birth paths).
Only players with at least one ACTIVE entity are candidates: the
eliminated cannot win a *future* crossing, but a stamp already made stands
forever (append-only).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from econengine.models import (
    Account,
    Entity,
    EntityStatus,
    Technology,
    Tick,
    Unlock,
    WorldSetting,
)

EPOCH_STATE_KEY = "epoch.state"
VICTORY_STAMPS_KEY = "victory.stamps"
ELIMINATIONS_KEY = "epoch.eliminations"

#: The §7 victory menu (§14.2). ``rule`` is deliberately absent -- it waits
#: on the office model (§13 open question).
VICTORY_CODES = {"accumulate", "innovate", "endure", "grow"}


class EpochError(ValueError):
    """Invalid epoch operation (bad condition spec, epoch already running)."""


# ---------------------------------------------------------------------------
# Register access -- WorldSetting.value is plain JSON, so every read copies
# and every write reassigns the whole structure.
# ---------------------------------------------------------------------------

def get_epoch_state(session: Session) -> dict[str, Any] | None:
    """The current/last epoch's state, or None if no epoch was ever started.

    An epoch **runs** iff state exists and ``ended_tick`` is None. Absent
    state means the world simply plays without a victory condition -- the
    observer is inert until the operator starts one.
    """
    row = session.get(WorldSetting, EPOCH_STATE_KEY)
    if row is None:
        return None
    return dict(row.value)


def get_stamps(session: Session) -> list[dict[str, Any]]:
    row = session.get(WorldSetting, VICTORY_STAMPS_KEY)
    return list(row.value) if row else []


def get_eliminations(session: Session) -> list[dict[str, Any]]:
    row = session.get(WorldSetting, ELIMINATIONS_KEY)
    return list(row.value) if row else []


def _append(session: Session, key: str, record: dict[str, Any]) -> None:
    row = session.get(WorldSetting, key)
    records = list(row.value) if row else []
    records.append(record)
    if row is None:
        session.add(WorldSetting(key=key, value=records))
    else:
        row.value = records


def _last_tick(session: Session) -> int:
    """Total committed ticks (0 before tick 1)."""
    n = session.execute(select(func.max(Tick.number))).scalar_one_or_none()
    return int(n) if n is not None else 0


# ---------------------------------------------------------------------------
# Starting / closing an epoch (operator-only; §14.1)
# ---------------------------------------------------------------------------

def normalise_condition(session: Session, code: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise a ``{code, params}`` achievement spec.

    Returns the normalised spec stored in ``epoch.state``; raises
    ``EpochError`` on anything not on the §7 menu. Validation happens once,
    at start time -- the condition is frozen afterwards, so the observer
    never has to defend against malformed specs.
    """
    code = str(code).strip().lower()
    if code not in VICTORY_CODES:
        raise EpochError(f"unknown victory code {code!r}; menu: {sorted(VICTORY_CODES)}")
    params = dict(params or {})

    if code == "accumulate":
        try:
            threshold = Decimal(str(params.get("threshold")))
        except Exception:
            raise EpochError("accumulate requires a numeric 'threshold'")
        if threshold <= 0:
            raise EpochError("accumulate threshold must be positive")
        return {"code": code, "params": {"threshold": str(threshold)}}

    if code == "innovate":
        technology = str(params.get("technology", "")).strip().upper()
        if not technology:
            raise EpochError("innovate requires a 'technology' code")
        exists = session.execute(
            select(Technology.id).where(Technology.code == technology)
        ).first()
        if exists is None:
            raise EpochError(f"innovate: unknown technology {technology!r}")
        return {"code": code, "params": {"technology": technology}}

    if code == "endure":
        try:
            ticks = int(params.get("ticks"))
        except Exception:
            raise EpochError("endure requires an integer 'ticks'")
        if ticks <= 0:
            raise EpochError("endure ticks must be positive")
        return {"code": code, "params": {"ticks": ticks}}

    # grow
    try:
        threshold = int(params.get("threshold"))
    except Exception:
        raise EpochError("grow requires an integer 'threshold'")
    if threshold <= 0:
        raise EpochError("grow threshold must be positive")
    return {"code": code, "params": {"threshold": threshold}}


def start_epoch(session: Session, code: str, params: dict[str, Any]) -> dict[str, Any]:
    """Start the next epoch under the given condition (operator action).

    Fails if an epoch is still running. ``started_tick`` is the tick count
    at start, so the observer only judges ticks resolved *after* the
    condition was declared -- no retroactive wins (§7 defence 3).
    """
    current = get_epoch_state(session)
    if current is not None and current.get("ended_tick") is None:
        raise EpochError(
            f"epoch {current['number']} is still running; close it before starting another"
        )
    condition = normalise_condition(session, code, params)
    number = 1 if current is None else int(current["number"]) + 1
    state = {
        "number": number,
        "condition": condition,
        "started_tick": _last_tick(session),
        "ended_tick": None,
        "winner_user_ids": [],
    }
    row = session.get(WorldSetting, EPOCH_STATE_KEY)
    if row is None:
        session.add(WorldSetting(key=EPOCH_STATE_KEY, value=state))
    else:
        row.value = state
    session.flush()
    return state


def close_epoch(session: Session) -> dict[str, Any]:
    """Close the running epoch without a winner (operator action, §14.1).

    The epoch boundary is the fresh start: ended epochs make their
    elimination register historical, so eliminated players may rejoin.
    """
    state = get_epoch_state(session)
    if state is None or state.get("ended_tick") is not None:
        raise EpochError("no epoch is running")
    state["ended_tick"] = _last_tick(session)
    row = session.get(WorldSetting, EPOCH_STATE_KEY)
    row.value = state
    session.flush()
    return state


# ---------------------------------------------------------------------------
# The observer -- pure reads, called once per resolved tick (§14.2)
# ---------------------------------------------------------------------------

def _dynasty_money(session: Session, user_id: str) -> Decimal:
    """Sum of account balances across the dynasty's ACTIVE entities."""
    total = session.execute(
        select(func.coalesce(func.sum(Account.balance), 0))
        .join(Entity, Account.entity_id == Entity.id)
        .where(Entity.owner_id == user_id, Entity.status == EntityStatus.ACTIVE)
    ).scalar_one()
    return Decimal(total)


def _dynasty_has_unlock(session: Session, user_id: str, technology: str) -> bool:
    """Does any owned entity hold an unlock of the technology?

    Any status: an unlock is a monotonic historical fact -- if the
    researcher died after the discovery, the fact stands. World-scope
    unlocks (``Unlock.entity_id`` NULL) are held by the world, not a
    dynasty, so they do not count (inner join on Entity).
    """
    hit = session.execute(
        select(Unlock.id)
        .join(Technology, Unlock.technology_id == Technology.id)
        .join(Entity, Unlock.entity_id == Entity.id)
        .where(Technology.code == technology, Entity.owner_id == user_id)
        .limit(1)
    ).first()
    return hit is not None


def _dynasty_size(session: Session, user_id: str) -> int:
    return int(session.execute(
        select(func.count())
        .select_from(Entity)
        .where(Entity.owner_id == user_id, Entity.status == EntityStatus.ACTIVE)
    ).scalar_one())


def _candidates(session: Session) -> list[str]:
    """Users with >= 1 ACTIVE owned entity: the elimination filter (§14.2)."""
    return sorted(session.execute(
        select(Entity.owner_id)
        .where(Entity.owner_id.isnot(None), Entity.status == EntityStatus.ACTIVE)
        .distinct()
    ).scalars().all())


def _crossing(session: Session, state: dict[str, Any], user_id: str, tick_number: int) -> Any | None:
    """The dynasty's metric value if it crosses the condition, else None."""
    cond = state["condition"]
    code = cond["code"]
    params = cond["params"]

    if code == "accumulate":
        total = _dynasty_money(session, user_id)
        return total if total >= Decimal(params["threshold"]) else None

    if code == "innovate":
        return params["technology"] if _dynasty_has_unlock(session, user_id, params["technology"]) else None

    if code == "endure":
        endured = tick_number - int(state["started_tick"])
        return endured if endured >= int(params["ticks"]) else None

    if code == "grow":
        size = _dynasty_size(session, user_id)
        return size if size >= int(params["threshold"]) else None

    return None  # unreachable: conditions are validated at start


def observe_tick(session: Session, tick_number: int) -> list[dict[str, Any]]:
    """Evaluate the running condition against every dynasty after a tick.

    Called by the round scheduler after **each** of the K ticks it resolves
    (§14.2): per *tick*, not per round -- an ``accumulate`` crossing that
    dips back below before the batch ends still counts, which is the
    anti-flash-dump defence (§7.1) operationalized. Monotonic conditions
    (innovate, endure, grow) are crossing-safe by construction.

    On genuine crossings: stamp every crosser (same-tick ties co-stamp as
    co-winners -- a tie is a result, not a dispute), end the epoch at this
    tick, stop observing. Returns the stamps made (empty if none).
    """
    state = get_epoch_state(session)
    if state is None or state.get("ended_tick") is not None:
        return []
    if tick_number <= int(state["started_tick"]):
        return []  # only judge ticks resolved after the condition was declared

    crossings: list[tuple[str, Any]] = []
    for user_id in _candidates(session):
        value = _crossing(session, state, user_id, tick_number)
        if value is not None:
            crossings.append((user_id, value))

    if not crossings:
        return []
    crossings.sort(key=lambda pair: pair[0])  # deterministic order

    stamps = [
        {"epoch": state["number"], "user_id": user_id, "tick": tick_number,
         "code": state["condition"]["code"],
         # Decimals stringify at stamp time (exact value, JSON-safe).
         "value": str(value) if isinstance(value, Decimal) else value}
        for user_id, value in crossings
    ]
    for stamp in stamps:
        _append(session, VICTORY_STAMPS_KEY, stamp)

    state["ended_tick"] = tick_number
    state["winner_user_ids"] = [user_id for user_id, _ in crossings]
    row = session.get(WorldSetting, EPOCH_STATE_KEY)
    row.value = state
    session.flush()
    return stamps


# ---------------------------------------------------------------------------
# The elimination scan -- once per round (§14.2/§14.3)
# ---------------------------------------------------------------------------

def scan_eliminations(session: Session, tick_number: int) -> list[dict[str, Any]]:
    """Stamp players whose dynasty went extinct during the running epoch.

    A player is eliminated when they own entities, **none** ACTIVE, and
    they demonstrably took part in *this* epoch: at least one entity was
    born in it (``birth_tick > started_tick``) or died in it
    (``incapacitated_tick > started_tick``). Participation is reconstructed
    from immutable columns, so a player eliminated in a *previous* epoch
    (dead before this one began) is not re-stamped and may rejoin
    immediately -- the epoch boundary is the fresh start (§14.3).

    Append-only and deduplicated per epoch: once stamped, a player is not
    stamped again even if the round advances again.
    """
    state = get_epoch_state(session)
    if state is None or state.get("ended_tick") is not None:
        return []
    started = int(state["started_tick"])
    number = state["number"]

    already = {
        rec["user_id"] for rec in get_eliminations(session)
        if rec.get("epoch") == number
    }

    records: list[dict[str, Any]] = []
    owner_ids = sorted(session.execute(
        select(Entity.owner_id)
        .where(Entity.owner_id.isnot(None))
        .distinct()
    ).scalars().all())

    for user_id in owner_ids:
        if user_id in already:
            continue
        entities = list(session.execute(
            select(Entity).where(Entity.owner_id == user_id)
        ).scalars().all())
        if any(e.status == EntityStatus.ACTIVE for e in entities):
            continue  # dynasty lives
        took_part = any(
            (e.incapacitated_tick is not None and e.incapacitated_tick > started)
            or (e.birth_tick is not None and e.birth_tick > started)
            for e in entities
        )
        if not took_part:
            continue
        records.append({"epoch": number, "user_id": user_id, "tick": tick_number})

    for record in records:
        _append(session, ELIMINATIONS_KEY, record)
    return records


def player_eliminated_in_running_epoch(session: Session, user_id: str) -> bool:
    """The §14.3 rejoin check: was this user eliminated in the running epoch?

    True only while that epoch still runs; once it ends the register is
    historical and the player may found again.
    """
    state = get_epoch_state(session)
    if state is None or state.get("ended_tick") is not None:
        return False
    return any(
        rec.get("epoch") == state["number"] and rec.get("user_id") == user_id
        for rec in get_eliminations(session)
    )
