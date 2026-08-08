"""Liquid democracy — the `liquid` weight model (the last form of government).

Liquid democracy is direct democracy plus a *delegation*: a voter may
redirect their vote to a trusted delegate, who votes it together with
their own (and may themselves delegate onward — transitively). It is the
final weight model (docs/actors.md): the proposal → vote → enact
machinery is untouched, only "who votes, and how much?" changes.

The base electorate pool is every active INDIVIDUAL (as in direct
democracy). A delegation graph (authored policy data, a WorldSetting in
`delegations.py`) is layered over it. Each member's weight is 1 plus the
weight delegated *to* them, resolved transitively. A delegator leaves the
electorate — they voted by redirecting. An empty graph is identical to
`citizen`.

This proves, end to end:

  - with no delegations, `liquid:NAME` == `citizen` (everyone, weight 1);
  - a single delegation transfers weight (A→B: A gone, B has 2);
  - delegation is transitive (A→B→C: C has 3, A and B gone);
  - diamonds converge (A→C, B→C: C has 3);
  - a cycle strands the weight of its members (fail-safe, no inflation);
  - a delegation outside the active-individual pool strands the delegator;
  - an incapacitated delegate withdraws the redirected weight;
  - a delegator cannot propose or vote (they are not in the electorate);
  - the full governance cycle runs under `liquid`, and a delegated bloc
    outvotes an individual.
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities, delegations, weights
from econengine.lua_engine import Intent
from econengine.models import (
    Base, EntityStatus, EntityType, ProposalStatus, Script, ScriptType,
)
from econengine.scripting import resolve_intent
from econengine.services import create_entity


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def polity(session):
    """A government the polity governs (it holds `legislate` and
    `set_fiscal_policy`), and five active individuals A–E. No delegations
    seeded yet — tests call `delegations.set_delegations` to plant a graph."""
    gov = create_entity(session, "Republic", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.LEGISLATE, capabilities.SET_FISCAL_POLICY]
    a = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    b = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    c = create_entity(session, "Carol", EntityType.INDIVIDUAL)
    d = create_entity(session, "Dave", EntityType.INDIVIDUAL)
    e = create_entity(session, "Eve", EntityType.INDIVIDUAL)
    session.flush()
    return {"s": session, "gov": gov, "a": a, "b": b, "c": c, "d": d, "e": e}


# ---------------------------------------------------------------------------
# intent helpers
# ---------------------------------------------------------------------------

def propose(s, proposer_id, target_id, model, mutations, title="",
            threshold="0.5", quorum="0"):
    return resolve_intent(s, Intent(
        entity_id=proposer_id, intent_type="create_proposal",
        params={"target_id": target_id, "mutations": json.dumps(mutations),
                "weight_model": model, "threshold": threshold,
                "quorum": quorum, "title": title},
        resource_ids=[target_id],
    ))


def vote(s, voter_id, proposal_id, choice):
    return resolve_intent(s, Intent(
        entity_id=voter_id, intent_type="vote",
        params={"proposal_id": proposal_id, "choice": choice},
        resource_ids=[proposal_id],
    ))


def enact(s, target_id, proposal_id):
    return resolve_intent(s, Intent(
        entity_id=target_id, intent_type="enact",
        params={"proposal_id": proposal_id},
        resource_ids=[proposal_id],
    ))


def fiscal_mutation(policy):
    return {"type": "set_fiscal_policy", "params": {"policy": json.dumps(policy)}}


def pid_of(result):
    return result["proposal_id"]


# ===========================================================================
# delegations.py — the register data access
# ===========================================================================

def test_set_delegations_round_trips(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    b = create_entity(session, "B", EntityType.INDIVIDUAL)
    session.flush()
    delegations.set_delegations(session, "senate", {a.id: b.id})
    assert delegations.get_delegations(session, "senate") == {a.id: b.id}


def test_set_delegations_replaces_wholesale(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    b = create_entity(session, "B", EntityType.INDIVIDUAL)
    session.flush()
    delegations.set_delegations(session, "senate", {a.id: b.id})
    delegations.set_delegations(session, "senate", {b.id: a.id})  # replaces
    assert delegations.get_delegations(session, "senate") == {b.id: a.id}


def test_set_delegations_rejects_empty(session):
    with pytest.raises(ValueError, match="at least one edge"):
        delegations.set_delegations(session, "senate", {})


def test_set_delegations_rejects_self_delegation(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    session.flush()
    with pytest.raises(ValueError, match="self-delegation"):
        delegations.set_delegations(session, "senate", {a.id: a.id})


def test_delete_delegations(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    b = create_entity(session, "B", EntityType.INDIVIDUAL)
    session.flush()
    delegations.set_delegations(session, "senate", {a.id: b.id})
    assert delegations.delete_delegations(session, "senate") is True
    assert delegations.get_delegations(session, "senate") == {}
    assert delegations.delete_delegations(session, "senate") is False


# ===========================================================================
# the resolver — an empty graph is direct democracy
# ===========================================================================

def test_empty_graph_equals_citizen(polity):
    """With no delegations, `liquid:senate` is identical to `citizen`:
    every active individual, weight 1 each."""
    s = polity["s"]
    assert weights.electorate(s, "liquid:senate") == {
        polity["a"].id: Decimal(1), polity["b"].id: Decimal(1),
        polity["c"].id: Decimal(1), polity["d"].id: Decimal(1),
        polity["e"].id: Decimal(1)}


def test_nonexistent_register_equals_citizen(polity):
    """A polity that never had a delegation register is pure direct
    democracy (everyone, weight 1)."""
    s = polity["s"]
    assert weights.electorate(s, "liquid:ghost") == {
        polity["a"].id: Decimal(1), polity["b"].id: Decimal(1),
        polity["c"].id: Decimal(1), polity["d"].id: Decimal(1),
        polity["e"].id: Decimal(1)}


def test_liquid_without_scope_raises(session):
    with pytest.raises(ValueError, match="polity name"):
        weights.electorate(session, "liquid:")


# ===========================================================================
# the resolver — delegation redirects weight
# ===========================================================================

def test_single_delegation_transfers_weight(polity):
    """A delegates to B: A leaves the electorate, B carries 2 (own + A)."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {polity["a"].id: polity["b"].id})
    el = weights.electorate(s, "liquid:senate")
    assert polity["a"].id not in el          # A delegated away
    assert el[polity["b"].id] == Decimal(2)  # B has own + A's
    assert el[polity["c"].id] == Decimal(1)  # C unaffected


