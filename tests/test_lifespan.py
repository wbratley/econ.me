"""Death-by-old-age — the invariant mortality floor of Step 6d
(``docs/actors.md``). A per-entity ``lifespan`` (nullable; NULL = immortal)
that the end-of-tick incapacity pass checks against derived age
(``age = tick - birth_tick``). At ``age >= lifespan`` the engine
deactivates the entity and applies the estate rule — firing the SAME
``entity_incapacitated`` event a starvation death fires, with
``condition: "age"``. An old-age death is, to the engine, indistinguishable
from a condition death: same event, same estate, same insurance trigger.
Only the cause label is new.

Three levers adjust mortality (see the design's "How mortality actually
adjusts" table); this floor is the deliberately non-votable one:
  - conditions (shipped)  — why/whether someone dies this tick;
  - the spawn POLICY      — what lifespan FUTURE births get (votable);
  - the stamped lifespan  — a LIVING entity's death date (immutable, not
                            votable per tick — the point of layer 2).
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities
from econengine.conditions import run_incapacity, set_estate_rule
from econengine.goods import create_good
from econengine.lua_engine import Intent
from econengine.markets import adjust_holding, get_holding
from econengine.models import Base, EntityType, EntityStatus
from econengine.scripting import build_queries, resolve_intent
from econengine.services import create_account, create_entity, spawn_entity
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _aged(session, name, *, birth_tick, lifespan):
    """An INDIVIDUAL with explicit birth_tick + lifespan so age is exact."""
    e = create_entity(session, name, EntityType.INDIVIDUAL)
    e.birth_tick = birth_tick
    e.lifespan = lifespan
    return e


# ---------------------------------------------------------------------------
# the threshold


def test_below_lifespan_is_no_op(session):
    entity = _aged(session, "Methuselah", birth_tick=0, lifespan=5)
    assert run_incapacity(session, tick_number=4) == []  # age 4 < 5
    assert entity.status == EntityStatus.ACTIVE


def test_at_lifespan_incapacitates(session):
    entity = _aged(session, "Elder", birth_tick=0, lifespan=5)
    (event,) = run_incapacity(session, tick_number=5)  # age 5 >= 5
    assert entity.status == EntityStatus.INCAPACITATED
    assert entity.incapacitated_tick == 5
    assert event["entity_id"] == entity.id
    assert event["condition"] == "age"
    assert event["quantity"] == "5"        # integer tick count, not a Decimal
    assert event["threshold"] == "5"
    assert event["estate_policy"] == "burn"


def test_null_lifespan_is_immortal(session):
    """No lifespan means the entity never dies of old age (the default)."""
    entity = _aged(session, "Immortal", birth_tick=0, lifespan=None)
    assert run_incapacity(session, tick_number=9999) == []
    assert entity.status == EntityStatus.ACTIVE


def test_null_birth_tick_is_immortal_by_age(session):
    """A lifespan without a birth_tick cannot compute age — immortal by age."""
    entity = _aged(session, "Untracked", birth_tick=None, lifespan=5)
    assert run_incapacity(session, tick_number=9999) == []
    assert entity.status == EntityStatus.ACTIVE


def test_lifespan_zero_dies_at_birth(session):
    """lifespan=0 is valid: the entity is mortal immediately (age 0 >= 0)."""
    entity = _aged(session, "Stillborn", birth_tick=3, lifespan=0)
    (event,) = run_incapacity(session, tick_number=3)
    assert event["condition"] == "age" and entity.status == EntityStatus.INCAPACITATED


# ---------------------------------------------------------------------------
# estate reuse — an old-age death is an old death, same estate machinery


def test_age_death_burns_the_estate(session):
    """Default estate policy is burn: holdings, money, and parcels vanish."""
    entity = _aged(session, "Pauper", birth_tick=0, lifespan=1)
    adjust_holding(session, entity, "GOLD", Decimal("10"))
    create_account(session, entity, "USD", initial_balance=Decimal("100"))

    (event,) = run_incapacity(session, tick_number=1)

    assert event["estate_policy"] == "burn"
    assert event["goods_burned"] == "10.0000"
    assert event["money_burned"] == "100.0000"
    assert get_holding(session, entity.id, "GOLD").quantity == Decimal("0")
    assert entity.accounts[0].balance == Decimal("0")


def test_age_death_heir_inherits(session):
    """The heir rule transfers the estate exactly as on a condition death."""
    set_estate_rule(session, "heir")
    entity = _aged(session, "Patriarch", birth_tick=0, lifespan=1)
    heir = create_entity(session, "Heir", EntityType.INDIVIDUAL)
    entity.heir_id = heir.id
    adjust_holding(session, entity, "GOLD", Decimal("10"))
    create_account(session, entity, "USD", initial_balance=Decimal("100"))

    (event,) = run_incapacity(session, tick_number=1)

    assert event["estate_policy"] == "heir" and event["recipient_id"] == heir.id
    assert get_holding(session, heir.id, "GOLD").quantity == Decimal("10")
    assert heir.accounts[0].balance == Decimal("100")


def test_age_death_incapacitated_entity_ignored_next_pass(session):
    """Once dead, a later pass must not re-process the entity."""
    entity = _aged(session, "Once", birth_tick=0, lifespan=1)
    run_incapacity(session, tick_number=1)
    assert run_incapacity(session, tick_number=2) == []  # already INCAPACITATED


# ---------------------------------------------------------------------------
# ordering — conditions fire before age (first cause wins)


def test_condition_fires_before_age(session):
    """An entity crossing a condition threshold AND its lifespan in one tick
    is recorded as dying of the condition (conditions run first)."""
    create_good(session, "COND-SICK", incapacitates_at=Decimal("50"))
    entity = _aged(session, "Doomed", birth_tick=0, lifespan=1)
    adjust_holding(session, entity, "COND-SICK", Decimal("50"))

    (event,) = run_incapacity(session, tick_number=1)

    assert event["condition"] == "COND-SICK"  # not "age"
    assert entity.status == EntityStatus.INCAPACITATED


def test_condition_and_age_kill_two_different_entities(session):
    """Both passes contribute: one dies of a condition, another of age."""
    create_good(session, "COND-SICK", incapacitates_at=Decimal("50"))
    sick = _aged(session, "Sick", birth_tick=0, lifespan=99)
    adjust_holding(session, sick, "COND-SICK", Decimal("50"))
    old = _aged(session, "Old", birth_tick=0, lifespan=1)

    events = run_incapacity(session, tick_number=1)

    by_cause = {e["entity_id"]: e["condition"] for e in events}
    assert by_cause == {sick.id: "COND-SICK", old.id: "age"}


# ---------------------------------------------------------------------------
# the lifespan query (mirror of age())


def test_lifespan_query(session):
    e = _aged(session, "Mortal", birth_tick=0, lifespan=42)
    immortal = create_entity(session, "God", EntityType.INDIVIDUAL)  # NULL lifespan
    q = build_queries(session, 0)
    assert q["lifespan"](e.id) == 42
    assert q["lifespan"](immortal.id) is None       # immortal
    assert q["lifespan"]("does-not-exist") is None  # unknown


# ---------------------------------------------------------------------------
# spawn_entity threads lifespan through opts


@pytest.fixture
def spawner(session):
    """A spawn-capable government with a USD account (currency derivation)."""
    gov = create_entity(session, "State", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.SPAWN]
    create_account(session, gov, "USD", initial_balance=Decimal("0"))
    parent = create_entity(session, "Parent", EntityType.INDIVIDUAL)
    return {"gov": gov, "parent": parent}


def _spawn_intent(entity_id, parents, **extra):
    params = {"parents": json.dumps([str(p) for p in parents])}
    for k, v in extra.items():
        params[k] = str(v)
    return Intent(entity_id=entity_id, intent_type="spawn_entity", params=params,
                  resource_ids=[str(p) for p in parents], priority=100)


def test_spawn_sets_lifespan(spawner, session):
    """Direct service call: lifespan is stamped on the child."""
    result = spawn_entity(session, spawner["gov"],
                          parents=[spawner["parent"].id], currency="USD", lifespan=10)
    from econengine.models import Entity
    child = session.get(Entity, result["child_id"])
    assert child.lifespan == 10
    assert build_queries(session, 0)["lifespan"](child.id) == 10


def test_spawn_without_lifespan_is_immortal(spawner, session):
    """Omitting lifespan leaves the child immortal (the default)."""
    result = spawn_entity(session, spawner["gov"],
                          parents=[spawner["parent"].id], currency="USD")
    from econengine.models import Entity
    child = session.get(Entity, result["child_id"])
    assert child.lifespan is None


def test_spawn_lifespan_via_resolve_intent(spawner, session):
    """The stringly intent param is parsed to an int."""
    out = resolve_intent(session, _spawn_intent(
        spawner["gov"].id, [spawner["parent"].id], lifespan="10"))
    assert out["status"] == "applied"
    from econengine.models import Entity
    child = session.get(Entity, out["child_id"])
    assert child.lifespan == 10


def test_spawn_rejects_negative_lifespan(spawner, session):
    out = resolve_intent(session, _spawn_intent(
        spawner["gov"].id, [spawner["parent"].id], lifespan="-5"))
    assert out["status"] == "rejected"
    assert "non-negative" in out["reason"]


def test_spawn_rejects_non_integer_lifespan(spawner, session):
    out = resolve_intent(session, _spawn_intent(
        spawner["gov"].id, [spawner["parent"].id], lifespan="forever"))
    assert out["status"] == "rejected"
    assert "lifespan" in out["reason"]


# ---------------------------------------------------------------------------
# end-to-end through a real tick


def test_age_death_lands_on_a_real_tick(session):
    """Through run_tick: an entity born at tick 0 with lifespan 3 is alive
    after ticks 1-2 and dies of old age on tick 3, the event recorded on
    that tick."""
    elder = create_entity(session, "Elder", EntityType.INDIVIDUAL)
    elder.birth_tick = 0
    elder.lifespan = 3

    tick1 = run_tick(session)  # age 1
    tick2 = run_tick(session)  # age 2
    assert elder.status == EntityStatus.ACTIVE
    assert not any(e["type"] == "entity_incapacitated" for e in tick1.events)
    assert not any(e["type"] == "entity_incapacitated" for e in tick2.events)

    tick3 = run_tick(session)  # age 3 >= 3 -> dies
    dead = next(e for e in tick3.events if e["type"] == "entity_incapacitated")
    assert dead["entity_id"] == elder.id
    assert dead["condition"] == "age"
    assert dead["quantity"] == "3" and dead["threshold"] == "3"
    assert elder.status == EntityStatus.INCAPACITATED


def test_immortal_entity_survives_many_ticks(session):
    """An entity with no lifespan runs indefinitely without dying."""
    create_good(session, "LABOR-PLAIN", auto_issue_quantity=Decimal("1"),
                auto_issue_entity_type=EntityType.INDIVIDUAL)
    immortal = create_entity(session, "Immortal", EntityType.INDIVIDUAL)
    immortal.birth_tick = 0
    # lifespan left NULL (the default)

    for _ in range(20):
        run_tick(session)

    assert immortal.status == EntityStatus.ACTIVE
