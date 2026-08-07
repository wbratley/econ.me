"""Proposal → vote → enact — the democracy layer (actors step 4a-ii).

This is "vote on code": citizens (the electorate, by weight) enact new
POLICY scripts and fiscal parameters through a proposal, not an operator.
The safety thesis (docs/actors.md) is tested here end to end:

  - participation is the electorate (the weight-model resolver), not a
    capability — a non-citizen cannot propose or vote;
  - enactment applies mutations as the target government through
    resolve_intent, so capability gates and VALIDATORs fire exactly as for
    a live intent — a citizen-enacted over-cap rate is still vetoed;
  - mutations apply atomically: a veto on any one rolls back the rest;
  - the tally is threshold-of-cast-weight AND quorum-of-electorate.

Only the "citizen" resolver ships (direct democracy); the resolver
registry is the seam a corporation / council / share-weight form plugs
into with data only.
"""
import json

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities, weights
from econengine.lua_engine import Intent
from econengine.models import (
    Base, EntityType, Proposal, ProposalStatus, Script, ScriptType, Vote,
    VoteChoice,
)
from econengine.scripting import build_queries, resolve_intent
from econengine.services import create_entity
from econengine.tick import run_tick


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
def world(session):
    """A legislating government, three citizens, a business (not in the
    electorate), and a capability-less government."""
    gov = create_entity(session, "Gov", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.LEGISLATE, capabilities.SET_FISCAL_POLICY]
    a = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    b = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    c = create_entity(session, "Carol", EntityType.INDIVIDUAL)
    biz = create_entity(session, "AcmeCorp", EntityType.BUSINESS)
    plain = create_entity(session, "PlainGov", EntityType.GOVERNMENT)
    session.flush()
    return {"gov": gov, "a": a, "b": b, "c": c, "biz": biz, "plain": plain}


# ---------------------------------------------------------------------------
# intent helpers (mirror how the API / scripts construct these)
# ---------------------------------------------------------------------------

def propose(session, proposer_id, target_id, mutations, title="",
            weight_model="citizen", threshold="0.5", quorum="0"):
    return resolve_intent(session, Intent(
        entity_id=proposer_id, intent_type="create_proposal",
        params={"target_id": target_id, "mutations": json.dumps(mutations),
                "weight_model": weight_model, "threshold": threshold,
                "quorum": quorum, "title": title},
        resource_ids=[target_id],
    ))


def vote(session, voter_id, proposal_id, choice):
    return resolve_intent(session, Intent(
        entity_id=voter_id, intent_type="vote",
        params={"proposal_id": proposal_id, "choice": choice},
        resource_ids=[proposal_id],
    ))


def enact(session, target_id, proposal_id):
    return resolve_intent(session, Intent(
        entity_id=target_id, intent_type="enact",
        params={"proposal_id": proposal_id},
        resource_ids=[proposal_id],
    ))


def fiscal_mutation(rate):
    return {"type": "set_fiscal_policy",
            "params": {"policy": json.dumps({"rate": str(rate)})}}


def script_mutation(lineage, source, stype="policy", entity_id=None):
    params = {"script_type": stype, "lineage_id": lineage, "source": source}
    if entity_id:
        params["entity_id"] = entity_id
    return {"type": "set_script", "params": params}


def make_script(session, name, source, script_type, entity=None):
    s = Script(name=name, source=source, script_type=script_type,
               entity_id=entity.id if entity else None)
    session.add(s)
    session.flush()
    return s


def proposal_id_of(result):
    return result["proposal_id"]


# ---------------------------------------------------------------------------
# weight model — the electorate is data
# ---------------------------------------------------------------------------

def test_citizen_electorate_is_active_individuals_only(world, session):
    elect = weights.electorate(session, "citizen")
    ids = set(elect)
    assert {world["a"].id, world["b"].id, world["c"].id} <= ids
    assert world["biz"].id not in ids      # business is not a citizen
    assert world["gov"].id not in ids      # government is not a citizen
    assert all(w == 1 for w in elect.values())  # one person, one vote


