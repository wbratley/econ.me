"""Futures + margin — the data/helpers side of a derivatives contract (Step 5d).

This is the reference contract that validates ``seize`` as a **margin call**
and validates the **signal convention** (Step 5c). An **exchange** (a central
counterparty, CCP) matches a **long** (agrees to buy) and a **short** (agrees
to sell) a quantity of a good at a contract price, expiring at a tick. Both
post **cash margin** (a ``transfer`` into the exchange's commingled pool). Each
tick ``futures.lua`` (BEHAVIOUR) reads a **signal price** — an oracle
``WorldSetting`` posted by the platform — and **marks to market**: each side's
*credit* (a book entry) is recomputed. Mark-to-market is a pure book update —
no money moves (the margin pool is commingled), exactly like the bank's
intra-bank ``pay``. On settlement the exchange pays out from the pool; if a
side is in **deficiency** (credit negative — losses exceeded posted margin),
the exchange ``seize``s goods from the defaulter and redirects them to the
winner, making the winner whole without any cash-conversion step.

Design (mirrors the bond/bank/loan: data in Python, policy in Lua):

  * ``futures.lua`` (BEHAVIOUR) marks every open position to market each tick
    from ``ctx.query.world_setting("futures:price:" .. symbol)`` and flags
    **breach** (a credit below maintenance margin) and **expiry**. It does NOT
    settle — settlement is a discrete act.
  * ``settle()`` (below) is that act. It reads the final signal, computes each
    side's credit, and pays out. It is Python (not Lua) because the deficiency
    case needs try/except branching (seize, and if the defaulter has no goods,
    the winner takes a haircut) that the engine's deferred Lua-intent
    resolution cannot express — the same reason ``loan.enforce()`` is Python.
  * ``margin_sufficiency.lua`` (VALIDATOR) gates the exchange's ``seize`` to a
    *documented deficiency* — a constitutional margin-sufficiency check,
    fail-closed. The deficiency is mirrored into a queryable WorldSetting (the
    5c signal pattern, as the loan's usury cap mirrors the loan book): a
    VALIDATOR has only its OWN state + queries, so it cannot read the
    exchange's position book; the oracle is where it looks.

The exchange must hold the ``SEIZE`` capability (for deficiency seizure) —
sovereign power delegated to a private counterparty, exactly as the lender's
``LEVY``/``SEIZE`` license in the loan contract.

State shape (exchange's BEHAVIOUR script ``state``)::

    {
      "currency": "USD",
      "maintenance_fraction": "0.5",
      "next_pos_id": 2,
      "positions": {
        "1": {
          "long":          "<eid>",
          "short":         "<eid>",
          "symbol":        "GRAIN",
          "quantity":      "100",
          "price":         "5.00",          # contract price
          "expiry":        10,              # absolute tick
          "long_margin":   "100",           # initial margin posted (constant)
          "short_margin":  "100",
          "long_credit":   "110.0000",      # mark-to-market (stamped by futures.lua)
          "short_credit":  "90.0000",
          "last_mark":     2,               # advanced by futures.lua
          "status":        "open"           # open | breached | expired | settled
        }
      },
      "total_open_interest": "100.0000"
    }
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from econengine import services
from econengine.markets import InsufficientHoldingsError
from econengine.models import (Account, Entity, EntityType, Script,
                                ScriptType, Tick, WorldSetting)
from econengine.scripting import OperationVetoedError
from econengine.services import (MissingCapabilityError, create_account,
                                create_entity, transfer)

SERVICER_SOURCE = (Path(__file__).parent / "futures.lua").read_text()
"""The Lua mark-to-market script. Install it bound to the exchange as BEHAVIOUR."""

#: The signal key convention (Step 5c): ``futures:price:<SYMBOL>`` = ``{"price": "..."}``.
SIGNAL_PREFIX = "futures:price"

#: The deficiency-oracle key convention: ``futures:deficiency:<EID>:<SYMBOL>``.
DEFICIENCY_PREFIX = "futures:deficiency"

#: Default maintenance margin = 50% of initial (a credit below this flags breach).
DEFAULT_MAINTENANCE = Decimal("0.5")

#: Numeric(18,4) — quantise seized quantities to 4dp.
_Q = Decimal("0.0001")


@dataclass
class Exchange:
    """A handle bundling the exchange's moving parts for ergonomic helper calls."""

    entity: Entity      # the CCP (a BUSINESS, granted SEIZE for deficiency seizure)
    account: Account    # the commingled margin pool (base money)
    script: Script      # the BEHAVIOUR mark-to-market script (holds the book)
    currency: str


