"""Options — the data/helpers side of an asymmetric derivatives contract (Step 5d).

This is the reference contract that closes the Step 5d library. An **exchange**
(a CCP) matches a **buyer** (the holder of a right) and a **writer** (the
obligated party). The buyer pays a one-time **premium** to the writer (the
price of the right) and posts *no* margin; the writer posts **margin**
(collateral for the obligation). Each tick ``option.lua`` (BEHAVIOUR) reads the
signal price and marks the position: the buyer's **intrinsic value** (what the
option is worth if exercised now) and the writer's **credit** (margin minus the
buyer's claim). On settlement the exchange pays the buyer the intrinsic value
**only if the option is in the money** — otherwise the writer's margin returns
whole (the premium has already settled hands).

The headline asymmetry vs futures: a future is a *symmetric* pair (both sides
obligated, both post margin, settlement pays both); an option is *asymmetric*
(the buyer has a *right*, the writer has an *obligation*, settlement pays the
buyer only if in the money). The premium — not margin — is what the buyer pays;
the margin — posted by the writer alone — is what guarantees the promise.

Design (mirrors futures, bond, bank, loan: data in Python, policy in Lua):

  * ``option.lua`` (BEHAVIOUR) marks every open position to market each tick
    from ``ctx.query.world_setting("futures:price:" .. symbol)`` (the same
    oracle a future reads — the underlying price is shared infrastructure) and
    flags **breach** (writer's credit below maintenance) and **expiry**. It
    does NOT settle — settlement is a discrete act.
  * ``settle()`` (below) is that act. It reads the final signal, computes the
    intrinsic value, and pays out. It is Python (not Lua) because the
    deficiency case needs try/except branching (seize, and if the writer has no
    goods, the buyer takes a haircut) — the same reason ``futures.settle()`` and
    ``loan.enforce()`` are Python. The deficiency case REUSES the futures
    pattern exactly: ``seize`` goods from the writer and redirect them to the
    buyer via ``to_entity``, making the buyer whole without cash-conversion.
  * ``option_sufficiency.lua`` (VALIDATOR) gates the exchange's ``seize`` to a
    *documented deficiency* — structurally identical to futures'
    ``margin_sufficiency.lua``, reading an ``option:deficiency:*`` oracle.

The exchange must hold the ``SEIZE`` capability for the deficiency case
(sovereign power delegated to a private CCP), exactly as in futures.

State shape (exchange's BEHAVIOUR script ``state``)::

    {
      "currency": "USD",
      "maintenance_fraction": "0.5",
      "next_pos_id": 2,
      "positions": {
        "1": {
          "kind":           "call",          # call | put
          "buyer":          "<eid>",         # holder of the right
          "writer":         "<eid>",         # the obligated party
          "symbol":         "GRAIN",
          "quantity":       "100",
          "strike":         "5.00",          # exercise price
          "premium":        "50",            # paid buyer -> writer at open
          "margin":         "200",           # writer's posted collateral
          "expiry":         10,              # absolute tick
          "buyer_value":    "100.0000",      # intrinsic value (stamped by option.lua)
          "writer_credit":  "100.0000",      # margin - buyer_value (stamped by option.lua)
          "last_mark":      2,               # advanced by option.lua
          "status":         "open"           # open | breached | expired | settled
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

SERVICER_SOURCE = (Path(__file__).parent / "option.lua").read_text()
"""The Lua mark-to-market script. Install it bound to the exchange as BEHAVIOUR."""

#: The signal key convention (shared with futures — the underlying price is one
#: oracle, read by many instruments): ``futures:price:<SYMBOL>`` = ``{"price": "..."}``.
SIGNAL_PREFIX = "futures:price"

#: The deficiency-oracle key convention: ``option:deficiency:<EID>:<SYMBOL>``.
DEFICIENCY_PREFIX = "option:deficiency"

#: Default maintenance margin = 50% of posted (writer's credit below this flags breach).
DEFAULT_MAINTENANCE = Decimal("0.5")

#: Numeric(18,4) — quantise seized quantities to 4dp.
_Q = Decimal("0.0001")


@dataclass
class Exchange:
    """A handle bundling the exchange's moving parts for ergonomic helper calls."""

    entity: Entity      # the CCP (a BUSINESS, granted SEIZE for deficiency seizure)
    account: Account    # the margin pool (writer collateral; base money)
    script: Script      # the BEHAVIOUR mark-to-market script (holds the book)
    currency: str


