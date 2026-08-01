"""Government as policy actor — docs/actors.md step 3 (Fork 4B).

The mechanism/data/policy split (docs/design.md §2), made concrete:

  - **mechanism** is `services.levy` (step 2) — the engine moves money;
  - **data** is the `fiscal_policy` WorldSetting, a votable JSON dict whose
    *numbers* (rates, schedules) citizens change without touching code;
  - **policy** is a government POLICY script that reads that dict
    (`ctx.query.fiscal_policy()`) and turns it into `ctx.action.levy(...)`
    calls each tick.

`set_fiscal_policy` is the privileged intent that writes the data. Its
safety, like levy's, is all in the gating: the `set_fiscal_policy`
capability replaces admin god-mode for fiscal policy, and a VALIDATOR may
veto a change — so a validator becomes a *constitutional constraint* on
the rate (fail-closed: a broken policy gate never silently changes policy).
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities, fiscal
from econengine.lua_engine import Intent
from econengine.models import Base, EntityType, Script, ScriptType
from econengine.scripting import OperationVetoedError, resolve_intent
from econengine.services import (
    MissingCapabilityError, create_account, create_entity, set_fiscal_policy,
)
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """A levy+fiscal-capable government (treasury), a no-capability
    government, and a funded taxpayer."""
    gov = create_entity(session, "Treasury", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.LEVY, capabilities.SET_FISCAL_POLICY]
    plain = create_entity(session, "PlainGov", EntityType.GOVERNMENT)  # no caps
    taxpayer = create_entity(session, "Taxpayer", EntityType.INDIVIDUAL)
    ga = create_account(session, gov, "USD", Decimal("0"))
    pa = create_account(session, plain, "USD", Decimal("0"))
    ta = create_account(session, taxpayer, "USD", Decimal("1000"))
    session.flush()
    return {
        "gov": gov, "plain": plain, "taxpayer": taxpayer,
        "ga": ga, "pa": pa, "ta": ta,
    }


def make_script(session, name, source, script_type, entity=None, **kwargs):
    script = Script(
        name=name, source=source, script_type=script_type,
        entity_id=entity.id if entity else None, **kwargs,
    )
    session.add(script)
    session.flush()
    return script


# ---------------------------------------------------------------------------
# Data layer — fiscal.get_/set_fiscal_policy (pure storage)
# ---------------------------------------------------------------------------

def test_get_fiscal_policy_unset_returns_empty(session):
    assert fiscal.get_fiscal_policy(session) == {}


def test_set_then_get_fiscal_policy(session):
    fiscal.set_fiscal_policy(session, {"rate": "0.10", "rule": "income"})
    assert fiscal.get_fiscal_policy(session) == {"rate": "0.10", "rule": "income"}


def test_set_fiscal_policy_replaces_wholesale(session):
    """Replace, not merge — a change is atomic; readers never see a
    half-updated schedule."""
    fiscal.set_fiscal_policy(session, {"rate": "0.10", "band": "low"})
    fiscal.set_fiscal_policy(session, {"rate": "0.20"})
    assert fiscal.get_fiscal_policy(session) == {"rate": "0.20"}  # band gone


def test_set_fiscal_policy_rejects_non_object(session):
    with pytest.raises(ValueError):
        fiscal.set_fiscal_policy(session, [1, 2, 3])      # a JSON array
    with pytest.raises(ValueError):
        fiscal.set_fiscal_policy(session, "0.10")          # a scalar
    with pytest.raises(ValueError):
        fiscal.set_fiscal_policy(session, None)


# ---------------------------------------------------------------------------
# services.set_fiscal_policy — the privileged action
# ---------------------------------------------------------------------------

def test_service_set_fiscal_policy_requires_capability(world, session):
    with pytest.raises(MissingCapabilityError) as exc:
        set_fiscal_policy(session, world["plain"], {"rate": "0.10"})
    assert exc.value.capability == capabilities.SET_FISCAL_POLICY
    assert fiscal.get_fiscal_policy(session) == {}          # nothing written


def test_service_set_fiscal_policy_writes_for_capability_holder(world, session):
    set_fiscal_policy(session, world["gov"], {"rate": "0.10", "rule": "income"})
    assert fiscal.get_fiscal_policy(session) == {"rate": "0.10", "rule": "income"}


def test_service_set_fiscal_policy_fires_validator(world, session):
    """A VALIDATOR reads the proposed policy from ctx.op and may veto it —
    the constitutional-cap property: an enacted rate over the statutory
    ceiling is refused, fail-closed, and nothing is written."""
    make_script(session, "cap", """
    if ctx.op.type == 'set_fiscal_policy' then
      local rate = tonumber(ctx.op.policy.rate)
      if rate and rate > 0.5 then
        return {allow=false, reason="rate exceeds constitutional cap"}
      end
    end
    """, ScriptType.VALIDATOR)
    with pytest.raises(OperationVetoedError, match="constitutional cap"):
        set_fiscal_policy(session, world["gov"], {"rate": "0.9"})
    assert fiscal.get_fiscal_policy(session) == {}          # unchanged


def test_service_set_fiscal_policy_allows_under_cap(world, session):
    make_script(session, "cap", """
    if ctx.op.type == 'set_fiscal_policy' then
      if tonumber(ctx.op.policy.rate) > 0.5 then
        return {allow=false, reason="over cap"}
      end
    end
    """, ScriptType.VALIDATOR)
    set_fiscal_policy(session, world["gov"], {"rate": "0.3"})
    assert fiscal.get_fiscal_policy(session) == {"rate": "0.3"}


def test_service_set_fiscal_policy_fires_hook(world, session):
    make_script(session, "log", """
    if ctx.op.type == 'set_fiscal_policy' then
      ctx.state.last_rate = ctx.op.policy.rate
    end
    """, ScriptType.HOOK)
    set_fiscal_policy(session, world["gov"], {"rate": "0.15"})
    assert fiscal.get_fiscal_policy(session)["rate"] == "0.15"


# ---------------------------------------------------------------------------
# resolve_intent — the shared gate
# ---------------------------------------------------------------------------

def _policy_intent(entity_id, policy):
    return Intent(
        entity_id=entity_id,
        intent_type="set_fiscal_policy",
        params={"policy": json.dumps(policy)},
        resource_ids=[],
        priority=10,
    )


def test_intent_set_fiscal_policy_applied(world, session):
    out = resolve_intent(session, _policy_intent(world["gov"].id, {"rate": "0.10"}))
    assert out["status"] == "applied", out
    assert fiscal.get_fiscal_policy(session) == {"rate": "0.10"}


def test_intent_set_fiscal_policy_rejected_without_capability(world, session):
    out = resolve_intent(session, _policy_intent(world["plain"].id, {"rate": "0.10"}))
    assert out["status"] == "rejected"
    assert "set_fiscal_policy" in out["reason"]
    assert fiscal.get_fiscal_policy(session) == {}


def test_intent_set_fiscal_policy_rejects_invalid_json(world, session):
    bad = Intent(
        entity_id=world["gov"].id, intent_type="set_fiscal_policy",
        params={"policy": "not json"}, resource_ids=[], priority=10,
    )
    out = resolve_intent(session, bad)
    assert out["status"] == "rejected"
    assert "json" in out["reason"].lower()


def test_intent_set_fiscal_policy_rejects_non_object_json(world, session):
    for bad_value in ("[1,2,3]", '"0.10"', "42", "true"):
        out = resolve_intent(session, Intent(
            entity_id=world["gov"].id, intent_type="set_fiscal_policy",
            params={"policy": bad_value}, resource_ids=[], priority=10,
        ))
        assert out["status"] == "rejected", bad_value
        assert "object" in out["reason"]


def test_intent_set_fiscal_policy_validator_veto_is_rejected(world, session):
    """Through the intent surface a constitutional veto becomes a clean
    rejection, and the existing policy is left intact."""
    set_fiscal_policy(session, world["gov"], {"rate": "0.1"})   # incumbent
    make_script(session, "cap", """
    if ctx.op.type == 'set_fiscal_policy' and tonumber(ctx.op.policy.rate) > 0.5 then
      return {allow=false, reason="over constitutional cap"}
    end
    """, ScriptType.VALIDATOR)
    out = resolve_intent(session, _policy_intent(world["gov"].id, {"rate": "0.8"}))
    assert out["status"] == "rejected"
    assert "constitutional cap" in out["reason"]
    assert fiscal.get_fiscal_policy(session) == {"rate": "0.1"}  # unchanged


# ---------------------------------------------------------------------------
# ctx.query.fiscal_policy — the read side a POLICY script uses
# ---------------------------------------------------------------------------

def test_query_fiscal_policy_readable_by_script(world, session):
    """A POLICY script reads the enacted policy through the query — this is
    the binding between votable data and policy code."""
    set_fiscal_policy(session, world["gov"], {"rate": "0.25", "rule": "income"})
    make_script(
        session, "reader",
        "ctx.state.seen_rate = ctx.query.fiscal_policy().rate\n"
        "ctx.state.seen_rule = ctx.query.fiscal_policy().rule",
        ScriptType.POLICY, entity=world["gov"],
    )
    run_tick(session)
    script = session.query(Script).filter_by(name="reader").one()
    assert script.state["seen_rate"] == "0.25"
    assert script.state["seen_rule"] == "income"


# ---------------------------------------------------------------------------
# The full loop: votable rate → POLICY script → enforced levy (mechanism)
# ---------------------------------------------------------------------------

# A government POLICY script that binds votable data to the mechanism: it
# reads the rate from fiscal_policy, the treasury account and rule from the
# same dict, and levies each seeded taxpayer. THIS IS THE WHOLE POINT —
# the rate is data, the script never changes, and collection is enforced.
TAX_SCRIPT = """
local policy = ctx.query.fiscal_policy()
local rate = tonumber(policy.rate) or 0
local treasury = policy.treasury_account
local rule = policy.rule or "tax:income"
local taxpayers = ctx.state.taxpayers or {}
for _, acct in ipairs(taxpayers) do
  local bal = tonumber(ctx.query.balance(acct) or "0")
  local amount = bal * rate
  if amount >= 0.01 then
    ctx.action.levy(acct, treasury, string.format("%.4f", amount), rule)
  end
