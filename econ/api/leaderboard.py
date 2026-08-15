"""The leaderboard -- per-dynasty standings (docs/game.md §14.5; Phase 2c).

A pure platform read over engine tables plus the immutable registers: no
new surface, no writes, nothing the engine must know about. A dynasty is
``Entity.owner_id`` -- the same scope the victory observer judges (§14.2),
which already propagates through both birth paths (join and spawn), so
the standings and the wins can never disagree about *who* played.

One row per dynasty:

  * ``money``          -- sum of account balances across ACTIVE entities.
                          The *same* definition the observer's
                          ``accumulate`` condition judges
                          (``epochs.dynasty_money``): the leaderboard must
                          never show a different fortune than the one a
                          win was stamped on.
  * ``entities``       -- ACTIVE / total owned entities.
  * ``oldest_age``     -- the oldest lineage age in the dynasty
                          (``tick - birth_tick``; entities predating
                          age tracking -- NULL ``birth_tick`` -- are
                          excluded; None if the dynasty tracks none).
  * ``unlocks``        -- distinct technologies unlocked by owned
                          entities. Entity-scoped only: a world-scope
                          unlock is held by the world, not a dynasty (the
                          same join the observer's ``innovate`` uses).
  * ``epoch_wins``     -- the player's stamps in ``victory.stamps``
                          across *all* epochs. A stamp **is** the win
                          (§14.2); co-winners each carry theirs.
  * ``status``         -- ``active`` (>= 1 ACTIVE entity) /
                          ``eliminated`` (stamped in the *running*
                          epoch's elimination register) / ``extinct``
                          (dynasty dead, not in the running register --
                          an earlier epoch's elimination, or death before
                          any epoch). Active wins over eliminated: a
                          living dynasty is never mislabelled by a stale
                          stamp.

**Public facts only** (§13): every column is a standing, not a secret --
the same facts the observer judged publicly. No per-dynasty detail beyond
the standings row (no holdings, no scripts, no events).

Standings order (deterministic): epoch wins desc, then money desc, then
user id ascending. Wins first: an epoch's whole point is the victory
condition (§7), and across epochs the win count is the durable ranking;
money breaks ties within it.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from econ.api.epochs import dynasty_money, get_epoch_state, get_stamps
from econengine.models import Entity, EntityStatus, Tick, Unlock


def _latest_tick(session: Session) -> int:
    n = session.execute(select(func.max(Tick.number))).scalar_one_or_none()
    return int(n) if n is not None else 0


def _dynasty_counts(session: Session, user_id: str) -> tuple[int, int]:
    """(active, total) owned entities."""
    total = int(session.execute(
        select(func.count()).select_from(Entity).where(Entity.owner_id == user_id)
    ).scalar_one())
    active = int(session.execute(
        select(func.count()).select_from(Entity).where(
            Entity.owner_id == user_id, Entity.status == EntityStatus.ACTIVE
        )
    ).scalar_one())
    return active, total


def _dynasty_oldest_age(session: Session, user_id: str, tick: int) -> int | None:
    """Oldest lineage age in the dynasty, or None if no member is tracked.

    ``birth_tick`` NULL means "predates age tracking" (Step 6a) -- such
    entities have no honest age, so they are skipped rather than read as
    zero (which would make every dynasty look newly founded).
    """
    oldest_birth = session.execute(
        select(func.min(Entity.birth_tick)).where(
            Entity.owner_id == user_id, Entity.birth_tick.isnot(None)
        )
    ).scalar_one_or_none()
    return (tick - oldest_birth) if oldest_birth is not None else None


def _dynasty_unlocks(session: Session, user_id: str) -> int:
    """Distinct technologies unlocked by owned entities (entity-scoped).

    The inner join on Entity drops world-scope unlocks (``entity_id``
    NULL): those are held by the world, not the dynasty -- the same rule
    the observer's ``innovate`` condition applies.
    """
    return int(session.execute(
        select(func.count(func.distinct(Unlock.technology_id)))
        .join(Entity, Unlock.entity_id == Entity.id)
        .where(Entity.owner_id == user_id)
    ).scalar_one())


def leaderboard_state(session: Session) -> dict[str, Any]:
    """The standings as a pure read (never persists).

    Dynasties are derived from ``Entity.owner_id`` (never from the User
    table): a player who has never joined owns nothing and has no row --
    the leaderboard ranks dynasties, not accounts.
    """
    epoch = get_epoch_state(session)
    running_epoch = (
        int(epoch["number"]) if epoch is not None and epoch.get("ended_tick") is None else None
    )
    # epoch_wins: one per stamp, across all epochs (a stamp *is* a win).
    wins: dict[str, int] = {}
    for stamp in get_stamps(session):
        wins[stamp.get("user_id", "")] = wins.get(stamp.get("user_id", ""), 0) + 1
    eliminated_now: set[str] = set()
    if epoch is not None and epoch.get("ended_tick") is None:
        from econ.api.epochs import get_eliminations
        eliminated_now = {
            rec.get("user_id") for rec in get_eliminations(session)
            if rec.get("epoch") == epoch["number"]
        }

    tick = _latest_tick(session)
    rows: list[dict[str, Any]] = []
    owner_ids = sorted(session.execute(
        select(Entity.owner_id)
        .where(Entity.owner_id.isnot(None))
        .distinct()
    ).scalars().all())

    for user_id in owner_ids:
        active, total = _dynasty_counts(session, user_id)
        if active > 0:
            status = "active"
        elif user_id in eliminated_now:
            status = "eliminated"
        else:
            status = "extinct"
        rows.append({
            "user_id": user_id,
            "money": str(dynasty_money(session, user_id)),  # exact, JSON-safe
            "entities_active": active,
            "entities_total": total,
            "oldest_age": _dynasty_oldest_age(session, user_id, tick),
            "unlocks": _dynasty_unlocks(session, user_id),
            "epoch_wins": wins.get(user_id, 0),
            "status": status,
        })

    rows.sort(key=lambda r: (-r["epoch_wins"], -Decimal(r["money"]), r["user_id"]))
    return {
        "epoch_number": int(epoch["number"]) if epoch is not None else 0,
        "epoch_running": epoch is not None and epoch.get("ended_tick") is None,
        "rows": rows,
    }
