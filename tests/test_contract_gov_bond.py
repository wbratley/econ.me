"""Government bond reference contract (Step 5d) — engine validation.

These tests run the real Lua servicing script (gov_bond.lua) against the
real engine, exercising the substrate decisions it locks:

  * Fork A    -- a bond is a Holding; the register is the live cap table, so
                 a traded bond pays its NEW holder (no stale cache).
  * ctx.tick  -- the schedule is anchored to wall-tick, so a budget-skipped
                 tick does not drift maturities (Step 5a).
  * two-tier  -- a bond sale is a transfer of existing money; it never
                 creates money (only a MONETARY_AUTHORITY purchase does).
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine.markets import adjust_holding, get_holding
from econengine.models import Account, EntityType, Holding
from econengine.services import create_account, create_entity
from econengine.tick import run_tick, set_compute_budget_ms

from pathlib import Path

from contracts.bond.bond import BondTerms, issue_bond, redeem_holdings

MONETIZATION_CAP = (Path(__file__).resolve().parent.parent
                    / "contracts" / "bond" / "monetization_cap.lua").read_text()

# A small, readable bond: $100 face, 5% per period, 2 periods of 2 ticks.
# Issued at tick 0 -> coupons at ticks 2 and 4, redemption at tick 4.
SYM = "GOVBOND-T4"
TERMS = BondTerms(
    symbol=SYM, face=Decimal("100"), coupon_rate=Decimal("0.05"),
    interval=2, periods=2, currency="USD",
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    from econengine.models import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    a = create_account(session, alice, "USD", initial_balance=Decimal("10000"))
    b = create_account(session, bob, "USD", initial_balance=Decimal("10000"))
    g = create_account(session, gov, "USD", initial_balance=Decimal("5000"))
    return session, alice, bob, gov, a, b, g


def usd_supply(session) -> Decimal:
    return session.execute(
        select(func.coalesce(func.sum(Account.balance), 0)).where(Account.currency == "USD")
    ).scalar_one()


# --- the full lifecycle -----------------------------------------------------

def test_bond_coupons_and_redemption(world):
    """Issued at par: buyer pays face, receives coupons then face back."""
    session, alice, bob, gov, a, b, g = world
    issue_bond(session, gov, TERMS, [(alice, 10)])  # 10 bonds at par = 1000

    run_tick(session)   # tick 1: nothing due
    run_tick(session)   # tick 2: coupon #1 (5 * 10 = 50)
    run_tick(session)   # tick 3: nothing due
    tick4 = run_tick(session)  # tick 4: coupon #2 (50) + face (1000)

    # Alice paid 1000 -> 9000, got 50 + 50 + 1000 back -> 10100
    # (net +100 = the two coupons; face is her principal returned).
    assert a.balance == Decimal("10100")
    # Gov kept the 1000 sale proceeds, paid 1100 -> net -100 (coupon cost).
    assert g.balance == Decimal("4900")

    # The bond is fully settled: both coupons paid, face paid, redeemed.
    from econengine.models import Script
    bond = session.execute(
        select(Script).where(Script.entity_id == gov.id)
    ).scalar_one().state["bonds"][SYM]
    assert bond["coupons_paid"] == 2
    assert bond["redeemed"] is True

    # The face payment is visible as a settled intent event.
    redeems = [
        e for e in tick4.events
        if e.get("params", {}).get("reference", "").startswith("redeem:")
    ]
    assert redeems and redeems[0]["status"] == "applied"


def test_bond_sale_does_not_create_money(world):
    """The headline two-tier claim: a bond sale is a transfer, not issuance."""
    session, alice, bob, gov, a, b, g = world
    before = usd_supply(session)

    issue_bond(session, gov, TERMS, [(alice, 10), (bob, 5)])
    assert usd_supply(session) == before          # the sale: no creation

    for _ in range(4):
        run_tick(session)                          # coupons + face: transfers
    assert usd_supply(session) == before           # still no creation


# --- Fork A: a traded bond pays its new holder ------------------------------

def test_transferred_bond_pays_new_holder(world):
    """The register is the live cap table. Alice sells bonds to Bob on the
    secondary market; at maturity Bob is paid on what he holds -- a cap
    table cached in script state would have paid Alice. This is the test
    Fork A exists to pass."""
    session, alice, bob, gov, a, b, g = world
    issue_bond(session, gov, TERMS, [(alice, 10)])

    # Alice sells 4 bonds to Bob for cash (a goods transfer + a payment).
    adjust_holding(session, alice, SYM, Decimal("-4"))
    adjust_holding(session, bob, SYM, Decimal("4"))

    for _ in range(4):
        run_tick(session)

    # Two coupons of 5/unit: Alice on 6, Bob on 4. Face likewise.
    # Alice: paid 1000, got back 6*5*2 (coupons) + 6*100 (face) = 60 + 600.
    assert a.balance == Decimal("10000") - 1000 + 60 + 600
    # Bob: bought nothing at issue, holds 4 -> 4*5*2 + 4*100 = 40 + 400.
    assert b.balance == Decimal("10000") + 40 + 400


def test_multiple_holders_paid_proportionally(world):
    session, alice, bob, gov, a, b, g = world
    issue_bond(session, gov, TERMS, [(alice, 6), (bob, 4)])

    for _ in range(4):
        run_tick(session)

    # Total coupon stream = 10 units * 5 * 2 = 100; face = 10 * 100 = 1000.
    # Split 60/40 by holdings.
    assert a.balance == Decimal("10000") - 600 + 60 + 600   # 6 units
    assert b.balance == Decimal("10000") - 400 + 40 + 400   # 4 units


# --- Step 5a: ctx.tick anchors the schedule ---------------------------------

def test_ctx_tick_drives_schedule_after_skip(world):
    """A compute-budget skip must not drift the maturity. The bond pays on
    wall-tick (ctx.tick), not on a count of the script's own runs."""
    session, alice, bob, gov, a, b, g = world
    issue_bond(session, gov, TERMS, [(alice, 10)])

    run_tick(session)                       # tick 1: nothing
    run_tick(session)                       # tick 2: coupon #1 (50)
    set_compute_budget_ms(session, 0)       # skip tick 3 entirely
    skipped = run_tick(session)
    set_compute_budget_ms(session, None)    # restore
    run_tick(session)                       # tick 4: coupon #2 + face

    assert any(e["type"] == "compute_budget_exceeded" for e in skipped.events)
    # Identical outcome to the no-skip lifecycle: maturity still fired at 4.
    assert a.balance == Decimal("10100")


