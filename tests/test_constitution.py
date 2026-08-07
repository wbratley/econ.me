"""The constitutional tier — amend_constitution (actors step 4b / 4a-4).

Above ordinary law sits the constitution: the VALIDATOR scripts (the
constraints ordinary law must clear) and the voting-system floor (the
threshold/quorum a constitutional amendment itself must clear). Both are
amendable only through a constitutional proposal — set_validator /
set_constitution, gated by the ``amend_constitution`` capability and bound
by a supermajority. This is what keeps "vote on code" safe at the top of
the stack: the legislature can change anything *except* the rules that
bind it, and those move only by a harder bar.

What this file proves end to end:

  - the two tiers never cross — an ordinary proposal cannot carry a
    validator/constitution mutation, and a constitutional one cannot carry
    ordinary law (enforced at propose time);
  - set_script still cannot touch validators — set_validator is the only
    path, and it needs amend_constitution;
  - enacting a constitutional proposal needs amend_constitution (not just
    legislate) and must clear the supermajority floor — a simple majority
    on a constitutional amendment fails, unanimity passes;
  - a validator installed by amendment binds the very next op, including a
    later mutation in the same enactment (atomic), and a subsequent direct
    op (persistent) — the constitution is live, not advisory;
  - the floor is itself amendable (set_constitution), and a lowered floor
    governs the next amendment; a validator may veto even that.
"""
import json

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities, constitution
from econengine.lua_engine import Intent
from econengine.models import (
    Base, EntityType, Proposal, ProposalStatus, ProposalType, Script, ScriptType,
)
from econengine.scripting import build_queries, resolve_intent
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
def world(session):
    """A government that may legislate, set fiscal policy, AND amend the
    constitution; three citizens (the electorate); a government that may
    legislate but NOT amend (to prove the gate); a business (not a citizen)."""
    gov = create_entity(session, "Gov", EntityType.GOVERNMENT)
    gov.capabilities = [
        capabilities.LEGISLATE,
        capabilities.SET_FISCAL_POLICY,
        capabilities.AMEND_CONSTITUTION,
    ]
    a = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    b = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    c = create_entity(session, "Carol", EntityType.INDIVIDUAL)
    leg_only = create_entity(session, "LegOnly", EntityType.GOVERNMENT)
    leg_only.capabilities = [capabilities.LEGISLATE]
    biz = create_entity(session, "Acme", EntityType.BUSINESS)
    session.flush()
    return {"gov": gov, "a": a, "b": b, "c": c, "leg_only": leg_only, "biz": biz}


# ---------------------------------------------------------------------------
# intent helpers
# ---------------------------------------------------------------------------

