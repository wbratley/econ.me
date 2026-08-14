"""Epoch endpoints (docs/game.md §7, §14; Phase 2a).

The operator owns the epoch lifecycle: start one under a declared victory
condition, close one early if a run must end without a winner. Players read
the epoch as a world fact -- plus their own elimination status, the one
dynasty-specific bit (and it is theirs).

The condition is set once, at start, and frozen for the epoch's life
(§14.1); there is deliberately no amend endpoint. Enactment of wins is not
here either: the observer inside the round scheduler stamps crossings
(§14.2) -- **a win is something the engine witnesses, not something the
polity votes**, and not something an endpoint does on request.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.epochs import (
    EpochError,
    close_epoch,
    get_epoch_state,
    player_eliminated_in_running_epoch,
    start_epoch,
)
from econ.api.schemas import EpochRead, EpochStart, EpochStatusRead
from econengine.models import User

router = APIRouter(tags=["epochs"])


def _as_read(state: dict | None) -> EpochRead:
    if state is None:
        return EpochRead(running=False, number=0)
    return EpochRead(
        running=state.get("ended_tick") is None,
        number=int(state["number"]),
        condition=state.get("condition"),
        started_tick=int(state.get("started_tick", 0)),
        ended_tick=state.get("ended_tick"),
        winner_user_ids=list(state.get("winner_user_ids", [])),
    )


@router.post("/admin/epochs", response_model=EpochRead, status_code=201)
def start(
    body: EpochStart,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Start the next epoch under the given victory condition.

    Refused (409) while an epoch still runs, and 422 for any condition off
    the §7 menu (validated once, here -- the observer never sees a
    malformed spec). ``started_tick`` is the tick count at start, so only
    ticks resolved *after* the declaration are judged: no retroactive wins.
    """
    try:
        state = start_epoch(session, body.code, body.params)
    except EpochError as exc:
        # Distinguish "already running" (a lifecycle conflict) from a bad
        # condition spec (a validation failure) by message content.
        if "still running" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    return _as_read(state)


@router.post("/admin/epochs/close", response_model=EpochRead)
def close(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Close the running epoch without a winner (operator action).

    The epoch boundary is the fresh start: ended epochs make their
    elimination register historical, so eliminated players may rejoin."""
    try:
        state = close_epoch(session)
    except EpochError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()
    return _as_read(state)


@router.get("/admin/epochs/current", response_model=EpochRead)
def admin_current(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """The epoch state (admin view)."""
    return _as_read(get_epoch_state(session))


@router.get("/epochs/current", response_model=EpochStatusRead)
def current(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The epoch, for players: the declared condition, whether it has been
    won (and by whom), and whether *you* were eliminated this epoch.

    An in-world fact plus the caller's own status -- no other dynasty's
    affairs are visible (no omniscience, §13)."""
    read = _as_read(get_epoch_state(session))
    return EpochStatusRead(
        **read.model_dump(),
        eliminated_this_epoch=player_eliminated_in_running_epoch(session, current_user.id),
    )