def test_citizen_weight_distinguishes_membership(world, session):
    assert weights.weight_of(session, "citizen", world["a"].id) == 1
    assert weights.weight_of(session, "citizen", world["biz"].id) == 0
    assert weights.weight_of(session, "citizen", "nonexistent") == 0
    # an incapacitated individual leaves the electorate (death withdraws the vote)
    world["a"].status = "incapacitated"
    session.flush()
    assert weights.weight_of(session, "citizen", world["a"].id) == 0


def test_unknown_weight_model_raises():
    with pytest.raises(ValueError, match="unknown weight model"):
        weights.electorate(None, "oligarchy")
    with pytest.raises(ValueError, match="unknown weight model"):
        weights.weight_of(None, "aristocracy", "x")


# ---------------------------------------------------------------------------
# create_proposal
# ---------------------------------------------------------------------------

def test_create_proposal_opens_a_proposal(world, session):
    r = propose(session, world["a"].id, world["gov"].id,
                [fiscal_mutation(0.1)], title="flat tax")
    assert r["status"] == "applied", r
    p = session.get(Proposal, r["proposal_id"])
    assert p.status == ProposalStatus.OPEN
    assert p.proposer_id == world["a"].id
    assert p.target_id == world["gov"].id
    assert p.weight_model == "citizen"
    assert p.mutations == [fiscal_mutation(0.1)]
    assert p.threshold == "0.5"


def test_create_proposal_rejects_non_citizen(world, session):
    # a business is not in the citizen electorate
    r = propose(session, world["biz"].id, world["gov"].id, [fiscal_mutation(0.1)])
    assert r["status"] == "rejected"
    assert "electorate" in r["reason"]


def test_create_proposal_rejects_unknown_weight_model(world, session):
    r = propose(session, world["a"].id, world["gov"].id,
                [fiscal_mutation(0.1)], weight_model="aristocracy")
    assert r["status"] == "rejected"
    assert "unknown weight model" in r["reason"]


def test_create_proposal_rejects_malformed_mutations(world, session):
    assert propose(session, world["a"].id, world["gov"].id, [])["status"] == "rejected"
    assert propose(session, world["a"].id, world["gov"].id,
                   [{}])["status"] == "rejected"                      # no type/params
    assert propose(session, world["a"].id, world["gov"].id,
                   [{"type": "set_fiscal_policy", "params": "x"}])["status"] == "rejected"


def test_create_proposal_rejects_unknown_target(world, session):
    r = propose(session, world["a"].id, "no-such-entity", [fiscal_mutation(0.1)])
    assert r["status"] == "rejected"
    assert "target" in r["reason"]


# ---------------------------------------------------------------------------
# vote
# ---------------------------------------------------------------------------

def test_vote_records_for_and_against_with_snapshot_weight(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id, [fiscal_mutation(0.1)]))
    assert vote(session, world["a"].id, pid, "for")["status"] == "applied"
    assert vote(session, world["b"].id, pid, "against")["status"] == "applied"
    votes = session.query(Vote).filter_by(proposal_id=pid).all()
    assert {v.choice for v in votes} == {VoteChoice.FOR, VoteChoice.AGAINST}
    assert all(v.weight == "1" for v in votes)   # snapshot at cast time


def test_vote_is_idempotent_on_same_choice(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id, [fiscal_mutation(0.1)]))
    first = vote(session, world["b"].id, pid, "for")
    second = vote(session, world["b"].id, pid, "for")
    assert first["status"] == "applied" and second["status"] == "applied"
    assert first["vote_id"] == second["vote_id"]   # same row
    assert session.query(Vote).filter_by(proposal_id=pid).count() == 1


def test_vote_rejects_changing_choice(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id, [fiscal_mutation(0.1)]))
    assert vote(session, world["b"].id, pid, "for")["status"] == "applied"
    r = vote(session, world["b"].id, pid, "against")
    assert r["status"] == "rejected"
    assert "already voted" in r["reason"]


def test_vote_rejects_non_citizen(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id, [fiscal_mutation(0.1)]))
    r = vote(session, world["biz"].id, pid, "for")
    assert r["status"] == "rejected"
    assert "electorate" in r["reason"]


