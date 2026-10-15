"""Market escrows — sellers must deliver (S5b, "the far book").

docs/spatial.md S5b: a SELL on a placed market debits the goods onto
the market's shelf at placement; fills draw the shelf (a placed market
literally cannot fill from goods that aren't there); cancel/expiry
keeps the goods ON the shelf, and the auction pass auto-reclaims them
to a seller standing at the place — walking back IS the mechanism, no
intent, no recipe. Global markets (place_id NULL) never engage the
shelf: the settle-from-holdings legacy path, bit for bit.
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econengine import markets, places, production, services
from econengine.lua_engine import Intent
from econengine.markets import (
    InsufficientHoldingsError, cancel_order, create_market, place_order,
    run_auctions,
)
from econengine.models import Base, EntityType, MarketEscrow, OrderStatus
from econengine.scripting import build_queries, resolve_intent
from econengine.tick import run_tick

_PACK = "escrow-world"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def post(session):
    """A home place and a placed WHEAT/USD market at the post."""
    places.create_place(session, "HOME", kind="HEARTH", name="Home",
                        pack_id=_PACK)
    places.create_place(session, "POST", kind="POST", name="The Post",
                        pack_id=_PACK)
    return create_market(session, "WHEAT", "USD", place="POST")


def trader(session, name, at, balance="0", goods="0", symbol="WHEAT"):
    entity = services.create_entity(session, name, EntityType.INDIVIDUAL)
    account = services.create_account(session, entity, "USD",
                                      initial_balance=Decimal(balance))
    markets.adjust_holding(session, entity, symbol, Decimal(goods))
    places.move_entity(session, entity, at)
    return entity, account


def escrow_qty(session, market, seller):
    row = markets.get_escrow(session, market, seller.id, market.symbol)
    return row.quantity if row else Decimal("0")


def holding_qty(session, entity, symbol="WHEAT"):
    h = markets.get_holding(session, entity.id, symbol)
    return h.quantity if h else Decimal("0")


def reclaims(events):
    return [e for e in events if e["type"] == "market_reclaim"]


# --- placement: the goods move when the order does -------------------------

def test_a_sell_on_a_placed_market_delivers_at_placement(session, post):
    seller, account = trader(session, "Seller", "POST", goods="10")
    place_order(session, seller.id, "WHEAT", "sell",
                Decimal("10"), Decimal("2"), account.id)

    assert holding_qty(session, seller) == Decimal("0")
    assert escrow_qty(session, post, seller) == Decimal("10")


def test_a_refused_delivery_leaves_nothing(session, post):
    seller, account = trader(session, "Seller", "POST", goods="5")
    with pytest.raises(InsufficientHoldingsError, match="you must deliver"):
        place_order(session, seller.id, "WHEAT", "sell",
                    Decimal("10"), Decimal("2"), account.id)

    assert holding_qty(session, seller) == Decimal("5")
    assert escrow_qty(session, post, seller) == Decimal("0")
    from econengine.models import Order
    open_orders = session.execute(
        select(Order).where(Order.status == OrderStatus.OPEN)
    ).scalars().all()
    assert open_orders == []


def test_reserved_goods_cannot_be_delivered(session, post):
    """The oven mid-bake: good-requirements of RUNNING processes are
    unavailable to delivery, exactly as they are to settlement."""
    seller, account = trader(session, "Smith", "POST", goods="5")
    production.create_recipe(
        session, "BAKE", inputs={}, outputs={"ASH": Decimal("1")},
        duration_ticks=3, good_requirements={"WHEAT": Decimal("2")},
    )
    production.start_process(session, seller, "BAKE")

    with pytest.raises(InsufficientHoldingsError, match="you must deliver"):
        place_order(session, seller.id, "WHEAT", "sell",
                    Decimal("4"), Decimal("2"), account.id)

    # the unreserved three deliver; the two backing the bake do not
    place_order(session, seller.id, "WHEAT", "sell",
                Decimal("3"), Decimal("2"), account.id)
    assert escrow_qty(session, post, seller) == Decimal("3")


def test_requoting_a_level_reasserts_without_redelivering(session, post):
    """The stone-run7 lesson, escrow edition: a script re-asserting its
    quote every tick must not deliver the goods again on every tick."""
    seller, account = trader(session, "Seller", "POST", goods="10")
    first = place_order(session, seller.id, "WHEAT", "sell",
                        Decimal("10"), Decimal("2"), account.id)
    second = place_order(session, seller.id, "WHEAT", "sell",
                         Decimal("10"), Decimal("2"), account.id)

    assert first.status == OrderStatus.CANCELLED
    assert escrow_qty(session, post, seller) == Decimal("10")  # once
    assert holding_qty(session, seller) == Decimal("0")


def test_a_bigger_requote_delivers_only_the_shortfall(session, post):
    seller, account = trader(session, "Seller", "POST", goods="12")
    place_order(session, seller.id, "WHEAT", "sell",
                Decimal("10"), Decimal("2"), account.id)
    place_order(session, seller.id, "WHEAT", "sell",
                Decimal("12"), Decimal("2"), account.id)

    assert holding_qty(session, seller) == Decimal("0")
    assert escrow_qty(session, post, seller) == Decimal("12")


def test_the_shelf_backs_a_requote_after_a_cancel(session, post):
    """Goods left on the shelf by a cancelled order are not hostage: a
    later sell at the same market draws on them before touching the
    seller's holdings again."""
    seller, account = trader(session, "Seller", "POST", goods="10")
    order = place_order(session, seller.id, "WHEAT", "sell",
                        Decimal("10"), Decimal("2"), account.id)
    cancel_order(session, order.id, seller.id)

    markets.adjust_holding(session, seller, "WHEAT", Decimal("10"))  # restock
    place_order(session, seller.id, "WHEAT", "sell",
                Decimal("6"), Decimal("1.5"), account.id)

    # 6 backed by the 10 already on the shelf: nothing new delivered
    assert holding_qty(session, seller) == Decimal("10")
    assert escrow_qty(session, post, seller) == Decimal("10")


