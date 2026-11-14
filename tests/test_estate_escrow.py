"""Estate escrow — the succession cost model (the generational game).

The fourth estate policy: on incapacity the estate leaves the world
into a per-owner pot; an heir joining inside the window takes delivery,
decayed by the wait; expiry burns what nobody claimed. The decay is the
anti-rebirth tax — dying to reset one's vitals must cost more capital
than the reset is worth.
"""
import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.conditions import (
    get_escrow_config, run_escrow_expiry, run_incapacity,
    set_escrow_config, set_estate_rule,
)
from econengine.goods import create_good
from econengine.markets import adjust_holding, get_holding
from econengine.models import Base, EntityStatus, EntityType, EstateEscrow, Tick
from econengine.parcels import create_parcel
from econengine.places import create_place, move_entity
from econengine.services import create_account, create_entity, deliver_escrow


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _escrow_world(session, ticks=240, decay="0.02"):
    create_good(session, "COND-SICK", incapacitates_at=Decimal("50"))
    set_estate_rule(session, "escrow")
    set_escrow_config(session, ticks, decay)


def _dying_camp_owner(session, owner="u-dynasty", tick=1):
    entity = create_entity(session, "Old Hand", EntityType.INDIVIDUAL)
    entity.owner_id = owner
    adjust_holding(session, entity, "COND-SICK", Decimal("50"))   # the fever
    adjust_holding(session, entity, "WOOD", Decimal("4.4366"))    # the kit
    adjust_holding(session, entity, "AXE", Decimal("1"))
    create_account(session, entity, "COIN", initial_balance=Decimal("10"))
    parcel = create_parcel(session, "CAMP", owner=entity)
    place = create_place(session, "camp", "CAMP", "the camp")
    move_entity(session, entity, place)
    return entity, parcel, place


# ---------------------------------------------------------------------------
# The policy: death fills the pot, not the pyre


def test_escrow_config_validation(session):
    set_escrow_config(session, 240, "0.02") == get_escrow_config(session)
    cfg = get_escrow_config(session)
    assert cfg == {"ticks": 240, "decay": Decimal("0.02")}

    with pytest.raises(ValueError):
        set_escrow_config(session, 0, "0.02")       # window must be positive
    with pytest.raises(ValueError):
        set_escrow_config(session, 240, "1")        # decay in [0, 1)
    with pytest.raises(ValueError):
        set_escrow_config(session, 240, "-0.1")


def test_death_escrows_the_estate(session):
    _escrow_world(session)
    entity, parcel, place = _dying_camp_owner(session)

    (event,) = run_incapacity(session, tick_number=100)

    assert event["estate_policy"] == "escrow"
    assert event["goods_escrowed"] == "5.4366"      # WOOD + AXE, fever burned
    assert event["goods_burned"] == "50.0000"       # conditions never inherit
    assert event["money_escrowed"] == "10.0000"
    assert event["money_burned"] == "0.0000"
    assert event["parcels_escrowed"] == 1

    (row,) = session.query(EstateEscrow).all()
    assert row.owner_id == "u-dynasty"
    assert row.goods == {"WOOD": "4.4366", "AXE": "1.0000"}
    assert row.money == {"COIN": "10.0000"}
    assert row.parcels == [parcel.id]
    assert row.place_id == place.id
    assert (row.created_tick, row.expires_tick) == (100, 340)
    # the world side: everything left the body
    assert get_holding(session, entity.id, "WOOD").quantity == Decimal("0")
    assert entity.accounts[0].balance == Decimal("0")
    assert parcel.owner_id is None                  # the roof waits, unclaimed


def test_escrow_fails_closed_to_burn_without_config(session):
    create_good(session, "COND-SICK", incapacitates_at=Decimal("50"))
    set_estate_rule(session, "escrow")
    # no estate.escrow setting: the policy cannot hang the estate mid-air
    entity, _, _ = _dying_camp_owner(session)

    (event,) = run_incapacity(session, tick_number=1)

    assert event["estate_policy"] == "burn"
    assert event["goods_burned"] == "55.4366"
    assert session.query(EstateEscrow).count() == 0


def test_second_death_merges_into_the_open_pot(session):
    _escrow_world(session, ticks=240)
    first, _, _ = _dying_camp_owner(session, owner="u-dynasty")
    run_incapacity(session, tick_number=100)

    second = create_entity(session, "Second Hand", EntityType.INDIVIDUAL)
    second.owner_id = "u-dynasty"
    adjust_holding(session, second, "COND-SICK", Decimal("50"))
    adjust_holding(session, second, "WOOD", Decimal("2"))
    other_place = create_place(session, "newcamp", "CAMP", "the later camp")
    move_entity(session, second, other_place)
    run_incapacity(session, tick_number=150)

    rows = session.query(EstateEscrow).all()
    assert len(rows) == 1                           # one pot per dynasty
    row = rows[0]
    assert row.goods["WOOD"] == "6.4366"            # summed
    assert row.created_tick == 100                  # the pot ages from the first
    assert row.expires_tick == 390                  # the window extends
    assert row.place_id == other_place.id           # the camp moves


# ---------------------------------------------------------------------------
# Delivery: the heir walks into the camp and finds what's left