def open_exchange(
    session: Session,
    name: str,
    currency: str = "USD",
    *,
    maintenance: Decimal = DEFAULT_MAINTENANCE,
) -> Exchange:
    """Stand up an options exchange: a ``BUSINESS`` entity (the CCP), a margin
    pool account, and a bound BEHAVIOUR mark-to-market script.

    The exchange is NOT born with seizure power: ``SEIZE`` is a sovereign
    capability the state grants separately (a clearinghouse license). Without
    it, ``settle()`` cannot seize a defaulting writer's goods and the buyer
    takes a haircut. Grant it with ``entity.capabilities`` (test/operator) or
    the ``grant_capability`` primitive (governed).
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
    """Read the oracle's price for ``symbol`` (``None`` = dark feed / unset).

    Shares the ``futures:price:`` namespace with futures — the underlying's
    price is one oracle, read by whichever instrument needs it.
    """
    setting = session.get(WorldSetting, f"{SIGNAL_PREFIX}:{symbol}")
    if setting is None:
        return None
    return Decimal(setting.value["price"])


def _position(exchange: Exchange, pid) -> dict | None:
    rec = exchange.script.state.get("positions", {}).get(str(pid))
    return dict(rec) if rec else None


def _write_deficiency_oracle(session: Session, writer_id: str, symbol: str,
                             max_qty: Decimal) -> None:
    """Mirror a position's deficiency into its queryable WorldSetting so the
    option-sufficiency VALIDATOR can see it (the same oracle pattern as futures'
    margin-sufficiency check — a validator cannot read another script's state)."""
    key = f"{DEFICIENCY_PREFIX}:{writer_id}:{symbol}"
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

def open_option(
    session: Session,
    exchange: Exchange,
    buyer: Entity,
    writer: Entity,
    kind: str,
    symbol: str,
    quantity: Decimal,
    strike: Decimal,
    expiry: int,
    premium: Decimal,
    margin: Decimal,
) -> dict:
    """Match a buyer and a writer: premium flows buyer -> writer, margin flows
    writer -> exchange pool, the position is booked.

    Two ``transfer``s (REAL base money, no issuance):

      1. **Premium** (buyer -> writer): the price of the right. The buyer pays
         this regardless of outcome — it is the cost of the option, not
         collateral. It settles hands immediately and is never returned.
      2. **Margin** (writer -> exchange): collateral for the writer's
         obligation. Held in the exchange's pool; returned (minus any payout)
         at settlement.

    The buyer posts *no* margin: the buyer has a right, not an obligation. This
    is the structural asymmetry vs futures (where both sides post margin).

    ``kind`` is ``"call"`` (right to buy at strike) or ``"put"`` (right to sell
    at strike). ``strike`` is the exercise price; ``expiry`` is an absolute
    tick. At settlement the payout is the **intrinsic value**: for a call,
    ``max(0, signal - strike) * qty``; for a put, ``max(0, strike - signal) * qty``.
    """
    if kind not in ("call", "put"):
        raise ValueError(f"kind must be 'call' or 'put', not {kind!r}")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if strike <= 0:
        raise ValueError("strike must be positive")
    if premium < 0:
        raise ValueError("premium must be non-negative")
    if margin <= 0:
        raise ValueError("margin must be positive")
    now = _latest_tick(session)
    if expiry <= now:
        raise ValueError("expiry must be in the future")
    buyer_acct = _account(buyer, exchange.currency)
    writer_acct = _account(writer, exchange.currency)
    # Premium: the price of the right (buyer -> writer, direct).
    if premium > 0:
        transfer(session, buyer_acct, writer_acct, premium, "option-premium")
    # Margin: the writer's collateral (writer -> exchange pool).
    transfer(session, writer_acct, exchange.account, margin, "option-margin:writer")
    state = dict(exchange.script.state)
    positions = dict(state.get("positions") or {})
    pid = str(state.get("next_pos_id", 1))
    positions[pid] = {
        "kind": kind, "buyer": buyer.id, "writer": writer.id,
        "symbol": symbol, "quantity": str(quantity), "strike": str(strike),
        "premium": str(premium), "margin": str(margin), "expiry": expiry,
        "buyer_value": "0", "writer_credit": str(margin),
        "last_mark": now, "status": "open",
    }
    state["positions"] = positions
    state["next_pos_id"] = int(pid) + 1
    exchange.script.state = state
    return {"position": pid, "kind": kind, "buyer": buyer.id, "writer": writer.id,
            "symbol": symbol, "quantity": str(quantity), "strike": str(strike),
            "premium": str(premium), "margin": str(margin), "expiry": expiry}


def settle(session: Session, exchange: Exchange, pid) -> dict:
    """Settle a position at the current signal price — pay the buyer only if in
    the money, seizing goods from a defaulting writer if margin is exhausted.

    Reads the final signal and computes the **intrinsic value**:

      * **call**: ``max(0, signal - strike) * qty``
      * **put**:  ``max(0, strike - signal) * qty``

    Then:

      * **Out of the money** (intrinsic 0): the option expires worthless. The
        writer's full margin returns. The buyer gets nothing (the premium was
        already the cost of the gamble). This is the headline difference from
        futures: settlement pays the long *only if in the money*.
      * **In the money, collateralized** (0 < intrinsic <= margin): the exchange
        pays the buyer the intrinsic value from the pool; the writer gets the
        remainder back. Money-conserving.
      * **Deficiency** (intrinsic > margin — the writer's posted collateral
        cannot cover the payout): the buyer takes the ENTIRE cash pool, PLUS
        the exchange ``seize``s goods worth the deficiency from the writer and
        redirects them to the buyer (``to_entity``) — exactly the futures
        pattern. If the writer holds none (or the exchange lacks ``SEIZE``, or
        a validator vetoes), the buyer takes a haircut.

    Note the buyer is NEVER the one seized from — the buyer has no obligation.
    This is simpler than futures' settle (which branches on long-vs-short
    deficiency); here the writer is always the party at risk.

    May be called at expiry (``status == "expired"``) or early as a forced
    close-out (``status == "breached"``). Returns a summary.
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
    kind = rec["kind"]
    qty = Decimal(rec["quantity"])
    strike = Decimal(rec["strike"])
    margin = Decimal(rec["margin"])

    if kind == "call":
        intrinsic = max(Decimal("0"), signal - strike) * qty
    else:  # put
        intrinsic = max(Decimal("0"), strike - signal) * qty

    buyer_ent = session.get(Entity, rec["buyer"])
    writer_ent = session.get(Entity, rec["writer"])
    buyer_acct = _account(buyer_ent, exchange.currency)
    writer_acct = _account(writer_ent, exchange.currency)

    summary = {"signal": str(signal), "intrinsic": str(intrinsic),
               "payout": None, "seized": None, "settled": True}

    if intrinsic == 0:
        # Out of the money: writer's margin returns whole; buyer gets nothing.
        transfer(session, exchange.account, writer_acct, margin,
                 f"option-return:{pid}")
    elif intrinsic <= margin:
        # In the money, fully collateralized: buyer gets intrinsic, writer the rest.
        transfer(session, exchange.account, buyer_acct, intrinsic,
                 f"option-exercise:{pid}")
        transfer(session, exchange.account, writer_acct, margin - intrinsic,
                 f"option-return:{pid}")
        summary["payout"] = str(intrinsic)
    else:
        # Deficiency: the writer's margin cannot cover the payout.
        transfer(session, exchange.account, buyer_acct, margin,
                 f"option-exercise:{pid}")
        summary["payout"] = str(margin)
        deficiency = intrinsic - margin
        seize_qty = (deficiency / signal).quantize(_Q)
        if seize_qty > 0:
            try:
                _write_deficiency_oracle(session, writer_ent.id, symbol, seize_qty)
                services.seize(session, exchange.entity, writer_ent,
                               symbol=symbol, quantity=seize_qty,
                               to_entity=buyer_ent, rule_ref=f"option:{pid}",
                               reference=f"option-deficiency:{pid}")
                summary["seized"] = {"symbol": symbol, "quantity": str(seize_qty),
                                     "from": writer_ent.id, "to": buyer_ent.id,
                                     "value": str(deficiency)}
            except (InsufficientHoldingsError, OperationVetoedError,
                    MissingCapabilityError):
                pass  # no goods / no SEIZE / vetoed — buyer takes a haircut

    # Mark settled.
    state = dict(exchange.script.state)
    positions = dict(state["positions"])
    rec["status"] = "settled"
    positions[str(pid)] = rec
    state["positions"] = positions
    exchange.script.state = state
    return summary


# ---------------------------------------------------------------------------
# read helpers — the book at a glance. buyer_value/writer_credit are as of the
# last tick the BEHAVIOUR script ran; settle() recomputes fresh from the signal.
# ---------------------------------------------------------------------------

def position(exchange: Exchange, pid) -> dict | None:
    """A position record (or ``None``). A snapshot of ``script.state``."""
    return _position(exchange, pid)


def position_status(exchange: Exchange, pid) -> str:
    """``open`` | ``breached`` | ``expired`` | ``settled`` (``"none"`` if absent)."""
    rec = _position(exchange, pid)
    return rec.get("status", "none") if rec else "none"


def buyer_value(exchange: Exchange, pid) -> Decimal:
    """The buyer's intrinsic value (as of the last tick the script ran)."""
    rec = _position(exchange, pid)
    return Decimal(rec["buyer_value"]) if rec else Decimal("0")


def writer_credit(exchange: Exchange, pid) -> Decimal:
    """The writer's credit: margin minus the buyer's claim (as of last mark)."""
    rec = _position(exchange, pid)
    return Decimal(rec["writer_credit"]) if rec else Decimal("0")


def total_open_interest(exchange: Exchange) -> Decimal:
    """Sum of quantities over all non-settled positions (stamped by option.lua)."""
    return Decimal(exchange.script.state.get("total_open_interest", "0"))