# --- fills: the shelf pays --------------------------------------------------

def test_a_fill_draws_the_shelf_and_pays_the_absent_seller(session, post):
    seller, s_account = trader(session, "Seller", "POST", goods="10")
    buyer, b_account = trader(session, "Buyer", "POST", balance="100")
    order = place_order(session, seller.id, "WHEAT", "sell",
                        Decimal("10"), Decimal("2"), s_account.id)

    places.move_entity(session, seller, "HOME")  # the seller goes home
    place_order(session, buyer.id, "WHEAT", "buy",
                Decimal("10"), Decimal("2"), b_account.id)
    events = run_auctions(session, tick_number=1)

    assert order.status == OrderStatus.FILLED
    assert holding_qty(session, buyer) == Decimal("10")
    assert holding_qty(session, seller) == Decimal("0")  # never touched
    assert escrow_qty(session, post, seller) == Decimal("0")
    assert s_account.balance == Decimal("20")
    assert not reclaims(events)  # nothing to reclaim: order filled


def test_a_partial_fill_leaves_the_remainder_on_the_shelf(session, post):
    seller, s_account = trader(session, "Seller", "POST", goods="10")
    buyer, b_account = trader(session, "Buyer", "POST", balance="10")
    order = place_order(session, seller.id, "WHEAT", "sell",
                        Decimal("10"), Decimal("2"), s_account.id)

    places.move_entity(session, seller, "HOME")
    place_order(session, buyer.id, "WHEAT", "buy",
                Decimal("5"), Decimal("2"), b_account.id)
    run_auctions(session, tick_number=1)

    assert order.status == OrderStatus.OPEN
    assert order.remaining == Decimal("5")
    assert escrow_qty(session, post, seller) == Decimal("5")


