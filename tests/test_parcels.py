"""Parcels, facilities, and deposits at the engine level: land ownership
invariants, located production (facility requirements, deposit draw-down,
construction), and the present-but-not-consumed requirement check with
reservation — one mechanism for machinery and facilities alike."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econ.markets import (
    InsufficientHoldingsError, adjust_holding, get_holding, reserved_quantity,
)
from econ.models import Base, EntityType, Facility, ProcessStatus
from econ.parcels import (
    InsufficientDepositError,
    add_deposit,
    add_facility,
    create_parcel,
    draw_deposit,
    facility_count,
    get_deposit,
    grant_parcel,
    reserved_facilities,
    transfer_parcel,
)
from econ.production import (
    cancel_process,
    complete_processes,
    create_recipe,
    recipe_needs_parcel,
    start_process,
)
from econ.services import create_entity


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# --- land ownership -------------------------------------------------------

def test_create_parcel_normalizes_and_starts_unclaimed(session):
    parcel = create_parcel(session, "field", region_id="R1", extent_ref="chunk:0,0")
    assert parcel.parcel_type == "FIELD"
    assert parcel.owner_id is None
    assert parcel.facilities == [] and parcel.deposits == []


def test_transfer_requires_owner_intent(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "LOT", owner=alice)

    with pytest.raises(ValueError, match="does not own"):
        transfer_parcel(session, parcel.id, bob.id, bob)  # bob is not the owner
    transfer_parcel(session, parcel.id, alice.id, bob)
    assert parcel.owner_id == bob.id


def test_grant_assigns_and_revokes_by_policy(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "LOT")
    grant_parcel(session, parcel, alice)
    assert parcel.owner_id == alice.id
    grant_parcel(session, parcel, None)
    assert parcel.owner_id is None


def test_parcel_with_running_process_cannot_change_hands(session):
    """A half-built smithy cannot be gifted onto (or stolen from) someone
    else's land — the binding made at start stays valid through completion."""
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "LOT", owner=alice)
    adjust_holding(session, alice, "TIMBER", Decimal("10"))
    create_recipe(session, "BUILD_SMITHY", inputs={"TIMBER": Decimal("10")},
                  outputs={}, duration_ticks=2, builds_facility="SMITHY")
    start_process(session, alice, "BUILD_SMITHY", parcel_id=parcel.id)

    with pytest.raises(ValueError, match="running processes"):
        transfer_parcel(session, parcel.id, alice.id, bob)
    with pytest.raises(ValueError, match="running processes"):
        grant_parcel(session, parcel, bob)

    complete_processes(session, tick_number=3)
    transfer_parcel(session, parcel.id, alice.id, bob)  # free again once done
    assert parcel.owner_id == bob.id


# --- construction (builds_facility) ---------------------------------------

def test_construction_erects_facility_on_bound_parcel(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "LOT", owner=alice)
    adjust_holding(session, alice, "TIMBER", Decimal("10"))
    recipe = create_recipe(session, "BUILD_SMITHY", inputs={"TIMBER": Decimal("10")},
                           outputs={}, duration_ticks=2, builds_facility="smithy")
    assert recipe.builds_facility == "SMITHY"
    assert recipe_needs_parcel(recipe)

    process = start_process(session, alice, "BUILD_SMITHY", parcel_id=parcel.id)
    assert facility_count(session, parcel.id, "SMITHY") == 0  # not while building

    events = complete_processes(session, tick_number=3)
    assert facility_count(session, parcel.id, "SMITHY") == 1
    facility = session.query(Facility).one()
    assert facility.built_tick == 3 and facility.parcel_id == parcel.id
    event = next(e for e in events if e["type"] == "process_completed")
    assert event["facility"] == "SMITHY" and event["parcel_id"] == parcel.id
    assert process.status == ProcessStatus.COMPLETED


def test_parcel_bound_recipe_refuses_unbound_or_uncontrolled_start(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "LOT", owner=bob)
    adjust_holding(session, alice, "TIMBER", Decimal("10"))
    create_recipe(session, "BUILD_SMITHY", inputs={"TIMBER": Decimal("10")},
                  outputs={}, duration_ticks=1, builds_facility="SMITHY")

    with pytest.raises(ValueError, match="must be bound to a parcel"):
        start_process(session, alice, "BUILD_SMITHY")
    with pytest.raises(ValueError, match="does not control"):
        start_process(session, alice, "BUILD_SMITHY", parcel_id=parcel.id)
    with pytest.raises(ValueError, match="unknown parcel"):
        start_process(session, alice, "BUILD_SMITHY", parcel_id="nope")
    # nothing was consumed by the refused attempts
    assert get_holding(session, alice.id, "TIMBER").quantity == Decimal("10")