def test_delegation_is_transitive(polity):
    """A→B→C: A and B leave the electorate; C carries 3 (own + A + B)."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {
        polity["a"].id: polity["b"].id, polity["b"].id: polity["c"].id})
    el = weights.electorate(s, "liquid:senate")
    assert polity["a"].id not in el
    assert polity["b"].id not in el
    assert el[polity["c"].id] == Decimal(3)


def test_diamond_converges(polity):
    """A→C and B→C: C carries 3 (own + A + B). Two delegators, one sink."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {
        polity["a"].id: polity["c"].id, polity["b"].id: polity["c"].id})
    el = weights.electorate(s, "liquid:senate")
    assert el[polity["c"].id] == Decimal(3)


def test_delegator_weight_is_zero(polity):
    s = polity["s"]
    delegations.set_delegations(s, "senate", {polity["a"].id: polity["b"].id})
    assert weights.weight_of(s, "liquid:senate", polity["a"].id) == Decimal(0)


# ===========================================================================
# the resolver — cycles and broken edges strand weight (fail-safe)
# ===========================================================================

def test_cycle_strands_its_members(polity):
    """A↔B is a cycle: neither reaches a terminal voter, so both A and B's
    weight is dropped (not counted anywhere). C, D, E vote normally."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {
        polity["a"].id: polity["b"].id, polity["b"].id: polity["a"].id})
    el = weights.electorate(s, "liquid:senate")
    assert polity["a"].id not in el
    assert polity["b"].id not in el
    assert el[polity["c"].id] == Decimal(1)
    assert el[polity["d"].id] == Decimal(1)


def test_longer_cycle_strands_its_members(polity):
    """A→B→C→A is a 3-cycle: all three are stranded; D and E unaffected."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {
        polity["a"].id: polity["b"].id,
        polity["b"].id: polity["c"].id,
        polity["c"].id: polity["a"].id})
    el = weights.electorate(s, "liquid:senate")
    assert polity["a"].id not in el
    assert polity["b"].id not in el
    assert polity["c"].id not in el
    assert el[polity["d"].id] == Decimal(1)


def test_delegation_to_inactive_entity_strands_the_delegator(polity):
    """A delegates to B, but B is incapacitated: the chain leaves the
    active-individual pool, so A's weight is stranded (dropped), and B is
    not a voter either."""
    s = polity["s"]
    polity["b"].status = EntityStatus.INCAPACITATED
    delegations.set_delegations(s, "senate", {polity["a"].id: polity["b"].id})
    el = weights.electorate(s, "liquid:senate")
    assert polity["a"].id not in el          # A stranded
    assert polity["b"].id not in el          # B inactive, not in pool
    assert el[polity["c"].id] == Decimal(1)


