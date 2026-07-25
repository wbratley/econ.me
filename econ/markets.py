"""
Commodity markets — holdings, limit orders, and the per-tick call auction.

Orders are limit-only and good-til-cancelled; partial fills stay OPEN with
`remaining` decremented. There is no escrow: funds and holdings are checked
live at settlement, and an order that cannot cover a fill is CANCELLED with
a reason. Settlement money moves through services.transfer under
scripting._suppressed(), so validators cannot veto individual fills
mid-auction and hooks cannot recurse — the clearing price was computed from
the whole book and must settle atomically. Policies observe markets through
the `trade` / `auction` tick events instead.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services
from .models import Account, Entity, Holding, Market, Order, OrderSide, OrderStatus, Trade
from .scripting import _suppressed
from .services import CurrencyMismatchError

_QUANTUM = Decimal("0.0001")


class InsufficientHoldingsError(ValueError):
    pass


class MarketInactiveError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def create_market(session: Session, symbol: str, currency: str, name: str = "") -> Market:
    market = Market(symbol=symbol.upper(), currency=currency.upper(), name=name)
    session.add(market)
    session.flush()
    return market


def get_market(session: Session, symbol: str) -> Market | None:
    return session.execute(
        select(Market).where(Market.symbol == str(symbol).upper())
    ).scalar_one_or_none()


def get_holding(session: Session, entity_id: str, symbol: str) -> Holding | None:
    return session.execute(
        select(Holding).where(
            Holding.entity_id == entity_id, Holding.symbol == str(symbol).upper()
        )
    ).scalar_one_or_none()


def adjust_holding(session: Session, entity: Entity, symbol: str, delta: Decimal) -> Holding:
    symbol = symbol.upper()
    holding = get_holding(session, entity.id, symbol)
    if holding is None:
        holding = Holding(entity_id=entity.id, symbol=symbol, quantity=Decimal("0"))
        session.add(holding)
    new_quantity = holding.quantity + delta
    if new_quantity < 0:
        raise InsufficientHoldingsError(
            f"entity {entity.id} holds {holding.quantity} {symbol}, cannot adjust by {delta}"
        )
    holding.quantity = new_quantity
    session.flush()
    return holding


def place_order(
    session: Session,
    entity_id: str,
    symbol: str,
    side: str | OrderSide,
    quantity: Decimal,
    limit_price: Decimal,
    account_id: str,
    reference: str = "",
) -> Order:
    market = get_market(session, symbol)
    if market is None:
        raise ValueError(f"no market for symbol {str(symbol).upper()!r}")
    if not market.is_active:
        raise MarketInactiveError(f"market {market.symbol} is inactive")

    if isinstance(side, str):
        try:
            side = OrderSide(side.lower())
        except ValueError:
            raise ValueError(f"invalid order side {side!r}")

    quantity = Decimal(quantity).quantize(_QUANTUM)
    limit_price = Decimal(limit_price).quantize(_QUANTUM)
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if limit_price <= 0:
        raise ValueError("limit_price must be positive")

    account = session.get(Account, account_id)
    if account is None:
        raise ValueError("unknown settlement account")
    if account.entity_id != entity_id:
        raise ValueError("settlement account does not belong to ordering entity")
    if account.currency != market.currency:
        raise CurrencyMismatchError(
            f"account currency {account.currency} does not match market currency {market.currency}"
        )

    order = Order(
        market_id=market.id,
        entity_id=entity_id,
        account_id=account_id,
        side=side,
        quantity=quantity,
        remaining=quantity,
        limit_price=limit_price,
        reference=reference,
    )
    session.add(order)
    session.flush()
    return order


def cancel_order(session: Session, order_id: str, entity_id: str) -> Order:
    order = session.get(Order, order_id)
    if order is None:
        raise ValueError("unknown order")
    if order.entity_id != entity_id:
        raise ValueError("entity does not own order")
    if order.status != OrderStatus.OPEN:
        raise ValueError(f"order is {order.status.value}, only open orders can be cancelled")
    order.status = OrderStatus.CANCELLED
    order.cancel_reason = "cancelled by owner"
    session.flush()
    return order


# ---------------------------------------------------------------------------
# Call auction
# ---------------------------------------------------------------------------

def run_auctions(session: Session, tick_number: int) -> list[dict]:
    """Clear every active market once. Returns tick events (trades,
    cancellations, per-market auction summaries)."""
    events: list[dict] = []
    markets = session.execute(
        select(Market).where(Market.is_active.is_(True)).order_by(Market.symbol)
    ).scalars().all()
    for market in markets:
        events.extend(_clear_market(session, market, tick_number))
    return events


def _clear_market(session: Session, market: Market, tick_number: int) -> list[dict]:
    orders = session.execute(
        select(Order)
        .where(
            Order.market_id == market.id,
            Order.status == OrderStatus.OPEN,
            Order.remaining > 0,
        )
        .order_by(Order.created_at, Order.id)
    ).scalars().all()

    buys = [o for o in orders if o.side == OrderSide.BUY]
    sells = [o for o in orders if o.side == OrderSide.SELL]
    if not buys or not sells:
        return []

    clearing_price = _clearing_price(buys, sells, market.last_price)
    if clearing_price is None:
        return []

    # Price-time priority: better-priced orders fill first; the marginal
    # price level rations naturally in time order, last order partially.
    eligible_buys = sorted(
        (o for o in buys if o.limit_price >= clearing_price),
        key=lambda o: (-o.limit_price, o.created_at, o.id),
    )
    eligible_sells = sorted(
        (o for o in sells if o.limit_price <= clearing_price),
        key=lambda o: (o.limit_price, o.created_at, o.id),
    )

    events = _settle(session, market, clearing_price, eligible_buys, eligible_sells, tick_number)

    if any(e["type"] == "trade" for e in events):
        market.last_price = clearing_price
        traded = sum(
            Decimal(e["quantity"]) for e in events if e["type"] == "trade" and e["side"] == "buy"
        )
        events.append({
            "type": "auction",
            "entity_id": None,
            "market": market.symbol,
            "price": str(clearing_price),
            "volume": str(traded),
            "trades": sum(1 for e in events if e["type"] == "trade" and e["side"] == "buy"),
        })
    return events


def _clearing_price(buys: list, sells: list, last_price: Decimal | None) -> Decimal | None:
    """Uniform price maximizing executed volume; ties broken by smallest
    demand/supply imbalance, then closeness to last_price (lower price on
    exact equidistance), or — with no reference price yet — the median of
    the tied candidates (the centre of the equilibrium range)."""
    candidates = sorted({o.limit_price for o in buys} | {o.limit_price for o in sells})
    scored: list[tuple] = []
    for price in candidates:
        demand = sum((o.remaining for o in buys if o.limit_price >= price), Decimal("0"))
        supply = sum((o.remaining for o in sells if o.limit_price <= price), Decimal("0"))
        volume = min(demand, supply)
        if volume > 0:
            scored.append((-volume, abs(demand - supply), price))
    if not scored:
        return None
    best_volume, best_imbalance, _ = min(scored)
    tied = sorted(p for v, imb, p in scored if v == best_volume and imb == best_imbalance)
    if last_price is not None:
        return min(tied, key=lambda p: (abs(p - last_price), p))
    return tied[(len(tied) - 1) // 2]


def _settle(
    session: Session,
    market: Market,
    price: Decimal,
    buys: list,
    sells: list,
    tick_number: int,
) -> list[dict]:
    """Zipper match with live funds/holdings checks. Balances and holdings
    mutate per fill, so an entity cannot double-spend across orders or
    markets — a later fill sees the already-decremented value and its order
    is cancelled."""
    events: list[dict] = []
    bi = si = 0
    while bi < len(buys) and si < len(sells):
        buy, sell = buys[bi], sells[si]
        qty = min(buy.remaining, sell.remaining)
        cost = (qty * price).quantize(_QUANTUM, rounding=ROUND_HALF_UP)

        buyer_account = session.get(Account, buy.account_id)
        if buyer_account.balance < cost:
            events.append(_cancel_at_auction(buy, market, "insufficient funds at auction"))
            bi += 1
            continue

        seller_holding = get_holding(session, sell.entity_id, market.symbol)
        if seller_holding is None or seller_holding.quantity < qty:
            events.append(_cancel_at_auction(sell, market, "insufficient holdings at auction"))
            si += 1
            continue

        seller_account = session.get(Account, sell.account_id)
        with session.begin_nested():
            if cost > 0:
                with _suppressed():
                    services.transfer(
                        session, buyer_account, seller_account, cost,
                        reference=f"trade {market.symbol} {qty} @ {price}",
                    )
            adjust_holding(session, sell.entity, market.symbol, -qty)
            adjust_holding(session, buy.entity, market.symbol, qty)
            trade = Trade(
                market_id=market.id,
                tick_number=tick_number,
                buy_order_id=buy.id,
                sell_order_id=sell.id,
                buyer_entity_id=buy.entity_id,
                seller_entity_id=sell.entity_id,
                price=price,
                quantity=qty,
            )
            session.add(trade)
            buy.remaining -= qty
            sell.remaining -= qty
            if buy.remaining == 0:
                buy.status = OrderStatus.FILLED
            if sell.remaining == 0:
                sell.status = OrderStatus.FILLED
            session.flush()

        for order, entity_id, side in (
            (buy, buy.entity_id, "buy"),
            (sell, sell.entity_id, "sell"),
        ):
            events.append({
                "type": "trade",
                "entity_id": entity_id,
                "side": side,
                "market": market.symbol,
                "price": str(price),
                "quantity": str(qty),
                "cost": str(cost),
                "order_id": order.id,
                "trade_id": trade.id,
            })

        if buy.remaining == 0:
            bi += 1
        if sell.remaining == 0:
            si += 1
    return events


def _cancel_at_auction(order: Order, market: Market, reason: str) -> dict:
    order.status = OrderStatus.CANCELLED
    order.cancel_reason = reason
    return {
        "type": "order_cancelled",
        "entity_id": order.entity_id,
        "order_id": order.id,
        "market": market.symbol,
        "reason": reason,
    }
