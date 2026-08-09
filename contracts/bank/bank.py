"""Commercial bank — the book (data) side of a two-tier money contract.

This module is the *data* side of a bank (design.md §2: an instrument is
data + a script). The *policy* side -- interest accrual and a per-tick
reconciliation of the books -- lives in ``bank.lua``, bound to the bank as a
BEHAVIOUR script.

The model (the whole point -- design.md, actors.md "Step 5 design"):

  * **Base money / reserves** is a real engine ``Account`` (the bank's
    reserve account). Only ``issue_money`` (a MONETARY_AUTHORITY op) creates
    it. The bank never calls ``issue_money``.
  * **Deposit money** is a *shadow ledger* in the bank's script ``state`` --
    ``state.deposits[customer_id]``. A deposit is a *claim on the bank*, not
    base money. A customer's "checking balance" is this number.
  * **Lending creates money.** ``lend`` credits a deposit (a book entry in
    state) and books a loan -- it moves NO base money and calls NO engine
    primitive. After a loan, total deposits exceed reserves: the bank has
    created money out of a book entry, exactly as real banks do. The engine
    did nothing; credit money is a book, not a ledger feature.

Operations (each is the honest mechanic, traceable end to end):

  * ``deposit``  -- customer cash-in:  ``transfer`` cash in  + credit deposit
  * ``withdraw`` -- customer cash-out: ``transfer`` cash out + debit deposit
  * ``pay``      -- pay another depositor: PURE BOOK (no base money moves;
                    payments clear through the deposit book)
  * ``lend``     -- credit deposit + book loan (CREATE deposit money)
  * ``repay``    -- debit deposit + reduce loan due (DESTROY deposit money)
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from econengine.models import Account, Entity, EntityType, Script, ScriptType, Tick
from econengine.services import create_account, create_entity, transfer

SERVICER_SOURCE = (Path(__file__).parent / "bank.lua").read_text()
"""The Lua servicing script. Install it bound to the bank as a BEHAVIOUR."""

#: Default per-tick interest rate on loans (1% of outstanding principal/tick).
DEFAULT_RATE = Decimal("0.01")


class InsufficientDepositError(ValueError):
    """A debit/repay asked for more deposit money than the holder has."""


@dataclass
class Bank:
    """A handle bundling the bank's moving parts for ergonomic helper calls."""

    entity: Entity
    reserve: Account      # the base-money reserve account (real engine Account)
    script: Script        # the BEHAVIOUR servicing script (holds the book)
    currency: str
    default_rate: Decimal


def open_bank(
    session: Session,
    name: str,
    currency: str = "USD",
    *,
    default_rate: Decimal = DEFAULT_RATE,
    capital: Decimal = Decimal("0"),
) -> Bank:
    """Charter a commercial bank: a ``BANK`` entity, a reserve account, and
    a bound BEHAVIOUR servicing script.

    ``capital`` is seed equity -- real base money the bank holds but owes to
    no depositor (shareholders' capital, the loss-absorbing buffer). It is
    seeded straight into the reserve account; a real world would raise it by
    selling shares (a ``transfer`` of existing money in). The bank is NOT a
    monetary authority and never creates base money.
    """
    entity = create_entity(session, name, EntityType.BANK)
    reserve = create_account(session, entity, currency, initial_balance=capital)
    script = Script(
        name=f"{name}-servicer",
        source=SERVICER_SOURCE,
        script_type=ScriptType.BEHAVIOUR,
        entity_id=entity.id,
        is_active=True,
        state={
            "currency": currency,
            "default_rate": str(default_rate),
            "deposits": {},
            "loans": {},
        },
    )
    session.add(script)
    session.flush()
    return Bank(entity=entity, reserve=reserve, script=script,
                currency=currency, default_rate=default_rate)


# ---------------------------------------------------------------------------
# book helpers -- mutate the deposit/loan ledgers in the script's state.
# state is a plain JSON column (not mutable-tracked): read-copy-mutate, then
# reassign the WHOLE dict to persist.
# ---------------------------------------------------------------------------

def _account(entity: Entity, currency: str) -> Account:
    for acct in entity.accounts:
        if acct.currency == currency:
            return acct
    raise ValueError(f"{entity.name} has no {currency} account")


def _latest_tick(session: Session) -> int:
    """Number of the most recently committed Tick, or 0 before tick 1."""
    row = session.execute(select(func.max(Tick.number))).scalar()
    return row if row is not None else 0