def test_delegation_to_nonexistent_entity_strands_the_delegator(polity):
    """A delegates to a ghost id: the chain leaves the pool, A's weight is
    stranded."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {polity["a"].id: "no-such-entity"})
    el = weights.electorate(s, "liquid:senate")
    assert polity["a"].id not in el
    assert el[polity["c"].id] == Decimal(1)


# ===========================================================================
# membership is alive state — incapacitating a delegate withdraws the weight
# ===========================================================================

def test_incapacitating_a_delegate_reverts_weights(polity):
    """A→B (B has 2). When B is incapacitated, A's chain leaves the pool
    (stranded) and B leaves the electorate — so both A and B vanish, and
    no one inherits A's weight."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {polity["a"].id: polity["b"].id})
    assert weights.electorate(s, "liquid:senate")[polity["b"].id] == Decimal(2)
    polity["b"].status = EntityStatus.INCAPACITATED
    el = weights.electorate(s, "liquid:senate")
    assert polity["a"].id not in el
    assert polity["b"].id not in el


# ===========================================================================
# per-polity scope — different graphs over the same population
# ===========================================================================

def test_delegation_graphs_are_per_polity(polity):
    """Two liquid polities over the same voters can have different
    delegation graphs (delegate your econ vote elsewhere from your foreign
    vote)."""
    s = polity["s"]
    delegations.set_delegations(s, "econ", {polity["a"].id: polity["b"].id})
    delegations.set_delegations(s, "foreign", {polity["a"].id: polity["c"].id})
    assert weights.electorate(s, "liquid:econ")[polity["b"].id] == Decimal(2)
    assert weights.electorate(s, "liquid:foreign")[polity["c"].id] == Decimal(2)


# ===========================================================================
# the governance cycle — participation and delegated weight
# ===========================================================================

def test_delegator_cannot_propose(polity):
    """A delegated away; they are not in the electorate, so their proposal
    is rejected before it opens."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {polity["a"].id: polity["b"].id})
    out = propose(s, polity["a"].id, polity["gov"].id, "liquid:senate",
                  [fiscal_mutation({"flat_rate": "0.1"})])
    assert out["status"] == "rejected"


def test_undelegated_voter_can_propose(polity):
    s = polity["s"]
    delegations.set_delegations(s, "senate", {polity["a"].id: polity["b"].id})
    out = propose(s, polity["c"].id, polity["gov"].id, "liquid:senate",
                  [fiscal_mutation({"flat_rate": "0.1"})])
    assert out["status"] == "applied"
    assert "proposal_id" in out


def test_delegated_bloc_outvotes_an_individual(polity):
    """A→C and B→C: C votes FOR with weight 3; D votes AGAINST with weight
    1. At threshold 0.5 the delegated bloc (3/4 = 0.75) carries."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {
        polity["a"].id: polity["c"].id, polity["b"].id: polity["c"].id})
    pid = pid_of(propose(s, polity["c"].id, polity["gov"].id, "liquid:senate",
                         [fiscal_mutation({"flat_rate": "0.1"})], threshold="0.5"))
    vote(s, polity["c"].id, pid, "for")     # weight 3
    vote(s, polity["d"].id, pid, "against")  # weight 1
    out = enact(s, polity["gov"].id, pid)
    assert out["proposal_status"] == "enacted"


def test_delegator_cannot_vote(polity):
    """A delegated to B and cannot cast a direct vote (weight 0)."""
    s = polity["s"]
    delegations.set_delegations(s, "senate", {polity["a"].id: polity["b"].id})
    pid = pid_of(propose(s, polity["c"].id, polity["gov"].id, "liquid:senate",
                         [fiscal_mutation({"flat_rate": "0.1"})]))
    out = vote(s, polity["a"].id, pid, "for")
    assert out["status"] == "rejected"


def test_liquid_with_no_delegations_runs_full_cycle(polity):
    """With an empty graph, `liquid:senate` behaves exactly like direct
    democracy: a simple majority of individuals carries."""
    s = polity["s"]
    pid = pid_of(propose(s, polity["a"].id, polity["gov"].id, "liquid:senate",
                         [fiscal_mutation({"flat_rate": "0.1"})], threshold="0.5"))
    vote(s, polity["a"].id, pid, "for")
    vote(s, polity["b"].id, pid, "for")
    vote(s, polity["c"].id, pid, "against")
    out = enact(s, polity["gov"].id, pid)
    assert out["proposal_status"] == "enacted"
