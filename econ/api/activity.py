"""The audit-trail read (Phase 3b, game.md §15.3): events → rendered rows.

Shared by the REST activity endpoints and MCP ``entity_activity`` — one
read, two surfaces (the leaderboard pattern). A pure read: ticks are
scanned newest-first, events filtered by attribution, and rendered
through the action registry with the catalog name join.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine.catalog import catalog_state
from econengine.describe import render_event, symbol_names
from econengine.models import Tick


def activity_rows(session: Session, entity_id: str | None, last_ticks: int) -> list[dict]:
    """Rendered activity: the entity's own events (own id) or the world's
    unattributed public facts (``None``), newest tick first."""
    ticks = session.execute(
        select(Tick).order_by(Tick.number.desc()).limit(last_ticks)
    ).scalars().all()
    names = symbol_names(catalog_state(session))
    rows = []
    for tick in sorted(ticks, key=lambda t: t.number, reverse=True):
        for event in tick.events or []:
            if event.get("entity_id") == entity_id:
                rows.append({
                    "tick": tick.number,
                    "type": event.get("type"),
                    "text": render_event(event, names),
                })
    return rows