# --- cancel/reclaim: walking back IS the mechanism ---------------------------

def test_cancel_keeps_the_goods_on_the_shelf(session, post):
    seller, account = trader(session, "Seller", "POST", goods="10")
    order = place_order(session, seller.id, "WHEAT", "sell",
                        Decimal("10"), Decimal("2"), account.id)

    places.move_entity(session, seller, "HOME")
    cancel_order(session, order.id, seller.id)
    events = run_auctions(session, tick_number=1)

    assert not reclaims(events)
    assert escrow_qty(session, post, seller) == Decimal("10")
    assert holding_qty(session, seller) == Decimal("0")


def test_walking_back_reclaims_with_an_event(session, post):
    seller, account = trader(session, "Seller", "POST", goods="10")
    order = place_order(session, seller.id, "WHEAT", "sell",
                        Decimal("10"), Decimal("2"), account.id)
    places.move_entity(session, seller, "HOME")
    cancel_order(session, order.id, seller.id)
    run_auctions(session, tick_number=1)

    places.move_entity(session, seller, "POST")
    events = run_auctions(session, tick_number=2)

    reclaimed = reclaims(events)
    assert len(reclaimed) == 1
    assert reclaimed[0]["entity_id"] == seller.id
    assert reclaimed[0]["market"] == "WHEAT"
    assert reclaimed[0]["quantity"] == "10.0000"  # str, JSON-safe
    assert escrow_qty(session, post, seller) == Decimal("0")
    assert holding_qty(session, seller) == Decimal("10")


def test_the_reclaim_takes_only_the_unclaimed_shelf(session, post):
    """"6 jerky on the shelf, 2 claimed": the open order's backing stays
    deliverable; only the free remainder flows home."""
    seller, account = trader(session, "Seller", "POST", goods="6")
    base = place_order(session, seller.id, "WHEAT", "sell",
                       Decimal("2"), Decimal("2"), account.id)
    assert escrow_qty(session, post, seller) == Decimal("2")

    # fully claimed -> present or not, nothing flows back
    assert not reclaims(run_auctions(session, tick_number=1))

    # a second ask delivers 4 more, then cancels: 4 free on the shelf
    far = place_order(session, seller.id, "WHEAT", "sell",
                      Decimal("4"), Decimal("9"), account.id)
    cancel_order(session, far.id, seller.id)

    events = run_auctions(session, tick_number=2)  # seller still at POST
    assert len(reclaims(events)) == 1
    assert reclaims(events)[0]["quantity"] == "4.0000"
    assert escrow_qty(session, post, seller) == Decimal("2")  # base intact
    assert holding_qty(session, seller) == Decimal("4")

    # the base order's 2 are still deliverable: a buyer takes them
    buyer, b_account = trader(session, "Buyer", "POST", balance="10")
    place_order(session, buyer.id, "WHEAT", "buy",
                Decimal("2"), Decimal("2"), b_account.id)
    run_auctions(session, tick_number=3)
    assert base.status == OrderStatus.FILLED
    assert holding_qty(session, buyer) == Decimal("2")


# --- global markets: the legacy path, untouched -------------------------------

def test_global_markets_never_escrow(session):
    seller, s_account = trader(session, "Seller", None, goods="10")
    buyer, b_account = trader(session, "Buyer", None, balance="100")
    create_market(session, "WHEAT", "USD")

    order = place_order(session, seller.id, "WHEAT", "sell",
                        Decimal("10"), Decimal("2"), s_account.id)
    assert holding_qty(session, seller) == Decimal("10")  # not debited
    assert session.execute(select(MarketEscrow)).scalars().first() is None

    place_order(session, buyer.id, "WHEAT", "buy",
                Decimal("10"), Decimal("2"), b_account.id)
    run_auctions(session, tick_number=1)

    assert holding_qty(session, seller) == Decimal("0")  # drawn from holdings
    assert order.status == OrderStatus.FILLED


