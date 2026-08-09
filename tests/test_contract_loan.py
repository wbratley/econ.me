"""Secured loan reference contract (Step 5d) -- engine validation.

These tests prove the contract's reason for existing: ``levy`` and ``seize``
are the enforcement spine of private debt. A lender lends real base money, and
when the borrower defaults the lender COMPELS recovery -- cash by ``levy``,
collateral by ``seize`` -- using the same privileged primitives the state uses
to tax and expropriate. The lender can only do this because the state granted
it those capabilities (a lender's license); without them, enforcement fails.

Contrast with the bank (``test_contract_bank``): the bank creates deposit money
by a book entry and has no collateral to seize -- its failure mode is a bank
run, not a foreclosure. This contract lends base money against collateral, and
its failure mode IS the foreclosure.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine import capabilities, markets
from econengine.models import Account, Base, EntityType
from econengine.services import (
    InsufficientFundsError, MissingCapabilityError, create_account,
    create_entity,
)
from econengine.tick import run_tick, set_compute_budget_ms

from contracts.loan.loan import (
    DEFAULT_RATE, create_loan, enforce, is_in_default, loan_due, loan_status,
    open_lender, repay, total_outstanding,
)

USURY_CAP = (Path(__file__).resolve().parent.parent
             / "contracts" / "loan" / "usury_cap.lua").read_text()


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """A funded lender (with state-granted LEVY + SEIZE) and a borrower with
    cash and pledged collateral."""
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    create_account(session, gov, "USD", initial_balance=Decimal("1000000"))
    lender = open_lender(session, "Shylock", "USD", capital=Decimal("100000"))
    # The state delegates enforcement power to the lender (a license).
    lender.entity.capabilities = [capabilities.LEVY, capabilities.SEIZE]
    session.flush()
    borrower = create_entity(session, "Antonio", EntityType.INDIVIDUAL)
    b_acct = create_account(session, borrower, "USD", initial_balance=Decimal("1000"))
    markets.adjust_holding(session, borrower, "GRAIN", Decimal("50"))  # collateral
    session.flush()
    return session, lender, borrower, b_acct


def _lender_account(lender):
    return lender.account


def _grain(session, entity):
    h = markets.get_holding(session, entity.id, "GRAIN")
    return h.quantity if h else Decimal("0")


# ---------------------------------------------------------------------------
# origination: a loan is real base money lent against a pledge
# ---------------------------------------------------------------------------

def test_create_loan_disburses_base_money(world):
    """Origination is a transfer of existing money -- the lender must have it,
    the borrower receives it, and the debt + pledge are booked."""
    session, lender, borrower, b_acct = world
    lender_before = _lender_account(lender).balance

    create_loan(session, lender, borrower, Decimal("500"),
                rate=Decimal("0.02"), term=5,
                collateral={"symbol": "GRAIN", "quantity": "50"})

    assert _lender_account(lender).balance == lender_before - Decimal("500")
    assert b_acct.balance == Decimal("1500")
    assert loan_due(lender, borrower) == Decimal("500")
    assert loan_status(lender, borrower) == "active"
    rec = lender.script.state["loans"][borrower.id]
    assert rec["collateral"] == {"symbol": "GRAIN", "quantity": "50"}
    assert rec["maturity"] == 5


def test_create_loan_requires_lender_to_have_the_money(world):
    session, lender, borrower, b_acct = world
    with pytest.raises(InsufficientFundsError):
        create_loan(session, lender, borrower, Decimal("100000000"))


# ---------------------------------------------------------------------------
# voluntary repayment: money returns, debt shrinks
# ---------------------------------------------------------------------------

def test_repay_settles_the_loan(world):
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("500"), term=5)
    lender_before = _lender_account(lender).balance

    repay(session, lender, borrower, Decimal("500"))

    assert loan_status(lender, borrower) == "settled"
    assert loan_due(lender, borrower) == Decimal("0")
    assert _lender_account(lender).balance == lender_before + Decimal("500")


def test_repay_clamps_to_owed(world):
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("500"), term=5)
    out = repay(session, lender, borrower, Decimal("9999"))  # owes only 500
    assert Decimal(out["applied"]) == Decimal("500")
    assert loan_status(lender, borrower) == "settled"


# ---------------------------------------------------------------------------
# the BEHAVIOUR script: interest accrual (ctx.tick) + default detection
# ---------------------------------------------------------------------------

def test_interest_accrues_each_tick(world):
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("300"),
                rate=Decimal("0.02"), term=10)

    run_tick(session)                                  # tick 1: +6.00
    assert loan_due(lender, borrower) == Decimal("306")
    run_tick(session)                                  # tick 2: +6.00
    assert loan_due(lender, borrower) == Decimal("312")


def test_interest_is_skip_safe(world):
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("300"),
                rate=Decimal("0.02"), term=10)

    run_tick(session)                                  # tick 1: due 306
    set_compute_budget_ms(session, 0)                  # tick 2: skipped
    run_tick(session)
    assert loan_due(lender, borrower) == Decimal("306")

    set_compute_budget_ms(session, None)
    run_tick(session)                                  # tick 3: catches up 2 ticks
    assert loan_due(lender, borrower) == Decimal("318")  # 306 + 300*0.02*2


def test_default_marked_at_maturity(world):
    """The script flags a loan DEFAULT at maturity if it remains unpaid."""
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("300"),
                rate=Decimal("0.02"), term=3)
    assert loan_status(lender, borrower) == "active"

    for _ in range(3):
        run_tick(session)                              # ticks 1,2,3 -> maturity
    assert loan_status(lender, borrower) == "default"
    assert is_in_default(lender, borrower)


def test_default_not_marked_if_repaid_before_maturity(world):
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("300"), term=3)
    run_tick(session)                                  # tick 1: some interest
    repay(session, lender, borrower, loan_due(lender, borrower))
    for _ in range(2):
        run_tick(session)                              # to maturity
    assert loan_status(lender, borrower) == "settled"


def test_total_outstanding_stamp(world):
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("300"), rate=Decimal("0.02"), term=10)
    run_tick(session)                                  # due 306
    assert Decimal(lender.script.state["total_outstanding"]) == Decimal("306")


# ---------------------------------------------------------------------------
# THE ENFORCEMENT SPINE: levy (cash) + seize (collateral) on default
# ---------------------------------------------------------------------------

def test_enforce_levies_cash_from_borrower(world):
    """The headline: on default the lender COMPELS payment by levy -- money
    leaves the borrower's account by engine authority, not by consent. The
    borrower had the cash but would not pay; the levy takes it."""
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("500"),
                rate=Decimal("0.02"), term=2)
    for _ in range(2):
        run_tick(session)                              # -> default at tick 2
    assert loan_status(lender, borrower) == "default"
    owed = loan_due(lender, borrower)                  # 500 + interest
    lender_before = _lender_account(lender).balance

    summary = enforce(session, lender, borrower)

    assert Decimal(summary["levied"]) == owed          # borrower had >= owed
    assert summary["seized"] is None                   # no seizure needed
    # Cash moved by force: borrower down, lender up. (Borrower received the
    # 500 principal on top of its 1000 starting cash.)
    assert b_acct.balance == Decimal("1500") - owed
    assert _lender_account(lender).balance == lender_before + owed
    assert loan_status(lender, borrower) == "settled"


def test_enforce_seizes_collateral_when_borrower_is_broke(world):
    """The borrower has NO cash -- levy collects nothing, so the lender SEIZES
    the pledged collateral by force. The goods move to the lender."""
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("500"),
                rate=Decimal("0.02"), term=2,
                collateral={"symbol": "GRAIN", "quantity": "50"})
    # Drain the borrower's cash so the levy has nothing to take.
    b_acct.balance = Decimal("0")
    session.flush()
    for _ in range(2):
        run_tick(session)                              # -> default
    assert b_acct.balance == Decimal("0")
    assert _grain(session, borrower) == Decimal("50")
    assert _grain(session, lender.entity) == Decimal("0")

    summary = enforce(session, lender, borrower)

    assert Decimal(summary["levied"]) == Decimal("0")  # nothing to levy
    assert summary["seized"] is not None               # collateral seized
    # Goods moved by force: borrower stripped, lender now holds the GRAIN.
    assert _grain(session, borrower) == Decimal("0")
    assert _grain(session, lender.entity) == Decimal("50")
    assert loan_status(lender, borrower) == "settled"


def test_enforce_levies_partial_then_seizes_collateral(world):
    """The borrower has SOME cash but not enough: levy takes what's there,
    then seize takes the collateral for the shortfall."""
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("5000"),
                rate=Decimal("0.02"), term=2,
                collateral={"symbol": "GRAIN", "quantity": "50"})
    # Borrower received 5000 but spent nearly all of it: only 100 cash left.
    b_acct.balance = Decimal("100")
    session.flush()
    for _ in range(2):
        run_tick(session)                              # -> default, owes ~5200
    lender_before = _lender_account(lender).balance

    summary = enforce(session, lender, borrower)

    assert Decimal(summary["levied"]) == Decimal("100")  # all the cash left
    assert summary["seized"] is not None                  # plus the collateral
    assert _lender_account(lender).balance == lender_before + Decimal("100")
    assert _grain(session, borrower) == Decimal("0")
    assert _grain(session, lender.entity) == Decimal("50")


def test_enforce_without_levy_capability_cannot_levy(world):
    """A lender stripped of LEVY cannot compel cash -- the capability gate
    stops it cold. (MissingCapabilityError propagates: it is a setup error,
    not a recovery failure.)"""
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("500"), term=2)
    for _ in range(2):
        run_tick(session)
    lender.entity.capabilities = [capabilities.SEIZE]  # LEVY revoked
    session.flush()
    with pytest.raises(MissingCapabilityError):
        enforce(session, lender, borrower)


def test_enforce_without_seize_capability_cannot_foreclose(world):
    """No SEIZE, and the borrower is broke: the levy collects nothing and the
    seizure cannot happen. ``MissingCapabilityError`` propagates -- it is a
    setup error (an unlicensed foreclosure attempt), not a recovery failure."""
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("500"), term=2,
                collateral={"symbol": "GRAIN", "quantity": "50"})
    b_acct.balance = Decimal("0")     # broke: levy skipped, goes straight to seize
    session.flush()
    for _ in range(2):
        run_tick(session)
    lender.entity.capabilities = [capabilities.LEVY]   # SEIZE revoked
    session.flush()
    with pytest.raises(MissingCapabilityError):
        enforce(session, lender, borrower)


def test_enforce_when_collateral_fled(world):
    """The borrower sold the pledged goods before default (the engine has no
    lien). Seize raises InsufficientHoldingsError -- caught, not crashed; the
    loan settles (non-recourse) with nothing recovered. The honest signal that
    un-liened collateral is a risk the lender bears."""
    session, lender, borrower, b_acct = world
    create_loan(session, lender, borrower, Decimal("500"), term=2,
                collateral={"symbol": "GRAIN", "quantity": "50"})
    b_acct.balance = Decimal("0")
    markets.adjust_holding(session, borrower, "GRAIN", Decimal("-50"))  # sold it
    session.flush()
    for _ in range(2):
        run_tick(session)
    summary = enforce(session, lender, borrower)
    assert Decimal(summary["levied"]) == Decimal("0")
    assert summary["seized"] is None                   # fled
    assert loan_status(lender, borrower) == "settled"


# ---------------------------------------------------------------------------
# the VALIDATOR: a usury cap on collectible interest
# ---------------------------------------------------------------------------

def _install_usury_cap(session, lender):
    from econengine.models import Script, ScriptType
    session.add(Script(
        name="usury-cap", source=USURY_CAP,
        script_type=ScriptType.VALIDATOR, entity_id=lender.entity.id, is_active=True,
    ))
    session.flush()


def test_usury_cap_allows_a_legal_loan(world):
    """A loan within the cap: the levy enforces normally."""
    session, lender, borrower, b_acct = world
    _install_usury_cap(session, lender)
    # rate 2%/tick, cap default 5%/tick: legal.
    create_loan(session, lender, borrower, Decimal("500"),
                rate=Decimal("0.02"), term=2)
    for _ in range(2):
        run_tick(session)                              # -> default, owes 520
    summary = enforce(session, lender, borrower)
    assert Decimal(summary["levied"]) > Decimal("0")   # levy succeeded
    assert loan_status(lender, borrower) == "settled"


def test_usury_cap_vetoes_usurious_levy(world):
    """A loan above the cap: the levy is vetoed, so the lender falls back to
    seizing the collateral. Usurious interest is UNCOLLECTIBLE by force."""
    from econengine.models import WorldSetting
    session, lender, borrower, b_acct = world
    _install_usury_cap(session, lender)
    # Cap the legal rate at 1%/tick, then write a 10%/tick loan: usurious.
    session.add(WorldSetting(key="loan:usury_cap", value={"rate": "0.01"}))
    session.flush()
    create_loan(session, lender, borrower, Decimal("500"),
                rate=Decimal("0.10"), term=2,
                collateral={"symbol": "GRAIN", "quantity": "50"})
    for _ in range(2):
        run_tick(session)                              # -> default
    lender_before = _lender_account(lender).balance

    summary = enforce(session, lender, borrower)

    # The levy was vetoed -- no cash collected despite the borrower having it.
    assert Decimal(summary["levied"]) == Decimal("0")
    assert b_acct.balance == Decimal("1500")           # untouched (1000 + 500 loan)
    # The lender fell back to seizing the collateral.
    assert summary["seized"] is not None
    assert _grain(session, lender.entity) == Decimal("50")
    assert loan_status(lender, borrower) == "settled"


def test_usury_cap_unset_uses_default(world):
    """No WorldSetting -> the source DEFAULT_CAP (5%/tick) governs."""
    session, lender, borrower, b_acct = world
    _install_usury_cap(session, lender)                # no WorldSetting posted
    create_loan(session, lender, borrower, Decimal("500"),
                rate=Decimal("0.03"), term=2)          # 3% < 5% default: legal
    for _ in range(2):
        run_tick(session)
    summary = enforce(session, lender, borrower)
    assert Decimal(summary["levied"]) > Decimal("0")   # levy allowed under default cap