# --- honest engine behavior -------------------------------------------------

def test_missed_coupon_when_issuer_insolvent(world):
    """With no funds, the coupon transfer is rejected -- recorded as an
    event, not a crash. (Carrying arrears forward is a documented
    extension; the reference script marks a coupon settled at queue time.)"""
    session, alice, bob, gov, a, b, g = world
    g.balance = Decimal("0")   # gov is broke
    issue_bond(session, gov, TERMS, [(alice, 10)])   # gov now has the 1000 sale

    run_tick(session)   # tick 1
    tick2 = run_tick(session)   # tick 2: coupon 50 due, gov has 1000 -> pays
    coupons = [
        e for e in tick2.events
        if e.get("params", {}).get("reference", "").startswith("coupon:")
    ]
    assert coupons and coupons[0]["status"] == "applied"
    assert g.balance == Decimal("950")

    # Drain gov so the next coupon cannot be paid.
    g.balance = Decimal("0")
    run_tick(session)   # tick 3: nothing due
    tick4 = run_tick(session)   # tick 4: coupon + face due, gov broke -> rejected
    rejects = [e for e in tick4.events if e.get("status") == "rejected"]
    assert rejects and "need" in rejects[0]["reason"].lower()


# --- the goods half of redemption ------------------------------------------

def test_redeem_holdings_extinguishes_units(world):
    """The script pays face (money); redeem_holdings retires the units
    (goods) -- the two halves of redemption, split like issuance."""
    session, alice, bob, gov, a, b, g = world
    issue_bond(session, gov, TERMS, [(alice, 10)])

    for _ in range(4):
        run_tick(session)
    # Bonds still on Alice's book (the script can't retire goods)...
    assert get_holding(session, alice.id, SYM).quantity == Decimal("10")

    retired = redeem_holdings(session, SYM)
    assert retired == Decimal("10")
    # adjust_holding zeroes the row rather than deleting it; a zero holding is
    # invisible to holders() (which filters quantity > 0), so it is harmless.
    leftover = get_holding(session, alice.id, SYM)
    assert leftover is not None and leftover.quantity == Decimal("0")


# --- the constitutional constraint (ships-with VALIDATOR) ------------------

def test_monetization_cap_forbids_issuance(world):
    """A bond world may bind its issuer at the constitutional tier: forbid
    money creation so bonds can only be serviced from existing funds. The
    same VALIDATOR pattern fiscal_policy uses, aimed at the two-tier-money
    boundary the bond demonstrates."""
    from econengine.models import Script, ScriptType
    session, alice, bob, gov, a, b, g = world

    # A policy that tries to create base money.
    session.add(Script(
        name="printer", source=f"ctx.action.issue_money('{g.id}', '100', 'print')",
        script_type=ScriptType.POLICY, entity_id=gov.id, is_active=True,
    ))
    session.flush()

    # Without the cap, the monetary authority creates money freely.
    run_tick(session)
    assert g.balance == Decimal("5100")   # 5000 start + 100 issued

    # Install the constitutional cap (a re-enact at supermajority in a real
    # world) and the same op is now vetoed.
    session.add(Script(
        name="monetization-cap", source=MONETIZATION_CAP,
        script_type=ScriptType.VALIDATOR, is_active=True,
    ))
    session.flush()
    tick = run_tick(session)
    assert g.balance == Decimal("5100")   # no new money created
    vetoed = [e for e in tick.events if e.get("status") == "rejected"]
    assert vetoed and "monetization cap" in vetoed[0]["reason"].lower()


def test_monetization_cap_data_driven_override(world):
    """The cap is retunable by writing data (Step 5c), not by re-enacting the
    validator: a monetary:issue_cap WorldSetting overrides the in-source
    DEFAULT_CAP, read live each op through ctx.query.world_setting."""
    from econengine.models import Script, ScriptType, WorldSetting
    session, alice, bob, gov, a, b, g = world

    session.add(Script(
        name="printer", source=f"ctx.action.issue_money('{g.id}', '100', 'print')",
        script_type=ScriptType.POLICY, entity_id=gov.id, is_active=True,
    ))
    session.add(Script(
        name="monetization-cap", source=MONETIZATION_CAP,
        script_type=ScriptType.VALIDATOR, is_active=True,
    ))
    session.flush()

    # Default cap ("0", in source) forbids -- no setting posted yet.
    run_tick(session)
    assert g.balance == Decimal("5000")

    # An oracle lifts the ceiling to 200 via a WorldSetting; the same op now
    # passes without touching the validator script.
    session.add(WorldSetting(key="monetary:issue_cap", value={"cap": "200"}))
    session.flush()
    run_tick(session)
    assert g.balance == Decimal("5100")   # 100 created, under the 200 cap
