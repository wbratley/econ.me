from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine import needs
from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.schemas import NeedCreate, NeedRead, NeedUpdate
from econengine.models import Need, NeedSatisfier, User

router = APIRouter(tags=["needs"])

_QUANTUM = Decimal("0.0001")


def _need_or_404(session: Session, code: str) -> Need:
    need = needs.get_need(session, code)
    if need is None:
        raise HTTPException(status_code=404, detail="Need not found")
    return need


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail=f"Invalid {field}")


# ---------------------------------------------------------------------------
# Needs — public data, admin-managed
# ---------------------------------------------------------------------------

@router.get("/needs", response_model=list[NeedRead])
def list_needs(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return session.execute(select(Need).order_by(Need.code)).scalars().all()


@router.get("/needs/{code}", response_model=NeedRead)
def get_need(
    code: str,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _need_or_404(session, code)


@router.post("/admin/needs", response_model=NeedRead, status_code=201, tags=["admin"])
def create_need(
    body: NeedCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    if needs.get_need(session, body.code) is not None:
        raise HTTPException(status_code=409, detail="Need already exists")
    try:
        need = needs.create_need(
            session,
            body.code,
            quantity_per_tick=_parse_decimal(body.quantity_per_tick, "quantity_per_tick"),
            satisfiers=body.satisfiers,
            entity_type=body.entity_type,
            priority=body.priority,
            name=body.name,
            condition_symbol=body.condition_symbol,
            condition_quantity=_parse_decimal(body.condition_quantity, "condition_quantity"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(need)
    return need


@router.patch("/admin/needs/{code}", response_model=NeedRead, tags=["admin"])
def update_need(
    code: str,
    body: NeedUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    need = _need_or_404(session, code)
    if body.name is not None:
        need.name = body.name
    if body.quantity_per_tick is not None:
        quantity = _parse_decimal(body.quantity_per_tick, "quantity_per_tick").quantize(_QUANTUM)
        if quantity <= 0:
            raise HTTPException(status_code=422, detail="quantity_per_tick must be positive")
        need.quantity_per_tick = quantity
    if body.priority is not None:
        need.priority = body.priority
    if body.is_active is not None:
        need.is_active = body.is_active
    if body.satisfiers is not None:
        symbols = sorted({str(s).upper() for s in body.satisfiers})
        if not symbols:
            raise HTTPException(status_code=422, detail="need must declare at least one satisfier")
        need.satisfiers = [NeedSatisfier(symbol=s) for s in symbols]
    if "condition_symbol" in body.model_fields_set or "condition_quantity" in body.model_fields_set:
        symbol = (
            body.condition_symbol if "condition_symbol" in body.model_fields_set
            else need.condition_symbol
        )
        quantity = (
            _parse_decimal(body.condition_quantity, "condition_quantity").quantize(_QUANTUM)
            if body.condition_quantity is not None
            else (Decimal("0") if "condition_symbol" in body.model_fields_set and symbol is None
                  else need.condition_quantity)
        )
        if (symbol is None) != (quantity <= 0):
            raise HTTPException(
                status_code=422,
                detail="condition_symbol and a positive condition_quantity go together",
            )
        need.condition_symbol = symbol.upper() if symbol else None
        need.condition_quantity = quantity
    session.commit()
    session.refresh(need)
    return need
