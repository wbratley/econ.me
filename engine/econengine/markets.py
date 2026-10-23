"""
Commodity markets — holdings, limit orders, and the per-tick call auction.

Orders are limit-only and good-til-cancelled; partial fills stay OPEN with
`remaining` decremented. Re-quoting your own resting price level (same
market, side, and limit price) cancel-replaces: the stale orders are
superseded and the new one carries the level -- a script that runs every
tick can re-assert its quotes without stacking duplicates (see
`place_order`, the stone-run7 lesson). Money is never escrowed: funds are checked
live at settlement, and an order that cannot cover a fill is CANCELLED with
a reason. GOODS escrow is the placed-market rule (S5b): a sell on a market
with a place debits the goods onto the market's shelf at placement and
fills draw that shelf — sellers must deliver; global markets keep the
settle-from-holdings path unchanged. Holdings reserved as good-requirements of running processes
(reserved_quantity) are unavailable to settlement — you cannot sell the
oven mid-bake. Settlement money moves through services.transfer under
scripting._suppressed(), so validators cannot veto individual fills
mid-auction and hooks cannot recurse — the clearing price was computed from
the whole book and must settle atomically. Policies observe markets through
the `trade` / `auction` tick events instead.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import services
from .models import (
    Account, Entity, EntityStatus, Holding, Market, MarketEscrow, Order,
    OrderSide, OrderStatus, Process, ProcessStatus, Recipe,
    RecipeGoodRequirement, Trade,
)
from .scripting import _suppressed
from .services import CurrencyMismatchError

_QUANTUM = Decimal("0.0001")

# Key for the per-session symbol -> Market memo held in Session.info. Scoped to
# the session rather than the module so two sessions (parallel runs, tests)
# never see each other's rows.
_MARKET_CACHE = "_econengine_market_cache"


class InsufficientHoldingsError(ValueError):
    pass


class MarketInactiveError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def create_market(
    session: Session, symbol: str, currency: str, name: str = "", description: str = "",
    place: "Place | str | None" = None,
) -> Market:
    existing = get_market(session, symbol)
    if existing is not None:
        raise ValueError(
            f"market {str(symbol).upper()} already installed by "
            f"{existing.pack_id or 'the platform'} -- a pack may not claim another installer's key")
    place_id: str | None = None
    if place is not None:
        from . import places as places_mod

        resolved = (
            places_mod.get_place(session, place) if isinstance(place, str) else place
        )
        if resolved is None:
            raise ValueError(
                f"market {str(symbol).upper()!r} sits at unknown place "
                f"{str(place).upper()!r} -- install the map before siting markets")
        place_id = resolved.id
    market = Market(symbol=symbol.upper(), currency=currency.upper(), name=name,
                    description=description, place_id=place_id)
    session.add(market)
    session.flush()
    session.info.setdefault(_MARKET_CACHE, {})[market.symbol] = market
    return market


def get_market(session: Session, symbol: str) -> Market | None:
    """Symbol -> Market, memoised per session.

    Called ~220 times a tick (every quote, order and settlement resolves its
    market by symbol), and the lookup is by symbol rather than primary key so
    the ORM identity map never short-circuits it. Measured, near enough all of
    this engine's runtime is per-statement ORM overhead rather than SQLite
    itself, so a repeated lookup costs real time even though the row is
    trivially cached.

    Caching the mapped OBJECT is safe where caching a value would not be:
    `last_price` mutates on every trade, but it mutates *on this instance*, so
    a cached reference sees the update. Only found rows are cached, so a
    market created later is still discovered; `create_market` seeds the entry,
    and a session rollback drops the cache since objects may be detached.
    """
    sym = str(symbol).upper()
    cache = session.info.setdefault(_MARKET_CACHE, {})
    hit = cache.get(sym)
    if hit is not None and hit in session:
        return hit
    market = session.execute(
        select(Market).where(Market.symbol == sym)
    ).scalar_one_or_none()
    if market is not None:
        cache[sym] = market
    elif hit is not None:
        cache.pop(sym, None)
    return market


def get_holding(session: Session, entity_id: str, symbol: str) -> Holding | None:
    return session.execute(
        select(Holding).where(
            Holding.entity_id == entity_id, Holding.symbol == str(symbol).upper()
        )
    ).scalar_one_or_none()


def reserved_quantity(session: Session, entity_id: str, symbol: str) -> Decimal:
    """The symbol's good-requirements summed across the entity's RUNNING
    processes — machinery backing work in progress. A query against running
    processes, not an escrow: nothing is moved or locked, but reserved
    quantities are unavailable to settlement (you cannot sell the oven
    mid-bake) and to further reservation at start_process."""
    total = session.execute(
        select(func.coalesce(func.sum(RecipeGoodRequirement.quantity), 0))
        .select_from(Process)
        .join(Recipe, Process.recipe_id == Recipe.id)
        .join(RecipeGoodRequirement, RecipeGoodRequirement.recipe_id == Recipe.id)
        .where(
            Process.entity_id == entity_id,
            Process.status == ProcessStatus.RUNNING,
            RecipeGoodRequirement.symbol == str(symbol).upper(),
        )
    ).scalar_one()
    return Decimal(total)


def adjust_holding(session: Session, entity: Entity, symbol: str, delta: Decimal) -> Holding:
    symbol = symbol.upper()
    holding = get_holding(session, entity.id, symbol)
    if holding is None:
        holding = Holding(entity_id=entity.id, symbol=symbol, quantity=Decimal("0"))
        session.add(holding)
    new_quantity = holding.quantity + delta
    # The banking cap (P1): positive credits clip at the good's max_holding
    # -- excess is simply not creditable (you cannot get warmer than warm),
    # never an error, so a recipe's output can't explode mid-flight. Debits
    # and uncapped goods are untouched.
    if delta > 0:
        from . import goods as goods_mod
        good = goods_mod.get_good(session, symbol)
        if good is not None and good.max_holding is not None \
                and new_quantity > good.max_holding:
            new_quantity = good.max_holding
    if new_quantity < 0:
        raise InsufficientHoldingsError(
            f"entity {entity.id} holds {holding.quantity} {symbol}, cannot adjust by {delta}"
        )
    holding.quantity = new_quantity
    session.flush()
    return holding


# ---------------------------------------------------------------------------
# Escrow — goods physically at a placed market (S5b, "sellers must
# deliver"). Global markets never engage it: their fills keep drawing
# remote holdings exactly as before.
# ---------------------------------------------------------------------------

def get_escrow(
    session: Session, market: Market, seller_entity_id: str, symbol: str
) -> MarketEscrow | None:
    """The seller's pool for this (market, symbol) — goods standing on
    the market's shelf, or None when nothing was ever delivered."""
    return session.execute(
        select(MarketEscrow).where(
            MarketEscrow.market_id == market.id,
            MarketEscrow.seller_entity_id == seller_entity_id,
            MarketEscrow.symbol == str(symbol).upper(),
        )
    ).scalar_one_or_none()


