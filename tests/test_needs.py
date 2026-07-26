"""Needs — definitions and the consumption pass mechanics."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econ.markets import adjust_holding, get_holding
from econ.models import Base, EntityType, NeedState
from econ.needs import create_need, get_need, run_consumption
from econ.services import create_entity


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_create_need_normalizes_and_validates(session):
    need = create_need(session, "food", Decimal("2"), ["bread", "FISH", "bread"],
                       entity_type=EntityType.INDIVIDUAL, priority=1, name="Food")
    assert need.code == "FOOD"
    assert [s.symbol for s in need.satisfiers] == ["BREAD", "FISH"]  # upper, sorted, deduped
    assert need.quantity_per_tick == Decimal("2")
    assert get_need(session, "food") is need

    with pytest.raises(ValueError):
        create_need(session, "BAD", Decimal("0"), ["BREAD"])
    with pytest.raises(ValueError):
        create_need(session, "BAD", Decimal("1"), [])


def test_consumption_draws_satisfiers_symbol_ascending(session):
    create_need(session, "FOOD", Decimal("3"), ["FISH", "BREAD"])
    eater = create_entity(session, "Eater", EntityType.INDIVIDUAL)
    adjust_holding(session, eater, "BREAD", Decimal("2"))
    adjust_holding(session, eater, "FISH", Decimal("2"))

    events = run_consumption(session, tick_number=1)

    # BREAD before FISH regardless of the order given at creation.
    assert get_holding(session, eater.id, "BREAD").quantity == Decimal("0")
    assert get_holding(session, eater.id, "FISH").quantity == Decimal("1")
    assert events == [{
        "type": "need_satisfied", "entity_id": eater.id, "need": "FOOD",
        "consumed": "3.0000", "required": "3.0000", "satisfaction": "1.0000",
    }]


def test_partial_satisfaction_rounds_down(session):
    create_need(session, "FOOD", Decimal("3"), ["BREAD"])
    eater = create_entity(session, "Eater", EntityType.INDIVIDUAL)
    adjust_holding(session, eater, "BREAD", Decimal("1"))

    events = run_consumption(session, tick_number=1)

    # 1/3 rounds DOWN so 1.0000 is reachable only by full consumption.
    assert events[0]["type"] == "need_unmet"
    assert events[0]["satisfaction"] == "0.3333"
    state = session.execute(select(NeedState)).scalar_one()
    assert state.satisfaction == Decimal("0.3333")
    assert state.updated_tick == 1


def test_entity_type_filter_and_null_means_everyone(session):
    create_need(session, "FOOD", Decimal("1"), ["BREAD"], entity_type=EntityType.INDIVIDUAL)
    create_need(session, "POWER", Decimal("1"), ["COAL"])  # every entity
    person = create_entity(session, "Person", EntityType.INDIVIDUAL)
    firm = create_entity(session, "Firm", EntityType.BUSINESS)
    adjust_holding(session, firm, "BREAD", Decimal("5"))

    events = run_consumption(session, tick_number=1)

    # The firm's bread is untouched by an INDIVIDUAL-scoped need...
    assert get_holding(session, firm.id, "BREAD").quantity == Decimal("5")
    # ...but both entities are evaluated for the unscoped one.
    assert {(e["need"], e["entity_id"]) for e in events} == {
        ("FOOD", person.id), ("POWER", person.id), ("POWER", firm.id),
    }


def test_priority_orders_draw_on_shared_satisfier(session):
    # WHEAT satisfies both; FOOD (priority 0) must draw before BREWING (5).
    create_need(session, "BREWING", Decimal("2"), ["WHEAT"], priority=5)
    create_need(session, "FOOD", Decimal("2"), ["WHEAT"], priority=0)
    eater = create_entity(session, "Eater", EntityType.INDIVIDUAL)
    adjust_holding(session, eater, "WHEAT", Decimal("3"))

    events = run_consumption(session, tick_number=1)

    by_need = {e["need"]: e for e in events}
    assert by_need["FOOD"]["type"] == "need_satisfied"
    assert by_need["BREWING"] == {
        "type": "need_unmet", "entity_id": eater.id, "need": "BREWING",
        "consumed": "1.0000", "required": "2.0000", "satisfaction": "0.5000",
    }
    # Events come out in consumption order: essential first.
    assert [e["need"] for e in events] == ["FOOD", "BREWING"]


def test_state_is_rewritten_not_duplicated(session):
    need = create_need(session, "FOOD", Decimal("1"), ["BREAD"])
    eater = create_entity(session, "Eater", EntityType.INDIVIDUAL)
    adjust_holding(session, eater, "BREAD", Decimal("1"))

    run_consumption(session, tick_number=1)   # eats the bread: satisfied
    run_consumption(session, tick_number=2)   # nothing left: unmet

    states = session.execute(select(NeedState)).scalars().all()
    assert len(states) == 1
    assert states[0].need_id == need.id
    assert states[0].satisfaction == Decimal("0")
    assert states[0].updated_tick == 2


def test_inactive_needs_are_skipped(session):
    need = create_need(session, "FOOD", Decimal("1"), ["BREAD"])
    need.is_active = False
    eater = create_entity(session, "Eater", EntityType.INDIVIDUAL)
    adjust_holding(session, eater, "BREAD", Decimal("1"))

    assert run_consumption(session, tick_number=1) == []
    assert get_holding(session, eater.id, "BREAD").quantity == Decimal("1")
