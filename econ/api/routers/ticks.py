from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from econ.api.deps import get_session, require_admin
from econ.api.schemas import TickRead
from econ.models import Tick, User
from econ.tick import run_tick

router = APIRouter(prefix="/admin/ticks", tags=["ticks"])


@router.post("", response_model=TickRead, status_code=201)
def advance_tick(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Run all active behaviour scripts and resolve their intents."""
    tick = run_tick(session)
    session.commit()
    session.refresh(tick)
    return tick


@router.get("", response_model=list[TickRead])
def list_ticks(
    limit: int = 50,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    return session.execute(
        select(Tick).order_by(Tick.number.desc()).limit(limit)
    ).scalars().all()


@router.get("/{number}", response_model=TickRead)
def get_tick(
    number: int,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    tick = session.execute(
        select(Tick).where(Tick.number == number)
    ).scalar_one_or_none()
    if tick is None:
        raise HTTPException(status_code=404, detail="Tick not found")
    return tick
