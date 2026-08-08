"""Council governance — the `council` / `weighted` weight models.

A council is the third electorate form (after citizen and share): a named
*register* of members (authored policy data in a WorldSetting), not
something the engine can derive from entity type or holdings. Two weight
models read the same register through different lenses:

  - `council:NAME`  — every member, weight 1 each (an equal-weight council,
    oligarchy, senate);
  - `weighted:NAME` — every member, weight = the declared per-member weight
    (a weighted council, which subsumes a *representative* chamber: set each
    MP's weight to their constituency size).

This is "forms of government are data, not mechanism" (docs/actors.md):
the proposal → vote → enact machinery is untouched — only "who votes, and
how much?" changes. `weighted` lets one register stand for both a council
of equals and a weighted/representative body.
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities, councils, weights
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
def senate(session):
    """A government the council governs (it holds `legislate` — the data
    that lets an enacted directive bind its behaviour script), and a
    five-seat senate whose weights declare a 3/1/1 split — so the *council*
    model treats all five as equals (weight 1) while the *weighted* model
    treats Alice as holding three votes. Carol holds no seat."""
    gov = create_entity(session, "Republic", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.LEGISLATE, capabilities.SET_FISCAL_POLICY]
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    dave = create_entity(session, "Dave", EntityType.INDIVIDUAL)
    carol = create_entity(session, "Carol", EntityType.INDIVIDUAL)   # not on council
    councils.set_register(session, "senate", {
        alice.id: "3", bob.id: "1", dave.id: "1"})
    session.flush()
    return {"s": session, "gov": gov, "alice": alice, "bob": bob,
            "dave": dave, "carol": carol}


# ---------------------------------------------------------------------------
# intent helpers (mirror test_share_governance)
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
# councils.py — the register data access
# ===========================================================================

def test_set_register_with_mapping_round_trips(session):
    e = create_entity(session, "X", EntityType.INDIVIDUAL)
    session.flush()
    councils.set_register(session, "c", {e.id: "2"})
    assert councils.get_register(session, "c") == {e.id: "2"}


def test_set_register_with_list_normalises_to_weight_one(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    b = create_entity(session, "B", EntityType.INDIVIDUAL)
    session.flush()
    councils.set_register(session, "c", [a.id, b.id])
    assert councils.get_register(session, "c") == {a.id: "1", b.id: "1"}


def test_set_register_replaces_wholesale(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    b = create_entity(session, "B", EntityType.INDIVIDUAL)
    session.flush()
    councils.set_register(session, "c", [a.id])
    councils.set_register(session, "c", [b.id])     # replaces, not merges
    assert councils.get_register(session, "c") == {b.id: "1"}


def test_set_register_rejects_empty_membership(session):
    with pytest.raises(ValueError, match="at least one member"):
        councils.set_register(session, "c", [])


def test_delete_register(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    session.flush()
    councils.set_register(session, "c", [a.id])
    assert councils.delete_register(session, "c") is True
    assert councils.get_register(session, "c") == {}
    assert councils.delete_register(session, "c") is False   # already gone


def test_member_weight_defaults_to_one_for_unparseable(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    session.flush()
    councils.set_register(session, "c", {a.id: "garbage"})
    assert councils.member_weight(session, "c", a.id) == Decimal(1)


def test_member_weight_zero_for_non_member(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    b = create_entity(session, "B", EntityType.INDIVIDUAL)
    session.flush()
    councils.set_register(session, "c", {b.id: "1"})
    # a is not a member of this council at all
    assert councils.member_weight(session, "c", a.id) == Decimal(0)


# ===========================================================================
# the resolver — council (equal weight)
# ===========================================================================

def test_council_electorate_is_members_weight_one(senate):
    s, a = senate["s"], senate["alice"]
    assert weights.electorate(s, "council:senate") == {
        a.id: Decimal(1), senate["bob"].id: Decimal(1),
        senate["dave"].id: Decimal(1)}


def test_council_weight_is_one_for_members(senate):
    s = senate["s"]
    assert weights.weight_of(s, "council:senate", senate["alice"].id) == Decimal(1)
    assert weights.weight_of(s, "council:senate", senate["bob"].id) == Decimal(1)


def test_council_weight_is_zero_for_non_member(senate):
    # Carol holds no seat
    assert weights.weight_of(senate["s"], "council:senate",
                             senate["carol"].id) == Decimal(0)


def test_council_ignores_declared_weights(senate):
    """Alice's register weight is 3, but under `council` everyone is 1 — the
    declared weights only matter under `weighted`."""
    assert weights.weight_of(senate["s"], "council:senate",
                             senate["alice"].id) == Decimal(1)


def test_council_electorate_is_per_register(session):
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    b = create_entity(session, "B", EntityType.INDIVIDUAL)
    session.flush()
    councils.set_register(session, "senate", [a.id])
    councils.set_register(session, "judges", [b.id])
    assert weights.electorate(session, "council:senate") == {a.id: Decimal(1)}
    assert weights.electorate(session, "council:judges") == {b.id: Decimal(1)}


def test_unknown_register_is_empty_electorate(session):
    assert weights.electorate(session, "council:ghost") == {}


def test_council_without_scope_raises(session):
    with pytest.raises(ValueError, match="register name"):
        weights.electorate(session, "council:")


# ===========================================================================
# the resolver — weighted (declared weights; subsumes representative)
# ===========================================================================

def test_weighted_electorate_uses_declared_weights(senate):
    s, a = senate["s"], senate["alice"]
    assert weights.electorate(s, "weighted:senate") == {
        a.id: Decimal("3"), senate["bob"].id: Decimal("1"),
        senate["dave"].id: Decimal("1")}


def test_weighted_weight_is_declared(senate):
    s = senate["s"]
    assert weights.weight_of(s, "weighted:senate", senate["alice"].id) == Decimal("3")
    assert weights.weight_of(s, "weighted:senate", senate["bob"].id) == Decimal("1")


def test_weighted_weight_zero_for_non_member(senate):
    assert weights.weight_of(senate["s"], "weighted:senate",
                             senate["carol"].id) == Decimal(0)


def test_weighted_subsumes_representative(session):
    """A representative chamber is a weighted council whose weights are
    constituency sizes: the majority is of represented population."""
    mp1 = create_entity(session, "North", EntityType.INDIVIDUAL)
    mp2 = create_entity(session, "South", EntityType.INDIVIDUAL)
    session.flush()
    councils.set_register(session, "parliament", {mp1.id: "1000", mp2.id: "300"})
    assert weights.electorate(session, "weighted:parliament") == {
        mp1.id: Decimal("1000"), mp2.id: Decimal("300")}


# ===========================================================================
# membership is alive state, not just a list — incapacitation withdraws a vote
# ===========================================================================

def test_incapacitated_councillor_leaves_the_electorate(senate):
    senate["alice"].status = EntityStatus.INCAPACITATED
    session = senate["s"]
    # gone from the electorate ...
    assert senate["alice"].id not in weights.electorate(session, "council:senate")
    assert senate["alice"].id not in weights.electorate(session, "weighted:senate")
    # ... and zero weight
    assert weights.weight_of(session, "council:senate",
                             senate["alice"].id) == Decimal(0)


# ===========================================================================
# the governance cycle — participation is council membership
# ===========================================================================

def test_councillor_can_propose(senate):
    out = propose(senate["s"], senate["alice"].id, senate["gov"].id,
                  "council:senate", [fiscal_mutation({"flat_rate": "0.1"})])
    assert out["status"] == "applied"
    assert "proposal_id" in out


def test_non_member_cannot_propose(senate):
    """Participation is the electorate: a non-member's proposal is rejected
    before it opens (exactly as a non-holder cannot propose to a firm)."""
    out = propose(senate["s"], senate["carol"].id, senate["gov"].id,
                  "council:senate", [fiscal_mutation({"flat_rate": "0.1"})])
    assert out["status"] == "rejected"
    assert "electorate" in out["reason"]


def test_non_member_cannot_vote(senate):
    pid = pid_of(propose(senate["s"], senate["alice"].id, senate["gov"].id,
                         "council:senate", [fiscal_mutation({"flat_rate": "0.1"})]))
    out = vote(senate["s"], senate["carol"].id, pid, "for")
    assert out["status"] == "rejected"


def test_council_vote_is_one_each_simple_majority_carries(senate):
    """Under `council` Alice/Bob/Dave each have one vote: a 2-of-3 majority
    (Bob + Dave FOR, Alice AGAINST) carries at threshold 0.5."""
    pid = pid_of(propose(senate["s"], senate["alice"].id, senate["gov"].id,
                         "council:senate",
                         [fiscal_mutation({"flat_rate": "0.1"})], threshold="0.5"))
    vote(senate["s"], senate["alice"].id, pid, "against")
    vote(senate["s"], senate["bob"].id, pid, "for")
    vote(senate["s"], senate["dave"].id, pid, "for")
    out = enact(senate["s"], senate["gov"].id, pid)
    assert out["proposal_status"] == "enacted"
    assert out["status"] == "applied"


def test_council_quorum_gates_low_turnout(senate):
    """A quorum of 0.67 needs at least two of three members to vote; one
    voter alone falls short and the proposal fails on turnout."""
    pid = pid_of(propose(senate["s"], senate["alice"].id, senate["gov"].id,
                         "council:senate",
                         [fiscal_mutation({"flat_rate": "0.1"})],
                         threshold="0.5", quorum="0.67"))
    vote(senate["s"], senate["alice"].id, pid, "for")   # only one votes
    out = enact(senate["s"], senate["gov"].id, pid)
    assert out["proposal_status"] == "failed"
    assert "quorum" in (out.get("reason") or "").lower() or \
        "turnout" in (out.get("reason") or "").lower()


# ===========================================================================
# the governance cycle — weighted votes
# ===========================================================================

def test_weighted_minority_outvoted_by_weighted_majority(senate):
    """Under `weighted` Alice has 3, Bob 1, Dave 1 (total 5). Alice alone is
    3/5 = 0.6, short of a 0.67 threshold; but Alice + Bob = 4/5 = 0.8,
    which carries over Dave's objection."""
    pid = pid_of(propose(senate["s"], senate["alice"].id, senate["gov"].id,
                         "weighted:senate",
                         [fiscal_mutation({"flat_rate": "0.2"})], threshold="0.67"))
    vote(senate["s"], senate["alice"].id, pid, "for")
    vote(senate["s"], senate["bob"].id, pid, "for")
    vote(senate["s"], senate["dave"].id, pid, "against")
    out = enact(senate["s"], senate["gov"].id, pid)
    assert out["proposal_status"] == "enacted"


