"""Tech tree — technologies, prerequisite enforcement, unlock scope, and
recipe gating / research through the production layer."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econ.markets import adjust_holding, get_holding
from econ.models import Base, EntityType, TechScope, Unlock
from econ.production import create_recipe, start_process
from econ.services import create_entity
from econ.tech import (
    create_technology, entity_unlocks, get_technology, grant_unlock, has_unlock,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ---------------------------------------------------------------------------
# Technologies and the DAG
# ---------------------------------------------------------------------------

def test_create_technology_normalizes_and_validates(session):
    base = create_technology(session, "fire", name="Fire")
    assert base.code == "FIRE"
    assert base.scope == TechScope.ENTITY  # per-entity is the default
    assert get_technology(session, "fire") is base

    smelting = create_technology(session, "SMELTING", prerequisites=["fire", "FIRE"])
    assert [p.prerequisite.code for p in smelting.prerequisites] == ["FIRE"]  # deduped

    # Prerequisites must already exist as rows — the DAG is acyclic by
    # construction: no technology can ever reference itself or a later one.
    with pytest.raises(ValueError):
        create_technology(session, "BAD", prerequisites=["WHEELS"])
    with pytest.raises(ValueError):
        create_technology(session, "SELFREF", prerequisites=["SELFREF"])


def test_entity_scope_unlocks_one_entity(session):
    fire = create_technology(session, "FIRE")
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)

    unlock = grant_unlock(session, alice, fire, tick_number=1)
    assert unlock.entity_id == alice.id and unlock.unlocked_tick == 1
    assert has_unlock(session, alice.id, fire)
    assert not has_unlock(session, bob.id, fire)
    assert entity_unlocks(session, alice.id) == ["FIRE"]
    assert entity_unlocks(session, bob.id) == []

    # Idempotent: a second grant creates nothing.
    assert grant_unlock(session, alice, fire, tick_number=2) is None
    assert len(session.execute(select(Unlock)).scalars().all()) == 1


def test_world_scope_unlocks_everyone(session):
    physics = create_technology(session, "PHYSICS", scope=TechScope.WORLD)
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)

    unlock = grant_unlock(session, alice, physics, tick_number=1)
    assert unlock.entity_id is None  # held by the world, not the discoverer
    assert has_unlock(session, bob.id, physics)
    assert entity_unlocks(session, bob.id) == ["PHYSICS"]

    # Bob "rediscovering" it grants nothing — the world already holds it.
    assert grant_unlock(session, bob, physics, tick_number=2) is None


def test_grants_enforce_prerequisites(session):
    create_technology(session, "FIRE")
    smelting = create_technology(session, "SMELTING", prerequisites=["FIRE"])
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)

    # The DAG binds admin grants exactly as it binds research.
    with pytest.raises(ValueError, match="requires FIRE"):
        grant_unlock(session, alice, smelting, tick_number=1)

    grant_unlock(session, alice, get_technology(session, "FIRE"), tick_number=1)
    assert grant_unlock(session, alice, smelting, tick_number=1) is not None


def test_world_scoped_prerequisite_satisfies_everyone(session):
    create_technology(session, "FIRE", scope=TechScope.WORLD)
    smelting = create_technology(session, "SMELTING", prerequisites=["FIRE"])
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)

    grant_unlock(session, alice, get_technology(session, "FIRE"), tick_number=1)
    # Bob never researched FIRE, but the world holds it.
    assert grant_unlock(session, bob, smelting, tick_number=2) is not None


# ---------------------------------------------------------------------------
# Recipe gating and research recipes
# ---------------------------------------------------------------------------

def test_recipe_validates_technology_codes(session):
    with pytest.raises(ValueError, match="no technology"):
        create_recipe(session, "FORGE", {}, {"SWORD": Decimal("1")}, 1,
                      requires=["SMITHING"])
    with pytest.raises(ValueError, match="output, branch, or unlock"):
        create_recipe(session, "NOTHING", {"ORE": Decimal("1")}, {}, 1)

    create_technology(session, "SMITHING")
    recipe = create_recipe(session, "RESEARCH_SMITHING", {"LABOR": Decimal("5")}, {}, 3,
                           unlocks=["SMITHING"])  # no goods output: pure research
    assert [u.technology.code for u in recipe.unlocks] == ["SMITHING"]


def test_start_process_gated_on_requirements(session):
    smithing = create_technology(session, "SMITHING")
    create_recipe(session, "FORGE", {"IRON": Decimal("1")}, {"SWORD": Decimal("1")}, 1,
                  requires=["SMITHING"])
    smith = create_entity(session, "Smith", EntityType.INDIVIDUAL)
    adjust_holding(session, smith, "IRON", Decimal("5"))

    with pytest.raises(ValueError, match="requires SMITHING"):
        start_process(session, smith, "FORGE")
    # Nothing was consumed by the refused start.
    assert get_holding(session, smith.id, "IRON").quantity == Decimal("5")

    grant_unlock(session, smith, smithing, tick_number=1)
    process = start_process(session, smith, "FORGE")
    assert get_holding(session, smith.id, "IRON").quantity == Decimal("4")
    assert process.status.value == "running"


def test_research_recipe_grants_unlock_on_completion(session):
    create_technology(session, "SMITHING")
    create_recipe(session, "STUDY", {"LABOR": Decimal("2")}, {}, 0, unlocks=["SMITHING"])
    smith = create_entity(session, "Smith", EntityType.INDIVIDUAL)
    adjust_holding(session, smith, "LABOR", Decimal("4"))

    start_process(session, smith, "STUDY")  # duration 0: completes at start
    assert entity_unlocks(session, smith.id) == ["SMITHING"]

    # Researching again wastes the inputs but grants nothing new.
    start_process(session, smith, "STUDY")
    assert len(session.execute(select(Unlock)).scalars().all()) == 1


def test_research_start_checks_the_granted_techs_prerequisites(session):
    create_technology(session, "FIRE")
    create_technology(session, "SMELTING", prerequisites=["FIRE"])
    create_recipe(session, "STUDY_SMELTING", {}, {}, 2, unlocks=["SMELTING"])
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)

    # Checked at start, not completion: unlocks are never revoked, so what
    # is satisfiable at start stays satisfiable.
    with pytest.raises(ValueError, match="SMELTING requires FIRE"):
        start_process(session, alice, "STUDY_SMELTING")

    grant_unlock(session, alice, get_technology(session, "FIRE"), tick_number=1)
    assert start_process(session, alice, "STUDY_SMELTING").status.value == "running"
