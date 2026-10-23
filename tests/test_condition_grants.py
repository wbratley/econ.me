"""Condition grants — the festering register (stone-age-expansion Bundle A).

A condition breeding another, the deficit->condition precedent one tier
up: a Need row grants a condition on shortfall, and the world_settings
register grants a SECOND condition from a held first one past a severity
floor (an unbound WOUND breeds INFECTION; a riding EXPOSURE breeds
FROSTBITE). Declared per world, votable, engine-run — scripts never
touch holdings, so the festering is mechanism, not policy.
"""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import tick as tick_mod
from econengine.conditions import (
    get_condition_grants, run_condition_grants, set_condition_grants,
)
from econengine.goods import create_good
from econengine.markets import adjust_holding
from econengine.services import create_entity
from econengine.models import Base, EntityStatus, EntityType


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _world(session):
    """Two conditions wired as a festering pair: WOUND -> INFECTION."""
    create_good(session, "WOUND", incapacitates_at=Decimal("12"),
                decay_per_tick=Decimal("0.02"))
    create_good(session, "INFECTION", incapacitates_at=Decimal("8"),
                decay_per_tick=Decimal("0.01"))
    return create_entity(session, "House", EntityType.INDIVIDUAL)


# ---------------------------------------------------------------------------
# Declaring the register (votable data, validated fail-closed)


def test_register_round_trip_and_normalization(session):
    _world(session)
    set_condition_grants(session, {
        "WOUND": {"grants": "INFECTION", "at": 0.5, "quantity": 0.05}})
    got = get_condition_grants(session)
    # quantities are stored as strings -- exact decimals, the JSON rule
    assert got["WOUND"] == {"grants": "INFECTION", "at": "0.5",
                            "quantity": "0.05"}


def test_empty_and_absent_register_is_noop(session):
    _world(session)
    assert get_condition_grants(session) == {}
    assert run_condition_grants(session, 1) == []


def test_register_validation(session):
    _world(session)
    create_good(session, "BREAD")  # not a condition
    with pytest.raises(ValueError):  # unknown source good
        set_condition_grants(session, {"NOPE": {
            "grants": "INFECTION", "at": 1, "quantity": 1}})
    with pytest.raises(ValueError):  # unknown target good
        set_condition_grants(session, {"WOUND": {
            "grants": "NOPE", "at": 1, "quantity": 1}})
    with pytest.raises(ValueError):  # a plain good is not a condition
        set_condition_grants(session, {"BREAD": {
            "grants": "INFECTION", "at": 1, "quantity": 1}})
    with pytest.raises(ValueError):  # ...on either side
        set_condition_grants(session, {"WOUND": {
            "grants": "BREAD", "at": 1, "quantity": 1}})
    with pytest.raises(ValueError):  # never itself
        set_condition_grants(session, {"WOUND": {
            "grants": "WOUND", "at": 1, "quantity": 1}})
    with pytest.raises(ValueError):  # floor must be positive
        set_condition_grants(session, {"WOUND": {
            "grants": "INFECTION", "at": 0, "quantity": 1}})
    with pytest.raises(ValueError):  # drip must be positive
        set_condition_grants(session, {"WOUND": {
            "grants": "INFECTION", "at": 1, "quantity": 0}})
    with pytest.raises(ValueError):  # exactly the three keys
        set_condition_grants(session, {"WOUND": {
            "grants": "INFECTION", "at": 1, "quantity": 1, "extra": 2}})
    # nothing leaked into the register from the failures
    assert get_condition_grants(session) == {}


# ---------------------------------------------------------------------------
# The pass: a held condition past its floor breeds the target


def test_grant_fires_at_or_past_the_floor_only(session):
    house = _world(session)
    set_condition_grants(session, {
        "WOUND": {"grants": "INFECTION", "at": 0.5, "quantity": 0.05}})

    adjust_holding(session, house, "WOUND", Decimal("0.4"))
    assert run_condition_grants(session, 1) == []          # below the floor

    adjust_holding(session, house, "WOUND", Decimal("0.1"))  # 0.5 exactly
    events = run_condition_grants(session, 2)
    assert len(events) == 1
    assert events[0]["type"] == "condition_granted"
    assert events[0]["source"] == "WOUND"
    assert events[0]["condition"] == "INFECTION"
    assert events[0]["granted"] == "0.0500"
    assert events[0]["held"] == "0.5000"
    assert get_holding_qty(session, house.id, "INFECTION") == Decimal("0.05")


