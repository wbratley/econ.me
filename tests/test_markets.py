import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from econ.markets import (
    InsufficientHoldingsError,
    MarketInactiveError,
    adjust_holding,
    cancel_order,
    create_market,
    place_order,
)
from econ.models import Base, EntityType, OrderSide, OrderStatus
from econ.services import CurrencyMismatchError, create_account, create_entity


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