end
"""


def _set_policy_via_intent(session, gov, **policy):
    out = resolve_intent(session, _policy_intent(gov.id, policy))
    assert out["status"] == "applied", out


def test_policy_script_collects_at_enacted_rate(world, session):
    _set_policy_via_intent(session, world["gov"], rate="0.10",
                           treasury_account=world["ga"].id, rule="tax:income")
    make_script(session, "income-tax", TAX_SCRIPT, ScriptType.POLICY,
                entity=world["gov"], state={"taxpayers": [world["ta"].id]})
    run_tick(session)
    assert world["ta"].balance == Decimal("900")    # 10% of 1000
    assert world["ga"].balance == Decimal("100")


def test_changing_the_rate_collects_differently_next_tick(world, session):
    """The vote: the rate is votable DATA. Citizens change the number via a
    set_fiscal_policy intent (the script is untouched); the very next tick
    the enforced collection reflects the new rate. No code changed hands."""
    _set_policy_via_intent(session, world["gov"], rate="0.10",
                           treasury_account=world["ga"].id)
    make_script(session, "income-tax", TAX_SCRIPT, ScriptType.POLICY,
                entity=world["gov"], state={"taxpayers": [world["ta"].id]})
    run_tick(session)
    assert world["ga"].balance == Decimal("100")    # 10% of 1000

    # the vote — raise the rate to 20%; the script is unchanged
    _set_policy_via_intent(session, world["gov"], rate="0.20",
                           treasury_account=world["ga"].id)
    run_tick(session)
    assert world["ta"].balance == Decimal("720")    # 900 - 20% of 900
    assert world["ga"].balance == Decimal("280")


def test_policy_script_cannot_collect_without_levy_capability(world, session):
    """The data and the policy script are not enough — the mechanism is
    still gated: a government without the levy capability reads the rate
    and fires levies, but every levy is rejected at the capability gate."""
    _set_policy_via_intent(session, world["gov"], rate="0.10",
                           treasury_account=world["pa"].id)
    # plain gov has no levy capability; give it set_fiscal_policy so the
    # above intent was admissible, but it still cannot collect.
    world["plain"].capabilities = [capabilities.SET_FISCAL_POLICY]
    make_script(session, "wannabe-tax", TAX_SCRIPT, ScriptType.POLICY,
                entity=world["plain"], state={"taxpayers": [world["ta"].id]})
    tick = run_tick(session)
    levy_events = [e for e in tick.events if e.get("type") == "levy"]
    assert levy_events and all(e["status"] == "rejected" for e in levy_events)
    assert world["ta"].balance == Decimal("1000")   # untouched


def test_government_cannot_change_rate_without_capability(world, session):
    """Symmetric gate on the data side: a government that cannot set fiscal
    policy cannot vote itself a new rate, even if it runs the script."""
    _set_policy_via_intent(session, world["gov"], rate="0.10",
                           treasury_account=world["ga"].id)
    out = resolve_intent(session, _policy_intent(world["plain"].id, {"rate": "0.99"}))
    assert out["status"] == "rejected"
    # incumbent policy intact
    assert fiscal.get_fiscal_policy(session)["rate"] == "0.10"


def test_constitutional_cap_blocks_rate_increase(world, session):
    """End-to-end: a VALIDATOR caps the rate. The legislature (capability
    holder) tries to raise it past the cap; the veto fires and the old,
    legal rate survives."""
    set_fiscal_policy(session, world["gov"],
                      {"rate": "0.1", "treasury_account": world["ga"].id})
    make_script(session, "cap", """
    if ctx.op.type == 'set_fiscal_policy' and tonumber(ctx.op.policy.rate) > 0.5 then
      return {allow=false, reason="rate over constitutional cap"}
    end
    """, ScriptType.VALIDATOR)
    out = resolve_intent(session, _policy_intent(world["gov"].id,
                          {"rate": "0.6", "treasury_account": world["ga"].id}))
    assert out["status"] == "rejected"
    assert fiscal.get_fiscal_policy(session)["rate"] == "0.1"   # unchanged


# ---------------------------------------------------------------------------
# ctx.action.set_fiscal_policy — a script can set policy too
# ---------------------------------------------------------------------------

def test_script_can_set_fiscal_policy(world, session):
    """A government's own script may enact policy (e.g. a self-amending
    POLICY script), gated by the same capability + validator."""
    make_script(
        session, "auto-legislate",
        f"ctx.action.set_fiscal_policy({{rate='0.07', "
        f"treasury_account='{world['ga'].id}'}})",
        ScriptType.POLICY, entity=world["gov"],
    )
    run_tick(session)
    assert fiscal.get_fiscal_policy(session)["rate"] == "0.07"


def test_script_set_fiscal_policy_rejected_without_capability(world, session):
    """A capability-less government's script queues the intent, but it is
    rejected at the gate — the script cannot grant itself power."""
    make_script(
        session, "rogue-legislate",
        f"ctx.action.set_fiscal_policy({{rate='0.07'}})",
        ScriptType.POLICY, entity=world["plain"],
    )
    tick = run_tick(session)
    events = [e for e in tick.events if e.get("type") == "set_fiscal_policy"]
    assert events and events[0]["status"] == "rejected"
    assert fiscal.get_fiscal_policy(session) == {}
