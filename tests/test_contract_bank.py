"""Commercial bank reference contract (Step 5d) -- engine validation.

These tests run the real Lua servicing script (bank.lua) and the Python book
helpers against the real engine, proving the substrate claim this contract
exists to validate:

  * two-tier money -- a bank's DEPOSITS are a shadow ledger in script state,
    not engine accounts and not base money. LENDING creates deposit money by
    a book entry: it moves no base money and calls no engine primitive, so
    after a loan total deposits exceed reserves. Credit money is a book, not
    a ledger feature.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine.models import (
    Account, Base, EntityType, TransactionType, WorldSetting,
)
from econengine.services import (
    InsufficientFundsError, create_account, create_entity,
)
from econengine.tick import run_tick, set_compute_budget_ms

from contracts.bank.bank import (
    Bank, InsufficientDepositError, deposit, deposit_balance, interbank_pay,
    lend, loan_due, open_bank, pay, repay, reserve_ratio, total_deposits,
    total_loans, total_reserves, withdraw,
)

RESERVE_FLOOR = (Path(__file__).resolve().parent.parent
                 / "contracts" / "bank" / "reserve_floor.lua").read_text()


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """A government (monetary authority) + two depositors + a bank."""
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    g = create_account(session, gov, "USD", initial_balance=Decimal("100000"))
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    a = create_account(session, alice, "USD", initial_balance=Decimal("10000"))
    b = create_account(session, bob, "USD", initial_balance=Decimal("10000"))
    bank = open_bank(session, "FirstBank", "USD")
    return session, gov, g, alice, bob, a, b, bank


def base_supply(session):
    """Total base money: the sum of every engine account balance. Only
    issue_money/retire_money move this; transfer conserves it. Lending must
    leave it untouched -- that is the two-tier proof."""
    return session.execute(select(func.sum(Account.balance))).scalar_one()


# ---------------------------------------------------------------------------
# the star: lending creates deposit money, not base money
# ---------------------------------------------------------------------------

def test_lending_creates_more_deposit_money_than_reserves(world):
    """THE two-tier test. A deposit is 1:1 with reserves; a loan breaks that.
    After lending, total deposits exceed reserves, the reserve ratio drops
    below 1, and NOT ONE ISSUANCE transaction exists -- the bank created
    money by writing a book entry, never by calling issue_money."""
    session, gov, g, alice, bob, a, b, bank = world

    deposit(session, bank, alice, Decimal("100"))
    assert total_reserves(bank) == Decimal("100")
    assert total_deposits(bank) == Decimal("100")
    assert reserve_ratio(bank) == Decimal("1")        # 1:1, no lending yet
    assert _no_issuance_transactions(session)         # no money created yet
    supply_before = base_supply(session)

    # The bank lends Bob 300 it does not hold -- creating deposit money.
    lend(session, bank, bob, Decimal("300"))

    assert total_reserves(bank) == Decimal("100")     # base money UNCHANGED
    assert total_deposits(bank) == Decimal("400")     # 100 + 300: money grew
    assert reserve_ratio(bank) == Decimal("0.25")     # fractional reserve
    assert deposit_balance(bank, bob) == Decimal("300")
    # No base money was created: the supply is invariant across the loan.
    assert base_supply(session) == supply_before
    # And not one ISSUANCE transaction was ever written.
    assert _no_issuance_transactions(session)


def _no_issuance_transactions(session):
    from econengine.models import Transaction
    n = session.execute(
        select(func.count()).select_from(Transaction)
        .where(Transaction.tx_type == TransactionType.ISSUANCE)
    ).scalar()
    return n == 0


# ---------------------------------------------------------------------------
# money conservation: deposit / withdraw / pay
# ---------------------------------------------------------------------------

def test_deposit_and_withdraw_are_money_conserving(world):
    """Cashing in moves base money to reserves and raises the deposit;
    cashing out is the exact reverse. Both conserve base money."""
    session, gov, g, alice, bob, a, b, bank = world
    supply0 = base_supply(session)

    deposit(session, bank, alice, Decimal("250"))
    assert a.balance == Decimal("9750")
    assert total_reserves(bank) == Decimal("250")
    assert deposit_balance(bank, alice) == Decimal("250")
    assert base_supply(session) == supply0           # conserved

    withdraw(session, bank, alice, Decimal("100"))
    assert a.balance == Decimal("9850")
    assert total_reserves(bank) == Decimal("150")
    assert deposit_balance(bank, alice) == Decimal("150")
    assert base_supply(session) == supply0           # still conserved


def test_pay_between_depositors_is_pure_book(world):
    """An intra-bank payment moves NO base money -- it clears through the
    deposit book. Reserves and the base supply are untouched; only the
    deposit balances shift."""
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("200"))
    deposit(session, bank, bob, Decimal("50"))
    reserves_before = total_reserves(bank)
    supply_before = base_supply(session)
    total_before = total_deposits(bank)

    pay(session, bank, alice, bob, Decimal("75"))

    assert deposit_balance(bank, alice) == Decimal("125")
    assert deposit_balance(bank, bob) == Decimal("125")
    assert total_deposits(bank) == total_before       # total unchanged
    assert total_reserves(bank) == reserves_before    # no base money moved
    assert base_supply(session) == supply_before


def test_pay_rejects_insufficient_deposit_balance(world):
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("10"))
    with pytest.raises(InsufficientDepositError):
        pay(session, bank, alice, bob, Decimal("50"))


# ---------------------------------------------------------------------------
# the bank run: fractional reserve meets a withdrawal demand
# ---------------------------------------------------------------------------

def test_withdraw_beyond_reserves_fails(world):
    """A fractional bank owes more deposit money than it holds base money.
    Withdrawing past reserves fails with the engine's InsufficientFundsError
    -- the signal of a bank run. The book is left intact (debit happens only
    after the transfer succeeds)."""
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("100"))
    lend(session, bank, bob, Decimal("300"))          # ratio now 0.25

    # Bob has 300 of deposit money but the bank holds only 100 of reserves.
    assert deposit_balance(bank, bob) == Decimal("300")
    with pytest.raises(InsufficientFundsError):
        withdraw(session, bank, bob, Decimal("300"))  # bank run: refused
    # Book intact: Bob's deposit is still 300.
    assert deposit_balance(bank, bob) == Decimal("300")
    # He can withdraw up to the reserves the bank actually holds.
    withdraw(session, bank, bob, Decimal("100"))
    assert deposit_balance(bank, bob) == Decimal("200")


# ---------------------------------------------------------------------------
# repayment destroys money (the mirror of lending)
# ---------------------------------------------------------------------------

def test_repay_destroys_deposit_money(world):
    """Repaying a loan debits a deposit and reduces what is owed -- the
    deposit money created by lending vanishes back into the book."""
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("100"))
    lend(session, bank, bob, Decimal("300"))
    assert total_deposits(bank) == Decimal("400")

    repay(session, bank, bob, Decimal("300"))         # no interest accrued yet
    assert total_deposits(bank) == Decimal("100")     # back to 1:1
    assert deposit_balance(bank, bob) == Decimal("0")
    assert loan_due(bank, bob) == Decimal("0")


def test_repay_clamps_to_amount_owed(world):
    """Repaying more than is due clamps: no negative loan."""
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("100"))
    lend(session, bank, bob, Decimal("50"))
    overpay = repay(session, bank, bob, Decimal("1000"))
    assert overpay["applied"] == "50.0000" or overpay["applied"] == "50"
    assert deposit_balance(bank, bob) == Decimal("0")  # only 50 debited


def test_repay_rejects_insufficient_deposit(world):
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("100"))
    lend(session, bank, bob, Decimal("50"))       # bob owes 50, has 50 on deposit
    # Bob withdraws his whole deposit as cash (draining it to zero)...
    withdraw(session, bank, bob, Decimal("50"))   # bob deposit now 0
    # ...so he has no deposit money left to repay with.
    with pytest.raises(InsufficientDepositError):
        repay(session, bank, bob, Decimal("50"))


# ---------------------------------------------------------------------------
# the BEHAVIOUR script: interest accrual (ctx.tick) + book reconciliation
# ---------------------------------------------------------------------------

def test_interest_accrues_each_tick(world):
    """The servicing script accrues simple interest on outstanding loans
    each tick, driven by ctx.tick."""
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("100"))
    lend(session, bank, bob, Decimal("300"))          # rate 0.01/tick default
    assert loan_due(bank, bob) == Decimal("300")      # no interest yet

    run_tick(session)                                  # tick 1: +3.00 interest
    assert loan_due(bank, bob) == Decimal("303")
    run_tick(session)                                  # tick 2: +3.00 more
    assert loan_due(bank, bob) == Decimal("306")


def test_interest_is_skip_safe(world):
    """A compute-budget skip does not lose interest: elapsed catches up next
    run, paying the missed ticks in one go (Step 5a)."""
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("100"))
    lend(session, bank, bob, Decimal("300"))          # due 300

    run_tick(session)                                  # tick 1: due 303
    assert loan_due(bank, bob) == Decimal("303")

    set_compute_budget_ms(session, 0)                  # tick 2: script skipped
    run_tick(session)
    assert loan_due(bank, bob) == Decimal("303")       # still 303 (no run)

    set_compute_budget_ms(session, None)               # budget lifted
    run_tick(session)                                  # tick 3: catches up 2 ticks
    assert loan_due(bank, bob) == Decimal("309")       # 303 + 300*0.01*2


def test_script_reconciles_the_books(world):
    """Each tick the script stamps reserves / total deposits / the reserve
    ratio into state for observation."""
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("100"))
    lend(session, bank, bob, Decimal("300"))

    run_tick(session)
    st = bank.script.state
    assert Decimal(st["reserves"]) == Decimal("100")
    assert Decimal(st["total_deposits"]) == Decimal("400")
    assert Decimal(st["reserve_ratio"]) == Decimal("0.25")


# ---------------------------------------------------------------------------
# the VALIDATOR: a reserve floor on outbound transfers
# ---------------------------------------------------------------------------

def _install_reserve_floor(session, bank):
    from econengine.models import Script, ScriptType
    session.add(Script(
        name="reserve-floor", source=RESERVE_FLOOR,
        script_type=ScriptType.VALIDATOR, entity_id=bank.entity.id, is_active=True,
    ))
    session.flush()


def test_reserve_floor_denies_draining_transfer(world):
    """With a floor set, a withdrawal that would breach it is vetoed -- even
    though the bank could technically afford it (it has the base money)."""
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("1000"))    # 1000 reserves, 1000 dep
    _install_reserve_floor(session, bank)
    session.add(WorldSetting(key="bank:reserve_floor", value={"floor": "500"}))
    session.flush()

    # Withdrawing 600 would leave 400 < 500 floor -> vetoed.
    with pytest.raises(Exception):
        withdraw(session, bank, alice, Decimal("600"))
    # But 400 (leaving 600 >= 500) is allowed.
    withdraw(session, bank, alice, Decimal("400"))
    assert deposit_balance(bank, alice) == Decimal("600")


def test_reserve_floor_unset_allows_freely(world):
    """No floor configured -> the validator is a no-op (engine solvency
    check alone governs)."""
    session, gov, g, alice, bob, a, b, bank = world
    deposit(session, bank, alice, Decimal("1000"))
    _install_reserve_floor(session, bank)             # no WorldSetting posted
    withdraw(session, bank, alice, Decimal("1000"))   # drains to zero: allowed
    assert deposit_balance(bank, alice) == Decimal("0")


# ---------------------------------------------------------------------------
# interbank settlement: base money moves bank-to-bank
# ---------------------------------------------------------------------------

def test_interbank_payment_settles_in_base_money(world):
    """A depositor pays someone who banks elsewhere: the intra-bank book is
    not enough, so the two banks settle on the reserve layer -- base money
    moves from the payer's bank to the payee's bank."""
    session, gov, g, alice, bob, a, b, bank_a = world
    bank_b = open_bank(session, "SecondBank", "USD")
    bank_a_acct = create_account(session, bank_a.entity, "USD",
                                 initial_balance=Decimal("0"))  # noqa (reserve)
    # Alice banks at A, Bob banks at B.
    deposit(session, bank_a, alice, Decimal("200"))
    deposit(session, bank_b, bob, Decimal("50"))
    a_reserves_before = total_reserves(bank_a)
    b_reserves_before = total_reserves(bank_b)

    interbank_pay(session, bank_a, alice, bank_b, bob, Decimal("75"))

    # Books shifted...
    assert deposit_balance(bank_a, alice) == Decimal("125")
    assert deposit_balance(bank_b, bob) == Decimal("125")
    # ...and base money moved bank-to-bank on the reserve layer.
    assert total_reserves(bank_a) == a_reserves_before - Decimal("75")
    assert total_reserves(bank_b) == b_reserves_before + Decimal("75")
