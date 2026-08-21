"""The catalog endpoint (Phase 3a, game.md §15.1).

`GET /catalog`: the whole readable world, rendered by
`econengine.catalog.catalog_state` — goods with derived condition effect
lines, recipes (inputs → outputs, duration, gates, branch odds), the tech
tree, needs, and markets. An in-world fact, public to authenticated
players: the world's vocabulary is what every script already reads.
Rendered text is computed at read time and never enters the hash chain.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from econ.api.deps import get_current_user, get_session
from econengine.catalog import catalog_state
from econengine.models import User

router = APIRouter(tags=["catalog"])


@router.get("/catalog")
def catalog(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The readable world: names, descriptions, and derived effect lines
    for every good, recipe, technology, need, and market."""
    return catalog_state(session)
