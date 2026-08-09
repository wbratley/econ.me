"""Insurance — the data/helpers side of an event-triggered contract (Step 5d).

This is the reference contract that validates **``ctx.events`` as a trigger
source** — the one engine affordance no earlier contract exercises. An
**insurer** (a BUSINESS) collects **premiums** into a **risk pool** and pays a
**death benefit** to a designated **beneficiary** when a **trigger event**
fires for a **policyholder**. The trigger is read from ``ctx.events`` — the
previous tick's outcomes — not polled from a signal (Step 5c's other face).

Why a POLICY script, not BEHAVIOUR. A BEHAVIOUR script sees only ITS OWN
entity's events; the insurer must watch its POLICYHOLDERS' events (a death is
an event on the deceased, not the insurer). Only a POLICY script sees every
entity's events — so the insurer's mark-to-trigger engine is a POLICY script.
(The POLICY/BEHAVIOUR distinction is exactly an event-visibility distinction:
POLICY = global, BEHAVIOUR = own-entity. See ``tick.py``.)

Design (mirrors bond/bank/loan/futures: data in Python, policy in Lua):

  * ``insurance.lua`` (POLICY) is the whole back office. Each tick it scans
    ``ctx.events`` for the trigger event type, marks any matched policyholder
    ``triggered``, and then **pays** triggered-but-unpaid claims from the risk
    pool — a real ``ctx.action.transfer``. Payout is driven from Lua (unlike
    futures' Python ``settle``) because there is no try/except branching to do:
    a local pool counter prevents over-commitment (it decrements as it queues
    payouts), and the coverage oracle matches the payout amount, so the
    transfer cannot be vetoed or overdraft. This is the cleanest possible
    demonstration of *event → action*.
  * ``coverage_cap.lua`` (VALIDATOR) gates the insurer's outbound transfers to
    a *documented coverage* — fail-closed. The coverage is mirrored into a
    queryable WorldSetting at underwriting (the 5c pattern, as the loan's usury
    cap mirrors the loan book and the futures margin-sufficiency check mirrors
    the deficiency): a VALIDATOR has only its OWN state + queries, so it cannot
    read the insurer's policy book; the oracle is where it looks. A transfer
    from the pool to an undocumented beneficiary, or for more than the
    coverage, is vetoed — the risk pool is locked to its payouts.

State shape (insurer's POLICY script ``state``)::

    {
      "currency": "USD",
      "pool_account_id": "<acct>",
      "trigger": "entity_incapacitated",
      "policies": {
        "<policyholder_id>": {
          "beneficiary_account_id": "<acct>",
          "coverage":   "1000",
          "premium":    "50",
          "term":       20,            # absolute tick; null = perpetual
          "issued_tick": 1,
          "triggered":  false,         # set by insurance.lua when the event fires
          "trigger_tick": null,
          "paid":       false          # set by insurance.lua when the payout queues
        }
      },
      "total_coverage": "1000.0000"     # stamped by insurance.lua
    }
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from econengine.models import (Account, Entity, EntityType, Script,
                                ScriptType, Tick, WorldSetting)
from econengine.services import create_account, create_entity, transfer

SERVICER_SOURCE = (Path(__file__).parent / "insurance.lua").read_text()
"""The Lua trigger+pay script. Install it bound to the insurer as POLICY."""

#: Default trigger event (the canonical insurance case: a death benefit).
DEFAULT_TRIGGER = "entity_incapacitated"

#: The coverage-oracle key convention: ``insurance:coverage:<ACCT_ID>``.
COVERAGE_PREFIX = "insurance:coverage"


@dataclass
class Insurer:
    """A handle bundling the insurer's moving parts for ergonomic helper calls."""

    entity: Entity       # the insurer (BUSINESS)
    pool: Account        # the risk pool (premiums in, payouts out)
    script: Script       # the POLICY trigger+pay script (holds the policy book)
    currency: str


def open_insurer(
    session: Session,
    name: str,
    currency: str = "USD",
    *,
    trigger: str = DEFAULT_TRIGGER,
) -> Insurer:
    """Stand up an insurer: a ``BUSINESS`` entity, a risk-pool account, and a
    bound POLICY script that scans ``ctx.events`` for the trigger each tick.

    The trigger defaults to ``entity_incapacitated`` (a death benefit): when a
    policyholder is incapacitated (crosses an incapacitating condition
    threshold — see ``conditions.py``), the next tick's ``ctx.events`` carries
    the event and the insurer's POLICY script marks the policy triggered and
    pays the beneficiary. Other trigger event types (``decay``, ``need_unmet``,
    …) are supported by passing ``trigger=``; the matching is on event type +
    ``entity_id`` == policyholder.
    """
    entity = create_entity(session, name, EntityType.BUSINESS)
    pool = create_account(session, entity, currency, initial_balance=Decimal("0"))
    script = Script(
        name=f"{name}-engine",
        source=SERVICER_SOURCE,
        script_type=ScriptType.POLICY,
        entity_id=entity.id,
        is_active=True,
        state={"currency": currency, "pool_account_id": pool.id,
               "trigger": trigger, "policies": {}, "total_coverage": "0"},
    )
    session.add(script)
    session.flush()
    return Insurer(entity=entity, pool=pool, script=script, currency=currency)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _account(entity: Entity, currency: str) -> Account:
    for acct in entity.accounts:
        if acct.currency == currency:
            return acct
    raise ValueError(f"{entity.name} has no {currency} account")