def test_delivery_decays_by_the_wait(session):
    _escrow_world(session, ticks=240, decay="0.02")
    entity, parcel, place = _dying_camp_owner(session)
    run_incapacity(session, tick_number=100)
    session.add(Tick(number=100, events=[]))
    session.add(Tick(number=148, events=[]))        # waited 48 ticks

    heir = create_entity(session, "Heir", EntityType.INDIVIDUAL)
    heir.owner_id = "u-dynasty"
    summary = deliver_escrow(session, heir)

    # Decimal("0.98") ** 48 = 0.37918...: the kit survives, taxed
    assert Decimal(summary["goods_delivered"]["WOOD"]) == Decimal("1.6822")
    assert Decimal(summary["money_delivered"]["COIN"]) == Decimal("3.7918")
    assert summary["from"] == "Old Hand"
    assert summary["waited_ticks"] == 48
    assert summary["parcels_reclaimed"] == 1
    assert summary["moved_to_camp"] is True

    assert get_holding(session, heir.id, "WOOD").quantity == Decimal("1.6822")
    assert heir.accounts[0].balance == Decimal("3.7918")
    assert parcel.owner_id == heir.id               # the roof reclaimed
    assert heir.location_place_id == place.id       # woke at the camp
    assert session.query(EstateEscrow).count() == 0  # the pot is spent

    assert deliver_escrow(session, heir) is None    # a second heir: empty hand


def test_delivery_without_wait_is_whole(session):
    _escrow_world(session, ticks=240, decay="0.02")
    entity, _, _ = _dying_camp_owner(session)
    run_incapacity(session, tick_number=100)
    session.add(Tick(number=100, events=[]))

    heir = create_entity(session, "Heir", EntityType.INDIVIDUAL)
    heir.owner_id = "u-dynasty"
    summary = deliver_escrow(session, heir)

    assert Decimal(summary["goods_delivered"]["AXE"]) == Decimal("1.0000")
    assert Decimal(summary["money_delivered"]["COIN"]) == Decimal("10.0000")
    assert get_holding(session, heir.id, "AXE").quantity == Decimal("1")


def test_cap_clip_burns_on_delivery(session):
    _escrow_world(session, ticks=240, decay="0")
    create_good(session, "WOOD", max_holding=Decimal("2"))
    entity, _, _ = _dying_camp_owner(session)
    # trim the estate to the cap-able case: the dying hand holds 1 WOOD
    # (accrual clipped at 2, so hold under it), the heir already holds 1.5
    adjust_holding(session, entity, "WOOD", Decimal("-1.0000"))
    run_incapacity(session, tick_number=1)

    heir = create_entity(session, "Heir", EntityType.INDIVIDUAL)
    heir.owner_id = "u-dynasty"
    adjust_holding(session, heir, "WOOD", Decimal("1.5"))
    summary = deliver_escrow(session, heir)

    # 1.5 held + 1.0000 escrowed clips at the 2 cap: 0.5 lands, 0.5 burns
    assert Decimal(summary["goods_delivered"]["WOOD"]) == Decimal("0.5000")
    assert get_holding(session, heir.id, "WOOD").quantity == Decimal("2")
    assert Decimal(summary["goods_burned"]) == Decimal("0.5000")  # clipped burns


def test_parcel_claim_race_is_a_loss(session):
    _escrow_world(session, ticks=240, decay="0")
    entity, parcel, _ = _dying_camp_owner(session)
    run_incapacity(session, tick_number=1)
    squatter = create_entity(session, "Squatter", EntityType.INDIVIDUAL)
    parcel.owner_id = squatter.id                   # claimed in the window

    heir = create_entity(session, "Heir", EntityType.INDIVIDUAL)
    heir.owner_id = "u-dynasty"
    summary = deliver_escrow(session, heir)

    assert summary["parcels_reclaimed"] == 0
    assert summary["parcels_lost"] == 1
    assert parcel.owner_id == squatter.id


def test_no_escrow_is_none(session):
    heir = create_entity(session, "Heir", EntityType.INDIVIDUAL)
    heir.owner_id = "u-nobody"
    assert deliver_escrow(session, heir) is None


# ---------------------------------------------------------------------------
# Expiry: the dead hand lets go


def test_expiry_burns_the_pot(session):
    _escrow_world(session, ticks=240)
    entity, _, _ = _dying_camp_owner(session)
    run_incapacity(session, tick_number=100)        # expires 340

    assert run_escrow_expiry(session, tick_number=340) == []   # claimable through
    (event,) = run_escrow_expiry(session, tick_number=341)

    assert event["type"] == "estate_expired"
    assert event["owner_id"] == "u-dynasty"
    assert event["goods"] == "5.4366"
    assert event["money"] == "10.0000"
    assert session.query(EstateEscrow).count() == 0

    # an heir joining after expiry finds nothing
    heir = create_entity(session, "Late Heir", EntityType.INDIVIDUAL)
    heir.owner_id = "u-dynasty"
    assert deliver_escrow(session, heir) is None


def test_ownerless_death_burns_the_world_has_no_dynasty_to_wait_for(session):
    """Wolves, boars, test bodies -- entities without an owner have no
    lineage to hold a pot for: the escrow policy does not apply."""
    _escrow_world(session)                 # installs COND-SICK
    entity = create_entity(session, "Wolf", EntityType.INDIVIDUAL)  # no owner
    adjust_holding(session, entity, "COND-SICK", Decimal("50"))
    adjust_holding(session, entity, "WOOD", Decimal("3"))

    (event,) = run_incapacity(session, tick_number=1)

    assert event["estate_policy"] == "burn"
    assert session.query(EstateEscrow).count() == 0