def propose_c(session, proposer_id, target_id, mutations, title="",
              weight_model="citizen", threshold="0.5", quorum="0"):
    """create_proposal with proposal_type=constitutional."""
    return resolve_intent(session, Intent(
        entity_id=proposer_id, intent_type="create_proposal",
        params={"target_id": target_id, "mutations": json.dumps(mutations),
                "weight_model": weight_model, "threshold": threshold,
                "quorum": quorum, "title": title, "proposal_type": "constitutional"},
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


def validator_mutation(lineage, source, entity_id=None):
    params = {"lineage_id": lineage, "source": source}
    if entity_id:
        params["entity_id"] = entity_id
    return {"type": "set_validator", "params": params}


def constitution_mutation(threshold=None, quorum=None):
    d = {}
    if threshold is not None:
        d["supermajority_threshold"] = str(threshold)
    if quorum is not None:
        d["supermajority_quorum"] = str(quorum)
    return {"type": "set_constitution",
            "params": {"constitution": json.dumps(d)}}


def pid_of(result):
    return result["proposal_id"]


def active_validator(session, lineage):
    return session.query(Script).filter_by(
        lineage_id=lineage, is_active=True, script_type=ScriptType.VALIDATOR
    ).first()


CAP_VALIDATOR = """
  if ctx.op.type == "set_fiscal_policy" then
    local r = tonumber(ctx.op.policy.rate)
    if r and r > 0.5 then
      return {allow = false, reason = "rate over the constitutional cap"}
    end
  end
"""

# a validator that forbids lowering the supermajority below two-thirds
FLOOR_FLOOR_VALIDATOR = """
  if ctx.op.type == "set_constitution" then
    local t = tonumber(ctx.op.constitution.supermajority_threshold)
    if t and t < 0.67 then
      return {allow = false, reason = "supermajority may not fall below 2/3"}
    end
  end
"""


# ---------------------------------------------------------------------------
# the constitution in force
# ---------------------------------------------------------------------------

def test_default_constitution_is_two_thirds_no_quorum(session):
    c = constitution.get_constitution(session)
    assert c["supermajority_threshold"] == "0.67"
    assert c["supermajority_quorum"] == "0"


def test_ctx_query_constitution_reads_the_floor(session):
    assert build_queries(session)["constitution"]() == {
        "supermajority_threshold": "0.67", "supermajority_quorum": "0"
    }


# ---------------------------------------------------------------------------
# set_validator / set_constitution are the only path to the constitution
# ---------------------------------------------------------------------------

def test_set_script_still_cannot_touch_validators(world, session):
    # ordinary legislation (legislate) is kept away from validators, even
    # when the authority also holds amend_constitution
    r = resolve_intent(session, Intent(
        entity_id=world["gov"].id, intent_type="set_script",
        params={"script_type": "validator", "lineage_id": "cap", "source": "return false"},
        resource_ids=["cap"],
    ))
    assert r["status"] == "rejected"
    assert "validator" in r["reason"].lower()


def test_set_validator_requires_amend_constitution(world, session):
    # a government that may legislate but not amend cannot write a validator
    r = resolve_intent(session, Intent(
        entity_id=world["leg_only"].id, intent_type="set_validator",
        params={"lineage_id": "cap", "source": CAP_VALIDATOR},
        resource_ids=["cap"],
    ))
    assert r["status"] == "rejected"
    assert "amend_constitution" in r["reason"]


def test_set_validator_installs_a_validator_directly(world, session):
    r = resolve_intent(session, Intent(
        entity_id=world["gov"].id, intent_type="set_validator",
        params={"lineage_id": "cap", "source": CAP_VALIDATOR},
        resource_ids=["cap"],
    ))
    assert r["status"] == "applied"
    assert active_validator(session, "cap") is not None


def test_set_constitution_requires_amend_constitution(world, session):
    r = resolve_intent(session, Intent(
        entity_id=world["leg_only"].id, intent_type="set_constitution",
        params={"constitution": json.dumps({"supermajority_threshold": "0.6"})},
        resource_ids=[],
    ))
    assert r["status"] == "rejected"
    assert "amend_constitution" in r["reason"]


def test_set_constitution_replaces_the_floor(world, session):
    resolve_intent(session, Intent(
        entity_id=world["gov"].id, intent_type="set_constitution",
        params={"constitution": json.dumps({"supermajority_threshold": "0.6"})},
        resource_ids=[],
    ))
    assert constitution.get_constitution(session)["supermajority_threshold"] == "0.6"


# ---------------------------------------------------------------------------
# the two tiers never cross (enforced at propose time)
# ---------------------------------------------------------------------------

def test_constitutional_proposal_carries_a_validator_mutation(world, session):
    r = propose_c(session, world["a"].id, world["gov"].id,
                  [validator_mutation("cap", CAP_VALIDATOR)])
    assert r["status"] == "applied"
    p = session.get(Proposal, r["proposal_id"])
    assert p.proposal_type == ProposalType.CONSTITUTIONAL


def test_ordinary_proposal_rejects_a_validator_mutation(world, session):
    # an ordinary (default) proposal cannot smuggle in a validator write
    r = resolve_intent(session, Intent(
        entity_id=world["a"].id, intent_type="create_proposal",
        params={"target_id": world["gov"].id,
                "mutations": json.dumps([validator_mutation("cap", CAP_VALIDATOR)]),
                "weight_model": "citizen"},
        resource_ids=[world["gov"].id],
    ))
    assert r["status"] == "rejected"
    assert "not allowed for ordinary" in r["reason"]


def test_constitutional_proposal_may_also_carry_ordinary_law(world, session):
    # the hierarchy: a constitutional amendment (a harder bar) may bundle
    # ordinary law. Here it installs a cap AND sets a compliant rate in one
    # enactment — both apply at unanimity.
    pid = pid_of(propose_c(session, world["a"].id, world["gov"].id, [
        validator_mutation("cap", CAP_VALIDATOR),
        {"type": "set_fiscal_policy",
         "params": {"policy": json.dumps({"rate": "0.4"})}},  # under the cap
    ]))
    for who in (world["a"], world["b"], world["c"]):
        vote(session, who.id, pid, "for")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "enacted"
    assert active_validator(session, "cap") is not None
    from econengine import fiscal
    assert fiscal.get_fiscal_policy(session) == {"rate": "0.4"}


# ---------------------------------------------------------------------------
# enactment gate + supermajority floor
# ---------------------------------------------------------------------------

def test_enact_constitutional_requires_amend_constitution(world, session):
    pid = pid_of(propose_c(session, world["a"].id, world["leg_only"].id,
                           [validator_mutation("cap", CAP_VALIDATOR)]))
    for who in (world["a"], world["b"], world["c"]):
        vote(session, who.id, pid, "for")
    r = enact(session, world["leg_only"].id, pid)   # legislate but not amend
    assert r["status"] == "rejected"
    assert "amend_constitution" in r["reason"]


def test_constitutional_amendment_fails_at_simple_majority(world, session):
    # 2-for-1-against is a simple majority but below the 2/3 floor
    pid = pid_of(propose_c(session, world["a"].id, world["gov"].id,
                           [validator_mutation("cap", CAP_VALIDATOR)]))
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "for")
    vote(session, world["c"].id, pid, "against")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "failed"
    assert active_validator(session, "cap") is None    # nothing applied


def test_constitutional_amendment_passes_at_unanimity(world, session):
    pid = pid_of(propose_c(session, world["a"].id, world["gov"].id,
                           [validator_mutation("cap", CAP_VALIDATOR)],
                           title="cap fiscal rate"))
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "for")
    vote(session, world["c"].id, pid, "for")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "enacted"
    assert active_validator(session, "cap") is not None


