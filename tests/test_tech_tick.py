"""Tech tree through the tick engine: research processes emit unlocked
events, gating rejects intents, and scripts see ctx.unlocks."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.markets import adjust_holding
from econengine.models import Base, EntityType, Script, ScriptType, TechScope
from econengine.production import create_recipe
from econengine.services import create_entity
from econengine.tech import create_technology, entity_unlocks
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


def test_research_process_emits_unlocked_event(session):
    """A research project is just a recipe whose output is an unlock: start
    it by intent, wait out the duration, and the completion tick carries
    both the process_completed and the unlocked event."""
    create_technology(session, "SMITHING")
    create_recipe(session, "STUDY", {"LABOR": Decimal("3")}, {}, 1, unlocks=["SMITHING"])
    smith = create_entity(session, "Smith", EntityType.INDIVIDUAL)
    adjust_holding(session, smith, "LABOR", Decimal("3"))
    make_script(session, "study once",
                "if ctx.state.done == nil then "
                "  ctx.action.start_process('STUDY') "
                "  ctx.state.done = true "
                "else ctx.state.done = true end", smith)

    tick1 = run_tick(session)  # start during tick 1 -> completes tick 2
    assert any(e["type"] == "start_process" and e["status"] == "applied"
               for e in tick1.events)
    assert entity_unlocks(session, smith.id) == []

    tick2 = run_tick(session)
    completed = next(e for e in tick2.events if e["type"] == "process_completed")
    assert completed["unlocks"] == ["SMITHING"]
    unlocked = next(e for e in tick2.events if e["type"] == "unlocked")
    assert unlocked == {"type": "unlocked", "entity_id": smith.id,
                        "technology": "SMITHING", "scope": "entity"}
    assert entity_unlocks(session, smith.id) == ["SMITHING"]


def test_gated_intent_rejected_until_unlocked(session):
    """Recipe gating through the intent path: the same start_process intent
    is rejected before the unlock exists and applied after."""
    create_technology(session, "SMITHING")
    create_recipe(session, "STUDY", {}, {}, 0, unlocks=["SMITHING"])
    create_recipe(session, "FORGE", {"IRON": Decimal("1")}, {"SWORD": Decimal("1")}, 0,
                  requires=["SMITHING"])
    smith = create_entity(session, "Smith", EntityType.INDIVIDUAL)
    adjust_holding(session, smith, "IRON", Decimal("2"))
    make_script(session, "always forging", "ctx.action.start_process('FORGE')", smith)

    tick1 = run_tick(session)
    rejected = next(e for e in tick1.events if e["type"] == "start_process")
    assert rejected["status"] == "rejected"
    assert "requires SMITHING" in rejected["reason"]

    make_script(session, "study", "ctx.action.start_process('STUDY')", smith)
    tick2 = run_tick(session)
    # Intents resolve in collection order: FORGE is still rejected this tick
    # (the STUDY grant lands later the same tick), then applies next tick.
    tick3 = run_tick(session)
    applied = next(e for e in tick3.events if e["type"] == "start_process"
                   and e["params"]["recipe"] == "FORGE")
    assert applied["status"] == "applied"


def test_world_unlock_serves_every_entity(session):
    """One entity's discovery of a WORLD-scoped technology gates open the
    recipe for everyone — shared physics, per-Technology scope."""
    create_technology(session, "PHYSICS", scope=TechScope.WORLD)
    create_recipe(session, "STUDY", {}, {}, 0, unlocks=["PHYSICS"])
    create_recipe(session, "BUILD", {}, {"MACHINE": Decimal("1")}, 0, requires=["PHYSICS"])
    uni = create_entity(session, "University", EntityType.BUSINESS)
    factory = create_entity(session, "Factory", EntityType.BUSINESS)
    make_script(session, "research", "ctx.action.start_process('STUDY')", uni)

    run_tick(session)
    assert entity_unlocks(session, factory.id) == ["PHYSICS"]

    make_script(session, "build", "ctx.action.start_process('BUILD')", factory)
    tick2 = run_tick(session)
    applied = next(e for e in tick2.events if e["type"] == "start_process"
                   and e["entity_id"] == factory.id)
    assert applied["status"] == "applied"


def test_ctx_exposes_unlocks_and_query(session):
    """Scripts plan around the tech tree: ctx.unlocks lists what the entity
    can use (own + world), ctx.query.has_unlock asks about anyone."""
    create_technology(session, "FIRE", scope=TechScope.WORLD)
    create_technology(session, "SMITHING")
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    from econengine.tech import get_technology, grant_unlock
    grant_unlock(session, alice, get_technology(session, "FIRE"), tick_number=1)
    grant_unlock(session, alice, get_technology(session, "SMITHING"), tick_number=1)

    make_script(session, "introspect",
                "ctx.state.count = #ctx.unlocks "
                "ctx.state.first = ctx.unlocks[1] "
                f"ctx.state.alice_smith = ctx.query.has_unlock('{alice.id}', 'SMITHING') "
                "ctx.state.own_smith = ctx.query.has_unlock(ctx.entity.id, 'SMITHING') "
                "ctx.state.ghost = ctx.query.has_unlock(ctx.entity.id, 'WARP')", bob)
    script = session.query(Script).one()

    run_tick(session)
    session.refresh(script)
    assert script.state["count"] == 1  # world FIRE only; SMITHING is Alice's
    assert script.state["first"] == "FIRE"
    assert script.state["alice_smith"] is True
    assert script.state["own_smith"] is False
    assert script.state["ghost"] is False