# --- the read surface ---------------------------------------------------------

def test_market_shelf_reads_the_shelf(session, post):
    seller, account = trader(session, "Seller", "POST", goods="10")
    base = place_order(session, seller.id, "WHEAT", "sell",
                       Decimal("6"), Decimal("2"), account.id)

    shelf = build_queries(session)["market_shelf"]("WHEAT")
    assert shelf == {"shelf": "6.0000", "claimed": "6.0000", "free": "0.0000"}

    # a cancelled ask frees its half of the shelf without moving goods
    far = place_order(session, seller.id, "WHEAT", "sell",
                      Decimal("4"), Decimal("9"), account.id)
    cancel_order(session, far.id, far.entity_id)
    shelf = build_queries(session)["market_shelf"]("WHEAT")
    assert shelf == {"shelf": "10.0000", "claimed": "6.0000", "free": "4.0000"}
    assert base.status == OrderStatus.OPEN


def test_market_shelf_is_nil_for_global_and_bare(session):
    create_market(session, "WHEAT", "USD")
    queries = build_queries(session)
    assert queries["market_shelf"]("WHEAT") is None
    assert queries["market_shelf"]("NOTHING") is None


def test_the_far_book_scenario(session):
    """The trip this feature exists for: carry the load to the far
    market, sell it onto the shelf, walk home — and the book fills from
    your delivery while you are away."""
    for key, kind in (("HEARTH", "HEARTH"), ("THICKET", "THICKET"),
                      ("RIVER", "RIVER")):
        places.create_place(session, key, kind=kind, name=key.title(),
                            pack_id=_PACK)
    from econengine import edges
    edges.create_edge(session, "HEARTH", "THICKET", "walk", 1, pack_id=_PACK)
    edges.create_edge(session, "THICKET", "RIVER", "walk", 2, pack_id=_PACK)
    production.create_recipe(session, "TRAVEL_WALK", inputs={}, outputs={},
                             duration_ticks=1)

    market = create_market(session, "FISH", "USD", place="RIVER")
    seller, s_account = trader(session, "Fisher", "HEARTH", goods="8", symbol="FISH")
    buyer, b_account = trader(session, "Cook", "RIVER", balance="24")
    markets.adjust_holding(session, buyer, "FISH", Decimal("0"))

    # the walk out: HEARTH -> THICKET -> RIVER
    assert _walk(session, seller, "RIVER")["status"] == "applied"
    _walk_out(session, seller, "RIVER")

    # sell the catch onto the far shelf
    order = place_order(session, seller.id, "FISH", "sell",
                        Decimal("8"), Decimal("3"), s_account.id)
    assert escrow_qty(session, market, seller) == Decimal("8")

    # walk home before the book speaks
    assert _walk(session, seller, "HEARTH")["status"] == "applied"
    _walk_out(session, seller, "HEARTH")

    # the buyer at the river fills from the delivery
    place_order(session, buyer.id, "FISH", "buy",
                Decimal("6"), Decimal("3"), b_account.id)
    events = run_tick(session).events

    assert order.status == OrderStatus.OPEN      # 6 of 8 taken
    assert order.remaining == Decimal("2")
    assert holding_qty(session, buyer, "FISH") == Decimal("6")
    assert s_account.balance == Decimal("18")    # paid while three hours home
    assert escrow_qty(session, market, seller) == Decimal("2")
    assert not any(e["type"] == "market_reclaim" for e in events)


def _walk(session, entity, to):
    return resolve_intent(session, Intent(
        entity_id=entity.id, intent_type="travel", params={"to": to},
        resource_ids=[],
    ))


def _walk_out(session, entity, key, limit=8):
    """Run ticks until the walker stands at `key` (a hop of duration d
    lands d+1 ticks after departure)."""
    for _ in range(limit):
        if entity.place is not None and entity.place.key == key:
            return
        run_tick(session)
    assert entity.place is not None and entity.place.key == key, "never arrived"
