"""Shareholder governance — the `share` weight model (actors step 4c).

A corporation is just a different row in the weight-model registry: the
electorate is the holders of a symbol (the cap table), each weighted by
shares held, deciding with a majority of shares. The proposal → vote →
enact machinery is untouched — only the question "who votes, and how
much?" changes, which is the whole point of "forms of government are
data, not mechanism" (docs/actors.md).

What this proves end to end:

  - the `share` electorate is the live cap table (holders of a symbol,
    weighted by quantity; reusing the same register
    `ctx.query.holders` exposes), and a share traded mid-vote changes
    both who can vote and how much — no stale snapshot;
  - participation is membership: a non-holder cannot propose or vote;
  - the directive an enacted share-weighted proposal carries is an
    ordinary `set_script` (a BEHAVIOUR script bound to the firm) — so the
    firm needs `legislate` (a capability grant, data not code), and the
    enacted script runs as the firm on the next tick;
  - the tally is threshold-of-cast-share-weight AND quorum-of-shares, so a
    minority shareholder is outvoted by a majority of shares and a high
    threshold can block even a 70% holder.
"""
import json

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities, weights
from econengine.lua_engine import Intent
from econengine.markets import adjust_holding
from econengine.models import (
    Base, EntityType, Proposal, Script, ScriptType,
)
from econengine.scripting import resolve_intent
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
def cap(session):
    """A firm whose shareholders govern it (it holds the `legislate`
    capability — the data that lets an enacted directive bind its
    behaviour script), and a 30/70 split of its shares between two
    individuals. Carol holds none. A second symbol (``OTHER``) is held by
    Alice to prove electorates are per-symbol. The session rides along so
    tests reach it without reaching into ORM internals."""
    firm = create_entity(session, "AcmeCorp", EntityType.BUSINESS)
    firm.capabilities = [capabilities.LEGISLATE]
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    carol = create_entity(session, "Carol", EntityType.INDIVIDUAL)
    adjust_holding(session, alice, "ACME", Decimal("30"))
    adjust_holding(session, bob, "ACME", Decimal("70"))
    adjust_holding(session, alice, "OTHER", Decimal("100"))   # a different firm
    session.flush()
    return {"s": session, "firm": firm, "alice": alice,
            "bob": bob, "carol": carol}


# ---------------------------------------------------------------------------
# intent helpers
# ---------------------------------------------------------------------------