def deposit(session: Session, bank: Bank, customer: Entity, amount: Decimal) -> None:
    """Customer cashes IN: base money moves to reserves, deposit balance rises.

    Money-conserving: a ``transfer`` of EXISTING money (customer -> reserve)
    plus a book entry crediting the deposit. No money is created.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    customer_acct = _account(customer, bank.currency)
    transfer(session, customer_acct, bank.reserve, amount, "deposit")
    state = dict(bank.script.state)
    deposits = dict(state.get("deposits") or {})
    cid = customer.id
    deposits[cid] = str(Decimal(deposits.get(cid, "0")) + amount)
    state["deposits"] = deposits
    bank.script.state = state


def withdraw(session: Session, bank: Bank, customer: Entity, amount: Decimal) -> None:
    """Customer cashes OUT: deposit balance falls, base money leaves reserves.

    Money-conserving in reverse. If reserves cannot cover the withdrawal the
    underlying ``transfer`` raises ``InsufficientFundsError`` -- the engine
    signal of a bank run (the bank owes more deposit money than it holds base
    money, because it lent the rest). The deposit is debited only after the
    transfer succeeds, so a failed withdrawal leaves the book intact.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    customer_acct = _account(customer, bank.currency)
    transfer(session, bank.reserve, customer_acct, amount, "withdraw")
    state = dict(bank.script.state)
    deposits = dict(state.get("deposits") or {})
    cid = customer.id
    bal = Decimal(deposits.get(cid, "0"))
    if bal < amount:
        raise InsufficientDepositError(
            f"deposit balance {bal} for {customer.name}, need {amount}")
    deposits[cid] = str(bal - amount)
    state["deposits"] = deposits
    bank.script.state = state


def pay(session: Session, bank: Bank, payer: Entity, payee: Entity,
        amount: Decimal) -> None:
    """One depositor pays another: a PURE BOOK transfer -- no base money moves.

    This is how the vast majority of payments clear in a real economy: both
    parties bank here, so the bank just debits one deposit and credits the
    other. Total deposits are unchanged; reserves are untouched. (An
    interbank payment -- payee banks elsewhere -- must settle in base money;
    see README.)
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    state = dict(bank.script.state)
    deposits = dict(state.get("deposits") or {})
    pid, payee_id = payer.id, payee.id
    bal = Decimal(deposits.get(pid, "0"))
    if bal < amount:
        raise InsufficientDepositError(
            f"deposit balance {bal} for {payer.name}, need {amount}")
    deposits[pid] = str(bal - amount)
    deposits[payee_id] = str(Decimal(deposits.get(payee_id, "0")) + amount)
    state["deposits"] = deposits
    bank.script.state = state


def interbank_pay(session: Session, from_bank: Bank, payer: Entity,
                   to_bank: Bank, payee: Entity, amount: Decimal) -> None:
    """A depositor pays someone who banks ELSEWHERE: settle in base money.

    Unlike an intra-bank ``pay`` (a pure book transfer), an interbank payment
    MUST move base money: the payer's bank transfers reserves to the payee's
    bank, and each adjusts its own deposit book. This is the reserve-layer
    settlement the roadmap names -- the moment the two-tier system touches
    base money. If ``from_bank``'s reserves cannot cover it (it lent them
    out), the ``transfer`` fails: an interbank liquidity squeeze. Both banks
    must use the same currency.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    from_bal = deposit_balance(from_bank, payer)
    if from_bal < amount:
        raise InsufficientDepositError(
            f"deposit balance {from_bal} for {payer.name}, need {amount}")
    # Settle on the reserve layer first (may raise on insufficient reserves);
    # commit both books only once the base money has moved.
    transfer(session, from_bank.reserve, to_bank.reserve, amount, "interbank")
    state = dict(from_bank.script.state)
    deposits = dict(state.get("deposits") or {})
    deposits[payer.id] = str(from_bal - amount)
    state["deposits"] = deposits
    from_bank.script.state = state
    state2 = dict(to_bank.script.state)
    deposits2 = dict(state2.get("deposits") or {})
    deposits2[payee.id] = str(Decimal(deposits2.get(payee.id, "0")) + amount)
    state2["deposits"] = deposits2
    to_bank.script.state = state2


