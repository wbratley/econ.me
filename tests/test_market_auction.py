import pytest
from decimal import Decimal
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econengine.markets import adjust_holding, create_market, place_order, run_auctions
from econengine.models import (
    Base,
    EntityType,
    OrderStatus,
    Script,
    ScriptType,
    Trade,
    Transaction,
    TransactionType,
)
from econengine.services import create_account, create_entity
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """Buyer (1000 USD), seller (100 WHEAT), one WHEAT/USD market."""
    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    seller = create_entity(session, "Seller", EntityType.BUSINESS)
    b = create_account(session, buyer, "USD", initial_balance=Decimal("1000"))
    s = create_account(session, seller, "USD")
    market = create_market(session, "WHEAT", "USD")
    adjust_holding(session, seller, "WHEAT", Decimal("100"))
    return session, buyer, seller, b, s, market


def buy(session, entity, account, qty, price):
    return place_order(session, entity.id, "WHEAT", "buy", Decimal(qty), Decimal(price), account.id)


def sell(session, entity, account, qty, price):
    return place_order(session, entity.id, "WHEAT", "sell", Decimal(qty), Decimal(price), account.id)


def holding_qty(session, entity, symbol="WHEAT"):
    from econengine.markets import get_holding
    h = get_holding(session, entity.id, symbol)
    return h.quantity if h else Decimal("0")


# --- basic clearing ---

def test_simple_cross_settles(world):
    session, buyer, seller, b, s, market = world
    bo = buy(session, buyer, b, "10", "12.5")
    so = sell(session, seller, s, "10", "12.5")

    events = run_auctions(session, tick_number=1)

    assert b.balance == Decimal("875")            # 1000 - 10*12.5
    assert s.balance == Decimal("125")
    assert holding_qty(session, buyer) == Decimal("10")
    assert holding_qty(session, seller) == Decimal("90")
    assert bo.status == OrderStatus.FILLED
    assert so.status == OrderStatus.FILLED
    assert market.last_price == Decimal("12.5")

    trades = session.execute(select(Trade)).scalars().all()
    assert len(trades) == 1
    assert trades[0].price == Decimal("12.5")
    assert trades[0].tick_number == 1

    # settlement produced real ledger rows
    tx_types = {t.tx_type for t in session.execute(select(Transaction)).scalars()}
    assert tx_types == {TransactionType.DEBIT, TransactionType.CREDIT}

    trade_events = [e for e in events if e["type"] == "trade"]
    assert {e["side"] for e in trade_events} == {"buy", "sell"}
    assert {e["entity_id"] for e in trade_events} == {buyer.id, seller.id}
    auction_events = [e for e in events if e["type"] == "auction"]
    assert len(auction_events) == 1
    assert auction_events[0]["volume"] == "10.0000"


def test_no_cross_no_trades(world):
    session, buyer, seller, b, s, market = world
    buy(session, buyer, b, "10", "10")     # bid 10
    so = sell(session, seller, s, "10", "20")   # ask 20

    events = run_auctions(session, tick_number=1)

    assert events == []
    assert market.last_price is None
    assert so.status == OrderStatus.OPEN
    assert b.balance == Decimal("1000")


# --- clearing price selection ---

def test_price_maximizes_volume(world):
    session, buyer, seller, b, s, market = world
    # Demand: 10@15, 10@12. Supply: 10@10, 10@13.
    # All candidates {10,12,13,15} tie at volume 10, imbalance 10; with no
    # last price the median (lower-middle) of the tied range wins -> 12.
    buy(session, buyer, b, "10", "15")
    buy(session, buyer, b, "10", "12")
    sell(session, seller, s, "10", "10")
    sell(session, seller, s, "10", "13")

    run_auctions(session, tick_number=1)
    assert market.last_price == Decimal("12")


def test_price_minimizes_imbalance(world):
    session, buyer, seller, b, s, market = world
    # Demand: 10@15, 5@12. Supply: 10@10, 5@12.
    # p=10: vol min(15,10)=10, imb 5. p=12: vol min(15,15)=15, imb 0. p=15: vol min(10,15)=10.
    # max volume unique at 12.
    buy(session, buyer, b, "10", "15")
    buy(session, buyer, b, "5", "12")
    sell(session, seller, s, "10", "10")
    sell(session, seller, s, "5", "12")

    run_auctions(session, tick_number=1)
    assert market.last_price == Decimal("12")