def open_exchange(
    session: Session,
    name: str,
    currency: str = "USD",
    *,
    maintenance: Decimal = DEFAULT_MAINTENANCE,
) -> Exchange:
    """Stand up a futures exchange: a ``BUSINESS`` entity (the CCP), a margin
    pool account, and a bound BEHAVIOUR mark-to-market script.

    The exchange is NOT born with seizure power: ``SEIZE`` is a sovereign
    capability the state grants separately (a clearinghouse license). Without
    it, ``settle()`` cannot seize a defaulter's goods and the winner takes a
    haircut. Grant it with ``entity.capabilities`` (test/operator) or the
    ``grant_capability`` primitive (governed).
    """
    entity = create_entity(session, name, EntityType.BUSINESS)
    account = create_account(session, entity, currency, initial_balance=Decimal("0"))
    script = Script(
        name=f"{name}-engine",
        source=SERVICER_SOURCE,
        script_type=ScriptType.BEHAVIOUR,
        entity_id=entity.id,
        is_active=True,
        state={"currency": currency, "maintenance_fraction": str(maintenance),
               "next_pos_id": 1, "positions": {}, "total_open_interest": "0"},
    )
    session.add(script)
    session.flush()
    return Exchange(entity=entity, account=account, script=script, currency=currency)


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


def _signal_price(session: Session, symbol: str) -> Decimal | None:
    """Read the oracle's price for ``symbol`` (``None`` = dark feed / unset)."""
    setting = session.get(WorldSetting, f"{SIGNAL_PREFIX}:{symbol}")
    if setting is None:
        return None
    return Decimal(setting.value["price"])


def _position(exchange: Exchange, pid) -> dict | None:
    rec = exchange.script.state.get("positions", {}).get(str(pid))
    return dict(rec) if rec else None


def _write_deficiency_oracle(session: Session, loser_id: str, symbol: str,
                             max_qty: Decimal) -> None:
    """Mirror a position's deficiency into its queryable WorldSetting so the
    margin-sufficiency VALIDATOR can see it (the same oracle pattern as the
    loan's usury cap — a validator cannot read another script's state)."""
    key = f"{DEFICIENCY_PREFIX}:{loser_id}:{symbol}"
    setting = session.get(WorldSetting, key)
    value = {"max": str(max_qty)}
    if setting is None:
        session.add(WorldSetting(key=key, value=value))
    else:
        setting.value = value
    session.flush()


# ---------------------------------------------------------------------------
# the position lifecycle: open / settle
# ---------------------------------------------------------------------------