def propose_share(s, proposer_id, target_id, symbol, mutations, title="",
                  threshold="0.5", quorum="0"):
    return resolve_intent(s, Intent(
        entity_id=proposer_id, intent_type="create_proposal",
        params={"target_id": target_id, "mutations": json.dumps(mutations),
                "weight_model": f"share:{symbol}", "threshold": threshold,
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


def behaviour_mutation(lineage, source, firm_id):
    """An enacted directive: a BEHAVIOUR script bound to the firm. This is
    what `set_script` already does — step 4c adds no new mutation type."""
    return {"type": "set_script", "params": {
        "script_type": "behaviour", "lineage_id": lineage,
        "source": source, "entity_id": firm_id}}


def pid_of(result):
    return result["proposal_id"]


# ===========================================================================
# the resolver — the electorate is the cap table
# ===========================================================================

def test_share_electorate_is_holders_weighted_by_quantity(cap):
    assert weights.electorate(cap["s"], "share:ACME") == {
        cap["alice"].id: Decimal("30"), cap["bob"].id: Decimal("70")}


def test_share_weight_reflects_quantity_held(cap):
    s = cap["s"]
    assert weights.weight_of(s, "share:ACME", cap["alice"].id) == Decimal("30")
    assert weights.weight_of(s, "share:ACME", cap["bob"].id) == Decimal("70")
    assert weights.weight_of(s, "share:ACME", cap["carol"].id) == Decimal("0")


def test_share_electorate_is_per_symbol(cap):
    # OTHER is held only by Alice; the ACME-only holders are not in it
    assert weights.electorate(cap["s"], "share:OTHER") == {
        cap["alice"].id: Decimal("100")}


def test_share_symbol_is_case_insensitive(cap):
    # symbols are stored upper-cased (adjust_holding); the scope matches
    assert weights.electorate(cap["s"], "share:acme") == {
        cap["alice"].id: Decimal("30"), cap["bob"].id: Decimal("70")}


def test_share_electorate_is_live(cap):
    # the cap table is read at vote time, so a trade mid-vote moves both
    # membership and weight — no stale snapshot
    s = cap["s"]
    assert weights.electorate(s, "share:ACME") == {
        cap["alice"].id: Decimal("30"), cap["bob"].id: Decimal("70")}
    adjust_holding(s, cap["bob"], "ACME", Decimal("-20"))   # bob sells 20
    adjust_holding(s, cap["carol"], "ACME", Decimal("20"))  # carol buys 20
    assert weights.electorate(s, "share:ACME") == {
        cap["alice"].id: Decimal("30"), cap["bob"].id: Decimal("50"),
        cap["carol"].id: Decimal("20")}


def test_zero_quantity_holder_is_not_in_the_electorate(cap):
    s = cap["s"]
    adjust_holding(s, cap["carol"], "ACME", Decimal("0"))   # explicit zero row
    assert weights.weight_of(s, "share:ACME", cap["carol"].id) == Decimal("0")
    assert cap["carol"].id not in weights.electorate(s, "share:ACME")


def test_share_spec_without_a_symbol_is_rejected(cap):
    s = cap["s"]
    with pytest.raises(ValueError, match="symbol"):
        weights.electorate(s, "share")
    with pytest.raises(ValueError, match="symbol"):
        weights.weight_of(s, "share", cap["alice"].id)


def test_unknown_weight_model_name_still_raises(cap):
    with pytest.raises(ValueError, match="unknown weight model"):
        weights.electorate(cap["s"], "bogus:ACME")


# ===========================================================================
# the governance cycle — propose / vote / enact by share weight
# ===========================================================================

def test_shareholder_can_propose(cap):
    r = propose_share(cap["s"], cap["alice"].id, cap["firm"].id, "ACME",
                      [behaviour_mutation("strategy", "ctx.state.directed='yes'",
                                          cap["firm"].id)])
    assert r["status"] == "applied"
    assert cap["s"].get(Proposal, r["proposal_id"]).weight_model == "share:ACME"


def test_non_shareholder_cannot_propose(cap):
    r = propose_share(cap["s"], cap["carol"].id, cap["firm"].id, "ACME",
                      [behaviour_mutation("s", "ctx.state.x=1", cap["firm"].id)])
    assert r["status"] == "rejected"
    assert "electorate" in r["reason"]


def test_non_shareholder_cannot_vote(cap):
    pid = pid_of(propose_share(
        cap["s"], cap["alice"].id, cap["firm"].id, "ACME",
        [behaviour_mutation("s", "ctx.state.x=1", cap["firm"].id)]))
    r = vote(cap["s"], cap["carol"].id, pid, "for")
    assert r["status"] == "rejected"
    assert "electorate" in r["reason"]


def test_vote_weights_snapshot_shares_held(cap):
    pid = pid_of(propose_share(
        cap["s"], cap["alice"].id, cap["firm"].id, "ACME",
        [behaviour_mutation("s", "ctx.state.x=1", cap["firm"].id)]))
    ra = vote(cap["s"], cap["alice"].id, pid, "for")
    rb = vote(cap["s"], cap["bob"].id, pid, "for")
    assert Decimal(ra["weight"]) == Decimal("30")
    assert Decimal(rb["weight"]) == Decimal("70")


def test_share_weight_decides_a_contested_vote_both_ways(cap):
    s = cap["s"]
    # the threshold is a fraction of CAST share weight that's FOR, so the
    # side carrying more shares wins a split vote — both directions.
    p_against = pid_of(propose_share(s, cap["alice"].id, cap["firm"].id, "ACME",
        [behaviour_mutation("m", "ctx.state.v=1", cap["firm"].id)], threshold="0.5"))
    vote(s, cap["alice"].id, p_against, "for")       # 30 FOR
    vote(s, cap["bob"].id, p_against, "against")     # 70 against -> 30/100 fails
    assert enact(s, cap["firm"].id, p_against)["proposal_status"] == "failed"

    p_for = pid_of(propose_share(s, cap["bob"].id, cap["firm"].id, "ACME",
        [behaviour_mutation("m2", "ctx.state.v=2", cap["firm"].id)], threshold="0.5"))
    vote(s, cap["bob"].id, p_for, "for")             # 70 FOR
    vote(s, cap["alice"].id, p_for, "against")       # 30 against -> 70/100 passes
    assert enact(s, cap["firm"].id, p_for)["proposal_status"] == "enacted"


def test_minority_shareholder_is_outvoted(cap):
    s = cap["s"]
    pid = pid_of(propose_share(s, cap["alice"].id, cap["firm"].id, "ACME",
        [behaviour_mutation("m", "ctx.state.v=1", cap["firm"].id)], threshold="0.5"))
    vote(s, cap["alice"].id, pid, "against")    # 30 against
    vote(s, cap["bob"].id, pid, "for")          # 70 for -> 70/100 >= 0.5
    assert enact(s, cap["firm"].id, pid)["proposal_status"] == "enacted"


def test_high_threshold_blocks_even_a_seventy_percent_holder(cap):
    s = cap["s"]
    # a 0.75 threshold on shares: Bob's 70% FOR is short of three-quarters.
    pid = pid_of(propose_share(s, cap["bob"].id, cap["firm"].id, "ACME",
        [behaviour_mutation("m", "ctx.state.v=1", cap["firm"].id)], threshold="0.75"))
    vote(s, cap["bob"].id, pid, "for")          # 70/100 = 0.7 < 0.75
    vote(s, cap["alice"].id, pid, "against")    # 30 against
    assert enact(s, cap["firm"].id, pid)["proposal_status"] == "failed"


def test_quorum_is_a_fraction_of_outstanding_shares(cap):
    s = cap["s"]
    # 100 shares outstanding; quorum 0.5 needs >= 50 cast. Only Alice votes
    # (30 cast = 0.3 turnout), so even unanimous-FOR fails quorum.
    pid = pid_of(propose_share(s, cap["alice"].id, cap["firm"].id, "ACME",
        [behaviour_mutation("m", "ctx.state.v=1", cap["firm"].id)],
        threshold="0.5", quorum="0.5"))
    vote(s, cap["alice"].id, pid, "for")
    assert enact(s, cap["firm"].id, pid)["proposal_status"] == "failed"


# ===========================================================================
# the directive binds the firm — enacted code runs as the firm next tick
# ===========================================================================

def test_enacted_directive_runs_as_the_firm_next_tick(cap):
    s = cap["s"]
    pid = pid_of(propose_share(
        s, cap["bob"].id, cap["firm"].id, "ACME",
        [behaviour_mutation("strategy", "ctx.state.directed='by-shareholders'",
                            cap["firm"].id)],
        title="pivot to services"))
    vote(s, cap["bob"].id, pid, "for")          # 70 of 100 -> majority
    assert enact(s, cap["firm"].id, pid)["proposal_status"] == "enacted"

    run_tick(s)                                   # the directive now runs
    law = [x for x in s.query(Script).filter_by(lineage_id="strategy") if x.is_active][0]
    assert law.entity_id == cap["firm"].id
    assert law.script_type == ScriptType.BEHAVIOUR
    assert law.state.get("directed") == "by-shareholders"


def test_two_directives_on_one_lineage_retire_the_first(cap):
    # the governed lifecycle holds under share-weighted enactment: a second
    # directive on the same lineage retires the first, leaving a history.
    s = cap["s"]
    for n in ("1", "2"):
        pid = pid_of(propose_share(
            s, cap["bob"].id, cap["firm"].id, "ACME",
            [behaviour_mutation("strategy", f"ctx.state.v={n}", cap["firm"].id)]))
        vote(s, cap["bob"].id, pid, "for")
        enact(s, cap["firm"].id, pid)
    rows = sorted(s.query(Script).filter_by(lineage_id="strategy").all(),
                  key=lambda x: x.name)
    assert len(rows) == 2
    assert rows[0].is_active is False            # retired
    assert rows[1].is_active is True             # current law


# ===========================================================================
# the firm needs legislate — a capability grant is the only new data
# ===========================================================================

def test_firm_without_legislate_cannot_enact(cap):
    s = cap["s"]
    cap["firm"].capabilities = []                 # strip legislate
    pid = pid_of(propose_share(
        s, cap["bob"].id, cap["firm"].id, "ACME",
        [behaviour_mutation("m", "ctx.state.v=1", cap["firm"].id)]))
    vote(s, cap["bob"].id, pid, "for")
    r = enact(s, cap["firm"].id, pid)
    assert r["status"] == "rejected"
    assert "legislate" in r["reason"]
