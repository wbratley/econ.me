"""The levy mechanism — enforced state action (docs/actors.md step 2 / Fork 1C).

Levy is the privilege layer above ownership made concrete: an entity holding
the ``levy`` capability may compel a money transfer out of an account it does
NOT own, into its own, under a declared ``rule_ref``. It generalises the
estate rule (``conditions._apply_estate``) — which already moves a dead
entity's assets by engine authority — from death to enacted policy.

All the safety lives in the gating, not the movement:
  - capability (``levy``) checked at the intent boundary AND in the service;
  - the recipient account must be the authority's own (collect into treasury);
  - a VALIDATOR may veto an illegal levy (fail-closed);
  - ``rule_ref`` records which votable rule authorised the seizure.
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities
from econengine.lua_engine import Intent
from econengine.models import Base, EntityType, Script, ScriptType
from econengine.models.transaction import TransactionType
from econengine.scripting import OperationVetoedError, resolve_intent
from econengine.services import (
    CurrencyMismatchError,
    InsufficientFundsError,
    MissingCapabilityError,
    create_account,
    create_entity,
    levy,
)
from econengine.tick import run_tick


@pytest.fixture
def session():
    # check_same_thread off: ctx.query.* callbacks run on the script thread
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """A funded taxpayer, a levy-capable government (treasury), and a
    government with NO levy capability, each with a USD account."""
    taxpayer = create_entity(session, "Taxpayer", EntityType.INDIVIDUAL)
    gov = create_entity(session, "Treasury", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.LEVY]
    plain = create_entity(session, "PlainGov", EntityType.GOVERNMENT)  # no caps
    ta = create_account(session, taxpayer, "USD", Decimal("1000"))
    ga = create_account(session, gov, "USD", Decimal("0"))
    pa = create_account(session, plain, "USD", Decimal("0"))
    session.flush()
    return {
        "taxpayer": taxpayer, "gov": gov, "plain": plain,
        "ta": ta, "ga": ga, "pa": pa,
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
# services.levy — direct (in-process) callers
# ---------------------------------------------------------------------------

def test_levy_moves_funds_from_taxpayer_to_treasury(world, session):
    debit, credit = levy(
        session, world["gov"], world["ta"], world["ga"],
        Decimal("250"), "tax_schedule:income",
    )
    assert world["ta"].balance == Decimal("750")
    assert world["ga"].balance == Decimal("250")
    assert debit.tx_type == TransactionType.DEBIT
    assert credit.tx_type == TransactionType.CREDIT


def test_levy_creates_paired_transactions(world, session):
    debit, credit = levy(
        session, world["gov"], world["ta"], world["ga"],
        Decimal("100"), "tax_schedule:income", reference="Q1 income tax",
    )
    assert debit.account_id == world["ta"].id      # debit hits the taxpayer
    assert credit.account_id == world["ga"].id     # credit hits the treasury
    assert debit.amount == credit.amount == Decimal("100")
    assert debit.from_account_id == world["ta"].id
    assert debit.to_account_id == world["ga"].id
    assert credit.from_account_id == world["ta"].id
    assert credit.to_account_id == world["ga"].id
    assert debit.date == credit.date
    assert debit.reference == "Q1 income tax"


def test_levy_conservates_money_supply(world, session):
    """A levy moves money; it neither creates nor destroys it (unlike
    issue/retire). Total supply is unchanged."""
    from sqlalchemy import func, select
    from econengine.models import Account

    before = session.execute(
        select(func.coalesce(func.sum(Account.balance), 0)).where(Account.currency == "USD")
    ).scalar_one()
    levy(session, world["gov"], world["ta"], world["ga"], Decimal("333"), "r")
    after = session.execute(
        select(func.coalesce(func.sum(Account.balance), 0)).where(Account.currency == "USD")
    ).scalar_one()
    assert before == after == Decimal("1000")


def test_levy_rejects_authority_without_capability(world, session):
    with pytest.raises(MissingCapabilityError) as exc_info:
        levy(session, world["plain"], world["ta"], world["pa"], Decimal("10"), "r")
    assert exc_info.value.entity_id == world["plain"].id
    assert exc_info.value.capability == capabilities.LEVY
    # nothing moved
    assert world["ta"].balance == Decimal("1000")
    assert world["pa"].balance == Decimal("0")


def test_missing_capability_error_is_value_error(session):
    assert issubclass(MissingCapabilityError, ValueError)


def test_levy_rejects_recipient_not_owned_by_authority(world, session):
    """The authority may levy FROM others but only INTO its own account.
    Levying into a third party's account is not levy, it is seizure under
    a different rule — refused here."""
    other_taxpayer = create_entity(session, "Other", EntityType.INDIVIDUAL)
    oa = create_account(session, other_taxpayer, "USD", Decimal("0"))
    session.flush()
    with pytest.raises(ValueError, match="recipient account"):
        levy(session, world["gov"], world["ta"], oa, Decimal("10"), "r")
    assert world["ta"].balance == Decimal("1000")


def test_levy_rejects_currency_mismatch(world, session):
    euro_acct = create_account(session, world["gov"], "EUR", Decimal("0"))
    session.flush()
    with pytest.raises(CurrencyMismatchError):
        levy(session, world["gov"], world["ta"], euro_acct, Decimal("10"), "r")


def test_levy_rejects_insufficient_funds_without_mutating(world, session):
    before = world["ta"].balance
    with pytest.raises(InsufficientFundsError):
        levy(session, world["gov"], world["ta"], world["ga"], Decimal("9999"), "r")
    assert world["ta"].balance == before
    assert world["ga"].balance == Decimal("0")


def test_levy_rejects_non_positive_amount(world, session):
    with pytest.raises(ValueError):
        levy(session, world["gov"], world["ta"], world["ga"], Decimal("0"), "r")
    with pytest.raises(ValueError):
        levy(session, world["gov"], world["ta"], world["ga"], Decimal("-1"), "r")


# ---------------------------------------------------------------------------
# resolve_intent — the shared gate (intents API + tick + scripts)
# ---------------------------------------------------------------------------

def _levy_intent(entity_id, from_id, to_id, amount="250", rule_ref="tax_schedule:income"):
    return Intent(
        entity_id=entity_id,
        intent_type="levy",
        params={"from_account_id": from_id, "to_account_id": to_id,
                "amount": amount, "rule_ref": rule_ref, "reference": ""},
        resource_ids=[from_id, to_id],
        priority=10,
    )


def test_intent_levy_bypasses_ownership_of_source(world, session):
    """The defining behaviour: the authority moves money out of an account
    it does NOT own (the taxpayer's). Ownership of the source is bypassed
    by capability + rule_ref; ownership of the recipient still holds."""
    out = resolve_intent(session, _levy_intent(
        world["gov"].id, world["ta"].id, world["ga"].id,
    ))
    assert out["status"] == "applied", out
    assert world["ta"].balance == Decimal("750")
    assert world["ga"].balance == Decimal("250")


def test_intent_levy_rejects_entity_without_capability(world, session):
    out = resolve_intent(session, _levy_intent(
        world["plain"].id, world["ta"].id, world["pa"].id,
    ))
    assert out["status"] == "rejected"
    assert "levy" in out["reason"]
    assert world["ta"].balance == Decimal("1000")   # nothing seized


def test_intent_levy_rejects_recipient_not_owned(world, session):
    out = resolve_intent(session, _levy_intent(
        world["gov"].id, world["ta"].id, world["pa"].id,  # pa is plain gov's
    ))
    assert out["status"] == "rejected"
    assert "recipient" in out["reason"]


def test_intent_levy_rejects_unknown_account(world, session):
    out = resolve_intent(session, _levy_intent(
        world["gov"].id, "no-such-account", world["ga"].id,
    ))
    assert out["status"] == "rejected"
    assert "unknown account" in out["reason"]


def test_intent_levy_rejects_insufficient_funds(world, session):
    out = resolve_intent(session, _levy_intent(
        world["gov"].id, world["ta"].id, world["ga"].id, amount="99999",
    ))
    assert out["status"] == "rejected"


def test_intent_levy_carries_rule_ref_into_op(world, session):
    """A hook observes ctx.op: the levy op carries rule_ref so policy
    scripts and audit can see WHICH rule authorised the seizure."""
    script = make_script(
        session, "audit",
        """
        if ctx.op.type == 'levy' then
          ctx.state.rule = ctx.op.rule_ref
          ctx.state.amt = ctx.op.amount
        end
        """,
        ScriptType.HOOK,
    )
    resolve_intent(session, _levy_intent(
        world["gov"].id, world["ta"].id, world["ga"].id,
        amount="100", rule_ref="wealth_tax:schedule_A",
    ))
    assert script.state["rule"] == "wealth_tax:schedule_A"
    assert script.state["amt"] == "100"


# ---------------------------------------------------------------------------
# Validators — the policy choke point (fail-closed)
# ---------------------------------------------------------------------------

LEVY_CAP_200 = """
if ctx.op.type == 'levy' and tonumber(ctx.op.amount) > 200 then
    return {allow=false, reason="levy exceeds statutory cap"}
end
"""


def test_validator_vetoes_excessive_levy(world, session):
    """A VALIDATOR can veto a levy — the safety valve against an authority
    over-reaching its declared schedule. Fail-closed: nothing is seized."""
    make_script(session, "cap", LEVY_CAP_200, ScriptType.VALIDATOR)
    with pytest.raises(OperationVetoedError, match="statutory cap"):
        levy(session, world["gov"], world["ta"], world["ga"], Decimal("500"), "r")
    assert world["ta"].balance == Decimal("1000")
    assert world["ga"].balance == Decimal("0")


def test_validator_allows_levy_under_cap(world, session):
    make_script(session, "cap", LEVY_CAP_200, ScriptType.VALIDATOR)
    levy(session, world["gov"], world["ta"], world["ga"], Decimal("150"), "r")
    assert world["ta"].balance == Decimal("850")


def test_vetoed_levy_intent_is_rejected_not_raised(world, session):
    """Through the intent surface a veto becomes a clean rejection (the
    resolver swallows ValueError subclasses), not a raised exception."""
    make_script(session, "cap", LEVY_CAP_200, ScriptType.VALIDATOR)
    out = resolve_intent(session, _levy_intent(
        world["gov"].id, world["ta"].id, world["ga"].id, amount="500",
    ))
    assert out["status"] == "rejected"
    assert "statutory cap" in out["reason"]


# ---------------------------------------------------------------------------
# ctx.action.levy — reachable from the script layer (step 3's driver)
# ---------------------------------------------------------------------------

def test_policy_script_can_levy_each_tick(world, session):
    """A levy-capable government's POLICY script fires ctx.action.levy each
    tick: the capability gate admits it and money moves from the taxpayer
    into the treasury. This is the mechanism step 3's policy actor drives."""
    make_script(
        session, "income-tax",
        f"ctx.action.levy('{world['ta'].id}', '{world['ga'].id}', '100', 'tax:income')",
        ScriptType.POLICY, entity=world["gov"],
    )
    run_tick(session)
    run_tick(session)
    assert world["ta"].balance == Decimal("800")   # 1000 - 2 * 100
    assert world["ga"].balance == Decimal("200")


def test_policy_script_without_capability_levy_is_rejected(world, session):
    """A government that lacks the levy capability cannot collect: its
    ctx.action.levy intent is rejected at the gate, and nothing is seized.
    The capability, not the script's willingness, is what authorises force."""
    make_script(
        session, "wannabe-tax",
        f"ctx.action.levy('{world['ta'].id}', '{world['pa'].id}', '100', 'tax:income')",
        ScriptType.POLICY, entity=world["plain"],
    )
    tick = run_tick(session)
    assert tick.events[0]["status"] == "rejected"
    assert "levy" in tick.events[0]["reason"]
    assert world["ta"].balance == Decimal("1000")  # taxpayer untouched