def escrowed_quantity(
    session: Session, market: Market, seller_entity_id: str | None = None,
    symbol: str | None = None,
) -> Decimal:
    """Shelf quantity: the whole market's, or one seller's pool, or one
    seller's pool for one symbol. The depth behind every open ask."""
    q = select(func.coalesce(func.sum(MarketEscrow.quantity), 0)).where(
        MarketEscrow.market_id == market.id
    )
    if seller_entity_id is not None:
        q = q.where(MarketEscrow.seller_entity_id == seller_entity_id)
    if symbol is not None:
        q = q.where(MarketEscrow.symbol == str(symbol).upper())
    return Decimal(session.execute(q).scalar_one())


def claimed_quantity(
    session: Session, market: Market, seller_entity_id: str, symbol: str
) -> Decimal:
    """The shelf quantity already claimed by the seller's OPEN sell
    orders — what fills will draw. Re-quoting a level supersedes the old
    order (releasing its claim) before the new one claims, so a
    tick-by-tick re-assertion never double-delivers."""
    total = session.execute(
        select(func.coalesce(func.sum(Order.remaining), 0)).where(
            Order.market_id == market.id,
            Order.entity_id == seller_entity_id,
            Order.side == OrderSide.SELL,
            Order.status == OrderStatus.OPEN,
            Order.remaining > 0,
        )
    ).scalar_one()
    return Decimal(total)


def free_shelf(
    session: Session, market: Market, seller_entity_id: str, symbol: str
) -> Decimal:
    """Delivered-but-unclaimed: what a new sell order can back without
    another delivery, and what a present seller's reclaim would return."""
    pool = escrowed_quantity(session, market, seller_entity_id, symbol)
    return pool - min(pool, claimed_quantity(session, market, seller_entity_id, symbol))


def _deliver_to_shelf(
    session: Session, market: Market, entity: Entity, symbol: str, quantity: Decimal
) -> None:
    """Placement-time delivery: holdings down, shelf up — one
    transaction, so a refused order debits nothing."""
    with session.begin_nested():
        adjust_holding(session, entity, symbol, -quantity)
        row = get_escrow(session, market, entity.id, symbol)
        if row is None:
            row = MarketEscrow(
                market_id=market.id,
                seller_entity_id=entity.id,
                symbol=str(symbol).upper(),
                quantity=Decimal("0"),
            )
            session.add(row)
        row.quantity = (row.quantity + quantity).quantize(_QUANTUM)
        session.flush()