def test_vote_rejects_bad_choice_and_unknown_proposal(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id, [fiscal_mutation(0.1)]))
    assert "must be" in vote(session, world["b"].id, pid, "maybe")["reason"]
    assert "unknown proposal" in vote(session, world["b"].id, "nope", "for")["reason"]


def test_vote_rejects_non_open_proposal(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id, [fiscal_mutation(0.1)]))
    p = session.get(Proposal, pid)
    p.status = ProposalStatus.ENACTED          # close it by hand
    session.flush()
    r = vote(session, world["b"].id, pid, "for")
    assert r["status"] == "rejected"
    assert "not open" in r["reason"]


# ---------------------------------------------------------------------------
# enact — the tally and atomic application
# ---------------------------------------------------------------------------

def _elect_and_vote_3(session, world, a_choice, b_choice, c_choice, quorum="0"):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id,
                                 [fiscal_mutation(0.1)], quorum=quorum))
    for who, ch in ((world["a"], a_choice), (world["b"], b_choice), (world["c"], c_choice)):
        vote(session, who.id, pid, ch)
    return pid


def test_enact_passes_on_simple_majority(world, session):
    pid = _elect_and_vote_3(session, world, "for", "for", "against")
    r = enact(session, world["gov"].id, pid)
    assert r["status"] == "applied", r
    assert r["proposal_status"] == "enacted"
    assert r["tally_yes"] == "2" and r["tally_no"] == "1"
    assert session.get(Proposal, pid).status == ProposalStatus.ENACTED


def test_enact_fails_below_threshold(world, session):
    pid = _elect_and_vote_3(session, world, "for", "against", "against")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "failed"
    assert session.get(Proposal, pid).status == ProposalStatus.FAILED
    assert "threshold" in session.get(Proposal, pid).failure_reason


def test_enact_fails_below_quorum(world, session):
    # only one of three citizens votes -> turnout 1/3 < 0.9 quorum
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id,
                                 [fiscal_mutation(0.1)], quorum="0.9"))
    vote(session, world["a"].id, pid, "for")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "failed"
    assert "threshold or quorum" in session.get(Proposal, pid).failure_reason


def test_enact_applies_set_script_mutation_enacting_a_law(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id,
                                 [script_mutation("wealth_tax", "-- a wealth tax")]))
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "for")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "enacted"
    active = [s for s in session.query(Script).filter_by(lineage_id="wealth_tax") if s.is_active]
    assert len(active) == 1                    # the vote enacted a law


def test_enact_validator_veto_is_the_constitutional_backstop(world, session):
    """A citizen-passed proposal to set an over-cap rate is still vetoed by
    the VALIDATOR — the constitution binds enacted law exactly as it binds
    operator law (the safety thesis)."""
    make_script(session, "cap", """
      if ctx.op.type == "set_fiscal_policy" then
        local r = tonumber(ctx.op.policy.rate)
        if r and r > 0.5 then
          return {allow = false, reason = "rate over constitutional cap"}
        end
      end
    """, ScriptType.VALIDATOR)
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id,
                                 [fiscal_mutation(0.9)]))   # over the cap
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "for")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "failed"
    assert "constitutional cap" in session.get(Proposal, pid).failure_reason
    # and policy was never changed
    from econengine import fiscal
    assert fiscal.get_fiscal_policy(session) == {}


def test_enact_is_atomic_veto_rolls_back_prior_mutations(world, session):
    """Two mutations: the first enacts a law, the second is vetoed. The whole
    enactment rolls back — no law created, no policy set (all-or-nothing)."""
    make_script(session, "cap", """
      if ctx.op.type == "set_fiscal_policy" then
        local r = tonumber(ctx.op.policy.rate)
        if r and r > 0.5 then
          return {allow = false, reason = "rate over constitutional cap" }
        end
      end
    """, ScriptType.VALIDATOR)
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id, [
        script_mutation("newlaw", "-- should be rolled back"),   # would apply...
        fiscal_mutation(0.9),                                    # ...but this is vetoed
    ]))
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "for")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "failed"
    # atomicity: mutation 1 was rolled back with mutation 2
    assert session.query(Script).filter_by(lineage_id="newlaw").count() == 0
    from econengine import fiscal
    assert fiscal.get_fiscal_policy(session) == {}


