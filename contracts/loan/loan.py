"""Secured loan — the data/helpers side of a private-debt contract (Step 5d).

This is the reference contract that validates ``levy``/``seize`` as the
enforcement spine of private debt. A **lender** lends REAL BASE MONEY (a
``transfer``), takes **collateral** (goods pledged and recorded in state), and
on default **enforces**: ``levy`` to take payment by force, ``seize`` to take
the collateral by force. Both are privileged engine primitives — the lender
must hold the ``LEVY`` and ``SEIZE`` capabilities, granted by the state (a
lender's license, a court judgment). This is the delegation of sovereign
enforcement power to a private creditor: the same primitives the state uses to
tax and expropriate, now lent to a creditor to collect a private debt.

Contrast with the bank (``contracts/bank``): the bank lends DEPOSIT MONEY (a
book entry, unsecured — there is nothing to seize, only a bank run). This
contract lends BASE MONEY (a ``transfer``, secured by collateral). The
enforcement dimension — ``levy``/``seize`` on default — is what the bank lacks
and what this contract exists to demonstrate.

Design (mirrors the bank: data in Python, policy in Lua):

  * ``loan.lua`` (BEHAVIOUR) accrues interest each tick from ``ctx.tick`` and
    marks a loan ``default`` at maturity if unpaid. It does NOT enforce —
    enforcement is a discrete legal act.
  * ``enforce()`` (below) is that act: ``levy`` available cash, then ``seize``
    the collateral. It is Python (not Lua) because it needs try/except
    branching — try the levy, fall through to seizure on shortfall — which the
    engine's deferred Lua-intent resolution cannot express (a script queues
    intents that resolve only after it returns, so it cannot branch on an
    outcome it cannot yet see). Both halves go through ``services.levy`` /
    ``services.seize``, which fire validators — so a usury cap (below) gates
    the levy regardless of the call path.
  * ``usury_cap.lua`` (VALIDATOR) gates the lender's ``levy`` to the statutory
    maximum claim — a constitutional constraint on predatory lending,
    fail-closed. The cap lives in a WorldSetting (``loan:usury_cap``); the
    validator is the backstop that makes it un-circumventable.

State shape (lender's BEHAVIOUR script ``state``)::

    {
      "currency": "USD",
      "default_rate": "0.01",
      "loans": {
        "<borrower_id>": {
          "account_id":   "<settlement account>",   # what gets levied
          "principal":    "100",                    # disbursed (money string)
          "rate":         "0.02",                   # per-tick interest fraction
          "interest_due": "4.0000",                 # accrued by loan.lua
          "issue_tick":   0,
          "maturity":     5,                        # absolute due tick
          "last_accrued_tick": 2,                   # advanced by loan.lua
          "paid":         "0",                      # principal+interest repaid
          "collateral":   {"symbol": "GRAIN", "quantity": "50"},  # or null
          "status":       "active",                 # active | default | settled
          "default_tick": null                      # set by loan.lua
        }
      }
    }
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from econengine import services
from econengine.markets import InsufficientHoldingsError
from econengine.models import (Account, Entity, EntityType, Script,
                                ScriptType, Tick, WorldSetting)
from econengine.scripting import OperationVetoedError
from econengine.services import (
    InsufficientFundsError, create_account, create_entity, transfer,
)
from sqlalchemy import func, select

SERVICER_SOURCE = (Path(__file__).parent / "loan.lua").read_text()
"""The Lua servicing script. Install it bound to the lender as a BEHAVIOUR."""

#: Default per-tick interest rate (1% of outstanding principal/tick).
DEFAULT_RATE = Decimal("0.01")


@dataclass
class Loan:
    """A handle bundling the lender's moving parts for ergonomic helper calls."""

    entity: Entity       # the lender (a BUSINESS with LEVY + SEIZE capabilities)
    account: Account     # the lender's settlement account (base money)
    script: Script       # the BEHAVIOUR servicing script (holds the loan book)
    currency: str
    default_rate: Decimal


def open_lender(
    session: Session,
    name: str,
    currency: str = "USD",
    *,
    default_rate: Decimal = DEFAULT_RATE,
    capital: Decimal = Decimal("0"),
) -> Loan:
    """Stand up a lending business: a ``BUSINESS`` entity, a settlement
    account, and a bound BEHAVIOUR servicing script.

    The lender is NOT born with enforcement power: ``LEVY`` and ``SEIZE`` are
    sovereign capabilities the state grants separately (a lender's license, a
    court order). Without them ``enforce()`` will raise ``MissingCapabilityError``
    — the honest signal that a creditor without the state's backing cannot
    collect by force. Grant them with ``entity.capabilities`` (test/operator)
    or the ``grant_capability`` primitive (governed). ``capital`` is seed base
    money to lend from.
    """
    entity = create_entity(session, name, EntityType.BUSINESS)
    account = create_account(session, entity, currency, initial_balance=capital)
    script = Script(
        name=f"{name}-servicer",
        source=SERVICER_SOURCE,
        script_type=ScriptType.BEHAVIOUR,
        entity_id=entity.id,
        is_active=True,
        state={"currency": currency, "default_rate": str(default_rate), "loans": {}},
    )
    session.add(script)
    session.flush()
    return Loan(entity=entity, account=account, script=script,
                currency=currency, default_rate=default_rate)