def _reclaim_escrows(session: Session, tick_number: int) -> list[dict]:
    """The sweep half of "walking back IS the mechanism": escrowed goods
    not claimed by open sells flow back to the holdings of a seller
    STANDING at the market's place. Cancelled/expired leftovers wait on
    the shelf until then — no intent, no recipe. Deterministic order
    (market symbol, seller id) for replay parity."""
    from . import places as places_mod

    events: list[dict] = []
    rows = session.execute(
        select(MarketEscrow)
        .join(Market, MarketEscrow.market_id == Market.id)
        .where(Market.is_active.is_(True), Market.place_id.is_not(None))
        .order_by(Market.symbol, MarketEscrow.seller_entity_id, MarketEscrow.symbol)
    ).scalars().all()
    for row in rows:
        market = session.get(Market, row.market_id)
        free = free_shelf(session, market, row.seller_entity_id, row.symbol)
        if free <= 0:
            continue
        seller = session.get(Entity, row.seller_entity_id)
        if seller is None or seller.place is None or seller.place.id != market.place_id:
            continue
        with session.begin_nested():
            row.quantity = (row.quantity - free).quantize(_QUANTUM)
            adjust_holding(session, seller, row.symbol, free)
            session.flush()
        events.append({
            "type": "market_reclaim",
            "entity_id": seller.id,
            "market": market.symbol,
            "symbol": row.symbol,
            "quantity": str(free),
            "place": places_mod.label(market.place),
        })
    return events


def place_order(
    session: Session,
    entity_id: str,
    symbol: str,
    side: str | OrderSide,
    quantity: Decimal,
    limit_price: Decimal,
    account_id: str | None = None,
    reference: str = "",
) -> Order:
    market = get_market(session, symbol)
    if market is None:
        raise ValueError(f"no market for symbol {str(symbol).upper()!r}")
    if not market.is_active:
        raise MarketInactiveError(f"market {market.symbol} is inactive")

    entity = session.get(Entity, entity_id)
    if entity is None:
        raise ValueError("unknown entity")
    if entity.status != EntityStatus.ACTIVE:
        raise ValueError("entity is incapacitated")

    if account_id is None:
        # A house trading on its own book does not name the account
        # (run-58 postmortem #3: the nil-account gap killed a derived
        # food strategy). Default to the entity's own settlement account
        # in the market currency -- first by id, the same deterministic
        # choice the holders() register makes.
        account = session.execute(
            select(Account)
            .where(
                Account.entity_id == entity_id,
                Account.currency == market.currency,
            )
            .order_by(Account.id)
            .limit(1)
        ).scalar_one_or_none()
        if account is None:
            raise ValueError(
                f"entity has no {market.currency} settlement account")
        account_id = account.id
    else:
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError("unknown settlement account")

    if market.place_id is not None:
        # The S2 seat (Fork 5): a placed market trades only for entities
        # standing on it. NULL stays the global market of today.
        from . import places as places_mod

        where = entity.place
        if where is None or where.id != market.place_id:
            raise ValueError(
                f"market {market.symbol} trades at "
                f"{places_mod.label(market.place)} -- "
                + (f"you are at {places_mod.label(where)}"
                   if where is not None else "you are not on the map")
            )

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

    if account.entity_id != entity_id:
        raise ValueError("settlement account does not belong to ordering entity")
    if account.currency != market.currency:
        raise CurrencyMismatchError(
            f"account currency {account.currency} does not match market currency {market.currency}"
        )

    # Cancel-replace at the price level. A behaviour script runs every
    # tick and re-asserts the quotes it wants ("buy 1 BERRIES at 1.00",
    # "sell my surplus YARN at 2.39"), and under plain good-til-cancelled
    # each re-assertion stacked one more OPEN order on the book -- a
    # stone-run7 house left 878, ~240 of them the SAME order. Re-quoting
    # your own price level now supersedes what rests there; the quantity
    # just placed carries the level. Different prices stack freely: a
    # ladder is intent, depth at one printed price is a re-assertion.
    stale = session.execute(
        select(Order).where(
            Order.entity_id == entity_id,
            Order.market_id == market.id,
            Order.side == side,
            Order.limit_price == limit_price,
            Order.status == OrderStatus.OPEN,
        )
    ).scalars().all()
    for old in stale:
        old.status = OrderStatus.CANCELLED
        old.cancel_reason = "superseded at price level"

    # S5b delivery: a SELL on a placed market debits the goods onto the
    # market's shelf at placement — the order is backed by delivered
    # goods, and a fill can never reach holdings that are not there.
    # The shelf first backs the order out of free (delivered but
    # unclaimed) quantity — re-quoting a level re-asserts, it does not
    # re-deliver — and only the shortfall is debited from holdings,
    # which must be present and unreserved (you cannot deliver the oven
    # mid-bake). Global markets never engage the shelf.
    if market.place_id is not None and side == OrderSide.SELL:
        from . import places as places_mod

        free = free_shelf(session, market, entity_id, market.symbol)
        shortfall = max(Decimal("0"), quantity - free)
        if shortfall > 0:
            holding = get_holding(session, entity_id, market.symbol)
            held = holding.quantity if holding else Decimal("0")
            available = held - reserved_quantity(session, entity_id, market.symbol)
            if available < shortfall:
                raise InsufficientHoldingsError(
                    f"you must deliver {quantity} {market.symbol} at "
                    f"{places_mod.label(market.place)} -- the shelf holds "
                    f"{free}, you carry {available} unreserved"
                )
            _deliver_to_shelf(session, market, entity, market.symbol, shortfall)

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
    """Cancel an open order; a no-op when it already rests nowhere.

    Idempotent by construction (run 15: 49 rejections were scripts
    cancel-then-replacing on a book whose fill arrived first). Cancelling
    an order that is already filled or cancelled IS the caller's success
    state -- "nothing rests at this level" -- so it returns the order
    untouched. Unknown ids and orders the entity does not own still
    raise: those are not idempotent retries, they are mistakes.
    """
    order = session.get(Order, order_id)
    if order is None:
        raise ValueError("unknown order")
    if order.entity_id != entity_id:
        raise ValueError("entity does not own order")
    if order.status != OrderStatus.OPEN:
        return order
    order.status = OrderStatus.CANCELLED
    order.cancel_reason = "cancelled by owner"
    session.flush()
    return order


