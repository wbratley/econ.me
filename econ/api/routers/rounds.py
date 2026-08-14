"""Round scheduler endpoints -- the platform's batched-tick clock (game.md §9).

The operator drives the clock (advance a round = resolve K ticks); players
observe it (which round is open for submission). The engine is untouched --
this is pure platform over ``run_tick``.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.rounds import advance_round, current_round_state
from econ.api.schemas import RoundState, RoundSummary
from econengine.models import User

router = APIRouter(tags=["rounds"])


@router.post("/admin/rounds/advance", response_model=RoundSummary, status_code=201)
def advance(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Resolve one round: run K ticks in a batch and mark a round complete.

    K is deployment config (``ECON_TICKS_PER_ROUND``, default 10). The whole
    round -- all K ticks and the round counter -- commits atomically."""
    summary = advance_round(session)
    session.commit()
    return RoundSummary(**summary)


@router.get("/admin/rounds/current", response_model=RoundState)
def admin_current_round(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """The round clock (admin view): which round, how many ticks run."""
    return RoundState(**current_round_state(session))


@router.get("/rounds/current", response_model=RoundState)
def current_round(
    session: Session = Depends(get_session),
    _: User = Depends(get_current_user),
):
    """The round clock, for players: which round is open for submission.

    A player reads this to know the world is accepting behaviour edits /
    queued votes before the next advance resolves them in a batch."""
    return RoundState(**current_round_state(session))
