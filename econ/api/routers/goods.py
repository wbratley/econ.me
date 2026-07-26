from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine import goods
from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.schemas import GoodCreate, GoodRead, GoodUpdate
from econengine.models import Good, User

router = APIRouter(tags=["goods"])

_QUANTUM = Decimal("0.0001")


def _good_or_404(session: Session, symbol: str) -> Good:
    good = goods.get_good(session, symbol)
    if good is None:
        raise HTTPException(status_code=404, detail="Good not found")
    return good


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail=f"Invalid {field}")


# ---------------------------------------------------------------------------
# Goods — public data, admin-managed
# ---------------------------------------------------------------------------

@router.get("/goods", response_model=list[GoodRead])
def list_goods(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return session.execute(select(Good).order_by(Good.symbol)).scalars().all()


@router.get("/goods/{symbol}", response_model=GoodRead)
def get_good(
    symbol: str,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _good_or_404(session, symbol)


@router.post("/admin/goods", response_model=GoodRead, status_code=201, tags=["admin"])
def create_good(
    body: GoodCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    if goods.get_good(session, body.symbol) is not None:
        raise HTTPException(status_code=409, detail="Good already exists")
    try:
        good = goods.create_good(
            session,
            body.symbol,
            name=body.name,
            decay_per_tick=_parse_decimal(body.decay_per_tick, "decay_per_tick"),
            auto_issue_quantity=_parse_decimal(body.auto_issue_quantity, "auto_issue_quantity"),
            auto_issue_entity_type=body.auto_issue_entity_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(good)
    return good


@router.patch("/admin/goods/{symbol}", response_model=GoodRead, tags=["admin"])
def update_good(
    symbol: str,
    body: GoodUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    good = _good_or_404(session, symbol)
    if body.name is not None:
        good.name = body.name
    if body.decay_per_tick is not None:
        decay = _parse_decimal(body.decay_per_tick, "decay_per_tick").quantize(_QUANTUM)
        if not Decimal("0") <= decay <= Decimal("1"):
            raise HTTPException(status_code=422, detail="decay_per_tick must be between 0 and 1")
        good.decay_per_tick = decay
    if body.auto_issue_quantity is not None:
        quantity = _parse_decimal(body.auto_issue_quantity, "auto_issue_quantity").quantize(_QUANTUM)
        if quantity < 0:
            raise HTTPException(status_code=422, detail="auto_issue_quantity must be >= 0")
        good.auto_issue_quantity = quantity
    if "auto_issue_entity_type" in body.model_fields_set:
        good.auto_issue_entity_type = body.auto_issue_entity_type
    session.commit()
    session.refresh(good)
    return good
