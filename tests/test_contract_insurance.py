"""Insurance reference contract (Step 5d) -- engine validation.

These tests prove the contract's reason for existing: **``ctx.events`` is a
trigger source** — the one engine affordance no earlier contract exercises. An
insurer collects premiums into a risk pool; when a policyholder is
incapacitated (a death), the insurer's POLICY script sees the event in
``ctx.events`` next tick and pays the beneficiary.

The trigger here is a REAL engine event (``entity_incapacitated``), produced
the way the world produces it: a policyholder crosses an incapacitating
condition threshold (``conditions.py``). No synthetic event injection.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine import capabilities, markets
from econengine.goods import create_good
from econengine.models import (Base, EntityStatus, EntityType, Transaction,
                                TransactionType)
from econengine.services import (create_account, create_entity)
from econengine.tick import run_tick

from contracts.insurance.insurance import (is_paid, is_triggered,
                                            open_insurer, policy,
                                            risk_pool_balance, total_coverage,
                                            underwrite)

COVERAGE_VALIDATOR = (Path(__file__).resolve().parent.parent
                      / "contracts" / "insurance" / "coverage_cap.lua").read_text()

THRESHOLD = Decimal("10")   # COND-DIE incapacitates at 10 units


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _cash(entity):
    return [a for a in entity.accounts if a.currency == "USD"][0].balance


def _add_validator(session, insurer):
    from econengine.models import Script, ScriptType
    session.add(Script(
        name="coverage-cap", source=COVERAGE_VALIDATOR,
        script_type=ScriptType.VALIDATOR, entity_id=insurer.entity.id,
        is_active=True, state={}))
    session.flush()


def _arm_death(session):
    """Stand up the condition that incapacitates: COND-DIE at threshold 10."""
    create_good(session, "COND-DIE", incapacitates_at=THRESHOLD)
    session.flush()


def _doomed(session, name):
    """An individual who will be incapacitated the next tick (holds the
    threshold quantity of COND-DIE)."""
    ent = create_entity(session, name, EntityType.INDIVIDUAL)
    create_account(session, ent, "USD", initial_balance=Decimal("10000"))
    markets.adjust_holding(session, ent, "COND-DIE", THRESHOLD)
    session.flush()
    return ent


@pytest.fixture
def world(session):
    """A funded insurer and a healthy beneficiary."""
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    create_account(session, gov, "USD", initial_balance=Decimal("1000000"))
    insurer = open_insurer(session, "Mutual", "USD")
    beneficiary = create_entity(session, "Heir", EntityType.INDIVIDUAL)
    create_account(session, beneficiary, "USD", initial_balance=Decimal("0"))
    _arm_death(session)
    session.flush()
    return session, insurer, beneficiary


def _no_issuance(session):
    n = session.execute(
        select(func.count()).select_from(Transaction)
        .where(Transaction.tx_type == TransactionType.ISSUANCE)
    ).scalar()
    return n == 0


# ---------------------------------------------------------------------------
# underwriting: premium in, policy booked, coverage oracle published

def test_underwrite_collects_premium_and_books_policy(world):
    session, insurer, beneficiary = world
    policyholder = _doomed(session, "Policyholder")
    underwrite(session, insurer, policyholder, beneficiary,
               coverage=Decimal("1000"), premium=Decimal("50"))
    assert risk_pool_balance(insurer) == Decimal("50")     # premium in
    assert _cash(policyholder) == Decimal("10000") - Decimal("50")
    rec = policy(insurer, policyholder.id)
    assert rec is not None
    assert rec["coverage"] == "1000"
    assert rec["triggered"] is False
    assert rec["paid"] is False
    assert _no_issuance(session)


def test_underwrite_publishes_coverage_oracle(world):
    from econengine.models import WorldSetting
    session, insurer, beneficiary = world
    policyholder = _doomed(session, "Policyholder")
    info = underwrite(session, insurer, policyholder, beneficiary,
                      coverage=Decimal("1000"), premium=Decimal("50"))
    oracle = session.get(WorldSetting,
                         f"insurance:coverage:{info['beneficiary_account']}")
    assert oracle is not None
    assert oracle.value["max"] == "1000"


# ---------------------------------------------------------------------------
# the trigger: a real entity_incapacitated event drives the payout

def test_death_triggers_and_pays_next_tick(world):
    """The headline: a policyholder is incapacitated (tick 1); the insurer's
    POLICY script sees the event in ctx.events (tick 2), marks the policy
    triggered, and pays the beneficiary from the risk pool."""
    session, insurer, beneficiary = world
    policyholder = _doomed(session, "Policyholder")
    underwrite(session, insurer, policyholder, beneficiary,
               coverage=Decimal("1000"), premium=Decimal("1000"))
    assert _cash(beneficiary) == Decimal("0")

    run_tick(session)   # tick 1: policyholder crosses the threshold -> incapacitated
    assert policyholder.status == EntityStatus.INCAPACITATED
    assert not is_triggered(insurer, policyholder.id)   # insurer sees it NEXT tick

    run_tick(session)   # tick 2: insurer's POLICY script sees the event -> pays
    assert is_triggered(insurer, policyholder.id)
    assert is_paid(insurer, policyholder.id)
    assert _cash(beneficiary) == Decimal("1000")        # death benefit paid
    assert risk_pool_balance(insurer) == Decimal("0")   # pool drained to payout
    assert _no_issuance(session)


def test_no_trigger_no_payout(world):
    """A policyholder who stays alive is never paid."""
    session, insurer, beneficiary = world
    # A policyholder BELOW the threshold (holds 0 COND-DIE) stays alive.
    healthy = create_entity(session, "Healthy", EntityType.INDIVIDUAL)
    create_account(session, healthy, "USD", initial_balance=Decimal("10000"))
    underwrite(session, insurer, healthy, beneficiary,
               coverage=Decimal("1000"), premium=Decimal("100"))
    run_tick(session)
    run_tick(session)
    assert healthy.status == EntityStatus.ACTIVE
    assert not is_triggered(insurer, healthy.id)
    assert _cash(beneficiary) == Decimal("0")
    assert risk_pool_balance(insurer) == Decimal("100")   # premium kept


def test_non_policyholder_death_ignored(world):
    """An incapacitation of a non-policyholder does not trigger a payout."""
    session, insurer, beneficiary = world
    policyholder = _doomed(session, "Policyholder")
    underwrite(session, insurer, policyholder, beneficiary,
               coverage=Decimal("1000"), premium=Decimal("1000"))
    stranger = _doomed(session, "Stranger")   # not insured, also doomed
    run_tick(session)   # both incapacitated
    run_tick(session)   # insurer sees both events; only the policyholder matches
    assert is_paid(insurer, policyholder.id)
    assert policy(insurer, stranger.id) is None           # never a policy
    assert _cash(beneficiary) == Decimal("1000")          # one payout only


# ---------------------------------------------------------------------------
# risk-pool exhaustion: the local counter prevents over-commit; unpaid claims retry

def test_pool_exhaustion_one_paid_one_deferred(world):
    """Two policyholders die the same tick; the pool covers only one. The
    local pool counter prevents over-commit (only one queues); the other
    retries and pays next tick once fresh premiums arrive."""
    session, insurer, beneficiary = world
    p1 = _doomed(session, "P1")
    p2 = _doomed(session, "P2")
    underwrite(session, insurer, p1, beneficiary,
               coverage=Decimal("500"), premium=Decimal("600"))
    underwrite(session, insurer, p2, beneficiary,
               coverage=Decimal("500"), premium=Decimal("300"))
    # Pool = 900; two deaths need 1000. Only one can pay this tick.
    run_tick(session)   # both incapacitated
    run_tick(session)   # insurer pays one (pool 900; first 500, leaving 400 < 500)
    paid_one = is_paid(insurer, p1.id) or is_paid(insurer, p2.id)
    assert paid_one
    assert not (is_paid(insurer, p1.id) and is_paid(insurer, p2.id))  # only one
    assert _cash(beneficiary) == Decimal("500")           # exactly one payout

    # A fresh premium funds the second payout next tick.
    p3 = _doomed(session, "P3")
    underwrite(session, insurer, p3, beneficiary,
              coverage=Decimal("1"), premium=Decimal("500"))
    run_tick(session)
    assert is_paid(insurer, p1.id) and is_paid(insurer, p2.id)   # both settled now


def test_paid_policy_not_paid_twice(world):
    session, insurer, beneficiary = world
    policyholder = _doomed(session, "Policyholder")
    underwrite(session, insurer, policyholder, beneficiary,
               coverage=Decimal("1000"), premium=Decimal("1000"))
    run_tick(session)
    run_tick(session)
    assert _cash(beneficiary) == Decimal("1000")
    run_tick(session)   # a third tick: no double payout
    run_tick(session)
    assert _cash(beneficiary) == Decimal("1000")


# ---------------------------------------------------------------------------
# term: a policy past its term does not pay

def test_expired_term_does_not_pay(world):
    session, insurer, beneficiary = world
    policyholder = _doomed(session, "Policyholder")
    underwrite(session, insurer, policyholder, beneficiary,
               coverage=Decimal("1000"), premium=Decimal("100"), term=5)
    # Run ticks past the term before the death.
    for _ in range(6):
        run_tick(session)
    assert policyholder.status == EntityStatus.INCAPACITATED   # died tick 1
    # By now (tick 6) the term (5) has expired; the payout is refused.
    assert not is_paid(insurer, policyholder.id)
    assert _cash(beneficiary) == Decimal("0")


# ---------------------------------------------------------------------------
# the coverage-cap VALIDATOR (the constitutional backstop)

def test_coverage_cap_allows_documented_payout(world):
    """With the validator installed, a documented payout proceeds normally."""
    session, insurer, beneficiary = world
    _add_validator(session, insurer)
    policyholder = _doomed(session, "Policyholder")
    underwrite(session, insurer, policyholder, beneficiary,
               coverage=Decimal("1000"), premium=Decimal("1000"))
    run_tick(session)
    run_tick(session)
    assert is_paid(insurer, policyholder.id)
    assert _cash(beneficiary) == Decimal("1000")


def test_coverage_cap_vetoes_undocumented_payout(world):
    """A payout to an undocumented beneficiary (no coverage oracle) is vetoed
    fail-closed. Tested directly: the insurer tries to pay a stranger."""
    from econengine.scripting import OperationVetoedError
    from econengine.services import transfer
    session, insurer, beneficiary = world
    _add_validator(session, insurer)
    stranger = create_entity(session, "Stranger", EntityType.INDIVIDUAL)
    stranger_acct = create_account(session, stranger, "USD",
                                   initial_balance=Decimal("0"))
    # Fund the pool so the transfer would otherwise succeed.
    underwrite(session, insurer, _doomed(session, "Funder"), beneficiary,
               coverage=Decimal("1"), premium=Decimal("500"))
    with pytest.raises(OperationVetoedError):
        transfer(session, insurer.pool, stranger_acct, Decimal("100"),
                 "insurance-payout:rogue")
    assert _cash(stranger) == Decimal("0")            # nothing moved


def test_coverage_cap_vetoes_excessive_payout(world):
    """A payout exceeding the documented coverage is vetoed."""
    from econengine.scripting import OperationVetoedError
    from econengine.services import transfer
    from econengine.models import WorldSetting
    session, insurer, beneficiary = world
    _add_validator(session, insurer)
    benef_acct = [a for a in beneficiary.accounts if a.currency == "USD"][0]
    # Document a small coverage, then try to pay more.
    session.add(WorldSetting(key=f"insurance:coverage:{benef_acct.id}",
                             value={"max": "100"}))
    underwrite(session, insurer, _doomed(session, "Funder"), beneficiary,
               coverage=Decimal("1"), premium=Decimal("1000"))  # funds the pool
    # Restore the small cap (underwrite overwrote it with "1").
    session.get(WorldSetting, f"insurance:coverage:{benef_acct.id}").value = {"max": "100"}
    session.flush()
    with pytest.raises(OperationVetoedError):
        transfer(session, insurer.pool, benef_acct, Decimal("500"),
                 "insurance-payout:excessive")
    assert _cash(beneficiary) == Decimal("0")


# ---------------------------------------------------------------------------
# observation: total_coverage is stamped each tick

def test_total_coverage_stamped(world):
    session, insurer, beneficiary = world
    p1 = _doomed(session, "P1")
    p2 = _doomed(session, "P2")
    underwrite(session, insurer, p1, beneficiary,
               coverage=Decimal("1000"), premium=Decimal("10"))
    underwrite(session, insurer, p2, beneficiary,
               coverage=Decimal("500"), premium=Decimal("10"))
    run_tick(session)
    assert total_coverage(insurer) == Decimal("1500.0000")