# ---------------------------------------------------------------------------
# helpers
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


# Each loan's terms are mirrored into a queryable WorldSetting (the 5c
# signal pattern) keyed by the borrower's settlement account. The BEHAVIOUR
# script's state is private to that script; a VALIDATOR has only its OWN
# state + queries. So for a usury-cap validator to see the loan book, the
# book must live where a query can reach it. This "loan oracle" is that
# place -- and a bonus: a loan's terms become observable without reading
# script internals. Maintained by the Python helpers below.
ORACLE_PREFIX = "loan:account"


def _oracle_key(account_id: str) -> str:
    return f"{ORACLE_PREFIX}:{account_id}"


def _sync_oracle(session: Session, account_id: str, principal, rate,
                 issue_tick, paid) -> None:
    """Mirror a loan's cap-relevant terms into its queryable WorldSetting."""
    key = _oracle_key(account_id)
    setting = session.get(WorldSetting, key)
    value = {"principal": str(principal), "rate": str(rate),
             "issue_tick": str(issue_tick), "paid": str(paid)}
    if setting is None:
        session.add(WorldSetting(key=key, value=value))
    else:
        setting.value = value
    session.flush()


def _loan_record(loan: Loan, borrower: Entity) -> dict | None:
    rec = loan.script.state.get("loans", {}).get(borrower.id)
    return dict(rec) if rec else None


# ---------------------------------------------------------------------------
# the loan lifecycle: create / repay / enforce
# ---------------------------------------------------------------------------

def create_loan(
    session: Session,
    loan: Loan,
    borrower: Entity,
    principal: Decimal,
    *,
    rate: Decimal | None = None,
    term: int = 5,
    collateral: dict | None = None,
) -> dict:
    """Originate a secured loan: disburse base money, book the debt + pledge.

    A ``transfer`` of REAL base money (lender -> borrower). Unlike the bank's
    ``lend`` (which creates deposit money by a book entry), this moves existing
    money — the lender must have it. ``collateral`` (``{"symbol", "quantity"}``)
    is recorded as a pledge; the goods STAY with the borrower until default (a
    mortgage, not a pawn — the borrower keeps using the collateral until
    foreclosure). Because the engine has no lien concept, a borrower who sells
    the pledged goods before default leaves the lender nothing to seize; that
    is a known limitation (see README), surfaced honestly by ``seize`` raising
    ``InsufficientHoldingsError`` at foreclosure time.

    ``term`` is the loan's duration in ticks; maturity is ``issue_tick + term``.

    Returns a summary of the booked loan.
    """
    if principal <= 0:
        raise ValueError("principal must be positive")
    if term <= 0:
        raise ValueError("term must be positive")
    loan_rate = loan.default_rate if rate is None else rate
    borrower_acct = _account(borrower, loan.currency)
    # Disburse: real base money leaves the lender for the borrower.
    transfer(session, loan.account, borrower_acct, principal, "loan-disburse")
    coll = None
    if collateral is not None:
        if "symbol" not in collateral or "quantity" not in collateral:
            raise ValueError("collateral needs 'symbol' and 'quantity'")
        coll = {"symbol": str(collateral["symbol"]),
                "quantity": str(Decimal(collateral["quantity"]))}
    now = _latest_tick(session)
    state = dict(loan.script.state)
    loans = dict(state.get("loans") or {})
    loans[borrower.id] = {
        "account_id": borrower_acct.id,
        "principal": str(principal),
        "rate": str(loan_rate),
        "interest_due": "0",
        "issue_tick": now,
        "maturity": now + term,
        "last_accrued_tick": now,
        "paid": "0",
        "collateral": coll,
        "status": "active",
        "default_tick": None,
    }
    state["loans"] = loans
    loan.script.state = state
    _sync_oracle(session, borrower_acct.id, principal, loan_rate, now, Decimal("0"))
    return {"borrower": borrower.id, "principal": str(principal),
            "rate": str(loan_rate), "maturity": now + term, "collateral": coll}


