"""Conditions through the tick engine: accumulation under starvation, the
auto-issue throttle reaching the labor market one tick later, natural
recovery racing the incapacity threshold, and the estate at the end."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.goods import create_good
from econengine.markets import adjust_holding, get_holding
from econengine.models import Base, EntityStatus, EntityType, Script, ScriptType
from econengine.needs import create_need
from econengine.production import create_recipe, start_process
from econengine.services import create_entity
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_starvation_throttles_next_ticks_labor(session):
    """The death-spiral wiring: an unmet FOOD tick grants COND-WEAK, and the
    NEXT tick's auto-issued labor is halved — auto-issue runs at the top of
    the tick, so tick N's labor is throttled by tick N-1's conditions."""
    create_need(session, "FOOD", Decimal("1"), ["BREAD"],
                condition_symbol="COND-WEAK", condition_quantity=Decimal("1"))
    create_good(session, "COND-WEAK", modifies_pattern="LABOR-*", modifies_factor=Decimal("0.5"))
    create_good(session, "LABOR-PLAIN", auto_issue_quantity=Decimal("10"),
                decay_per_tick=Decimal("1"))  # labor perishes each tick
    worker = create_entity(session, "Worker", EntityType.INDIVIDUAL)

    tick1 = run_tick(session)  # full 10 issued, then FOOD unmet -> +1 COND-WEAK
    issued1 = next(e for e in tick1.events if e["type"] == "auto_issue")
    assert issued1["issued"] == "10.0000"
    assert get_holding(session, worker.id, "COND-WEAK").quantity == Decimal("1")

    tick2 = run_tick(session)  # throttled: 10 x 0.5
    issued2 = next(e for e in tick2.events if e["type"] == "auto_issue")
    assert issued2["issued"] == "5.0000"


def test_condition_decays_toward_equilibrium_below_threshold(session):
    """Proportional decay against a constant grant converges to grant/decay;
    a threshold above that equilibrium never fires (tuning caution in the
    design). Grant 1, decay 0.5 -> equilibrium 2, threshold 3 stays safe."""
    create_need(session, "FOOD", Decimal("1"), ["BREAD"],
                condition_symbol="COND-WEAK", condition_quantity=Decimal("1"))
    create_good(session, "COND-WEAK", decay_per_tick=Decimal("0.5"),
                incapacitates_at=Decimal("3"))
    worker = create_entity(session, "Worker", EntityType.INDIVIDUAL)

    for _ in range(30):
        run_tick(session)

    assert worker.status == EntityStatus.ACTIVE
    assert get_holding(session, worker.id, "COND-WEAK").quantity <= Decimal("2")


def test_recovery_this_tick_counts_before_the_threshold(session):
    """The incapacity pass runs AFTER decay: a holding at the threshold that
    decays below it this tick does not incapacitate."""
    create_good(session, "COND-SICK", decay_per_tick=Decimal("0.5"),
                incapacitates_at=Decimal("10"))
    patient = create_entity(session, "Patient", EntityType.INDIVIDUAL)
    adjust_holding(session, patient, "COND-SICK", Decimal("10"))

    run_tick(session)

    assert patient.status == EntityStatus.ACTIVE
    assert get_holding(session, patient.id, "COND-SICK").quantity == Decimal("5")


def test_starvation_to_incapacity_and_the_world_moves_on(session):
    """Unchecked accumulation crosses the threshold; the entity is
    deactivated and every subsequent pass ignores it — no labor, no
    consumption, no script."""
    create_need(session, "FOOD", Decimal("1"), ["BREAD"],
                condition_symbol="COND-SICK", condition_quantity=Decimal("10"))
    create_good(session, "COND-SICK", incapacitates_at=Decimal("25"))
    create_good(session, "LABOR-PLAIN", auto_issue_quantity=Decimal("10"),
                auto_issue_entity_type=EntityType.INDIVIDUAL)
    starving = create_entity(session, "Starving", EntityType.INDIVIDUAL)
    script = Script(name="noop", source="ctx.state.ping = 1", script_type=ScriptType.BEHAVIOUR,
                    entity_id=starving.id)
    session.add(script)
    session.flush()

    run_tick(session)  # +10
    run_tick(session)  # +10
    tick3 = run_tick(session)  # +10 -> 30 >= 25: incapacitated

    dead = next(e for e in tick3.events if e["type"] == "entity_incapacitated")
    assert dead["entity_id"] == starving.id and dead["condition"] == "COND-SICK"
    assert starving.status == EntityStatus.INCAPACITATED
    assert starving.incapacitated_tick == 3

    tick4 = run_tick(session)
    assert all(e.get("entity_id") != starving.id for e in tick4.events)
    assert get_holding(session, starving.id, "LABOR-PLAIN").quantity == Decimal("0")  # estate burned, none reissued


def test_healing_is_a_recipe_consuming_the_condition(session):
    """Healing as an industry: a recipe consuming COND-SICK + MEDICINE —
    inputs are consumed atomically at start, so medicine has a market price
    and doctors emerge. No new mechanism."""
    create_good(session, "COND-SICK", incapacitates_at=Decimal("25"))
    create_recipe(session, "TREAT", inputs={"COND-SICK": Decimal("20"), "MEDICINE": Decimal("1")},
                  outputs={"RELIEF": Decimal("1")}, duration_ticks=0)
    patient = create_entity(session, "Patient", EntityType.INDIVIDUAL)
    adjust_holding(session, patient, "COND-SICK", Decimal("24"))
    adjust_holding(session, patient, "MEDICINE", Decimal("1"))

    start_process(session, patient, "TREAT")

    assert get_holding(session, patient.id, "COND-SICK").quantity == Decimal("4")
    run_tick(session)
    assert patient.status == EntityStatus.ACTIVE