def open_future(
    session: Session,
    exchange: Exchange,
    long: Entity,
    short: Entity,
    symbol: str,
    quantity: Decimal,
    price: Decimal,
    expiry: int,
    margin: Decimal,
) -> dict:
    """Match a long and a short: both post cash margin, the position is booked.

    A symmetric pair of ``transfer``s (long -> exchange, short -> exchange):
    REAL base money leaves each party for the exchange's commingled pool. No
    money is created — the exchange holds what was posted. The position is a
    book entry recording the agreement; nothing is delivered yet.

    ``price`` is the contract price (the fixed price the long will pay / the
    short will receive, settled against the signal at expiry). ``expiry`` is an
    absolute tick; ``margin`` is the per-side initial margin.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if price <= 0:
        raise ValueError("price must be positive")
    if margin <= 0:
        raise ValueError("margin must be positive")
    now = _latest_tick(session)
    if expiry <= now:
        raise ValueError("expiry must be in the future")
    long_acct = _account(long, exchange.currency)
    short_acct = _account(short, exchange.currency)
    transfer(session, long_acct, exchange.account, margin, "futures-margin:long")
    transfer(session, short_acct, exchange.account, margin, "futures-margin:short")
    state = dict(exchange.script.state)
    positions = dict(state.get("positions") or {})
    pid = str(state.get("next_pos_id", 1))
    positions[pid] = {
        "long": long.id, "short": short.id,
        "symbol": symbol, "quantity": str(quantity), "price": str(price),
        "expiry": expiry,
        "long_margin": str(margin), "short_margin": str(margin),
        "long_credit": str(margin), "short_credit": str(margin),
        "last_mark": now, "status": "open",
    }
    state["positions"] = positions
    state["next_pos_id"] = int(pid) + 1
    exchange.script.state = state
    return {"position": pid, "long": long.id, "short": short.id,
            "symbol": symbol, "quantity": str(quantity), "price": str(price),
            "expiry": expiry, "margin": str(margin)}


def settle(session: Session, exchange: Exchange, pid) -> dict:
    """Settle a position at the current signal price — cash to the solvent,
    seized goods from the defaulter to the winner.

    The whole point of the margin system. Reads the final signal price and
    computes each side's mark-to-market credit:

      * ``long_credit  = long_margin  + (signal - price) * qty``
      * ``short_credit = short_margin - (signal - price) * qty``

    (zero-sum: their sum is always the posted pool). Then:

      * **Both solvent** (credits ≥ 0): the exchange pays each side their
        credit from the pool. Clean — the pool is conserved exactly.
      * **Deficiency** (one credit < 0 — losses exceeded posted margin): the
        winner takes the ENTIRE cash pool, PLUS the exchange ``seize``s goods
        worth the deficiency from the defaulter (at the signal price) and
        redirects them to the winner (``to_entity``). This makes the winner
        whole without any cash-conversion step — the deficiency is settled *in
        goods*, directly. If the defaulter holds none (or the exchange lacks
        ``SEIZE``, or a validator vetoes), the winner takes a haircut: the
        exchange pays what cash it has and reports the failed seizure.

    May be called at expiry (``status == "expired"``) or early as a **margin
    call** (``status == "breached"``) — the latter is the headline ``seize``
    use case. Returns a summary of the settlement.
    """
    rec = _position(exchange, pid)
    if rec is None:
        raise ValueError(f"no position {pid}")
    if rec.get("status") == "settled":
        raise ValueError(f"position {pid} already settled")
    symbol = rec["symbol"]
    signal = _signal_price(session, symbol)
    if signal is None:
        raise ValueError(f"no signal price for {symbol}; cannot settle")
    qty = Decimal(rec["quantity"])
    contract = Decimal(rec["price"])
    long_margin = Decimal(rec["long_margin"])
    short_margin = Decimal(rec["short_margin"])
    pool = long_margin + short_margin

    long_pnl = (signal - contract) * qty
    long_credit = long_margin + long_pnl
    short_credit = short_margin - long_pnl

    summary = {"signal": str(signal),
               "long_credit": str(long_credit),
               "short_credit": str(short_credit),
               "seized": None, "settled": True}
    long_ent = session.get(Entity, rec["long"])
    short_ent = session.get(Entity, rec["short"])
    long_acct = _account(long_ent, exchange.currency)
    short_acct = _account(short_ent, exchange.currency)
    rule = f"futures:{pid}"

    if long_credit >= 0 and short_credit >= 0:
        # Both solvent: pay each side their credit from the pool. Their sum is
        # exactly the pool (the P&L cancels), so this is money-conserving.
        transfer(session, exchange.account, long_acct, long_credit,
                 f"futures-settle:long:{pid}")
        transfer(session, exchange.account, short_acct, short_credit,
                 f"futures-settle:short:{pid}")
    else:
        # Deficiency: one side's losses exceeded their posted margin. The
        # winner takes the whole pool (cash) + seized goods from the loser.
        if long_credit < 0:
            loser, winner, winner_acct = long_ent, short_ent, short_acct
            deficiency = -long_credit
        else:
            loser, winner, winner_acct = short_ent, long_ent, long_acct
            deficiency = -short_credit
        # Pay the winner the entire cash pool.
        transfer(session, exchange.account, winner_acct, pool,
                 f"futures-settle:winner:{pid}")
        # Seize goods worth the deficiency from the loser, redirect to winner.
        seize_qty = (deficiency / signal).quantize(_Q)
        if seize_qty > 0:
            try:
                _write_deficiency_oracle(session, loser.id, symbol, seize_qty)
                services.seize(session, exchange.entity, loser,
                               symbol=symbol, quantity=seize_qty,
                               to_entity=winner, rule_ref=rule,
                               reference=f"futures-deficiency:{pid}")
                summary["seized"] = {"symbol": symbol, "quantity": str(seize_qty),
                                     "from": loser.id, "to": winner.id,
                                     "value": str(deficiency)}
            except (InsufficientHoldingsError, OperationVetoedError,
                    MissingCapabilityError):
                pass  # no goods / no SEIZE / vetoed — winner takes a haircut

    # Mark settled.
    state = dict(exchange.script.state)
    positions = dict(state["positions"])
    rec["status"] = "settled"
    positions[str(pid)] = rec
    state["positions"] = positions
    exchange.script.state = state
    return summary


# ---------------------------------------------------------------------------
# read helpers — the book at a glance. credits are as of the last tick the
# BEHAVIOUR script ran; settle() recomputes fresh from the live signal.
# ---------------------------------------------------------------------------

def position(exchange: Exchange, pid) -> dict | None:
    """A position record (or ``None``). A snapshot of ``script.state``."""
    return _position(exchange, pid)


def position_status(exchange: Exchange, pid) -> str:
    """``open`` | ``breached`` | ``expired`` | ``settled`` (``"none"`` if absent)."""
    rec = _position(exchange, pid)
    return rec.get("status", "none") if rec else "none"


def long_credit(exchange: Exchange, pid) -> Decimal:
    """The long's mark-to-market credit (as of the last tick the script ran)."""
    rec = _position(exchange, pid)
    return Decimal(rec["long_credit"]) if rec else Decimal("0")


def short_credit(exchange: Exchange, pid) -> Decimal:
    """The short's mark-to-market credit (as of the last tick the script ran)."""
    rec = _position(exchange, pid)
    return Decimal(rec["short_credit"]) if rec else Decimal("0")


def total_open_interest(exchange: Exchange) -> Decimal:
    """Sum of quantities over all non-settled positions (stamped by futures.lua)."""
    return Decimal(exchange.script.state.get("total_open_interest", "0"))
