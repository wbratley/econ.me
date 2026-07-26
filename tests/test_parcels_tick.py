"""Parcels through the tick engine: deposit regeneration, reserved
holdings at auction settlement, and scripts working their land (ctx.parcels,
parcel-bound start_process, transfer_parcel intents)."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.markets import adjust_holding, create_market, get_holding, place_order
from econengine.models import Base, EntityType, Script, ScriptType
from econengine.parcels import add_deposit, add_facility, create_parcel, get_deposit
from econengine.production import create_recipe, start_process
from econengine.services import create_account, create_entity
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_script(session, name, source, entity):
    script = Script(name=name, source=source, script_type=ScriptType.BEHAVIOUR,
                    entity_id=entity.id)
    session.add(script)
    session.flush()
    return script


# --- deposit regeneration -------------------------------------------------

def test_deposits_regenerate_toward_capacity(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "WOOD", owner=alice)
    add_deposit(session, parcel, "TIMBER", Decimal("3"),
                capacity=Decimal("4.5"), regen_per_tick=Decimal("1"))
    add_deposit(session, parcel, "IRON", Decimal("5"))  # static: no regen

    tick = run_tick(session)
    event = next(e for e in tick.events if e["type"] == "deposit_regen")
    assert event["entity_id"] == alice.id and event["symbol"] == "TIMBER"
    assert event["regenerated"] == "1.0000" and event["quantity"] == "4.0000"

    tick = run_tick(session)  # capped at capacity: only 0.5 grows back
    event = next(e for e in tick.events if e["type"] == "deposit_regen")
    assert event["quantity"] == "4.5000"

    tick = run_tick(session)  # full: no event at all
    assert not any(e["type"] == "deposit_regen" for e in tick.events)
    assert get_deposit(session, parcel.id, "IRON").quantity == Decimal("5")


def test_extraction_loop_over_ticks(session):
    """A woodcutter script fells regenerating timber every tick — the
    deposit reaches a harvest/regen equilibrium instead of running dry."""
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "WOOD", owner=alice)
    add_deposit(session, parcel, "TIMBER", Decimal("10"),
                capacity=Decimal("10"), regen_per_tick=Decimal("1"))
    create_recipe(session, "FELL", inputs={}, outputs={"LOG": Decimal("2")},
                  duration_ticks=1, deposit_inputs={"TIMBER": Decimal("2")})
    make_script(session, "fell",
                "ctx.action.start_process('FELL', ctx.parcels[1].id)", alice)

    for _ in range(4):
        run_tick(session)

    # tick 1 starts the first FELL; ticks 2-4 complete one each
    assert get_holding(session, alice.id, "LOG").quantity == Decimal("6")
    # drawn 2 per tick (4x), regenerated 1 per tick after the first draw
    assert get_deposit(session, parcel.id, "TIMBER").quantity == Decimal("5")


# --- reservation at settlement --------------------------------------------

def test_reserved_machinery_cannot_be_sold_at_auction(session):
    """The no-escrow live check treats reserved quantities as unavailable:
    the baker's sell order for the oven backing a running bake is cancelled
    at settlement, not filled."""
    create_market(session, "OVEN", "USD")
    baker = create_entity(session, "Baker", EntityType.INDIVIDUAL)
    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    baker_acct = create_account(session, baker, "USD")
    buyer_acct = create_account(session, buyer, "USD", initial_balance=Decimal("100"))
    adjust_holding(session, baker, "OVEN", Decimal("1"))
    adjust_holding(session, baker, "FLOUR", Decimal("5"))
    create_recipe(session, "BAKE", inputs={"FLOUR": Decimal("1")},
                  outputs={"BREAD": Decimal("2")}, duration_ticks=3,
                  good_requirements={"OVEN": Decimal("1")})
    start_process(session, baker, "BAKE")

    sell = place_order(session, baker.id, "OVEN", "sell", Decimal("1"),
                       Decimal("10"), baker_acct.id)
    place_order(session, buyer.id, "OVEN", "buy", Decimal("1"),
                Decimal("10"), buyer_acct.id)

    tick = run_tick(session)

    cancelled = next(e for e in tick.events if e["type"] == "order_cancelled")
    assert cancelled["order_id"] == sell.id
    assert cancelled["reason"] == "insufficient holdings at auction"
    assert get_holding(session, baker.id, "OVEN").quantity == Decimal("1")
    assert not any(e["type"] == "trade" for e in tick.events)


# --- scripts working their land -------------------------------------------

def test_ctx_parcels_lists_facilities_and_deposits(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "CLAIM", owner=alice, region_id="R1")
    add_facility(session, parcel, "SMITHY")
    add_deposit(session, parcel, "IRON", Decimal("7"))
    make_script(session, "survey", """
        local p = ctx.parcels[1]
        ctx.state.parcel_type = p.parcel_type
        ctx.state.facility = p.facilities[1]
        ctx.state.iron = p.deposits.IRON
    """, alice)

    run_tick(session)

    script = session.query(Script).one()
    assert script.state == {"parcel_type": "CLAIM", "facility": "SMITHY",
                            "iron": "7.0000"}


def test_construction_completes_through_run_tick(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "LOT", owner=alice)
    adjust_holding(session, alice, "TIMBER", Decimal("10"))
    create_recipe(session, "BUILD_FARM", inputs={"TIMBER": Decimal("10")},
                  outputs={}, duration_ticks=1, builds_facility="FARM")
    start_process(session, alice, "BUILD_FARM", parcel_id=parcel.id)

    tick = run_tick(session)  # tick 1: still building
    assert not any(e["type"] == "process_completed" for e in tick.events)
    tick = run_tick(session)  # tick 2: completes

    event = next(e for e in tick.events if e["type"] == "process_completed")
    assert event["facility"] == "FARM" and event["parcel_id"] == parcel.id
    assert [f.facility_type for f in parcel.facilities] == ["FARM"]


def test_transfer_parcel_intent(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "LOT", owner=alice)
    make_script(session, "gift",
                f"ctx.action.transfer_parcel('{parcel.id}', '{bob.id}')", alice)

    tick = run_tick(session)

    event = next(e for e in tick.events if e["type"] == "transfer_parcel")
    assert event["status"] == "applied"
    assert parcel.owner_id == bob.id


def test_transfer_parcel_intent_rejected_for_non_owner(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "LOT", owner=alice)
    make_script(session, "steal",
                f"ctx.action.transfer_parcel('{parcel.id}', '{bob.id}')", bob)

    tick = run_tick(session)

    event = next(e for e in tick.events if e["type"] == "transfer_parcel")
    assert event["status"] == "rejected"
    assert parcel.owner_id == alice.id
