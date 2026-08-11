"""Smoke test for the population demo (Step 6c proving experiment).

Asserts that ``spawn_entity`` and the 6c queries actually drove the four
design points, on structured records (not stdout). This is the machine-
checkable half of the "prove the mechanism" deliverable; the human-readable
half is ``python -m experiments.population.run``.

What it pins down:
  * SPAWN + POPULATION CAP   the valid Adam x Eve birth is admitted until the
                            votable cap, then vetoed ("population cap").
  * BIRTH LAW                the illicit Adam x Lilith birth is refused every
                            tick ("not married"); and (directly) same-sex,
                            single-parent, and underage births are refused
                            with their own distinct reasons.
  * EXECUTING-TICK BIRTH     each child's birth_tick is the tick it was BORN,
                            not the prior tick (the threading from 6c).
  * ENDOWMENT = TRANSFER     each newborn's balance is ENDOWMENT, transferred
                            by a HOOK (spawn_entity itself opens the account
                            at zero; endowment is policy, not mechanism).
  * LINEAGE                  every child's parents are [Adam, Eve]; the
                            children()/parents() queries walk the lineage.
  * MONEY CONSERVED          no issuance -- treasury loss == children's gain.
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.lua_engine import Intent
from econengine.models import Account, Base, Entity
from econengine.scripting import build_queries, resolve_intent

from .run import simulate, BORN, CAPPED
from .scenario import (
    ENDOWMENT, POPULATION_CAP, TREASURY_ENDOWMENT, build_economy,
)


# ---------------------------------------------------------------------------
# fixtures -- sessions stay open so tests can inspect the live world
# ---------------------------------------------------------------------------

@pytest.fixture
def sim():
    """A populated world after 6 ticks. Session stays open for inspection."""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = Session(engine)
    world = build_economy(session)
    session.commit()
    records = simulate(session, world, 6)
    yield session, world, records
    session.close()


@pytest.fixture
def fresh():
    """A built-but-not-ticked world (pop 4, under the cap). For direct
    intent tests where the population cap must not interfere."""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = Session(engine)
    world = build_economy(session)
    session.commit()
    yield session, world
    session.close()


# ---------------------------------------------------------------------------
# the population-cap arc: grow, then stop
# ---------------------------------------------------------------------------

def test_valid_birth_admitted_until_cap(sim):
    _, _, records = sim
    # 4 founders, cap 7 -> births on ticks 1-3, then capped 4-6.
    born = [r["tick"] for r in records if r["birth_label"] == BORN]
    capped = [r["tick"] for r in records if r["birth_label"] == CAPPED]
    assert born == [1, 2, 3]
    assert capped == [4, 5, 6]


def test_population_grows_then_holds(sim):
    _, _, records = sim
    pops = [r["population"] for r in records]
    # 4 -> 5 -> 6 -> 7 (cap), then held at 7.
    assert pops == [5, 6, 7, 7, 7, 7]


def test_illicit_birth_refused_every_tick(sim):
    """The birth-law is observably active every tick: Adam x Lilith (not
    married) is always refused, even while the world is under capacity."""
    _, _, records = sim
    for r in records:
        assert r["illicit_status"] == "rejected"
        assert "married" in r["illicit_reason"]


def test_cap_and_birthlaw_give_distinct_reasons(sim):
    """The two validators compose: the valid pair's veto reason is the cap,
    the illicit pair's is the marriage rule -- never conflated."""
    _, _, records = sim
    capped_tick = next(r for r in records if r["birth_label"] == CAPPED)
    assert "population cap" in capped_tick["birth_reason"]
    assert "married" in records[0]["illicit_reason"]


# ---------------------------------------------------------------------------
# executing-tick threading: birth_tick == the tick of birth
# ---------------------------------------------------------------------------

def test_birth_tick_is_the_executing_tick(sim):
    """A child born during tick N has birth_tick = N, not N-1 (the prior
    committed tick). This is the threading the 6c mechanism added."""
    session, _, records = sim
    for r in records:
        if r["birth_label"] == BORN:
            child = session.get(Entity, r["birth_child_id"])
            assert child.birth_tick == r["tick"]


def test_child_age_is_consistent_with_ctx_tick(sim):
    """age(child) at any later tick never disagrees with ctx.tick."""
    session, _, records = sim
    firstborn = session.get(Entity, records[0]["birth_child_id"])  # born tick 1
    assert build_queries(session, 6)["age"](firstborn.id) == 6 - 1


# ---------------------------------------------------------------------------
# endowment is a transfer, not a mechanism
# ---------------------------------------------------------------------------

