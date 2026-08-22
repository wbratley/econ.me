"""Witness delivery (game.md 15.6): who perceived which event.

The engine stays the single event writer (the hash chain); delivery is
a *derived* fact, recorded at emission time -- whether you heard the
wolf attack depends on where you were THAT tick, so visibility freezes
with the tick rather than being re-derived at read time from a world
that has since moved on. Rows in this table never enter ``events_hash``.

v1 scope rule: broadcast, observable types only (speech and loud
facts). Distance, line-of-sight and networks are later delivery rules
-- the seam is ``record_delivery``; the table is the log of who knew
what when.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Entity, EntityStatus, EventObserver

#: The observable vocabulary: event types the world carries to other
#: entities. Everything else (script errors, refusals, private need
#: detail) stays with its actor -- rival privacy is layered, not
#: repealed, by the witness feed. New loud facts (wolf attacks) join
#: here as content lands.
OBSERVABLE_EVENT_TYPES: frozenset[str] = frozenset({"say", "entity_incapacitated"})


def _observable(event: dict) -> bool:
    if event.get("type") not in OBSERVABLE_EVENT_TYPES:
        return False
    # An intent event that was refused never happened -- the refusal
    # stays in the actor's own log, but the world heard nothing.
    if event.get("status") == "rejected":
        return False
    return True


def record_delivery(session: Session, tick_number: int, events: list[dict]) -> int:
    """Freeze delivery for one finalized tick: every observable event is
    delivered to every ACTIVE entity. Returns rows written (v1 fan-out
    is small; the point is the record, not the routing)."""
    audience = session.execute(
        select(Entity.id).where(Entity.status == EntityStatus.ACTIVE)
    ).scalars().all()
    rows = 0
    for index, event in enumerate(events):
        if not _observable(event):
            continue
        for observer_id in audience:
            session.add(EventObserver(
                tick_number=tick_number,
                event_index=index,
                observer_id=observer_id,
            ))
            rows += 1
    return rows


def witnessed_indexes(session: Session, tick_number: int,
                      observer_id: str) -> set[int]:
    """The events of one tick that were delivered to one entity."""
    return set(session.execute(
        select(EventObserver.event_index).where(
            EventObserver.tick_number == tick_number,
            EventObserver.observer_id == observer_id,
        )
    ).scalars())


def script_feed(session: Session, tick_number: int, entity_id: str,
                events: list[dict]) -> list[dict]:
    """The BEHAVIOUR script's window on a tick: its own events, plus
    whatever was delivered to it (speech, loud facts), deduplicated in
    event order. POLICY scripts keep seeing everything, as ever."""
    seen = witnessed_indexes(session, tick_number, entity_id)
    return [
        e for index, e in enumerate(events)
        if e.get("entity_id") == entity_id or index in seen
    ]
