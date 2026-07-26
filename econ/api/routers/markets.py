from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine import markets as market_services
from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.schemas import (
    HoldingGrant,
    HoldingRead,
    MarketCreate,
    MarketRead,
    MarketUpdate,
    OrderCreate,
    OrderRead,
    TradeRead,
)
from econengine.models import Account, Entity, Holding, Market, Order, Trade, User

router = APIRouter(tags=["markets"])


def _market_or_404(session: Session, symbol: str) -> Market:
    market = market_services.get_market(session, symbol)
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")
    return market


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail=f"Invalid {field}: {raw!r}")


# ---------------------------------------------------------------------------
# Public market data
# ---------------------------------------------------------------------------

@router.get("/markets", response_model=list[MarketRead])
def list_markets(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return session.execute(select(Market).order_by(Market.symbol)).scalars().all()


@router.get("/markets/{symbol}", response_model=MarketRead)
def get_market(
    symbol: str,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _market_or_404(session, symbol)


@router.get("/markets/{symbol}/trades", response_model=list[TradeRead])
def list_trades(
    symbol: str,
    limit: int = Query(default=50, ge=1, le=500),
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    market = _market_or_404(session, symbol)
    return session.execute(
        select(Trade)
        .where(Trade.market_id == market.id)
        .order_by(Trade.executed_at.desc(), Trade.id.desc())
        .limit(limit)
    ).scalars().all()


# ---------------------------------------------------------------------------
# Orders — settle from/into an account the user owns
# ---------------------------------------------------------------------------

@router.post("/orders", response_model=OrderRead, status_code=201)
def place_order(
    body: OrderCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    account = session.get(Account, body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.entity.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your account")
    try:
        order = market_services.place_order(
            session,
            account.entity_id,
            symbol=body.symbol,
            side=body.side,
            quantity=_parse_decimal(body.quantity, "quantity"),
            limit_price=_parse_decimal(body.limit_price, "limit_price"),
            account_id=body.account_id,
            reference=body.reference,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(order)
    return order


@router.get("/orders", response_model=list[OrderRead])
def list_orders(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return session.execute(
        select(Order)
        .join(Entity, Order.entity_id == Entity.id)
        .where(Entity.owner_id == current_user.id)
        .order_by(Order.created_at.desc(), Order.id.desc())
    ).scalars().all()


@router.post("/orders/{order_id}/cancel", response_model=OrderRead)
def cancel_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    order = session.get(Order, order_id)
    if order is None or order.entity.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Order not found")
    try:
        market_services.cancel_order(session, order_id, order.entity_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(order)
    return order


# ---------------------------------------------------------------------------
# Admin — market lifecycle and the goods faucet
# ---------------------------------------------------------------------------

@router.post("/admin/markets", response_model=MarketRead, status_code=201, tags=["admin"])
def create_market(
    body: MarketCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    if market_services.get_market(session, body.symbol) is not None:
        raise HTTPException(status_code=409, detail="Market already exists")
    try:
        market = market_services.create_market(session, body.symbol, body.currency, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(market)
    return market


@router.patch("/admin/markets/{symbol}", response_model=MarketRead, tags=["admin"])
def update_market(
    symbol: str,
    body: MarketUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    market = _market_or_404(session, symbol)
    if body.name is not None:
        market.name = body.name
    if body.is_active is not None:
        market.is_active = body.is_active
    session.commit()
    session.refresh(market)
    return market


@router.post("/admin/holdings", response_model=HoldingRead, tags=["admin"])
def grant_holding(
    body: HoldingGrant,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    entity = session.get(Entity, body.entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    try:
        holding = market_services.adjust_holding(
            session, entity, body.symbol, _parse_decimal(body.delta, "delta")
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(holding)
    return holding


@router.get("/admin/holdings", response_model=list[HoldingRead], tags=["admin"])
def list_holdings(
    entity_id: str | None = None,
    symbol: str | None = None,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    query = select(Holding).order_by(Holding.entity_id, Holding.symbol)
    if entity_id:
        query = query.where(Holding.entity_id == entity_id)
    if symbol:
        query = query.where(Holding.symbol == symbol.upper())
    return session.execute(query).scalars().all()