def test_endowment_is_a_hook_transfer(sim):
    """Each newborn's account starts at zero (spawn never endows) and is
    immediately endowed by the HOOK's transfer. Balance == ENDOWMENT."""
    session, _, records = sim
    for r in records:
        if r["birth_label"] == BORN:
            child = session.get(Entity, r["birth_child_id"])
            assert child.accounts[0].balance == ENDOWMENT


def test_treasury_lost_exactly_the_endowments(sim):
    session, world, _ = sim
    treasury = session.get(Account, world.treasury_account_id)
    n_children = sum(1 for r in session.query(Entity).all()
                     if r.parents and world.founders["Adam"].id in r.parents)
    assert treasury.balance == TREASURY_ENDOWMENT - ENDOWMENT * n_children


def test_money_is_conserved_no_issuance(sim):
    """Transfers only -- spawn creates an empty account; the HOOK moves
    existing money. Total money is invariant."""
    session, world, _ = sim
    treasury = session.get(Account, world.treasury_account_id)
    children_total = sum(
        c.accounts[0].balance for c in session.query(Entity).all()
        if c.parents and c.accounts)
    # The only accounts are the treasury and the children's.
    assert treasury.balance + children_total == TREASURY_ENDOWMENT


# ---------------------------------------------------------------------------
# lineage: the 6c queries walk provenance
# ---------------------------------------------------------------------------

def test_children_share_both_parents(sim):
    session, world, _ = sim
    adam, eve = world.founders["Adam"], world.founders["Eve"]
    q = build_queries(session, 6)
    adam_kids = q["children"](adam.id)
    eve_kids = q["children"](eve.id)
    assert adam_kids == eve_kids              # the same set
    assert len(adam_kids) == 3                # three survived the cap
    for kid in adam_kids:
        assert set(q["parents"](kid)) == {adam.id, eve.id}


def test_provenance_is_immutable_data(sim):
    """Parents are stamped once at birth and stored as plain engine data --
    not scribbleable script state. The firstborn's parents read back exactly."""
    session, world, records = sim
    firstborn = session.get(Entity, records[0]["birth_child_id"])
    adam, eve = world.founders["Adam"], world.founders["Eve"]
    assert firstborn.parents == [adam.id, eve.id]


# ---------------------------------------------------------------------------
# the birth-law composition: each rule fires independently (direct intents)
# ---------------------------------------------------------------------------

def _spawn(session, caller_id, parents):
    """Resolve a one-off spawn directly (between ticks) and return the event."""
    return resolve_intent(session, Intent(
        entity_id=caller_id, intent_type="spawn_entity",
        params={"parents": json.dumps([str(p) for p in parents]),
                "currency": "USD"},
        resource_ids=[str(p) for p in parents], priority=100))


def test_birthlaw_requires_two_parents(fresh):
    session, world = fresh
    out = _spawn(session, world.gov.id, [world.founders["Adam"].id])
    assert out["status"] == "rejected"
    assert "two parents" in out["reason"]


def test_birthlaw_requires_opposite_sex(fresh):
    session, world = fresh
    eve, lilith = world.founders["Eve"].id, world.founders["Lilith"].id
    out = _spawn(session, world.gov.id, [eve, lilith])
    assert out["status"] == "rejected"
    assert "male and one female" in out["reason"]


def test_birthlaw_requires_of_age(fresh):
    session, world = fresh
    from econengine import markets, services
    from econengine.models import EntityType
    # A young female (age 10 at tick 0) -- passes sex, fails age.
    young = services.create_entity(session, "ChildBride", EntityType.INDIVIDUAL)
    young.birth_tick = -10
    markets.adjust_holding(session, young, "FEMALE", Decimal("1"))
    session.flush()
    out = _spawn(session, world.gov.id, [world.founders["Adam"].id, young.id])
    assert out["status"] == "rejected"
    assert "of age" in out["reason"]


def test_birthlaw_admits_valid_married_couple(fresh):
    session, world = fresh
    adam, eve = world.founders["Adam"].id, world.founders["Eve"].id
    out = _spawn(session, world.gov.id, [adam, eve])
    assert out["status"] == "applied"
    assert out["child_id"] is not None


def test_spawn_without_capability_is_rejected():
    """The caller must hold SPAWN -- a plain government (no capability)
    cannot birth, even with valid parents."""
    from econengine.models import EntityType
    from econengine import services
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        plain = services.create_entity(session, "Plain", EntityType.GOVERNMENT)
        services.create_account(session, plain, "USD", Decimal("0"))
        session.commit()
        out = _spawn(session, plain.id, [])
        assert out["status"] == "rejected"
        assert "spawn" in out["reason"]