def repay(session: Session, loan: Loan, borrower: Entity, amount: Decimal) -> dict:
    """Voluntary repayment: base money returns to the lender, the debt shrinks.

    A ``transfer`` of the borrower's own money (no privilege needed). Applied
    to principal + accrued interest - already paid; overpayment clamps to what
    is owed. Settles the loan if the debt is fully covered.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    rec = _loan_record(loan, borrower)
    if rec is None or rec.get("status") == "settled":
        raise ValueError(f"no outstanding loan for {borrower.name}")
    borrower_acct = _account(borrower, loan.currency)
    owed = loan_due(loan, borrower)
    applied = min(amount, owed)
    if borrower_acct.balance < applied:
        raise InsufficientFundsError(
            f"account {borrower_acct.id} has {borrower_acct.balance} "
            f"{borrower_acct.currency}, need {applied}")
    transfer(session, borrower_acct, loan.account, applied, "loan-repay")
    state = dict(loan.script.state)
    loans = dict(state.get("loans") or {})
    rec = dict(loans[borrower.id])
    rec["paid"] = str(Decimal(rec["paid"]) + applied)
    if Decimal(rec["paid"]) >= (Decimal(rec["principal"])
                                + Decimal(rec["interest_due"])):
        rec["status"] = "settled"
    loans[borrower.id] = rec
    state["loans"] = loans
    loan.script.state = state
    _sync_oracle(session, rec["account_id"], rec["principal"], rec["rate"],
                 rec["issue_tick"], Decimal(rec["paid"]))
    return {"applied": str(applied), "due_after": str(owed - applied)}


def enforce(session: Session, loan: Loan, borrower: Entity) -> dict:
    """Foreclose: levy available cash, then seize the collateral.

    The enforcement spine of private debt — the whole point of this contract.
    Tries to collect the owed amount in two privileged steps:

      1. **Levy** — take cash by force from the borrower's settlement account
         (up to the debt). Gated by the ``LEVY`` capability and the usury-cap
         VALIDATOR: a usurious loan's levy is vetoed, and the lender falls
         through to seizure.
      2. **Seize** — take the pledged collateral by force (goods). Gated by the
         ``SEIZE`` capability. If the collateral has fled (the borrower sold
         it), ``seize`` raises ``InsufficientHoldingsError`` — caught and
         reported, not crashed.

    **Non-recourse:** seizing the collateral settles the loan regardless of
    recovery. The lender forecloses, takes what it can, the debt is
    extinguished — the lender bore the collateral risk. (A recourse model would
    keep the deficiency alive; see README.) Returns a summary of the recovery.
    """
    rec = _loan_record(loan, borrower)
    if rec is None:
        raise ValueError(f"no loan for {borrower.name}")
    borrower_acct = _account(borrower, loan.currency)
    owed = loan_due(loan, borrower)
    summary = {"owed": str(owed), "levied": "0", "seized": None, "settled": False}
    rule = f"loan:{borrower.id}"

    # 1. Levy: collect available cash, up to the debt. A shortfall (the
    #    borrower is broke) or a usury-cap veto is not fatal — fall through.
    if owed > 0:
        take = min(borrower_acct.balance, owed)
        if take > 0:
            try:
                services.levy(session, loan.entity, borrower_acct, loan.account,
                              take, rule_ref=rule,
                              reference=f"foreclose-levy:{borrower.id}")
                summary["levied"] = str(take)
                owed -= take
            except (InsufficientFundsError, OperationVetoedError):
                pass  # broke, or usury cap vetoed — try the collateral next

    # 2. Seize: take the pledged collateral (non-recourse: settles the loan).
    collateral = rec.get("collateral")
    if collateral and owed > 0:
        try:
            services.seize(session, loan.entity, borrower,
                           symbol=collateral["symbol"],
                           quantity=Decimal(collateral["quantity"]),
                           rule_ref=rule,
                           reference=f"foreclose-seize:{borrower.id}")
            summary["seized"] = collateral
        except (InsufficientHoldingsError, OperationVetoedError):
            pass  # collateral fled, or another validator vetoed — reported as None

    # 3. Foreclose: the loan is closed (non-recourse), whatever was recovered.
    state = dict(loan.script.state)
    loans = dict(state.get("loans") or {})
    rec = dict(loans[borrower.id])
    if Decimal(summary["levied"]) > 0:
        rec["paid"] = str(Decimal(rec["paid"]) + Decimal(summary["levied"]))
    rec["status"] = "settled"
    loans[borrower.id] = rec
    state["loans"] = loans
    loan.script.state = state
    _sync_oracle(session, rec["account_id"], rec["principal"], rec["rate"],
                 rec["issue_tick"], Decimal(rec["paid"]))
    summary["settled"] = True
    return summary


# ---------------------------------------------------------------------------
# read helpers — the book at a glance. interest_due is current as of the last
# tick the BEHAVIOUR script ran.
# ---------------------------------------------------------------------------

def loan_due(loan: Loan, borrower: Entity) -> Decimal:
    """Principal + accrued interest - paid for a borrower's loan (0 if none)."""
    rec = _loan_record(loan, borrower)
    if rec is None or rec.get("status") == "settled":
        return Decimal("0")
    return (Decimal(rec["principal"]) + Decimal(rec["interest_due"])
            - Decimal(rec["paid"]))


def loan_status(loan: Loan, borrower: Entity) -> str:
    """``active`` | ``default`` | ``settled`` (``"none"`` if no loan)."""
    rec = _loan_record(loan, borrower)
    return rec.get("status", "none") if rec else "none"


def is_in_default(loan: Loan, borrower: Entity) -> bool:
    return loan_status(loan, borrower) == "default"


def total_outstanding(loan: Loan) -> Decimal:
    """The lender's total book: sum of (principal + interest - paid) over all
    non-settled loans. A measure of credit extended and still at risk."""
    return sum(
        (Decimal(r["principal"]) + Decimal(r["interest_due"]) - Decimal(r["paid"])
         for r in loan.script.state.get("loans", {}).values()
         if r.get("status") != "settled"),
        Decimal("0"),
    )
