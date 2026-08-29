"""Threats — the demand side of the night (run 20: wolves).

Pressure arithmetic only: what accrues in the dark, what noise adds,
what a lit hearth quarters, and that dawn never grants. The teeth
(incapacitation at the threshold, fighting back by consuming the
condition) are the conditions and production systems, tested where
they live.
"""
import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import goods as goods_mod, markets, threats
from econengine.models import Base, EntityType
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _wolf_world(session):
    goods_mod.create_good(session, "WARMTH", decay_per_tick=Decimal("0"))
    goods_mod.create_good(session, "WOLF", decay_per_tick=Decimal("0.2"),
                          incapacitates_at=Decimal("6"))
    threats.create_threat(
        session, "WOLF", condition_symbol="WOLF",
        entity_type=EntityType.INDIVIDUAL,
        ambient_night_per_tick=Decimal("1.5"),
        per_say_night=Decimal("0.5"),
        deterred_by_symbol="WARMTH", deterred_by_quantity=Decimal("1"),
        deterrence_factor=Decimal("0.25"),
    )
    return markets.create_market(session, "WOLF", "COIN")


def _hold(session, entity_id, symbol):
    return markets.get_holding(session, entity_id, symbol).quantity


def _pressures(events):
    return [e for e in events if e.get("type") == "threat_pressure"]


def test_threat_presses_at_night_not_by_day(session):
    """The pack works the dark hours: fireless pressure lands every
    night tick and never by day."""
    _wolf_world(session)
    from econengine.services import create_entity
    house = create_entity(session, "House", EntityType.INDIVIDUAL)
    session.commit()
    run_tick(session); session.commit()          # tick 1 = hour 0, night
    run_tick(session); session.commit()          # tick 2 = hour 1, night
    # +1.5 then −20% of the stock each hour: 1.2, then 2.7−0.54 = 2.16
    assert _hold(session, house.id, "WOLF") == Decimal("2.16")
    # ticks 25..30 = hours 0..5 of day 2 -- keep pressing through the
    # night, then daylight (tick 30 = hour 5 is still night; 31 = 06:00)
    for _ in range(28):
        run_tick(session); session.commit()
    wolf_night_end = _hold(session, house.id, "WOLF")
    assert wolf_night_end > 0
    for _ in range(6):                            # daylight hours 6..11
        run_tick(session); session.commit()
    assert _hold(session, house.id, "WOLF") < wolf_night_end  # dawn scatters


def test_noise_carries_and_the_hearth_quarters(session):
    """Per-say pressure lands on the speaker, and a lit hearth (WARMTH
    held) quarters the whole rate -- shy, not deaf."""
    _wolf_world(session)
    from econengine.services import create_entity
    loud = create_entity(session, "Loud", EntityType.INDIVIDUAL)
    loud_warm = create_entity(session, "LoudWarm", EntityType.INDIVIDUAL)
    markets.adjust_holding(session, loud_warm, "WARMTH", Decimal("5"))
    session.commit()
    # one night tick with two delivered says from each speaker
    events = [
        {"type": "say", "entity_id": loud.id},
        {"type": "say", "entity_id": loud.id},
        {"type": "say", "entity_id": loud_warm.id},
        {"type": "say", "entity_id": loud_warm.id},
        {"type": "say", "entity_id": None},        # not a speaker
        {"type": "say", "entity_id": loud.id, "status": "rejected"},  # never heard
    ]
    out = threats.apply_pressure(session, 1, events)
    by_entity = {e["entity_id"]: e for e in _pressures(out)}
    assert Decimal(by_entity[loud.id]["pressure"]) == Decimal("2.5")   # 1.5 + 2*0.5
    assert by_entity[loud.id]["says"] == 2
    assert Decimal(by_entity[loud_warm.id]["pressure"]) == Decimal("0.625")  # (1.5+1.0)*0.25
    assert by_entity[loud_warm.id]["deterred"] is True
    assert by_entity[loud.id]["deterred"] is False


def test_threat_skips_business_and_day(session):
    """Entity scoping holds (the post may talk all night) and the pass
    is a no-op outside dark hours."""
    _wolf_world(session)
    from econengine.services import create_entity
    post = create_entity(session, "Post", EntityType.BUSINESS)
    session.commit()
    out = threats.apply_pressure(session, 1, [
        {"type": "say", "entity_id": post.id}])
    assert _pressures(out) == []
    # tick 10 = hour 10, daylight: nothing presses anyone
    from econengine.services import create_entity
    walker = create_entity(session, "Walker", EntityType.INDIVIDUAL)
    session.commit()
    assert threats.apply_pressure(session, 10, []) == []


def test_create_threat_validation(session):
    """Install guards: keys are exclusive, discounts must mean it."""
    goods_mod.create_good(session, "WOLF")
    with pytest.raises(ValueError, match="claim"):
        _wolf_world(session)                      # duplicate key
    with pytest.raises(ValueError, match="positive"):
        threats.create_threat(session, "BEAR", "WOLF",
                              ambient_night_per_tick=Decimal("0"))
    with pytest.raises(ValueError, match="mean anything"):
        threats.create_threat(session, "BEAR", "WOLF",
                              ambient_night_per_tick=Decimal("1"),
                              deterred_by_symbol="WARMTH",
                              deterred_by_quantity=Decimal("1"),
                              deterrence_factor=Decimal("1"))