def test_price_tie_breaks_toward_last_price(world):
    session, buyer, seller, b, s, market = world
    market.last_price = Decimal("13")
    # same book as test_price_maximizes_volume: {12,13} tie -> closest to 13 wins
    buy(session, buyer, b, "10", "15")
    buy(session, buyer, b, "10", "12")
    sell(session, seller, s, "10", "10")
    sell(session, seller, s, "10", "13")

    run_auctions(session, tick_number=1)
    assert market.last_price == Decimal("13")


# --- priority & partial fills ---

def test_price_time_priority_at_margin(world):
    session, buyer, seller, b, s, market = world
    first = buy(session, buyer, b, "6", "10")
    second = buy(session, buyer, b, "6", "10")
    sell(session, seller, s, "8", "10")

    run_auctions(session, tick_number=1)

    assert first.status == OrderStatus.FILLED
    assert second.status == OrderStatus.OPEN     # partial: 2 of 6
    assert second.remaining == Decimal("4")
    assert holding_qty(session, buyer) == Decimal("8")


def test_partial_order_fills_next_tick(world):
    session, buyer, seller, b, s, market = world
    bo = buy(session, buyer, b, "10", "10")
    sell(session, seller, s, "4", "10")
    run_auctions(session, tick_number=1)
    assert bo.remaining == Decimal("6")
    assert bo.status == OrderStatus.OPEN

    sell(session, seller, s, "6", "10")
    run_auctions(session, tick_number=2)
    assert bo.status == OrderStatus.FILLED
    assert holding_qty(session, buyer) == Decimal("10")


# --- live checks / double-spend defense ---

def test_buyer_insufficient_funds_cancelled(world):
    session, buyer, seller, b, s, market = world
    poor = create_entity(session, "Poor", EntityType.INDIVIDUAL)
    p = create_account(session, poor, "USD", initial_balance=Decimal("5"))
    # same limit; poor placed first so time priority settles it first
    bad = buy(session, poor, p, "10", "10")
    good = buy(session, buyer, b, "10", "10")
    sell(session, seller, s, "20", "10")

    events = run_auctions(session, tick_number=1)

    assert bad.status == OrderStatus.CANCELLED
    assert "insufficient funds" in bad.cancel_reason
    assert good.status == OrderStatus.FILLED     # book keeps trading
    cancelled = [e for e in events if e["type"] == "order_cancelled"]
    assert cancelled[0]["entity_id"] == poor.id


def test_seller_double_spend_within_auction(world):
    session, buyer, seller, b, s, market = world
    # seller holds 100 WHEAT but offers 100 twice; demand covers both, so
    # the second order is reached at settlement and must cancel
    first = sell(session, seller, s, "100", "10")
    second = sell(session, seller, s, "100", "10")
    b.balance = Decimal("2000")  # enough for 200 if the check were broken
    bo = buy(session, buyer, b, "200", "10")

    run_auctions(session, tick_number=1)

    assert first.status == OrderStatus.FILLED
    assert second.status == OrderStatus.CANCELLED
    assert "insufficient holdings" in second.cancel_reason
    assert holding_qty(session, seller) == Decimal("0")
    assert holding_qty(session, buyer) == Decimal("100")
    assert bo.status == OrderStatus.OPEN          # unfilled remainder is GTC
    assert bo.remaining == Decimal("100")


def test_cross_market_double_spend(world):
    session, buyer, seller, b, s, market = world
    create_market(session, "OIL", "USD")
    adjust_holding(session, seller, "OIL", Decimal("10"))
    b.balance = Decimal("100")
    # buyer bids 100 in OIL and 100 in WHEAT but only has 100 total.
    # markets clear in symbol order: OIL first, WHEAT order then cancels.
    oil_buy = place_order(session, buyer.id, "OIL", "buy", Decimal("10"), Decimal("10"), b.id)
    wheat_buy = buy(session, buyer, b, "10", "10")
    place_order(session, seller.id, "OIL", "sell", Decimal("10"), Decimal("10"), s.id)
    sell(session, seller, s, "10", "10")

    run_auctions(session, tick_number=1)

    assert oil_buy.status == OrderStatus.FILLED
    assert wheat_buy.status == OrderStatus.CANCELLED
    assert b.balance == Decimal("0")