def test_weighted_high_threshold_blocks_the_heavy_member(senate):
    """At threshold 0.81, even Alice's three votes (3/5 = 0.6 of cast weight,
    with Bob + Dave voting AGAINST) cannot carry — a supermajority floor can
    bind the heaviest councillor."""
    pid = pid_of(propose(senate["s"], senate["alice"].id, senate["gov"].id,
                         "weighted:senate",
                         [fiscal_mutation({"flat_rate": "0.2"})], threshold="0.81"))
    vote(senate["s"], senate["alice"].id, pid, "for")    # 3
    vote(senate["s"], senate["bob"].id, pid, "against")   # 1
    vote(senate["s"], senate["dave"].id, pid, "against")  # +1 → no=2
    out = enact(senate["s"], senate["gov"].id, pid)
    assert out["proposal_status"] == "failed"


# ===========================================================================
# enactment — the directive runs as the target government
# ===========================================================================

def test_enacted_council_proposal_binds_a_behaviour_script(senate):
    """An enacted council proposal carries an ordinary `set_script` (a
    BEHAVIOUR script bound to the government), exactly as a shareholder
    vote binds the firm. The directive runs as the government next tick."""
    from econengine.tick import run_tick
    s, gov = senate["s"], senate["gov"]
    src = "ctx.state.flag = 'enacted'"
    pid = pid_of(propose(s, senate["alice"].id, gov.id, "council:senate", [
        {"type": "set_script", "params": {
            "script_type": "behaviour", "lineage_id": "gov-policy",
            "source": src, "entity_id": gov.id}},
    ], threshold="0.5"))
    for who in ("alice", "bob"):
        vote(s, senate[who].id, pid, "for")
    out = enact(s, gov.id, pid)
    assert out["proposal_status"] == "enacted"
    # the directive runs as the government on the next tick
    run_tick(s)
    active = s.query(Script).filter_by(
        lineage_id="gov-policy", is_active=True).one()
    assert active.entity_id == gov.id