def test_floor_uses_the_constitution_in_force_not_the_proposals_own_bar(world, session):
    # the proposer writes threshold 0.5 on a constitutional proposal, but
    # the 0.67 floor binds: 2-for-1-against would pass at 0.5 (2/3 of cast)
    # yet fails the floor (2 < 0.67*3). Nothing applies.
    pid = pid_of(propose_c(session, world["a"].id, world["gov"].id,
                           [validator_mutation("cap", CAP_VALIDATOR)],
                           threshold="0.5"))
    vote(session, world["a"].id, pid, "for")
    vote(session, world["b"].id, pid, "for")
    vote(session, world["c"].id, pid, "against")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "failed"
    assert active_validator(session, "cap") is None


# ---------------------------------------------------------------------------
# an installed validator is live, not advisory
# ---------------------------------------------------------------------------

def test_installed_validator_binds_a_later_mutation_in_the_same_enactment(world, session):
    # the proposal installs the cap, then sets an over-cap rate: the second
    # mutation is vetoed by the just-installed validator, so the whole
    # enactment rolls back (atomic) — the constitution takes effect mid-vote.
    pid = pid_of(propose_c(session, world["a"].id, world["gov"].id, [
        validator_mutation("cap", CAP_VALIDATOR),
        {"type": "set_fiscal_policy",
         "params": {"policy": json.dumps({"rate": "0.9"})}},  # over the cap
    ]))
    for who in (world["a"], world["b"], world["c"]):
        vote(session, who.id, pid, "for")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "failed"
    assert active_validator(session, "cap") is None       # rolled back
    from econengine import fiscal
    assert fiscal.get_fiscal_policy(session) == {}        # not applied