# --- facility requirements (located production) ---------------------------

def smith_world(session):
    """Alice with an iron stock, a SMITHY on her lot, and a smelting recipe
    that must run at one."""
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "LOT", owner=alice)
    add_facility(session, parcel, "SMITHY")
    adjust_holding(session, alice, "IRON", Decimal("100"))
    create_recipe(session, "SMELT", inputs={"IRON": Decimal("2")},
                  outputs={"TOOL": Decimal("1")}, duration_ticks=2,
                  requires_facility="SMITHY")
    return alice, parcel


def test_facility_requirement_needs_a_facility_on_the_parcel(session):
    alice, parcel = smith_world(session)
    bare = create_parcel(session, "LOT", owner=alice)
    with pytest.raises(ValueError, match="no free SMITHY"):
        start_process(session, alice, "SMELT", parcel_id=bare.id)
    process = start_process(session, alice, "SMELT", parcel_id=parcel.id)
    assert process.parcel_id == parcel.id


def test_one_facility_backs_one_process_at_a_time(session):
    """Facilities reserve like machinery: each bound running process
    occupies one facility of the required type."""
    alice, parcel = smith_world(session)
    start_process(session, alice, "SMELT", parcel_id=parcel.id)
    assert reserved_facilities(session, parcel.id, "SMITHY") == 1
    with pytest.raises(ValueError, match="no free SMITHY"):
        start_process(session, alice, "SMELT", parcel_id=parcel.id)

    # a second smithy on the same parcel doubles capacity
    add_facility(session, parcel, "SMITHY")
    second = start_process(session, alice, "SMELT", parcel_id=parcel.id)
    with pytest.raises(ValueError, match="no free SMITHY"):
        start_process(session, alice, "SMELT", parcel_id=parcel.id)

    # cancellation releases the reservation — it is a query, not an escrow
    cancel_process(session, second.id, alice.id)
    start_process(session, alice, "SMELT", parcel_id=parcel.id)


def test_completion_frees_the_facility(session):
    alice, parcel = smith_world(session)
    start_process(session, alice, "SMELT", parcel_id=parcel.id)
    complete_processes(session, tick_number=3)
    assert reserved_facilities(session, parcel.id, "SMITHY") == 0
    start_process(session, alice, "SMELT", parcel_id=parcel.id)


# --- good requirements (machinery) ----------------------------------------

def bakery_world(session, ovens=1):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    adjust_holding(session, alice, "FLOUR", Decimal("100"))
    adjust_holding(session, alice, "OVEN", Decimal(ovens))
    create_recipe(session, "BAKE", inputs={"FLOUR": Decimal("1")},
                  outputs={"BREAD": Decimal("2")}, duration_ticks=2,
                  good_requirements={"OVEN": Decimal("1")})
    return alice


def test_good_requirement_checked_but_not_consumed(session):
    alice = bakery_world(session)
    start_process(session, alice, "BAKE")
    assert get_holding(session, alice.id, "OVEN").quantity == Decimal("1")  # still held
    assert get_holding(session, alice.id, "FLOUR").quantity == Decimal("99")  # consumed
    assert reserved_quantity(session, alice.id, "OVEN") == Decimal("1")


