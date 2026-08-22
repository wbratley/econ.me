"""The audit trail — a read, not a write path (Phase 3b, game.md §15.3).

`GET /entities/{id}/activity`: scan ticks descending, filter events by
entity attribution, render through the action registry with a catalog
join. Rejections are included — an attempt is an action. The world-level
`GET /activity` carries the unattributed public facts (auction
summaries, decay, auto-issue): the same public/private cut as §13 — your
log is your own events; the world's log is public facts.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from econ.api.activity import activity_rows
from econ.api.deps import get_current_user, get_session
from econ.api.routers.entities import _own_entity
from econengine.models import User

router = APIRouter(tags=["activity"])


@router.get("/entities/{entity_id}/activity")
def entity_activity(
    entity_id: str,
    last_ticks: int = Query(default=50, ge=1, le=200),
    witnessed: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The entity's own log: every attributed action, rendered as prose.
    Own entity only — your log is your events (§13). ``?witnessed=1``
    widens the window with what was DELIVERED to the entity (game.md
    §15.6: speech and loud facts) — flagged, and never more than the
    witness table recorded."""
    entity = _own_entity(entity_id, current_user, session)
    return {
        "entity_id": entity.id,
        "activity": activity_rows(session, entity.id, last_ticks,
                                  witnessed=witnessed),
    }


@router.get("/activity")
def world_activity(
    last_ticks: int = Query(default=20, ge=1, le=200),
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The world's log: the unattributed public facts (auctions, decay,
    auto-issue), rendered. No dynasty's private affairs ride along."""
    return {"activity": activity_rows(session, None, last_ticks)}
