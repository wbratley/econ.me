"""Government bond — issuance + retirement helpers (Step 5d reference).

This module is the *data* side of a bond (design.md §2: a bond is data +
a POLICY script — no more an engine feature than a tax schedule). It:

  * creates bond units        — ``adjust_holding`` of the bond symbol
                                (Fork A: a claim *is* a Holding row),
  * collects the issue price  — a ``transfer`` of existing money from
                                buyer to issuer (a bond sale does NOT
                                create money; only a MONETARY_AUTHORITY
                                purchase does),
  * registers the terms       — written into the issuer's servicing
                                script ``state`` (the *policy* side —
                                ``gov_bond.lua`` — reads them each tick),
  * retires redeemed units    — ``adjust_holding`` the units back to
                                zero once the script has paid the face.

The *policy* side — paying coupons and redeeming face on schedule driven
by ``ctx.tick`` — lives in ``gov_bond.lua``, bound to the issuer.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine.markets import adjust_holding
from econengine.models import Entity, Holding, Script, ScriptType
from econengine.services import transfer

SERVICER_SOURCE = (Path(__file__).parent / "gov_bond.lua").read_text()
"""The Lua servicing script. Install it bound to the issuer as a POLICY."""


@dataclass(frozen=True)
class BondTerms:
    """The terms of one bond issue. Maturity is derived: issue_tick +
    periods*interval (a bond ends exactly on its last coupon date)."""

    symbol: str           # Holding symbol, e.g. "GOVBOND-T4" (stored upper)
    face: Decimal         # redemption value per unit
    coupon_rate: Decimal  # per-period coupon as a fraction of face (0.05 = 5%)
    interval: int         # ticks per coupon period
    periods: int          # number of coupon periods
    currency: str = "USD"

    @property
    def coupon_per_unit(self) -> Decimal:
        return self.face * self.coupon_rate

    @property
    def maturity_offset(self) -> int:
        """Ticks from issue to redemption."""
        return self.periods * self.interval


def install_servicer(
    session: Session,
    issuer: Entity,
    *,
    name: str = "gov-bond-servicer",
) -> Script:
    """Create the issuer's POLICY bond-servicing script if absent.

    Idempotent: a second call returns the existing row (a world issues many
    bonds under one servicing script). The script is inert until
    ``issue_bond`` registers terms in its ``state``.
    """
    existing = session.execute(
        select(Script).where(Script.name == name, Script.entity_id == issuer.id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    script = Script(
        name=name,
        source=SERVICER_SOURCE,
        script_type=ScriptType.POLICY,
        entity_id=issuer.id,
        is_active=True,
    )
    session.add(script)
    session.flush()
    return script


def issue_bond(
    session: Session,
    issuer: Entity,
    terms: BondTerms,
    buyers,
    issue_tick: int = 0,
    *,
    price_per_unit: Decimal | None = None,
) -> dict:
    """Sell bonds to ``buyers`` at par (or ``price_per_unit``) and register
    the terms with the issuer's servicing script.

    ``buyers`` is an iterable of ``(entity, quantity)`` pairs. Each sale is a
    ``transfer`` of the buyer's existing money to the issuer plus an
    ``adjust_holding`` crediting the buyer with bond units — neither creates
    money. The bond's terms are written into the servicing script's
    ``state.bonds[SYMBOL]`` so ``gov_bond.lua`` can honour them.

    Returns a summary dict (symbol, total proceeds, absolute maturity tick).
    """
    servicer = install_servicer(session, issuer)
    symbol = terms.symbol.upper()
    price = terms.face if price_per_unit is None else price_per_unit

    issuer_acct = _account(issuer, terms.currency)
    proceeds = Decimal("0")
    for buyer, qty in buyers:
        buyer_acct = _account(buyer, terms.currency)
        # 1. Buyer pays the issue price — a transfer of EXISTING money.
        transfer(session, buyer_acct, issuer_acct, price * qty, f"issue:{symbol}")
        # 2. Buyer receives the claim — a Holding row (Fork A).
        adjust_holding(session, buyer, symbol, Decimal(qty))
        proceeds += price * qty

    # 3. Register the terms. The state column is a plain JSON dict (not
    #    mutable-tracked), so reassign the whole dict to persist.
    maturity = int(issue_tick) + terms.maturity_offset
    bonds = dict(servicer.state.get("bonds") or {})
    bonds[symbol] = {
        "currency": terms.currency,
        "face": str(terms.face),
        "coupon": str(terms.coupon_per_unit),
        "interval": int(terms.interval),
        "issue_tick": int(issue_tick),
        "periods": int(terms.periods),
        "maturity": maturity,
        "coupons_paid": 0,
        "redeemed": False,
    }
    servicer.state = {**servicer.state, "bonds": bonds}
    return {"symbol": symbol, "proceeds": str(proceeds), "maturity": maturity}


def redeem_holdings(session: Session, symbol: str) -> Decimal:
    """Extinguish every outstanding unit of ``symbol``.

    Call this AFTER the servicing script has paid the face (marked the bond
    ``redeemed``). The script moves money; this moves goods — the two halves
    of redemption, split exactly as issuance is (transfer + adjust_holding).
    Returns the total units retired.
    """
    symbol = symbol.upper()
    retired = Decimal("0")
    rows = session.execute(
        select(Holding).where(Holding.symbol == symbol, Holding.quantity > 0)
    ).scalars().all()
    for h in rows:
        retired += h.quantity
        adjust_holding(session, session.get(Entity, h.entity_id), symbol, -h.quantity)
    return retired


def _account(entity: Entity, currency: str):
    for acct in entity.accounts:
        if acct.currency == currency:
            return acct
    raise ValueError(f"{entity.name} has no {currency} account")
