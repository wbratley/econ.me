"""Camp storage — the load-release valve (docs/camp-storage.md).

Three surfaces, four actions. The private STORAGE at the hearth is
per-owner and capped (40 units); the shared DROP at every off-hearth
place and the shared WOODPILE at the hearth are unowned and unbounded.
Goods move between a holding and a facility's shelf (a FacilityHolding
row) — never created, never destroyed, never moved between two
entities. A pickup from a pool nobody owns is a normal goods move, not a
transfer: that is how theft and scavenging stay emergent.
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econengine import goods, markets, parcels, places, production, services
from econengine.lua_engine import Intent
from econengine.models import Base, EntityType, FacilityHolding
from econengine.scripting import build_queries, resolve_intent

_PACK = "camp-world"
SHARED = parcels.SHARED


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def hearth(session):
    """The hearth: an unowned ground parcel carrying the private STORAGE
    (cap 40) and the shared WOODPILE — the two home surfaces."""
    places.create_place(session, "HEARTH", kind="HEARTH",
                        name="The Hearth", pack_id=_PACK)
    ground = parcels.create_parcel(
        session, "COMMONS", name="The fire-ground", place="HEARTH")
    storage = parcels.add_facility(
        session, ground, "STORAGE", access="PLACE", capacity=40)
    pile = parcels.add_facility(
        session, ground, "WOODPILE", access="PLACE", capacity=1)
    return storage, pile


@pytest.fixture
def thicket(session, hearth):
    """The thicket: an unowned parcel carrying the shared DROP — the
    pile where goods sit for anyone to pick up."""
    places.create_place(session, "THICKET", kind="THICKET",
                        name="Thicket", pack_id=_PACK)
    drop_parcel = parcels.create_parcel(
        session, "COMMONS", name="Thicket drop", place="THICKET")
    return parcels.add_facility(
        session, drop_parcel, "DROP", access="PLACE", capacity=1)


def settler(session, name, at, goods="0", symbol="WOOD"):
    entity = services.create_entity(session, name, EntityType.INDIVIDUAL)
    if goods:
        markets.adjust_holding(session, entity, symbol, Decimal(goods))
    places.move_entity(session, entity, at)
    return entity


def act(session, entity, intent_type, symbol, qty):
    return resolve_intent(session, Intent(
        entity_id=entity.id, intent_type=intent_type,
        params={"symbol": symbol, "quantity": str(qty)},
        resource_ids=[symbol],
    ))


def store_qty(session, entity, symbol):
    row = session.execute(
        select(FacilityHolding).where(
            FacilityHolding.owner_id == entity.id,
            FacilityHolding.symbol == symbol.upper(),
        )
    ).scalars().first()
    return row.quantity if row else Decimal("0")


def pool_qty(session, facility, symbol):
    row = session.execute(
        select(FacilityHolding).where(
            FacilityHolding.facility_id == facility.id,
            FacilityHolding.owner_id == SHARED,
            FacilityHolding.symbol == symbol.upper(),
        )
    ).scalars().first()
    return row.quantity if row else Decimal("0")


def holding_qty(session, entity, symbol):
    h = markets.get_holding(session, entity.id, symbol)
    return h.quantity if h else Decimal("0")


# --- the private store: hold and reclaim -------------------------------------

def test_store_and_take_round_trip(session, hearth):
    storage, _ = hearth
    e = settler(session, "Astrid", "HEARTH", goods="10", symbol="WOOD")

    r = act(session, e, "facility_store", "WOOD", "10")
    assert r["status"] == "applied"
    assert r["facility_type"] == "STORAGE"
    assert r["symbol"] == "WOOD"
    assert r["quantity"] == "10.0000"
    assert r["place"] == "HEARTH"
    assert holding_qty(session, e, "WOOD") == Decimal("0")
    assert store_qty(session, e, "WOOD") == Decimal("10")

    r = act(session, e, "facility_take", "WOOD", "10")
    assert r["status"] == "applied"
    assert holding_qty(session, e, "WOOD") == Decimal("10")
    assert store_qty(session, e, "WOOD") == Decimal("0")


def test_store_is_capped(session, hearth):
    storage, _ = hearth
    e = settler(session, "Astrid", "HEARTH", goods="50", symbol="WOOD")

    assert act(session, e, "facility_store", "WOOD", "40")["status"] == "applied"
    assert store_qty(session, e, "WOOD") == Decimal("40")

    r = act(session, e, "facility_store", "WOOD", "1")
    assert r["status"] == "rejected"
    assert "store is full (40/40)" in r["reason"]
    # the 41st never moved
    assert holding_qty(session, e, "WOOD") == Decimal("10")
    assert store_qty(session, e, "WOOD") == Decimal("40")


def test_store_is_a_per_owner_cap(session, hearth):
    """Two houses, one store: each gets the full 40 (the cap is per
    owner, not per facility)."""
    storage, _ = hearth
    a = settler(session, "Astrid", "HEARTH", goods="40", symbol="WOOD")
    b = settler(session, "Bjorn", "HEARTH", goods="40", symbol="WOOD")

    assert act(session, a, "facility_store", "WOOD", "40")["status"] == "applied"
    r = act(session, b, "facility_store", "WOOD", "40")
    assert r["status"] == "applied"
    assert store_qty(session, a, "WOOD") == Decimal("40")
    assert store_qty(session, b, "WOOD") == Decimal("40")


def test_store_refuses_more_than_held(session, hearth):
    storage, _ = hearth
    e = settler(session, "Astrid", "HEARTH", goods="3", symbol="WOOD")
    r = act(session, e, "facility_store", "WOOD", "4")
    assert r["status"] == "rejected"
    assert "holds 3.0000 WOOD, cannot store 4" in r["reason"]
    assert holding_qty(session, e, "WOOD") == Decimal("3")
    assert store_qty(session, e, "WOOD") == Decimal("0")


def test_take_refuses_more_than_stored(session, hearth):
    storage, _ = hearth
    e = settler(session, "Astrid", "HEARTH", goods="5", symbol="WOOD")
    act(session, e, "facility_store", "WOOD", "2")
    r = act(session, e, "facility_take", "WOOD", "3")
    assert r["status"] == "rejected"
    assert "the store has 2.0000 WOOD, cannot take 3" in r["reason"]
    assert store_qty(session, e, "WOOD") == Decimal("2")
    assert holding_qty(session, e, "WOOD") == Decimal("3")


def test_store_without_a_store_at_the_place(session, thicket):
    """The thicket has a DROP, not a STORAGE: no private store here."""
    e = settler(session, "Astrid", "THICKET", goods="5", symbol="WOOD")
    r = act(session, e, "facility_store", "WOOD", "1")
    assert r["status"] == "rejected"
    assert "no storage at Thicket" in r["reason"]
    assert holding_qty(session, e, "WOOD") == Decimal("5")


# --- the shared drop: the theft/scavenging surface -------------------------

def test_drop_and_pickup_between_two_houses(session, hearth, thicket):
    """Astrid drops at the thicket; Bjorn, standing there, picks it up.
    A goods move into and out of a pool nobody owns — no transfer."""
    storage, _ = hearth
    drop = thicket
    a = settler(session, "Astrid", "THICKET", goods="6", symbol="BERRIES")
    b = settler(session, "Bjorn", "THICKET", goods="0", symbol="BERRIES")

    r = act(session, a, "facility_drop", "BERRIES", "6")
    assert r["status"] == "applied"
    assert r["facility_type"] == "DROP"
    assert r["place"] == "THICKET"
    assert holding_qty(session, a, "BERRIES") == Decimal("0")
    assert pool_qty(session, drop, "BERRIES") == Decimal("6")

    r = act(session, b, "facility_pickup", "BERRIES", "6")
    assert r["status"] == "applied"
    assert holding_qty(session, b, "BERRIES") == Decimal("6")
    assert pool_qty(session, drop, "BERRIES") == Decimal("0")
    assert holding_qty(session, a, "BERRIES") == Decimal("0")  # never returned


def test_pickup_refuses_more_than_the_pile(session, hearth, thicket):
    drop = thicket
    a = settler(session, "Astrid", "THICKET", goods="2", symbol="BERRIES")
    act(session, a, "facility_drop", "BERRIES", "2")
    b = settler(session, "Bjorn", "THICKET", goods="0", symbol="BERRIES")
    r = act(session, b, "facility_pickup", "BERRIES", "3")
    assert r["status"] == "rejected"
    assert "the shared pile has 2.0000 BERRIES, cannot pickup 3" in r["reason"]
    assert holding_qty(session, b, "BERRIES") == Decimal("0")
    assert pool_qty(session, drop, "BERRIES") == Decimal("2")


def test_drop_refuses_more_than_held(session, hearth, thicket):
    a = settler(session, "Astrid", "THICKET", goods="3", symbol="BERRIES")
    r = act(session, a, "facility_drop", "BERRIES", "4")
    assert r["status"] == "rejected"
    assert "holds 3.0000 BERRIES, cannot drop 4" in r["reason"]
    assert holding_qty(session, a, "BERRIES") == Decimal("3")


def test_wood_dropped_at_the_hearth_goes_to_the_woodpile(session, hearth):
    storage, pile = hearth
    e = settler(session, "Astrid", "HEARTH", goods="8", symbol="WOOD")
    r = act(session, e, "facility_drop", "WOOD", "8")
    assert r["status"] == "applied"
    assert r["facility_type"] == "WOODPILE"
    assert pool_qty(session, pile, "WOOD") == Decimal("8")
    assert holding_qty(session, e, "WOOD") == Decimal("0")


def test_the_hearth_takes_no_shared_non_wood(session, hearth):
    """Home is private: the hearth has a WOODPILE and a STORAGE, but no
    shared DROP — non-wood has nowhere to go (it is not the fire's fuel)."""
    e = settler(session, "Astrid", "HEARTH", goods="5", symbol="BERRIES")
    r = act(session, e, "facility_drop", "BERRIES", "1")
    assert r["status"] == "rejected"
    assert "no drop at The Hearth" in r["reason"]
    assert holding_qty(session, e, "BERRIES") == Decimal("5")


def test_pickup_wood_from_the_woodpile(session, hearth):
    storage, pile = hearth
    a = settler(session, "Astrid", "HEARTH", goods="5", symbol="WOOD")
    b = settler(session, "Bjorn", "HEARTH", goods="0", symbol="WOOD")
    act(session, a, "facility_drop", "WOOD", "5")
    r = act(session, b, "facility_pickup", "WOOD", "5")
    assert r["status"] == "applied"
    assert holding_qty(session, b, "WOOD") == Decimal("5")
    assert pool_qty(session, pile, "WOOD") == Decimal("0")


# --- the read surface --------------------------------------------------------

def test_stored_and_dropped_queries(session, hearth, thicket):
    storage, pile = hearth
    drop = thicket
    e = settler(session, "Astrid", "HEARTH", goods="10", symbol="WOOD")
    act(session, e, "facility_store", "WOOD", "4")
    act(session, e, "facility_drop", "WOOD", "6")

    queries = build_queries(session, owner_id=e.id)
    assert queries["stored"]("WOOD") == "4.0000"
    # at the hearth the shared pool for WOOD is the woodpile
    assert queries["dropped"]("WOOD") == "6.0000"

    # the store is private: a second house sees its own (empty) store
    b = settler(session, "Bjorn", "HEARTH", goods="0", symbol="WOOD")
    b_queries = build_queries(session, owner_id=b.id)
    assert b_queries["stored"]("WOOD") == "0.0000"
    # but the shared pile is shared
    assert b_queries["dropped"]("WOOD") == "6.0000"


def test_dropped_is_nil_where_there_is_no_pool(session, hearth):
    """The hearth has no shared non-wood: dropped('BERRIES') is nil."""
    e = settler(session, "Astrid", "HEARTH", goods="5", symbol="BERRIES")
    queries = build_queries(session, owner_id=e.id)
    assert queries["dropped"]("BERRIES") is None
    assert queries["stored"]("BERRIES") == "0.0000"  # store present, empty


# --- conservation ------------------------------------------------------------

def test_storage_never_creates_or_destroys(session, hearth, thicket):
    storage, pile = hearth
    drop = thicket
    a = settler(session, "Astrid", "HEARTH", goods="20", symbol="WOOD")
    b = settler(session, "Bjorn", "THICKET", goods="15", symbol="BERRIES")

    def total_wood():
        return (holding_qty(session, a, "WOOD")
                + holding_qty(session, b, "WOOD")
                + store_qty(session, a, "WOOD")
                + store_qty(session, b, "WOOD")
                + pool_qty(session, pile, "WOOD"))

    def total_berries():
        return (holding_qty(session, a, "BERRIES")
                + holding_qty(session, b, "BERRIES")
                + store_qty(session, a, "BERRIES")
                + store_qty(session, b, "BERRIES")
                + pool_qty(session, drop, "BERRIES")
                + pool_qty(session, pile, "BERRIES"))

    assert total_wood() == Decimal("20")
    assert total_berries() == Decimal("15")

    act(session, a, "facility_store", "WOOD", "5")
    act(session, a, "facility_drop", "WOOD", "3")
    act(session, b, "facility_drop", "BERRIES", "7")
    assert total_wood() == Decimal("20")
    assert total_berries() == Decimal("15")

    act(session, a, "facility_take", "WOOD", "5")
    act(session, a, "facility_pickup", "WOOD", "3")
    act(session, b, "facility_pickup", "BERRIES", "7")
    assert total_wood() == Decimal("20")
    assert total_berries() == Decimal("15")
    assert holding_qty(session, a, "WOOD") == Decimal("20")
    assert holding_qty(session, b, "BERRIES") == Decimal("15")


# --- the woodpile is the first fuel ------------------------------------------

def test_production_pulls_the_woodpile_before_holdings(session, hearth):
    storage, pile = hearth
    e = settler(session, "Astrid", "HEARTH", goods="10", symbol="WOOD")
    # the camp's pile is deep; the house's own back is thin
    act(session, e, "facility_drop", "WOOD", "8")
    assert pool_qty(session, pile, "WOOD") == Decimal("8")
    assert holding_qty(session, e, "WOOD") == Decimal("2")

    production.create_recipe(
        session, "CHOP", inputs={"WOOD": Decimal("3")},
        outputs={"ASH": Decimal("1")},
        duration_ticks=1)
    r = production.start_process(session, e, "CHOP")
    assert r.status.name == "RUNNING"

    # the three logs came off the pile, not the back
    assert pool_qty(session, pile, "WOOD") == Decimal("5")
    assert holding_qty(session, e, "WOOD") == Decimal("2")


def test_production_pulls_holdings_when_the_pile_is_thin(session, hearth):
    storage, pile = hearth
    e = settler(session, "Astrid", "HEARTH", goods="5", symbol="WOOD")
    production.create_recipe(
        session, "CHOP", inputs={"WOOD": Decimal("3")},
        outputs={"ASH": Decimal("1")},
        duration_ticks=1)
    production.start_process(session, e, "CHOP")

    # no pile at all: the three logs came off the back
    assert holding_qty(session, e, "WOOD") == Decimal("2")
    assert pool_qty(session, pile, "WOOD") == Decimal("0")


def test_the_woodpile_counts_toward_available_wood(session, hearth):
    """A thin back plus a deep pile can start a wood recipe that the
    back alone could not (the pull-first check sees both)."""
    storage, pile = hearth
    # a thin back (one log) plus a deep pile (five): the back alone could
    # not start CHOP, but the pile makes it. Pull-first takes the three
    # off the pile, leaving the back's log untouched.
    e = settler(session, "Astrid", "HEARTH", goods="6", symbol="WOOD")
    act(session, e, "facility_drop", "WOOD", "5")
    assert holding_qty(session, e, "WOOD") == Decimal("1")

    production.create_recipe(
        session, "CHOP", inputs={"WOOD": Decimal("3")},
        outputs={"ASH": Decimal("1")},
        duration_ticks=1)
    r = production.start_process(session, e, "CHOP")
    assert r.status.name == "RUNNING"
    # the three came off the pile; the back's log is untouched
    assert pool_qty(session, pile, "WOOD") == Decimal("2")
    assert holding_qty(session, e, "WOOD") == Decimal("1")


# --- decay and estate --------------------------------------------------------

def test_stored_perishables_decay(session, hearth):
    """The store is a shelf, not a vault: what rots there rots there
    (one summary decay event, as with holdings and escrows)."""
    storage, _ = hearth
    e = settler(session, "Astrid", "HEARTH", goods="10", symbol="BERRIES")
    act(session, e, "facility_store", "BERRIES", "10")

    from econengine.models import Good
    goods.create_good(session, "BERRIES", decay_per_tick=Decimal("0.05"))
    events = goods.apply_decay(session, 1)

    decayed = [ev for ev in events if ev["type"] == "decay"]
    assert len(decayed) == 1
    assert store_qty(session, e, "BERRIES") == Decimal("9.5")
    assert holding_qty(session, e, "BERRIES") == Decimal("0")


def test_the_estate_moves_the_private_store_to_the_recipient(session, hearth):
    """Incapacity transfers the deceased's PRIVATE store rows to the
    recipient (the shared pools are untouched)."""
    from econengine import conditions
    from econengine.models import Good
    storage, _ = hearth
    d = settler(session, "Old", "HEARTH", goods="10", symbol="WOOD")
    heir = settler(session, "Heir", "HEARTH", goods="0", symbol="WOOD")
    act(session, d, "facility_store", "WOOD", "6")
    assert store_qty(session, d, "WOOD") == Decimal("6")

    conditions._apply_estate(session, d, heir)

    assert store_qty(session, d, "WOOD") == Decimal("0")
    assert store_qty(session, heir, "WOOD") == Decimal("6")
    # the deceased's carried log (the 4 not shelved) also passes on;
    # the 6 shelved stay shelved, in the heir's own private store
    assert holding_qty(session, heir, "WOOD") == Decimal("4")