def test_installed_validator_binds_a_subsequent_direct_op(world, session):
    # pass the amendment at unanimity; then a direct over-cap set_fiscal_policy
    # by the same government is vetoed — the constitution persists.
    pid = pid_of(propose_c(session, world["a"].id, world["gov"].id,
                           [validator_mutation("cap", CAP_VALIDATOR)]))
    for who in (world["a"], world["b"], world["c"]):
        vote(session, who.id, pid, "for")
    enact(session, world["gov"].id, pid)

    r = resolve_intent(session, Intent(
        entity_id=world["gov"].id, intent_type="set_fiscal_policy",
        params={"policy": json.dumps({"rate": "0.9"})},   # over the cap
        resource_ids=[],
    ))
    assert r["status"] == "rejected"
    assert "cap" in r["reason"]


# ---------------------------------------------------------------------------
# the floor is itself amendable (and a validator may guard it)
# ---------------------------------------------------------------------------

def test_set_constitution_amendment_lowers_the_future_floor(world, session):
    # 1) lower the floor to 0.5 — needs unanimity under the 0.67 default
    pid1 = pid_of(propose_c(session, world["a"].id, world["gov"].id,
                            [constitution_mutation(threshold="0.5")]))
    for who in (world["a"], world["b"], world["c"]):
        vote(session, who.id, pid1, "for")
    assert enact(session, world["gov"].id, pid1)["proposal_status"] == "enacted"
    assert constitution.get_constitution(session)["supermajority_threshold"] == "0.5"

    # 2) a subsequent amendment now passes on a simple majority (2-for)
    pid2 = pid_of(propose_c(session, world["a"].id, world["gov"].id,
                            [validator_mutation("other", "return true")]))
    vote(session, world["a"].id, pid2, "for")
    vote(session, world["b"].id, pid2, "for")
    vote(session, world["c"].id, pid2, "against")
    assert enact(session, world["gov"].id, pid2)["proposal_status"] == "enacted"


def test_validator_can_veto_a_constitution_amendment(world, session):
    # plant a validator (directly, by the capable government) that guards the
    # floor, then a vote to lower it is vetoed even at unanimity.
    resolve_intent(session, Intent(
        entity_id=world["gov"].id, intent_type="set_validator",
        params={"lineage_id": "floor_guard", "source": FLOOR_FLOOR_VALIDATOR},
        resource_ids=["floor_guard"],
    ))
    pid = pid_of(propose_c(session, world["a"].id, world["gov"].id,
                           [constitution_mutation(threshold="0.5")]))
    for who in (world["a"], world["b"], world["c"]):
        vote(session, who.id, pid, "for")
    r = enact(session, world["gov"].id, pid)
    assert r["proposal_status"] == "failed"
    assert constitution.get_constitution(session)["supermajority_threshold"] == "0.67"


# ---------------------------------------------------------------------------
# ctx.query read side reflects amendments
# ---------------------------------------------------------------------------

def test_ctx_query_constitution_reflects_an_amendment(world, session):
    resolve_intent(session, Intent(
        entity_id=world["gov"].id, intent_type="set_constitution",
        params={"constitution": json.dumps({"supermajority_threshold": "0.75"})},
        resource_ids=[],
    ))
    assert build_queries(session)["constitution"]()["supermajority_threshold"] == "0.75"


def test_proposal_read_view_exposes_its_tier(world, session):
    pid = pid_of(propose_c(session, world["a"].id, world["gov"].id,
                           [validator_mutation("cap", CAP_VALIDATOR)]))
    view = build_queries(session)["proposal"](pid)
    assert view["proposal_type"] == "constitutional"
