"""Round scheduler endpoints -- the platform's batched-tick clock (game.md §9).

The operator drives the clock (advance a round = resolve K ticks); players
observe it (which round is open for submission) and -- in readiness mode
(§9.1) -- close it by consent: the final ready resolves the round. The
engine is untouched; this is pure platform over ``run_tick``.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.rounds import (
    NotEligibleError, advance_round, current_round_state, gate_mode,
    set_gate_mode, set_user_ready, unset_user_ready,
)
from econ.api.schemas import (
    GateMode, GateModeUpdate, ReadyResponse, RoundState, RoundSummary,
    UnreadyResponse,
)
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


# ===========================================================================
# The readiness gate (game.md §9.1) -- rounds close by player consent
# ===========================================================================

@router.post("/rounds/ready", response_model=ReadyResponse)
def ready(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Signal readiness for the round open now. The server derives the
    round (race-proof); idempotent. In readiness mode the **final ready
    resolves the round in this request** and the response carries the
    round summary (201); otherwise it merely records consent (200)."""
    try:
        out = set_user_ready(session, user.id)
    except NotEligibleError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    status = 201 if out["resolved"] is not None else 200
    return JSONResponse(status_code=status, content=jsonable_encoder(out))


@router.delete("/rounds/ready", response_model=UnreadyResponse)
def unready(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Withdraw readiness for the round open now. Idempotent; a no-op once
    the round has resolved (§9.1: un-ready allowed until the advance fires)."""
    out = unset_user_ready(session, user.id)
    session.commit()
    return UnreadyResponse(**out)


@router.get("/admin/rounds/gate", response_model=GateMode)
def get_gate(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Who closes rounds: ``readiness`` (player consent) or ``operator``."""
    return GateMode(mode=gate_mode(session))


@router.put("/admin/rounds/gate", response_model=GateMode)
def put_gate(
    update: GateModeUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Set the gate's mode -- operator-set world policy (§9.1). Flipping to
    ``readiness`` never advances implicitly: the gate fires on the next
    ready (or the operator's advance, which always works)."""
    set_gate_mode(session, update.mode)
    session.commit()
    return GateMode(mode=update.mode)