# ---------------------------------------------------------------------------
# Call auction
# ---------------------------------------------------------------------------

def run_auctions(session: Session, tick_number: int) -> list[dict]:
    """Clear every active market once. Returns tick events (trades,
    cancellations, per-market auction summaries, shelf reclaims)."""
    events: list[dict] = []
    markets = session.execute(
        select(Market).where(Market.is_active.is_(True)).order_by(Market.symbol)
    ).scalars().all()
    for market in markets:
        events.extend(_clear_market(session, market, tick_number))
    # After the clears: present sellers take their unclaimed shelf back
    # (S5b) — the auction has spoken, the leftovers are free goods.
    events.extend(_reclaim_escrows(session, tick_number))
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
    is cancelled.

    The buyer's credit clips at the good's max_holding (the banking cap,
    same stance as adjust_holding): the seller is paid in full and the
    trade records the full quantity, but the excess above the cap is
    simply not creditable. Refusing the fill instead — as the funds and
    holdings pre-checks below do — is a deliberate design boundary, not
    an oversight."""
    events: list[dict] = []
    bi = si = 0
    while bi < len(buys) and si < len(sells):
        buy, sell = buys[bi], sells[si]
        if buy.entity_id == sell.entity_id:
            # An auction is between counterparties. stone-run4: one
            # entity quoting both legs off the same reference crossed
            # itself 360 times -- wash volume that pinned the price at
            # its own anchor. When both heads belong to one entity the
            # YOUNGER order steps aside (time priority keeps the older
            # resting); it can still match further down the other side.
            if (sell.created_at, sell.id) > (buy.created_at, buy.id):
                si += 1
            else:
                bi += 1
            continue
        qty = min(buy.remaining, sell.remaining)
        cost = (qty * price).quantize(_QUANTUM, rounding=ROUND_HALF_UP)

        buyer_account = session.get(Account, buy.account_id)
        if buyer_account.balance < cost:
            events.append(_cancel_at_auction(buy, market, "insufficient funds at auction"))
            bi += 1
            continue

        if market.place_id is not None:
            # S5b: a placed market fills ONLY from goods delivered to its
            # shelf — delivery happened at placement, so the pool is the
            # order's own backing (pool >= claimed >= remaining by
            # construction; the branch below is the defensive invariant,
            # not an expected path). Remote holdings are unreachable
            # here by design: that is the whole feature.
            shelf = escrowed_quantity(session, market, sell.entity_id, market.symbol)
            if shelf < qty:
                events.append(_cancel_at_auction(
                    sell, market, "goods not on the shelf at auction"))
                si += 1
                continue
        else:
            seller_holding = get_holding(session, sell.entity_id, market.symbol)
            available = (
                seller_holding.quantity - reserved_quantity(session, sell.entity_id, market.symbol)
                if seller_holding else Decimal("0")
            )
            if available < qty:
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
            if market.place_id is not None:
                # S5b: the shelf pays — pool down in lockstep with
                # order.remaining, exactly the quantity the buyer takes.
                escrow = get_escrow(session, market, sell.entity_id, market.symbol)
                if escrow is None or escrow.quantity < qty:
                    raise InsufficientHoldingsError(
                        f"shelf for {sell.entity_id} on {market.symbol} "
                        f"cannot cover {qty}"
                    )
                escrow.quantity = (escrow.quantity - qty).quantize(_QUANTUM)
            else:
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