def _latest_tick(session: Session) -> int:
    row = session.execute(select(func.max(Tick.number))).scalar()
    return row if row is not None else 0


def _write_coverage_oracle(session: Session, beneficiary_account_id: str,
                           coverage: Decimal) -> None:
    """Mirror a policy's coverage into a queryable WorldSetting so the
    coverage-cap VALIDATOR can see it (a validator cannot read another
    script's state — the 5c oracle pattern, as the loan's usury cap)."""
    key = f"{COVERAGE_PREFIX}:{beneficiary_account_id}"
    setting = session.get(WorldSetting, key)
    value = {"max": str(coverage)}
    if setting is None:
        session.add(WorldSetting(key=key, value=value))
    else:
        setting.value = value
    session.flush()


# ---------------------------------------------------------------------------
# the policy lifecycle: underwrite
# ---------------------------------------------------------------------------

def underwrite(
    session: Session,
    insurer: Insurer,
    policyholder: Entity,
    beneficiary: Entity,
    coverage: Decimal,
    premium: Decimal,
    *,
    term: int | None = None,
) -> dict:
    """Issue a policy: collect the premium, record the policy, publish the
    coverage oracle.

    A single ``transfer`` moves the premium from the policyholder into the risk
    pool (REAL base money — no money created). The policy is a book entry
    recording the coverage, the beneficiary, and the term. The coverage is also
    mirrored into a WorldSetting (``insurance:coverage:<beneficiary_acct>``) so
    the coverage-cap VALIDATOR can gate the eventual payout — without it, the
    insurer's pool would be ungovernable from the constitutional tier (a
    VALIDATOR cannot read this script's state).

    ``coverage`` is the death benefit (paid to the beneficiary on trigger).
    ``premium`` is the one-time price (collected now). ``term`` is the expiry
    tick (``None`` = perpetual; past term, the policy lapses — no trigger
    pays).
    """
    if coverage <= 0:
        raise ValueError("coverage must be positive")
    if premium < 0:
        raise ValueError("premium must be non-negative")
    pol_acct = _account(policyholder, insurer.currency)
    benef_acct = _account(beneficiary, insurer.currency)
    if premium > 0:
        transfer(session, pol_acct, insurer.pool, premium, "insurance-premium")
    state = dict(insurer.script.state)
    policies = dict(state.get("policies") or {})
    policies[policyholder.id] = {
        "beneficiary_account_id": benef_acct.id,
        "coverage": str(coverage),
        "premium": str(premium),
        "term": term,
        "issued_tick": _latest_tick(session),
        "triggered": False,
        "trigger_tick": None,
        "paid": False,
    }
    state["policies"] = policies
    insurer.script.state = state
    _write_coverage_oracle(session, benef_acct.id, coverage)
    return {"policyholder": policyholder.id, "beneficiary": beneficiary.id,
            "beneficiary_account": benef_acct.id,
            "coverage": str(coverage), "premium": str(premium), "term": term}


# ---------------------------------------------------------------------------
# read helpers — the book at a glance
# ---------------------------------------------------------------------------

def policy(insurer: Insurer, policyholder_id) -> dict | None:
    """A policy record (or ``None``). A snapshot of ``script.state``."""
    rec = insurer.script.state.get("policies", {}).get(str(policyholder_id))
    return dict(rec) if rec else None


def is_triggered(insurer: Insurer, policyholder_id) -> bool:
    rec = policy(insurer, policyholder_id)
    return bool(rec and rec.get("triggered"))


def is_paid(insurer: Insurer, policyholder_id) -> bool:
    rec = policy(insurer, policyholder_id)
    return bool(rec and rec.get("paid"))


def total_coverage(insurer: Insurer) -> Decimal:
    """Sum of coverage over in-force policies (stamped by insurance.lua)."""
    return Decimal(insurer.script.state.get("total_coverage", "0"))


def risk_pool_balance(insurer: Insurer) -> Decimal:
    """The risk pool's current balance (premiums collected − payouts made)."""
    return insurer.pool.balance