def lend(session: Session, bank: Bank, borrower: Entity, principal: Decimal,
         *, rate: Decimal | None = None) -> dict:
    """Create a loan -- and the deposit money to spend it.

    Credits the borrower's deposit (a BOOK ENTRY in state) and books the loan.
    Moves NO base money, calls NO engine primitive: the deposit was created by
    the book entry, not by ``issue_money``. After this, total deposits exceed
    reserves -- the bank has created money by lending. That is the two-tier
    money boundary this contract exists to demonstrate.

    Returns a summary of the booked loan.
    """
    if principal <= 0:
        raise ValueError("principal must be positive")
    loan_rate = bank.default_rate if rate is None else rate
    state = dict(bank.script.state)
    deposits = dict(state.get("deposits") or {})
    loans = dict(state.get("loans") or {})
    bid = borrower.id
    deposits[bid] = str(Decimal(deposits.get(bid, "0")) + principal)
    loans[bid] = {
        "principal": str(principal),
        "rate": str(loan_rate),
        "interest_due": "0",
        "last_accrued_tick": _latest_tick(session),
        "paid": "0",
        "repaid": False,
    }
    state["deposits"] = deposits
    state["loans"] = loans
    bank.script.state = state
    return {"borrower": bid, "principal": str(principal), "rate": str(loan_rate)}


def repay(session: Session, bank: Bank, borrower: Entity, amount: Decimal) -> dict:
    """Repay a loan -- and destroy the deposit money used to do it.

    Debits the borrower's deposit and applies it to what is owed (principal +
    accrued interest - already paid). The deposit money vanishes (a book
    entry destroyed), mirroring how lending created it. ``amount`` over the
    due is clamped to the due (no negative loan).

    Returns how much was applied and what remains due.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    state = dict(bank.script.state)
    loans = dict(state.get("loans") or {})
    loan = dict(loans.get(borrower.id) or {})
    if not loan or loan.get("repaid"):
        raise ValueError(f"no outstanding loan for {borrower.name}")
    deposits = dict(state.get("deposits") or {})
    bid = borrower.id
    bal = Decimal(deposits.get(bid, "0"))
    owed = (Decimal(loan["principal"]) + Decimal(loan["interest_due"])
            - Decimal(loan["paid"]))
    applied = min(amount, owed)
    if bal < applied:
        raise InsufficientDepositError(
            f"deposit balance {bal} for {borrower.name}, need {applied}")
    deposits[bid] = str(bal - applied)
    loan["paid"] = str(Decimal(loan["paid"]) + applied)
    if Decimal(loan["paid"]) >= (Decimal(loan["principal"])
                                 + Decimal(loan["interest_due"])):
        loan["repaid"] = True
    loans[borrower.id] = loan
    state["deposits"] = deposits
    state["loans"] = loans
    bank.script.state = state
    return {"applied": str(applied), "due_after": str(owed - applied)}


# ---------------------------------------------------------------------------
# read helpers -- the book at a glance. Reads from state (loans' interest_due
# is current as of the last tick the BEHAVIOUR script ran).
# ---------------------------------------------------------------------------

def deposit_balance(bank: Bank, customer: Entity) -> Decimal:
    return Decimal(bank.script.state.get("deposits", {}).get(customer.id, "0"))


def total_deposits(bank: Bank) -> Decimal:
    """The deposit-money supply -- the sum of all deposit claims on the bank."""
    return sum(
        (Decimal(v) for v in bank.script.state.get("deposits", {}).values()),
        Decimal("0"),
    )


def total_reserves(bank: Bank) -> Decimal:
    """Base money the bank actually holds (its reserve account balance)."""
    return Decimal(bank.reserve.balance)


def reserve_ratio(bank: Bank) -> Decimal | None:
    """Reserves / deposits. ``None`` before any deposits (undefined). Below 1
    means fractional: the bank has lent deposits into existence and cannot
    honor every depositor at once -- the normal, intended state of a bank."""
    d = total_deposits(bank)
    return total_reserves(bank) / d if d > 0 else None


def loan_due(bank: Bank, borrower: Entity) -> Decimal:
    """Principal + accrued interest - paid for a borrower's loan (0 if none)."""
    loan = bank.script.state.get("loans", {}).get(borrower.id)
    if not loan or loan.get("repaid"):
        return Decimal("0")
    return (Decimal(loan["principal"]) + Decimal(loan["interest_due"])
            - Decimal(loan["paid"]))


def total_loans(bank: Bank) -> Decimal:
    """Outstanding loan principal (loans not yet repaid)."""
    return sum(
        (Decimal(l["principal"]) for l in bank.script.state.get("loans", {}).values()
         if not l.get("repaid")),
        Decimal("0"),
    )