def test_enact_rejects_non_target_government(world, session):
    # a second legislating government is not this proposal's target
    other = create_entity(session, "OtherGov", EntityType.GOVERNMENT)
    other.capabilities = [capabilities.LEGISLATE]
    session.flush()
    pid = _elect_and_vote_3(session, world, "for", "for", "against")
    r = enact(session, other.id, pid)
    assert r["status"] == "rejected"
    assert "target" in r["reason"]


def test_enact_requires_legislate_capability(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["plain"].id,
                                 [fiscal_mutation(0.1)]))
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "for")
    r = enact(session, world["plain"].id, pid)   # plain gov has no legislate
    assert r["status"] == "rejected"
    assert "legislate" in r["reason"]


def test_enact_rejects_non_open_proposal(world, session):
    pid = _elect_and_vote_3(session, world, "for", "for", "against")
    assert enact(session, world["gov"].id, pid)["proposal_status"] == "enacted"
    again = enact(session, world["gov"].id, pid)   # already enacted
    assert again["status"] == "rejected"
    assert "not open" in again["reason"]


# ---------------------------------------------------------------------------
# integration — the full self-governing cycle
# ---------------------------------------------------------------------------

def test_full_cycle_citizens_enact_fiscal_policy(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id,
                                 [fiscal_mutation(0.2)], title="20% tax"))
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "for")
    vote(session, world["c"].id, pid, "against")
    assert enact(session, world["gov"].id, pid)["proposal_status"] == "enacted"
    from econengine import fiscal
    assert fiscal.get_fiscal_policy(session) == {"rate": "0.2"}


def test_enacted_law_runs_on_the_next_tick(world, session):
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id,
                                 [script_mutation("marker", "ctx.state.live = 'yes'",
                                                  entity_id=world["gov"].id)]))
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "for")
    enact(session, world["gov"].id, pid)
    run_tick(session)                           # the citizen-enacted law now runs
    law = [s for s in session.query(Script).filter_by(lineage_id="marker") if s.is_active][0]
    assert law.state.get("live") == "yes"


def test_a_citizen_behaviour_script_can_propose(world, session):
    """The Lua bridge: a citizen's BEHAVIOUR script drives the cycle via
    ctx.action.create_proposal, drained through resolve_intent by the tick."""
    gov_id = world["gov"].id
    src = (
        "local m = {}\n"
        "m[1] = {type='set_fiscal_policy', params={policy='{\"rate\":\"0.15\"}'}}\n"
        f"ctx.action.create_proposal('{gov_id}', m, 'citizen', '0.5', '0', 'script-proposed')\n"
    )
    make_script(session, "agitant", src, ScriptType.BEHAVIOUR, entity=world["a"])
    run_tick(session)
    proposals = session.query(Proposal).filter_by(proposer_id=world["a"].id).all()
    assert len(proposals) == 1
    assert proposals[0].status == ProposalStatus.OPEN


# ---------------------------------------------------------------------------
# ctx.query — the read side
# ---------------------------------------------------------------------------

def test_ctx_query_proposal_and_proposals(world, session):
    q = build_queries(session)
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id,
                                 [fiscal_mutation(0.1)], title="t1"))
    propose(session, world["b"].id, world["gov"].id, [fiscal_mutation(0.2)], title="t2")
    one = q["proposal"](pid)
    assert one["title"] == "t1" and one["status"] == "open"
    assert [p["title"] for p in q["proposals"]()] == ["t1", "t2"]
    assert [p["title"] for p in q["proposals"]("open")] == ["t1", "t2"]
    assert q["proposals"]("enacted") == []
    assert q["proposal"]("nope") is None


def test_ctx_query_tally_is_live(world, session):
    q = build_queries(session)
    pid = proposal_id_of(propose(session, world["a"].id, world["gov"].id,
                                 [fiscal_mutation(0.1)], quorum="0"))
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "against")
    t = q["tally"](pid)
    assert t["yes"] == "1" and t["no"] == "1"
    assert t["electorate"] == "3"               # three active citizens
    assert Decimal(t["turnout"]) == Decimal(2) / Decimal(3)