def test_validator_cannot_veto_settlement(world):
    session, buyer, seller, b, s, market = world
    session.add(Script(name="no-transfers", source="return false", script_type=ScriptType.VALIDATOR))
    hook = Script(name="watcher", source="ctx.state.fired = true", script_type=ScriptType.HOOK)
    session.add(hook)
    session.flush()
    buy(session, buyer, b, "10", "10")
    sell(session, seller, s, "10", "10")

    run_auctions(session, tick_number=1)

    assert holding_qty(session, buyer) == Decimal("10")  # settled despite validator
    assert hook.state == {}                              # hooks not fired either


# --- script integration (through run_tick) ---

def test_script_places_order_and_trades_same_tick(world):
    session, buyer, seller, b, s, market = world
    session.add(Script(
        name="script-buyer",
        script_type=ScriptType.BEHAVIOUR,
        entity_id=buyer.id,
        source=f"ctx.action.place_order('WHEAT', 'buy', '10', '12', '{b.id}')",
    ))
    session.flush()
    sell(session, seller, s, "10", "12")   # resting API-style order

    tick = run_tick(session)

    applied = [e for e in tick.events if e["type"] == "place_order"]
    assert applied[0]["status"] == "applied"
    assert "order_id" in applied[0]
    assert holding_qty(session, buyer) == Decimal("10")
    trade_events = [e for e in tick.events if e["type"] == "trade"]
    assert len(trade_events) == 2  # one per side


def test_both_sides_see_trade_events_next_tick(world):
    session, buyer, seller, b, s, market = world
    for name, entity in (("b-watch", buyer), ("s-watch", seller)):
        session.add(Script(
            name=name, script_type=ScriptType.BEHAVIOUR, entity_id=entity.id,
            source="""
for i, e in ipairs(ctx.events) do
    if e.type == 'trade' then ctx.state.saw = e.side end
end
""",
        ))
    session.flush()
    buy(session, buyer, b, "5", "10")
    sell(session, seller, s, "5", "10")

    run_tick(session)  # auction happens here
    run_tick(session)  # scripts observe their fills here

    scripts = {sc.name: sc for sc in session.query(Script).all()}
    assert scripts["b-watch"].state["saw"] == "buy"
    assert scripts["s-watch"].state["saw"] == "sell"


def test_script_cancel_order(world):
    session, buyer, seller, b, s, market = world
    order = buy(session, buyer, b, "5", "10")
    session.add(Script(
        name="canceller", script_type=ScriptType.BEHAVIOUR, entity_id=buyer.id,
        source=f"ctx.action.cancel_order('{order.id}')",
    ))
    session.flush()

    tick = run_tick(session)

    assert order.status == OrderStatus.CANCELLED
    assert tick.events[0]["status"] == "applied"


def test_script_cannot_cancel_others_order(world):
    session, buyer, seller, b, s, market = world
    order = buy(session, buyer, b, "5", "10")
    session.add(Script(
        name="meddler", script_type=ScriptType.BEHAVIOUR, entity_id=seller.id,
        source=f"ctx.action.cancel_order('{order.id}')",
    ))
    session.flush()

    tick = run_tick(session)

    assert order.status == OrderStatus.OPEN
    assert tick.events[0]["status"] == "rejected"
    assert "does not own" in tick.events[0]["reason"]


def test_market_price_query_and_ctx_holdings(world):
    session, buyer, seller, b, s, market = world
    buy(session, buyer, b, "10", "12")
    sell(session, seller, s, "10", "12")
    run_tick(session)  # clears at 12; buyer now holds 10 WHEAT

    reader = Script(
        name="reader", script_type=ScriptType.BEHAVIOUR, entity_id=buyer.id,
        source="""
ctx.state.price = ctx.query.market_price('WHEAT')
ctx.state.nothing = ctx.query.market_price('GOLD')
ctx.state.mine = ctx.query.holding(ctx.entity.id, 'WHEAT')
ctx.state.first_holding = ctx.holdings[1] and ctx.holdings[1].symbol or 'none'
""",
    )
    session.add(reader)
    session.flush()
    run_tick(session)

    assert Decimal(reader.state["price"]) == Decimal("12")
    assert reader.state.get("nothing") is None
    assert Decimal(reader.state["mine"]) == Decimal("10")
    assert reader.state["first_holding"] == "WHEAT"
