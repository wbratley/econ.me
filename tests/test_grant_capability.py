"""Capability transfer — the `grant_capability` / `revoke_capability` primitives.

The last declared-but-unwired engine primitive (docs/actors.md): the
meta-privilege of changing *who can exercise power*. Both intents gate on
the single `GRANT_CAPABILITY` capability — conferring and withdrawing
power are the same meta-act. Three design decisions, each tested here:

  - **Free-grant model** — a GRANT_CAPABILITY holder may confer ANY
    *declared* capability on ANY entity (a legislature constitutes
    agencies with powers it does not itself exercise). The safety floor is
    the capability gate + a VALIDATOR veto + the constitutional
    supermajority (for voted grants), not "you may only delegate what you
    hold."
  - **Defense-in-depth** — the capability is checked at the intent
    boundary (INTENT_CAPABILITIES) AND in the service (MissingCapabilityError),
    exactly like levy/seize.
  - **Constitutional-tier mutations** — power transfer is meta; a simple
    majority must not be able to escalate power. So grant_capability /
    revoke_capability are constitutional mutations needing supermajority,
    and an ordinary proposal may not carry them.
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities, services
from econengine.lua_engine import Intent
from econengine.models import (
    Base, EntityStatus, EntityType, ProposalStatus, Script, ScriptType,
)
from econengine.scripting import resolve_intent
from econengine.services import (
    MissingCapabilityError, create_entity, grant_capability, revoke_capability,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """A government that holds grant_capability + amend_constitution (so it
    can both grant directly and enact constitutional proposals), and three
    individuals: a granter-in-world, an agency, and a peasant."""
    gov = create_entity(session, "Republic", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.GRANT_CAPABILITY,
                        capabilities.AMEND_CONSTITUTION,
                        capabilities.LEGISLATE]
    agency = create_entity(session, "Agency", EntityType.INDIVIDUAL)
    peasant = create_entity(session, "Peasant", EntityType.INDIVIDUAL)
    voter_a = create_entity(session, "A", EntityType.INDIVIDUAL)
    voter_b = create_entity(session, "B", EntityType.INDIVIDUAL)
    voter_c = create_entity(session, "C", EntityType.INDIVIDUAL)
    session.flush()
    return {"s": session, "gov": gov, "agency": agency, "peasant": peasant,
            "a": voter_a, "b": voter_b, "c": voter_c}


# ---------------------------------------------------------------------------
# intent helpers
# ---------------------------------------------------------------------------

def grant(s, grantor_id, to_id, capability):
    return resolve_intent(s, Intent(
        entity_id=grantor_id, intent_type="grant_capability",
        params={"to_entity_id": to_id, "capability": capability},
        resource_ids=[to_id],
    ))


def revoke(s, grantor_id, to_id, capability):
    return resolve_intent(s, Intent(
        entity_id=grantor_id, intent_type="revoke_capability",
        params={"to_entity_id": to_id, "capability": capability},
        resource_ids=[to_id],
    ))


def constitutional_mutation(capability):
    return {"type": "grant_capability",
            "params": {"to_entity_id": "PLACEHOLDER", "capability": capability}}


def propose(s, proposer_id, target_id, model, mutations, ptype="constitutional",
            threshold="0.5", quorum="0"):
    return resolve_intent(s, Intent(
        entity_id=proposer_id, intent_type="create_proposal",
        params={"target_id": target_id, "mutations": json.dumps(mutations),
                "weight_model": model, "threshold": threshold,
                "quorum": quorum, "proposal_type": ptype},
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


# ===========================================================================
# the service — capability gate + free grant
# ===========================================================================

def test_grant_requires_capability(session):
    """An entity without GRANT_CAPABILITY cannot grant (defense-in-depth,
    checked in the service)."""
    g = create_entity(session, "G", EntityType.INDIVIDUAL)
    t = create_entity(session, "T", EntityType.INDIVIDUAL)
    session.flush()
    with pytest.raises(MissingCapabilityError):
        grant_capability(session, g, t, capabilities.SEIZE)


def test_grant_confers_any_declared_capability(world):
    """Free-grant model: a holder may confer ANY declared capability, even
    one it does not itself hold (the government holds only grant_capability
    + amend_constitution, yet grants seize)."""
    s = world["s"]
    grant_capability(s, world["gov"], world["agency"], capabilities.SEIZE)
    assert world["agency"].has_capability(capabilities.SEIZE)
    # the government does NOT itself hold seize — free grant, not delegation
    assert not world["gov"].has_capability(capabilities.SEIZE)


def test_grant_is_idempotent(world):
    """Granting a capability already held is a no-op success (no dup)."""
    s = world["s"]
    grant_capability(s, world["gov"], world["agency"], capabilities.LEVY)
    grant_capability(s, world["gov"], world["agency"], capabilities.LEVY)
    assert world["agency"].capabilities.count(capabilities.LEVY) == 1


def test_grant_rejects_unknown_capability(world):
    """The capability name must be in the declared vocabulary."""
    s = world["s"]
    with pytest.raises(ValueError, match="unknown capability"):
        grant_capability(s, world["gov"], world["agency"], "bogus_power")


def test_revoke_removes_a_capability(world):
    s = world["s"]
    world["agency"].capabilities = [capabilities.SEIZE, capabilities.LEVY]
    s.flush()
    revoke_capability(s, world["gov"], world["agency"], capabilities.SEIZE)
    assert not world["agency"].has_capability(capabilities.SEIZE)
    assert world["agency"].has_capability(capabilities.LEVY)  # untouched


def test_revoke_is_idempotent(world):
    """Revoking a capability not held is a no-op success (the postcondition
    — target lacks it — already holds)."""
    s = world["s"]
    revoke_capability(s, world["gov"], world["agency"], capabilities.SEIZE)
    assert capabilities.SEIZE not in (world["agency"].capabilities or [])


def test_revoke_requires_capability(world):
    s = world["s"]
    world["agency"].capabilities = [capabilities.SEIZE]
    s.flush()
    with pytest.raises(MissingCapabilityError):
        revoke_capability(s, world["peasant"], world["agency"], capabilities.SEIZE)


# ===========================================================================
# the intent boundary — resolve_intent gating
# ===========================================================================

def test_intent_grant_without_capability_is_rejected(world):
    """The peasant (no grant_capability) cannot grant via an intent either."""
    s = world["s"]
    out = grant(s, world["peasant"].id, world["agency"].id, capabilities.SEIZE)
    assert out["status"] == "rejected"
    assert "grant_capability" in out["reason"]
    assert not world["agency"].has_capability(capabilities.SEIZE)


def test_intent_grant_applies(world):
    s = world["s"]
    out = grant(s, world["gov"].id, world["agency"].id, capabilities.SEIZE)
    assert out["status"] == "applied"
    assert world["agency"].has_capability(capabilities.SEIZE)


def test_intent_grant_rejects_unknown_capability(world):
    s = world["s"]
    out = grant(s, world["gov"].id, world["agency"].id, "bogus")
    assert out["status"] == "rejected"


def test_intent_grant_rejects_unknown_target(world):
    s = world["s"]
    out = grant(s, world["gov"].id, "no-such-entity", capabilities.SEIZE)
    assert out["status"] == "rejected"


def test_intent_revoke_applies(world):
    s = world["s"]
    world["agency"].capabilities = [capabilities.SEIZE]
    s.flush()
    out = revoke(s, world["gov"].id, world["agency"].id, capabilities.SEIZE)
    assert out["status"] == "applied"
    assert not world["agency"].has_capability(capabilities.SEIZE)


# ===========================================================================
# a granted capability actually empowers — the point of granting
# ===========================================================================

def test_granted_capability_empowers_the_target(world):
    """Granting seize lets the target then seize — the whole purpose. The
    peasant cannot seize before; after the government grants seize, it can
    (capability-wise — the op may still fail on other grounds, but not on
    the capability check)."""
    from econengine import markets
    s = world["s"]
    # peasant tries to seize before grant -> rejected (missing capability)
    out = resolve_intent(s, Intent(
        entity_id=world["peasant"].id, intent_type="seize",
        params={"from_entity_id": world["agency"].id, "symbol": "GRAIN",
                "quantity": "1", "rule_ref": "test"},
        resource_ids=[world["agency"].id]))
    assert out["status"] == "rejected"
    assert "seize" in out["reason"]
    # grant seize to the peasant
    grant_capability(s, world["gov"], world["peasant"], capabilities.SEIZE)
    # now the capability check passes (the op may fail on holdings, but the
    # reason is no longer 'missing capability seize')
    out = resolve_intent(s, Intent(
        entity_id=world["peasant"].id, intent_type="seize",
        params={"from_entity_id": world["agency"].id, "symbol": "GRAIN",
                "quantity": "1", "rule_ref": "test"},
        resource_ids=[world["agency"].id]))
    assert out["status"] != "rejected" or "seize" not in out["reason"]


# ===========================================================================
# VALIDATOR veto — the constitutional backstop on capability transfer
# ===========================================================================

VALIDATOR_SRC = """
-- forbid granting the seize capability, no matter who authorises it
if ctx.op.type == "grant_capability" and ctx.op.capability == "seize" then
  error("seize may not be conferred by grant")