def test_missing_requirement_refuses_start(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    adjust_holding(session, alice, "FLOUR", Decimal("100"))
    create_recipe(session, "BAKE", inputs={"FLOUR": Decimal("1")},
                  outputs={"BREAD": Decimal("2")}, duration_ticks=2,
                  good_requirements={"OVEN": Decimal("1")})
    with pytest.raises(InsufficientHoldingsError, match="OVEN"):
        start_process(session, alice, "BAKE")
    assert get_holding(session, alice.id, "FLOUR").quantity == Decimal("100")


def test_one_oven_cannot_back_two_concurrent_bakes(session):
    alice = bakery_world(session, ovens=1)
    first = start_process(session, alice, "BAKE")
    with pytest.raises(InsufficientHoldingsError, match="OVEN"):
        start_process(session, alice, "BAKE")
    cancel_process(session, first.id, alice.id)
    start_process(session, alice, "BAKE")  # reservation released


def test_two_ovens_back_two_bakes(session):
    alice = bakery_world(session, ovens=2)
    start_process(session, alice, "BAKE")
    start_process(session, alice, "BAKE")
    assert reserved_quantity(session, alice.id, "OVEN") == Decimal("2")
    with pytest.raises(InsufficientHoldingsError):
        start_process(session, alice, "BAKE")


def test_inputs_cannot_consume_reserved_holdings(session):
    """The oven backing a bake cannot be melted down as another recipe's
    input while the bake runs."""
    alice = bakery_world(session, ovens=1)
    create_recipe(session, "SCRAP_OVEN", inputs={"OVEN": Decimal("1")},
                  outputs={"SCRAP": Decimal("5")}, duration_ticks=1)
    start_process(session, alice, "BAKE")
    with pytest.raises(InsufficientHoldingsError, match="unreserved"):
        start_process(session, alice, "SCRAP_OVEN")
    complete_processes(session, tick_number=3)
    start_process(session, alice, "SCRAP_OVEN")  # free after completion


def test_machinery_wear_pattern_still_works(session):
    """Wear is a fractional input alongside the requirement (design.md
    § recipes): each bake consumes 0.01 OVEN, and the 0.5-OVEN requirement
    both blocks a second concurrent bake and retires the oven once it has
    worn down past half."""
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    adjust_holding(session, alice, "FLOUR", Decimal("10"))
    adjust_holding(session, alice, "OVEN", Decimal("1"))
    create_recipe(session, "BAKE_WEAR",
                  inputs={"FLOUR": Decimal("1"), "OVEN": Decimal("0.01")},
                  outputs={"BREAD": Decimal("2")}, duration_ticks=2,
                  good_requirements={"OVEN": Decimal("0.5")})
    start_process(session, alice, "BAKE_WEAR")
    assert get_holding(session, alice.id, "OVEN").quantity == Decimal("0.99")
    with pytest.raises(InsufficientHoldingsError):
        start_process(session, alice, "BAKE_WEAR")  # 0.49 unreserved < 0.5
    complete_processes(session, tick_number=3)
    start_process(session, alice, "BAKE_WEAR")  # ~50 more bakes in the oven


# --- deposits (extraction) ------------------------------------------------

def mine_world(session, deposit=Decimal("10")):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    parcel = create_parcel(session, "CLAIM", owner=alice)
    add_deposit(session, parcel, "IRON", deposit)
    create_recipe(session, "MINE_IRON", inputs={}, outputs={"IRON": Decimal("3")},
                  duration_ticks=1, deposit_inputs={"IRON": Decimal("3")})
    return alice, parcel


def test_extraction_draws_down_the_deposit(session):
    alice, parcel = mine_world(session)
    start_process(session, alice, "MINE_IRON", parcel_id=parcel.id)
    assert get_deposit(session, parcel.id, "IRON").quantity == Decimal("7")
    complete_processes(session, tick_number=2)
    assert get_holding(session, alice.id, "IRON").quantity == Decimal("3")


def test_extraction_refuses_when_deposit_short_or_absent(session):
    alice, parcel = mine_world(session, deposit=Decimal("2"))
    with pytest.raises(InsufficientDepositError, match="IRON"):
        start_process(session, alice, "MINE_IRON", parcel_id=parcel.id)
    barren = create_parcel(session, "CLAIM", owner=alice)
    with pytest.raises(InsufficientDepositError):
        start_process(session, alice, "MINE_IRON", parcel_id=barren.id)


def test_deposit_validations(session):
    parcel = create_parcel(session, "CLAIM")
    with pytest.raises(ValueError, match="capacity"):
        add_deposit(session, parcel, "IRON", Decimal("5"),
                    regen_per_tick=Decimal("1"))  # regen needs a ceiling
    with pytest.raises(ValueError, match="capacity"):
        add_deposit(session, parcel, "IRON", Decimal("5"), capacity=Decimal("3"),
                    regen_per_tick=Decimal("1"))
    deposit = add_deposit(session, parcel, "timber", Decimal("5"),
                          capacity=Decimal("20"), regen_per_tick=Decimal("1"))
    assert deposit.symbol == "TIMBER"


def test_deposits_deplete_only_through_extraction(session):
    """draw_deposit is the single depletion path; there is no negative
    balance to exploit."""
    parcel = create_parcel(session, "CLAIM")
    add_deposit(session, parcel, "IRON", Decimal("1"))
    with pytest.raises(InsufficientDepositError):
        draw_deposit(session, parcel, "IRON", Decimal("2"))
    assert get_deposit(session, parcel.id, "IRON").quantity == Decimal("1")
