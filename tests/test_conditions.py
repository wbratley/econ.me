"""Conditions — effective-quantity overlay, estate rule, incapacity pass."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.conditions import (
    effective_factor, effective_quantity, get_estate_rule, held_modifiers,
    run_incapacity, set_estate_rule,
)
from econengine.goods import create_good
from econengine.markets import adjust_holding, create_market, get_holding, place_order
from econengine.models import Base, EntityStatus, EntityType, OrderStatus, ProcessStatus
from econengine.needs import create_need, run_consumption
from econengine.parcels import create_parcel
from econengine.production import create_recipe, start_process
from econengine.services import create_account, create_entity


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ---------------------------------------------------------------------------
# Declaring conditions (votable data on Good / Need rows)


def test_condition_good_validation(session):
    good = create_good(session, "cond-weak", modifies_pattern="labor-*",
                       modifies_factor=Decimal("0.5"), incapacitates_at=Decimal("50"))
    assert good.modifies_pattern == "LABOR-*"
    assert good.incapacitates_at == Decimal("50")

    with pytest.raises(ValueError):  # pattern and factor go together
        create_good(session, "BAD-1", modifies_pattern="LABOR-*")
    with pytest.raises(ValueError):
        create_good(session, "BAD-2", modifies_factor=Decimal("0.5"))
    with pytest.raises(ValueError):
        create_good(session, "BAD-3", modifies_pattern="X-*", modifies_factor=Decimal("-1"))
    with pytest.raises(ValueError):
        create_good(session, "BAD-4", incapacitates_at=Decimal("0"))


def test_need_condition_validation(session):
    need = create_need(session, "FOOD", Decimal("1"), ["BREAD"],
                       condition_symbol="cond-weak", condition_quantity=Decimal("2"))
    assert need.condition_symbol == "COND-WEAK"

    with pytest.raises(ValueError):  # symbol without quantity
        create_need(session, "B1", Decimal("1"), ["BREAD"], condition_symbol="COND-X")
    with pytest.raises(ValueError):  # quantity without symbol
        create_need(session, "B2", Decimal("1"), ["BREAD"], condition_quantity=Decimal("1"))


# ---------------------------------------------------------------------------
# Effective-quantity overlay


def test_effective_factor_matches_pattern_and_multiplies(session):
    create_good(session, "COND-WEAK", modifies_pattern="LABOR-*", modifies_factor=Decimal("0.5"))
    create_good(session, "COND-FEVER", modifies_pattern="LABOR-*", modifies_factor=Decimal("0.5"))
    create_good(session, "COND-BLIND", modifies_pattern="SKILL-*", modifies_factor=Decimal("0"))
    worker = create_entity(session, "Worker", EntityType.INDIVIDUAL)
    adjust_holding(session, worker, "COND-WEAK", Decimal("1"))
    adjust_holding(session, worker, "COND-FEVER", Decimal("0.1"))

    # both LABOR modifiers apply and multiply; SKILL modifier is not held
    assert effective_factor(session, worker.id, "LABOR-SMITH") == Decimal("0.25")
    assert effective_factor(session, worker.id, "SKILL-SMITH") == Decimal("1")
    assert effective_quantity(Decimal("10"), Decimal("0.25")) == Decimal("2.5")


def test_zero_quantity_condition_does_not_apply(session):
    create_good(session, "COND-WEAK", modifies_pattern="LABOR-*", modifies_factor=Decimal("0.5"))
    worker = create_entity(session, "Worker", EntityType.INDIVIDUAL)
    adjust_holding(session, worker, "COND-WEAK", Decimal("1"))
    adjust_holding(session, worker, "COND-WEAK", Decimal("-1"))  # recovered to zero

    assert held_modifiers(session, worker.id) == []
    assert effective_factor(session, worker.id, "LABOR-SMITH") == Decimal("1")


def test_requirement_check_reads_effective_quantity(session):
    """A fever halves what SKILL-SMITH counts for without drawing it down —
    one of the exactly two effective-quantity read sites."""
    create_good(session, "COND-FEVER", modifies_pattern="SKILL-*", modifies_factor=Decimal("0.5"))
    create_recipe(session, "SMITH", inputs={"IRON": Decimal("1")},
                  outputs={"SWORD": Decimal("1")}, duration_ticks=1,
                  good_requirements={"SKILL-SMITH": Decimal("1")})
    smith = create_entity(session, "Smith", EntityType.INDIVIDUAL)
    adjust_holding(session, smith, "IRON", Decimal("5"))
    adjust_holding(session, smith, "SKILL-SMITH", Decimal("1"))
    adjust_holding(session, smith, "COND-FEVER", Decimal("1"))

    with pytest.raises(ValueError):  # 1 x 0.5 effective < 1 required
        start_process(session, smith, "SMITH")
    adjust_holding(session, smith, "SKILL-SMITH", Decimal("1"))  # 2 x 0.5 = 1
    process = start_process(session, smith, "SMITH")
    assert process.status == ProcessStatus.RUNNING
    # the skill holding itself was never drawn down — the overlay is computed
    assert get_holding(session, smith.id, "SKILL-SMITH").quantity == Decimal("2")


# ---------------------------------------------------------------------------
# Accumulation (the cause mechanism in the consumption pass)


def test_unmet_need_credits_condition_scaled_by_shortfall(session):
    create_need(session, "FOOD", Decimal("4"), ["BREAD"],
                condition_symbol="COND-WEAK", condition_quantity=Decimal("2"))
    eater = create_entity(session, "Eater", EntityType.INDIVIDUAL)
    adjust_holding(session, eater, "BREAD", Decimal("1"))  # satisfaction 0.25

    events = run_consumption(session, tick_number=1)

    assert events[0]["type"] == "need_unmet"
    assert events[0]["condition"] == "COND-WEAK"
    assert events[0]["condition_granted"] == "1.5000"  # 2 x (1 - 0.25)
    assert get_holding(session, eater.id, "COND-WEAK").quantity == Decimal("1.5")

    # a met tick grants nothing more
    adjust_holding(session, eater, "BREAD", Decimal("4"))
    events = run_consumption(session, tick_number=2)
    assert events[0]["type"] == "need_satisfied"
    assert "condition" not in events[0]
    assert get_holding(session, eater.id, "COND-WEAK").quantity == Decimal("1.5")


# ---------------------------------------------------------------------------
# Estate rule (votable data in world_settings)


def test_estate_rule_defaults_to_burn_and_validates(session):
    assert get_estate_rule(session) == {"policy": "burn"}
    set_estate_rule(session, "heir")
    assert get_estate_rule(session)["policy"] == "heir"

    with pytest.raises(ValueError):
        set_estate_rule(session, "guillotine")
    with pytest.raises(ValueError):  # treasury needs an entity
        set_estate_rule(session, "treasury")
    with pytest.raises(ValueError):
        set_estate_rule(session, "treasury", treasury_entity_id="nope")

    treasury = create_entity(session, "Treasury", EntityType.GOVERNMENT)
    set_estate_rule(session, "treasury", treasury_entity_id=treasury.id)
    assert get_estate_rule(session)["treasury_entity_id"] == treasury.id


# ---------------------------------------------------------------------------
# Incapacity pass


def _sick_entity(session, quantity=Decimal("50")):
    create_good(session, "COND-SICK", incapacitates_at=Decimal("50"))
    entity = create_entity(session, "Sick", EntityType.INDIVIDUAL)
    adjust_holding(session, entity, "COND-SICK", quantity)
    return entity


def test_below_threshold_is_no_op(session):
    entity = _sick_entity(session, Decimal("49.9999"))
    assert run_incapacity(session, tick_number=1) == []
    assert entity.status == EntityStatus.ACTIVE


def test_incapacity_burn_default(session):
    entity = _sick_entity(session)
    adjust_holding(session, entity, "GOLD", Decimal("10"))
    create_account(session, entity, "USD", initial_balance=Decimal("100"))
    parcel = create_parcel(session, "FIELD", owner=entity)

    events = run_incapacity(session, tick_number=7)

    assert entity.status == EntityStatus.INCAPACITATED
    assert entity.incapacitated_tick == 7
    assert get_holding(session, entity.id, "GOLD").quantity == Decimal("0")
    assert entity.accounts[0].balance == Decimal("0")  # buried with the dead
    assert parcel.owner_id is None  # unclaimed
    assert events == [{
        "type": "entity_incapacitated", "entity_id": entity.id,
        "condition": "COND-SICK", "quantity": "50.0000", "threshold": "50.0000",
        "estate_policy": "burn", "recipient_id": None,
        "goods_transferred": "0.0000", "goods_burned": "60.0000",
        "money_transferred": "0.0000", "money_burned": "100.0000", "parcels": 1,
    }]


def test_incapacity_heir_inherits_all_but_conditions(session):
    set_estate_rule(session, "heir")
    entity = _sick_entity(session)
    heir = create_entity(session, "Heir", EntityType.INDIVIDUAL)
    entity.heir_id = heir.id
    adjust_holding(session, entity, "GOLD", Decimal("10"))
    create_account(session, entity, "USD", initial_balance=Decimal("100"))
    parcel = create_parcel(session, "FIELD", owner=entity)

    (event,) = run_incapacity(session, tick_number=1)

    assert event["estate_policy"] == "heir" and event["recipient_id"] == heir.id
    assert get_holding(session, heir.id, "GOLD").quantity == Decimal("10")
    assert get_holding(session, heir.id, "COND-SICK") is None  # sickness is not heritable
    assert heir.accounts[0].balance == Decimal("100")
    assert parcel.owner_id == heir.id
    assert event["goods_burned"] == "50.0000"  # the condition itself


def test_heir_rule_without_heir_falls_back_to_burn(session):
    set_estate_rule(session, "heir")
    entity = _sick_entity(session)
    adjust_holding(session, entity, "GOLD", Decimal("10"))

    (event,) = run_incapacity(session, tick_number=1)

    assert event["estate_policy"] == "burn"
    assert get_holding(session, entity.id, "GOLD").quantity == Decimal("0")


def test_incapacity_treasury(session):
    treasury = create_entity(session, "Treasury", EntityType.GOVERNMENT)
    set_estate_rule(session, "treasury", treasury_entity_id=treasury.id)
    entity = _sick_entity(session)
    adjust_holding(session, entity, "GOLD", Decimal("10"))

    (event,) = run_incapacity(session, tick_number=1)

    assert event["estate_policy"] == "treasury" and event["recipient_id"] == treasury.id
    assert get_holding(session, treasury.id, "GOLD").quantity == Decimal("10")


def test_incapacity_cancels_orders_and_processes(session):
    entity = _sick_entity(session)
    create_market(session, "GOLD", "USD")
    account = create_account(session, entity, "USD", initial_balance=Decimal("10"))
    adjust_holding(session, entity, "GOLD", Decimal("5"))
    order = place_order(session, entity.id, "GOLD", "sell", Decimal("1"), Decimal("2"), account.id)
    create_recipe(session, "DIG", inputs={}, outputs={"DIRT": Decimal("1")}, duration_ticks=5)
    process = start_process(session, entity, "DIG")

    run_incapacity(session, tick_number=1)

    assert order.status == OrderStatus.CANCELLED
    assert order.cancel_reason == "entity incapacitated"
    assert process.status == ProcessStatus.CANCELLED


def test_incapacitated_entity_cannot_act(session):
    entity = _sick_entity(session)
    create_market(session, "GOLD", "USD")
    account = create_account(session, entity, "USD", initial_balance=Decimal("10"))
    create_recipe(session, "DIG", inputs={}, outputs={"DIRT": Decimal("1")}, duration_ticks=1)
    run_incapacity(session, tick_number=1)

    with pytest.raises(ValueError, match="incapacitated"):
        start_process(session, entity, "DIG")
    with pytest.raises(ValueError, match="incapacitated"):
        place_order(session, entity.id, "GOLD", "buy", Decimal("1"), Decimal("1"), account.id)


def test_entity_crossing_two_thresholds_is_incapacitated_once(session):
    create_good(session, "COND-PLAGUE", incapacitates_at=Decimal("10"))
    entity = _sick_entity(session)  # also at COND-SICK threshold
    adjust_holding(session, entity, "COND-PLAGUE", Decimal("10"))

    events = run_incapacity(session, tick_number=1)

    assert len(events) == 1
    assert events[0]["condition"] == "COND-PLAGUE"  # symbol-ascending: first wins