def test_grant_drips_each_pass_and_reports_the_source_hold(session):
    house = _world(session)
    set_condition_grants(session, {
        "WOUND": {"grants": "INFECTION", "at": 0.5, "quantity": 0.05}})
    adjust_holding(session, house, "WOUND", Decimal("1"))
    for _ in range(3):
        events = run_condition_grants(session, 1)
        assert len(events) == 1
    assert get_holding_qty(session, house.id, "INFECTION") == Decimal("0.15")


def test_multiple_rows_and_entities_order_deterministically(session):
    create_good(session, "WOUND", incapacitates_at=Decimal("12"))
    create_good(session, "INFECTION", incapacitates_at=Decimal("8"))
    create_good(session, "EXPOSURE", incapacitates_at=Decimal("18"))
    create_good(session, "FROSTBITE", incapacitates_at=Decimal("10"))
    set_condition_grants(session, {
        "WOUND": {"grants": "INFECTION", "at": 0.5, "quantity": 0.05},
        "EXPOSURE": {"grants": "FROSTBITE", "at": 9, "quantity": 0.25}})
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    b = create_entity(session, "B", EntityType.INDIVIDUAL)
    # ids are uuids: force an ordering and assert against the sorted law
    for e in (a, b):
        adjust_holding(session, e, "WOUND", Decimal("1"))
        adjust_holding(session, e, "EXPOSURE", Decimal("9.5"))
    events = run_condition_grants(session, 1)
    assert len(events) == 4  # two entities x two rows
    keys = [(e["entity_id"], e["source"]) for e in events]
    assert keys == sorted(keys, key=lambda k: (k[0], k[1]))


def test_inactive_entities_do_not_fester(session):
    house = _world(session)
    set_condition_grants(session, {
        "WOUND": {"grants": "INFECTION", "at": 0.5, "quantity": 0.05}})
    adjust_holding(session, house, "WOUND", Decimal("1"))
    house.status = EntityStatus.INCAPACITATED
    assert run_condition_grants(session, 1) == []


def test_max_holding_clips_the_grant(session):
    create_good(session, "WOUND", incapacitates_at=Decimal("12"))
    create_good(session, "INFECTION", incapacitates_at=Decimal("8"),
                max_holding=Decimal("1"))
    house = create_entity(session, "House", EntityType.INDIVIDUAL)
    set_condition_grants(session, {
        "WOUND": {"grants": "INFECTION", "at": 0.5, "quantity": 2}})
    adjust_holding(session, house, "WOUND", Decimal("1"))
    events = run_condition_grants(session, 1)
    assert get_holding_qty(session, house.id, "INFECTION") == Decimal("1")
    assert events[0]["granted"] == "1.0000"  # the credited truth


# ---------------------------------------------------------------------------
# The tick wires the pass: need grants first, the drip fades same tick


def test_run_tick_wires_the_fester_after_needs_before_decay(session):
    house = _world(session)
    set_condition_grants(session, {
        "WOUND": {"grants": "INFECTION", "at": 0.5, "quantity": 0.05}})
    adjust_holding(session, house, "WOUND", Decimal("1"))
    tick = tick_mod.run_tick(session)
    kinds = [e["type"] for e in tick.events]
    assert "condition_granted" in kinds
    fest = next(e for e in tick.events if e["type"] == "condition_granted")
    assert fest["source"] == "WOUND" and fest["condition"] == "INFECTION"
    # the decay pass faded the fresh drip in the tick it landed: the
    # wound's own 0.02 decay, the infection's 0.01 against a 0.05 drip
    assert get_holding_qty(session, house.id, "INFECTION") == Decimal("0.0495")  # proportional: 0.05 x (1 - 0.01)


def get_holding_qty(session, entity_id, symbol):
    from econengine.markets import get_holding
    got = get_holding(session, entity_id, symbol)
    return got.quantity if got is not None else Decimal("0")
