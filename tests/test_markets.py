import pytest
from decimal import Decimal
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from econengine.markets import (
    InsufficientHoldingsError,
    MarketInactiveError,
    adjust_holding,
    cancel_order,
    create_market,
    place_order,
)
from econengine.models import (
    Base, EntityType, Order, OrderSide, OrderStatus,
)
from econengine.services import CurrencyMismatchError, create_account, create_entity


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    a = create_account(session, alice, "USD", initial_balance=Decimal("1000"))
    market = create_market(session, "wheat", "usd", name="Wheat")
    return session, alice, a, market


# --- create_market ---

def test_create_market_uppercases(world):
    session, alice, a, market = world
    assert market.symbol == "WHEAT"
    assert market.currency == "USD"
    assert market.is_active
    assert market.last_price is None


def test_duplicate_symbol_rejected(world):
    session, alice, a, market = world
    with pytest.raises(IntegrityError):
        create_market(session, "WHEAT", "USD")


# --- re-quoting a price level (cancel-replace; the stone-run7 lesson) ---

def test_requoting_price_level_supersedes(world):
    """A script that runs every tick re-asserts its quotes; plain
    good-til-cancelled stacked one more OPEN order per tick (a stone-run7
    house left 878, ~240 of them the SAME order). Re-quoting your own
    price level now cancel-replaces: the new order carries the level."""
    session, alice, a, market = world
    first = place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("10"), a.id)
    second = place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("10"), a.id)
    session.refresh(first)
    assert first.status == OrderStatus.CANCELLED
    assert first.cancel_reason == "superseded at price level"
    assert second.status == OrderStatus.OPEN
    assert second.remaining == Decimal("1")


def test_requoting_with_new_quantity_carries_the_level(world):
    """The every-tick surplus sell: holdings grew, the script re-quotes
    the same ask with the new total. The level carries the quantity just
    placed -- re-assertion, not accumulation."""
    session, alice, a, market = world
    place_order(session, alice.id, "WHEAT", "sell", Decimal("5"), Decimal("10"), a.id)
    grown = place_order(session, alice.id, "WHEAT", "sell", Decimal("8"), Decimal("10"), a.id)
    open_orders = session.execute(
        select(Order).where(Order.status == OrderStatus.OPEN)
    ).scalars().all()
    assert [o.id for o in open_orders] == [grown.id]
    assert grown.quantity == Decimal("8") and grown.remaining == Decimal("8")


def test_ladder_prices_stack(world):
    """Depth is intent when the prices differ: a ladder of bids is a
    strategy, not a re-assertion."""
    session, alice, a, market = world
    deep = place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("9"), a.id)
    top = place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("10"), a.id)
    assert deep.status == OrderStatus.OPEN and top.status == OrderStatus.OPEN


def test_requote_leaves_other_sides_and_traders_alone(world):
    session, alice, a, market = world
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    b = create_account(session, bob, "USD", initial_balance=Decimal("1000"))
    alice_sell = place_order(session, alice.id, "WHEAT", "sell", Decimal("1"), Decimal("10"), a.id)
    bob_bid = place_order(session, bob.id, "WHEAT", "buy", Decimal("1"), Decimal("10"), b.id)
    place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("10"), a.id)
    session.refresh(alice_sell)
    session.refresh(bob_bid)
    assert alice_sell.status == OrderStatus.OPEN   # other side: not my level
    assert bob_bid.status == OrderStatus.OPEN      # other trader: not my level


# --- adjust_holding ---

def test_adjust_holding_creates_and_accumulates(world):
    session, alice, a, market = world
    h = adjust_holding(session, alice, "wheat", Decimal("10"))
    assert h.symbol == "WHEAT"
    assert h.quantity == Decimal("10")
    h2 = adjust_holding(session, alice, "WHEAT", Decimal("5"))
    assert h2 is h
    assert h.quantity == Decimal("15")


def test_adjust_holding_cannot_go_negative(world):
    session, alice, a, market = world
    adjust_holding(session, alice, "WHEAT", Decimal("10"))
    with pytest.raises(InsufficientHoldingsError):
        adjust_holding(session, alice, "WHEAT", Decimal("-11"))
    # unchanged after the failed adjustment
    assert adjust_holding(session, alice, "WHEAT", Decimal("0")).quantity == Decimal("10")


# --- place_order ---

def test_place_order_creates_open_order(world):
    session, alice, a, market = world
    order = place_order(session, alice.id, "wheat", "buy", Decimal("10"), Decimal("12.5"), a.id)
    assert order.status == OrderStatus.OPEN
    assert order.side == OrderSide.BUY
    assert order.remaining == Decimal("10")
    assert order.market_id == market.id


def test_place_order_unknown_market(world):
    session, alice, a, market = world
    with pytest.raises(ValueError, match="no market"):
        place_order(session, alice.id, "GOLD", "buy", Decimal("1"), Decimal("1"), a.id)


def test_place_order_inactive_market(world):
    session, alice, a, market = world
    market.is_active = False
    with pytest.raises(MarketInactiveError):
        place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("1"), a.id)


def test_place_order_rejects_nonpositive(world):
    session, alice, a, market = world
    with pytest.raises(ValueError, match="quantity"):
        place_order(session, alice.id, "WHEAT", "buy", Decimal("0"), Decimal("1"), a.id)
    with pytest.raises(ValueError, match="limit_price"):
        place_order(session, alice.id, "WHEAT", "sell", Decimal("1"), Decimal("-2"), a.id)


def test_place_order_rejects_bad_side(world):
    session, alice, a, market = world
    with pytest.raises(ValueError, match="side"):
        place_order(session, alice.id, "WHEAT", "hold", Decimal("1"), Decimal("1"), a.id)


def test_place_order_rejects_unowned_account(world):
    session, alice, a, market = world
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    with pytest.raises(ValueError, match="does not belong"):
        place_order(session, bob.id, "WHEAT", "buy", Decimal("1"), Decimal("1"), a.id)


def test_place_order_rejects_currency_mismatch(world):
    session, alice, a, market = world
    eur = create_account(session, alice, "EUR")
    with pytest.raises(CurrencyMismatchError):
        place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("1"), eur.id)


# --- cancel_order ---

def test_cancel_order(world):
    session, alice, a, market = world
    order = place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("1"), a.id)
    cancelled = cancel_order(session, order.id, alice.id)
    assert cancelled.status == OrderStatus.CANCELLED


def test_cancel_order_wrong_entity(world):
    session, alice, a, market = world
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    order = place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("1"), a.id)
    with pytest.raises(ValueError, match="does not own"):
        cancel_order(session, order.id, bob.id)


def test_cancel_order_not_open(world):
    session, alice, a, market = world
    order = place_order(session, alice.id, "WHEAT", "buy", Decimal("1"), Decimal("1"), a.id)
    cancel_order(session, order.id, alice.id)
    with pytest.raises(ValueError, match="only open"):
        cancel_order(session, order.id, alice.id)
