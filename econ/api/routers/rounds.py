"""Round scheduler endpoints -- the platform's batched-tick clock (game.md §9).

The operator drives the clock (advance a round = resolve K ticks); players
observe it (which round is open for submission) and -- in readiness mode
(§9.1) -- close it by consent: the final ready resolves the round, the
deadline backstop closes one nobody closes, and every resolution is
announced on the SSE event stream. The engine is untouched; this is pure
platform over ``run_tick``.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from econ.api import events
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
    events.publish_round_closed(jsonable_encoder(summary))
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
    events.publish("readiness", jsonable_encoder(out["readiness"]))
    if out["resolved"] is not None:
        events.publish_round_closed(jsonable_encoder(out["resolved"]))
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
    events.publish("readiness", jsonable_encoder(out["readiness"]))
    return UnreadyResponse(**out)


@router.get("/rounds/events")
async def round_events(
    session: Session = Depends(get_session),
):
    """The always-on host's voice: an SSE stream of round events, public
    read-only (round numbers and readiness are public facts, §9.1 -- this
    is what lets a waiting seat, a dashboard, or a plain ``curl`` hear the
    world without polling).

    On connect the stream states where the world is (a ``hello`` snapshot
    of the round clock -- closes the subscribe/next-event race without
    Last-Event-ID machinery), then forwards whatever the pub/sub emits:
    ``readiness`` as seats consent, ``round_closed``/``round_opened`` as
    rounds resolve -- with the deadline epoch when the backstop is armed,
    so a seat can budget its wall-clock. Comment heartbeats every 15s
    keep proxies from reaping an idle stream. The session closes before
    streaming starts; nothing DB-shaped is held for the stream's life.
    """
    hello = jsonable_encoder({"type": "hello",
                             **current_round_state(session)})
    session.close()                       # hold no connection while streaming

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    async def stream():
        q = events.subscribe()
        try:
            yield sse("hello", hello)
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                else:
                    yield sse(event, data)
        finally:
            events.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


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