end
"""


def test_validator_can_veto_a_grant(world):
    """A VALIDATOR fires during grant_capability and may veto it
    (fail-closed): the constitution forbids conferring 'seize'."""
    s = world["s"]
    v = Script(name="charter-cap", lineage_id="charter-cap",
               script_type=ScriptType.VALIDATOR, source=VALIDATOR_SRC,
               is_active=True)
    s.add(v)
    s.flush()
    out = grant(s, world["gov"].id, world["agency"].id, capabilities.SEIZE)
    assert out["status"] == "rejected"
    assert "seize may not be conferred" in out["reason"]
    assert not world["agency"].has_capability(capabilities.SEIZE)


def test_validator_does_not_veto_other_grants(world):
    """The same validator permits granting levy (only seize is forbidden)."""
    s = world["s"]
    v = Script(name="charter-cap", lineage_id="charter-cap",
               script_type=ScriptType.VALIDATOR, source=VALIDATOR_SRC,
               is_active=True)
    s.add(v)
    s.flush()
    out = grant(s, world["gov"].id, world["agency"].id, capabilities.LEVY)
    assert out["status"] == "applied"
    assert world["agency"].has_capability(capabilities.LEVY)


# ===========================================================================
# tier — capability transfer is a constitutional mutation
# ===========================================================================

def test_grant_mutation_is_constitutional_only(world):
    """An ordinary proposal may NOT carry a grant_capability mutation
    (power transfer is meta — a simple majority must not escalate power)."""
    s = world["s"]
    m = {"type": "grant_capability",
         "params": {"to_entity_id": world["agency"].id,
                    "capability": capabilities.SEIZE}}
    out = propose(s, world["a"].id, world["gov"].id, "citizen", [m],
                  ptype="ordinary")
    assert out["status"] == "rejected"
    assert "not allowed" in out["reason"]


def test_grant_mutation_allowed_in_constitutional_proposal(world):
    s = world["s"]
    m = {"type": "grant_capability",
         "params": {"to_entity_id": world["agency"].id,
                    "capability": capabilities.SEIZE}}
    out = propose(s, world["a"].id, world["gov"].id, "citizen", [m],
                  ptype="constitutional")
    assert out["status"] == "applied"


def test_voted_grant_enacts_under_default_supermajority(world):
    """The full cycle under the default floor (0.67): a constitutional
    proposal carries a grant_capability mutation; a unanimous vote (3/3 =
    1.0, well over the floor) enacts and confers the capability. The
    government holds grant_capability (the mutation's gate) AND
    amend_constitution (the constitutional tier's gate)."""
    s = world["s"]
    m = {"type": "grant_capability",
         "params": {"to_entity_id": world["agency"].id,
                    "capability": capabilities.SEIZE}}
    pid = propose(s, world["a"].id, world["gov"].id, "citizen", [m],
                  ptype="constitutional", threshold="0.5")["proposal_id"]
    for vid in (world["a"].id, world["b"].id, world["c"].id):
        vote(s, vid, pid, "for")
    out = enact(s, world["gov"].id, pid)
    assert out["proposal_status"] == "enacted"
    assert world["agency"].has_capability(capabilities.SEIZE)


def test_voted_grant_clears_an_explicit_low_floor(world):
    """With the constitution floor lowered (0.5), a 2/3 majority enacts the
    grant. This shows the voted grant path end-to-end."""
    from econengine import constitution
    s = world["s"]
    constitution.set_constitution(s, {"supermajority_threshold": "0.5"})
    m = {"type": "grant_capability",
         "params": {"to_entity_id": world["agency"].id,
                    "capability": capabilities.SEIZE}}
    pid = propose(s, world["a"].id, world["gov"].id, "citizen", [m],
                  ptype="constitutional", threshold="0.5")["proposal_id"]
    vote(s, world["a"].id, pid, "for")
    vote(s, world["b"].id, pid, "for")
    vote(s, world["c"].id, pid, "against")
    out = enact(s, world["gov"].id, pid)
    assert out["proposal_status"] == "enacted"
    assert world["agency"].has_capability(capabilities.SEIZE)


def test_enacted_grant_rolls_back_on_veto(world):
    """If a VALIDATOR vetoes the grant during enactment, the whole enactment
    fails (atomic rollback) and no capability is conferred."""
    s = world["s"]
    v = Script(name="charter-cap", lineage_id="charter-cap",
               script_type=ScriptType.VALIDATOR, source=VALIDATOR_SRC,
               is_active=True)
    s.add(v)
    s.flush()
    m = {"type": "grant_capability",
         "params": {"to_entity_id": world["agency"].id,
                    "capability": capabilities.SEIZE}}
    pid = propose(s, world["a"].id, world["gov"].id, "citizen", [m],
                  ptype="constitutional", threshold="0.5")["proposal_id"]
    for vid in (world["a"].id, world["b"].id, world["c"].id):
        vote(s, vid, pid, "for")
    out = enact(s, world["gov"].id, pid)
    assert out["proposal_status"] == "failed"
    assert "seize may not be conferred" in out["reason"]
    assert not world["agency"].has_capability(capabilities.SEIZE)


# ===========================================================================
# the Lua API — an enacted directive grants a capability
# ===========================================================================

DIRECTIVE_SRC = """
-- a behaviour bound to the government: grant seize to the named agency
ctx.action.grant_capability(ctx.state.agency_id, "seize")
"""


def test_lua_grant_capability_from_a_directive(world):
    """A BEHAVIOUR script bound to the government (which holds
    grant_capability) can grant via ctx.action.grant_capability. The queued
    intent is resolved with the government as the actor, so the capability
    gate passes."""
    from econengine import scripting, tick
    s = world["s"]
    world["gov"].capabilities = [capabilities.GRANT_CAPABILITY,
                                 capabilities.LEGISLATE]
    directive = Script(name="empower-agency", lineage_id="empower-agency",
                       script_type=ScriptType.BEHAVIOUR, source=DIRECTIVE_SRC,
                       is_active=True, entity_id=world["gov"].id,
                       state={"agency_id": world["agency"].id})
    s.add(directive)
    s.flush()
    tick.run_tick(s)
    assert world["agency"].has_capability(capabilities.SEIZE)


def test_lua_grant_blocked_when_actor_lacks_capability(world):
    """The same directive bound to the peasant (no grant_capability) queues
    an intent that is rejected at the capability gate — no grant."""
    from econengine import tick
    s = world["s"]
    directive = Script(name="empower-agency", lineage_id="empower-agency",
                       script_type=ScriptType.BEHAVIOUR, source=DIRECTIVE_SRC,
                       is_active=True, entity_id=world["peasant"].id,
                       state={"agency_id": world["agency"].id})
    s.add(directive)
    s.flush()
    tick.run_tick(s)
    assert not world["agency"].has_capability(capabilities.SEIZE)


# ===========================================================================
# the registry — every declared capability is now wired
# ===========================================================================

def test_every_declared_capability_is_wired():
    """No declared capability is left dangling — each gates at least one
    intent. (GRANT_CAPABILITY gates both grant and revoke.)"""
    for cap in capabilities.ALL:
        assert cap in capabilities.INTENT_CAPABILITIES.values(), (
            f"capability {cap!r} is declared but gates no intent")
