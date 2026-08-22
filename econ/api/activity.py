"""The audit-trail read (Phase 3b, game.md §15.3): events → rendered rows.

Shared by the REST activity endpoints and MCP ``entity_activity`` — one
read, two surfaces (the leaderboard pattern). A pure read: ticks are
scanned newest-first, events filtered by attribution, and rendered
through the action registry with the catalog name join.

``witnessed=True`` (game.md §15.6) widens the entity's window: its own
events PLUS the events delivered to it (speech, loud facts) from the
witness table — what it heard, not what rivals did. Delivered rows are
flagged and carry the actor's id; the world log (``entity_id=None``) is
unaffected: it stays the unattributed public facts.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine.catalog import catalog_state
from econengine.describe import render_event, symbol_names
from econengine.models import EventObserver, Tick


def activity_rows(session: Session, entity_id: str | None, last_ticks: int,
                  witnessed: bool = False) -> list[dict]:
    """Rendered activity: the entity's own events (own id) or the world's
    unattributed public facts (``None``), newest tick first."""
    ticks = session.execute(
        select(Tick).order_by(Tick.number.desc()).limit(last_ticks)
    ).scalars().all()
    names = symbol_names(catalog_state(session))

    delivered: dict[int, set[int]] = {}
    if witnessed and entity_id is not None:
        for row in session.execute(
            select(EventObserver).where(
                EventObserver.tick_number.in_([t.number for t in ticks]),
                EventObserver.observer_id == entity_id,
            )
        ).scalars():
            delivered.setdefault(row.tick_number, set()).add(row.event_index)

    rows = []
    for tick in sorted(ticks, key=lambda t: t.number, reverse=True):
        for index, event in enumerate(tick.events or []):
            own = event.get("entity_id") == entity_id
            if own:
                rows.append({
                    "tick": tick.number,
                    "type": event.get("type"),
                    "text": render_event(event, names),
                    **({"witnessed": False} if witnessed else {}),
                })
            elif index in delivered.get(tick.number, ()):
                # Heard, not done: flag it and name the actor, so the
                # reader can tell speech from its own actions.
                rows.append({
                    "tick": tick.number,
                    "type": event.get("type"),
                    "text": render_event(event, names),
                    "witnessed": True,
                    "entity_id": event.get("entity_id"),
                })
    return rows
