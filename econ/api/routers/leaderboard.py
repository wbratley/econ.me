"""Leaderboard endpoints (docs/game.md §14.5; Phase 2c).

The standings are a standing query, not a per-round payload (§14.5):
``RoundSummary`` already publishes the round's events, and the leaderboard
is read on demand -- before deciding, after advancing, any time. This is a
pure platform read over engine tables plus the immutable registers; the
engine is untouched.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from econ.api.deps import get_current_user, get_session
from econ.api.leaderboard import leaderboard_state
from econ.api.schemas import LeaderboardRead
from econengine.models import User

router = APIRouter(tags=["leaderboard"])


@router.get("/leaderboard", response_model=LeaderboardRead)
def leaderboard(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The standings: one row per dynasty, ranked by epoch wins then money.

    An in-world fact, public to authenticated players -- every column is
    something the world already witnessed (balances, entity counts, ages,
    unlocks, victory stamps). No dynasty's private affairs ride along
    (no omniscience, §13): holdings, scripts and events stay per-player.
    """
    return LeaderboardRead(**leaderboard_state(session))
