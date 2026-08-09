"""Futures + margin reference contract (Step 5d) -- engine validation.

These tests prove the contract's reason for existing: ``seize`` is a **margin
call**, and the **signal convention** (Step 5c) drives mark-to-market and
settlement. An exchange matches a long and a short, both post cash margin, a
Lua script marks to market each tick from a signal price, and ``settle()``
pays out — seizing goods from a defaulter whose losses exceeded their posted
margin and redirecting them to the winner.

Contrast with the loan (``test_contract_loan``): the loan seizes COLLATERAL a
borrower pledged (a mortgage foreclosure); the futures contract seizes a
defaulter's goods to cover a MARGIN DEFICIENCY (a margin call). Both use
``seize`` — the same primitive, the enforcement spine in two different private
contracts.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine import capabilities, markets
from econengine.models import (Account, Base, EntityType, Transaction,
                                TransactionType, WorldSetting)
from econengine.services import (InsufficientFundsError,
                                 MissingCapabilityError, create_account,
                                 create_entity)
from econengine.tick import run_tick

from contracts.futures.futures import (open_exchange, open_future,
                                       position_status, settle, total_open_interest)

MARGIN_VALIDATOR = (Path(__file__).resolve().parent.parent
                    / "contracts" / "futures" / "margin_sufficiency.lua").read_text()


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _add_validator(session, exchange):
    """Install the margin-sufficiency VALIDATOR on the exchange (optional)."""
    from econengine.models import Script, ScriptType
    session.add(Script(
        name="margin-sufficiency", source=MARGIN_VALIDATOR,
        script_type=ScriptType.VALIDATOR, entity_id=exchange.entity.id,
        is_active=True, state={}))
    session.flush()


def set_price(session, symbol, price):
    """Stand in for the platform oracle that posts signal prices between ticks."""
    key = f"futures:price:{symbol}"
    setting = session.get(WorldSetting, key)
    if setting is None:
        session.add(WorldSetting(key=key, value={"price": str(price)}))
    else:
        setting.value = {"price": str(price)}
    session.flush()


@pytest.fixture
def world(session):
    """A funded exchange (with state-granted SEIZE), a long and a short (both
    with cash and goods), and a starting signal price of 5.00/GRAIN."""
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    create_account(session, gov, "USD", initial_balance=Decimal("1000000"))
    exchange = open_exchange(session, "Clearing", "USD")
    # The state delegates seizure power to the exchange (a clearinghouse license).
    exchange.entity.capabilities = [capabilities.SEIZE]
    session.flush()

    def party(name, cash, grain):
        ent = create_entity(session, name, EntityType.INDIVIDUAL)
        create_account(session, ent, "USD", initial_balance=cash)
        markets.adjust_holding(session, ent, "GRAIN", grain)
        session.flush()
        return ent

    long_ent = party("Long", Decimal("10000"), Decimal("0"))
    short_ent = party("Short", Decimal("10000"), Decimal("1000"))
    set_price(session, "GRAIN", Decimal("5.00"))
    session.flush()
    return session, exchange, long_ent, short_ent


def _cash(session, entity):
    acct = [a for a in entity.accounts if a.currency == "USD"][0]
    return acct.balance


def _grain(session, entity):
    h = markets.get_holding(session, entity.id, "GRAIN")
    return h.quantity if h else Decimal("0")


def _no_issuance(session):
    n = session.execute(
        select(func.count()).select_from(Transaction)
        .where(Transaction.tx_type == TransactionType.ISSUANCE)
    ).scalar()
    return n == 0


def _pool(exchange):
    return exchange.account.balance


# ---------------------------------------------------------------------------
# origination: two margins in, no money created

def test_open_future_posts_two_margins(world):
    session, exchange, long_ent, short_ent = world
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10, margin=Decimal("200"))
    # Both margins flow into the commingled pool; no money created.
    assert _pool(exchange) == Decimal("400")
    assert _cash(session, long_ent) == Decimal("10000") - Decimal("200")
    assert _cash(session, short_ent) == Decimal("10000") - Decimal("200")
    assert _no_issuance(session)
    run_tick(session)  # the BEHAVIOUR script stamps open interest
    assert total_open_interest(exchange) == Decimal("100")


def test_open_future_rejects_past_expiry(world):
    session, exchange, long_ent, short_ent = world
    run_tick(session)  # now at tick 1
    with pytest.raises(ValueError, match="future"):
        open_future(session, exchange, long_ent, short_ent, "GRAIN",
                    Decimal("100"), Decimal("5.00"), expiry=1, margin=Decimal("100"))


# ---------------------------------------------------------------------------
# mark-to-market: the signal drives the book each tick

def test_mark_to_market_updates_credits(world):
    """A rising signal credits the long and debits the short (zero-sum)."""
    from contracts.futures.futures import long_credit, short_credit
    session, exchange, long_ent, short_ent = world
    pid = open_future(session, exchange, long_ent, short_ent, "GRAIN",
                      Decimal("100"), Decimal("5.00"), expiry=10,
                      margin=Decimal("200"))["position"]
    set_price(session, "GRAIN", Decimal("6.00"))  # +1.00: long up 100, short down 100
    run_tick(session)
    assert long_credit(exchange, pid) == Decimal("300.0000")   # 200 + 100
    assert short_credit(exchange, pid) == Decimal("100.0000")  # 200 - 100
    assert position_status(exchange, pid) == "open"
    assert _no_issuance(session)            # mark-to-market is a pure book update


def test_mark_to_market_is_zero_sum(world):
    """The two credits always sum to the posted pool."""
    from contracts.futures.futures import long_credit, short_credit
    session, exchange, long_ent, short_ent = world
    pid = open_future(session, exchange, long_ent, short_ent, "GRAIN",
                      Decimal("100"), Decimal("5.00"), expiry=10,
                      margin=Decimal("150"))["position"]
    for price in (Decimal("3.00"), Decimal("7.50"), Decimal("5.00"), Decimal("0.50")):
        set_price(session, "GRAIN", price)
        run_tick(session)
        pool = long_credit(exchange, pid) + short_credit(exchange, pid)
        assert pool == Decimal("300.0000")  # 150 + 150, always


def test_breach_flagged_below_maintenance(world):
    """A credit below 50% of initial margin flags the position 'breached'."""
    session, exchange, long_ent, short_ent = world
    pid = open_future(session, exchange, long_ent, short_ent, "GRAIN",
                      Decimal("100"), Decimal("5.00"), expiry=20,
                      margin=Decimal("200"))["position"]
    # Drop the price to 3.00: long loses 200 -> credit 0 (< 50% of 200).
    set_price(session, "GRAIN", Decimal("3.00"))
    run_tick(session)
    assert position_status(exchange, pid) == "breached"


def test_breach_clears_when_price_recovers(world):
    session, exchange, long_ent, short_ent = world
    pid = open_future(session, exchange, long_ent, short_ent, "GRAIN",
                      Decimal("100"), Decimal("5.00"), expiry=20,
                      margin=Decimal("200"))["position"]
    set_price(session, "GRAIN", Decimal("3.00"))
    run_tick(session)
    assert position_status(exchange, pid) == "breached"
    set_price(session, "GRAIN", Decimal("5.00"))
    run_tick(session)
    assert position_status(exchange, pid) == "open"


def test_expiry_flagged_at_maturity(world):
    session, exchange, long_ent, short_ent = world
    pid = open_future(session, exchange, long_ent, short_ent, "GRAIN",
                      Decimal("100"), Decimal("5.00"), expiry=3,
                      margin=Decimal("200"))["position"]
    run_tick(session)  # tick 1
    run_tick(session)  # tick 2
    assert position_status(exchange, pid) != "expired"
    run_tick(session)  # tick 3 == expiry
    assert position_status(exchange, pid) == "expired"


def test_dark_feed_no_signal_no_mark(world):
    """No signal (nil) is a dark feed: the position is not marked this tick."""
    from contracts.futures.futures import long_credit
    session, exchange, long_ent, short_ent = world
    pid = open_future(session, exchange, long_ent, short_ent, "GRAIN",
                      Decimal("100"), Decimal("5.00"), expiry=10,
                      margin=Decimal("200"))["position"]
    # Remove the signal (dark feed).
    setting = session.get(WorldSetting, "futures:price:GRAIN")
    session.delete(setting)
    session.flush()
    run_tick(session)
    assert long_credit(exchange, pid) == Decimal("200")  # unchanged


def test_mark_to_market_is_skip_safe(world):
    """Cumulative-from-contract mark-to-market catches up after a dark tick."""
    from contracts.futures.futures import long_credit
    session, exchange, long_ent, short_ent = world
    pid = open_future(session, exchange, long_ent, short_ent, "GRAIN",
                      Decimal("100"), Decimal("5.00"), expiry=20,
                      margin=Decimal("200"))["position"]
    # Dark feed on tick 1 (no signal set yet beyond the fixture's 5.00).
    set_price(session, "GRAIN", Decimal("7.00"))
    run_tick(session)
    # Long up 200 -> credit 400, even though the price jumped in one step.
    assert long_credit(exchange, pid) == Decimal("400.0000")


# ---------------------------------------------------------------------------
# settlement: the solvent case (the headline: money is conserved)

def test_settle_long_profits_both_solvent(world):
    """Signal rises: long profits, short loses but stays solvent. The pool is
    paid out exactly — no money created or destroyed."""
    session, exchange, long_ent, short_ent = world
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=5, margin=Decimal("200"))
    long0, short0 = _cash(session, long_ent), _cash(session, short_ent)
    set_price(session, "GRAIN", Decimal("6.00"))  # long +100
    run_tick(session)

    summary = settle(session, exchange, 1)

    assert summary["seized"] is None                 # no default -> no seizure
    assert Decimal(summary["long_credit"]) == Decimal("300")
    assert Decimal(summary["short_credit"]) == Decimal("100")
    # Long gets 300 back (200 margin + 100 profit); short gets 100 back.
    assert _cash(session, long_ent) == long0 + Decimal("300")
    assert _cash(session, short_ent) == short0 + Decimal("100")
    assert _pool(exchange) == Decimal("0")           # pool fully paid out
    assert position_status(exchange, 1) == "settled"
    assert _no_issuance(session)                     # pure redistribution


def test_settle_short_profits_both_solvent(world):
    """Signal falls: short profits, long loses but stays solvent."""
    session, exchange, long_ent, short_ent = world
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=5, margin=Decimal("200"))
    long0, short0 = _cash(session, long_ent), _cash(session, short_ent)
    set_price(session, "GRAIN", Decimal("4.00"))  # short +100
    run_tick(session)

    summary = settle(session, exchange, 1)

    assert summary["seized"] is None
    assert _cash(session, long_ent) == long0 + Decimal("100")
    assert _cash(session, short_ent) == short0 + Decimal("300")
    assert _pool(exchange) == Decimal("0")


def test_settle_needs_a_signal(world):
    session, exchange, long_ent, short_ent = world
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=5, margin=Decimal("200"))
    setting = session.get(WorldSetting, "futures:price:GRAIN")
    session.delete(setting)
    session.flush()
    with pytest.raises(ValueError, match="signal"):
        settle(session, exchange, 1)


def test_double_settle_rejected(world):
    session, exchange, long_ent, short_ent = world
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=5, margin=Decimal("200"))
    settle(session, exchange, 1)
    with pytest.raises(ValueError, match="settled"):
        settle(session, exchange, 1)


# ---------------------------------------------------------------------------
# the margin call: seize goods from a defaulter to make the winner whole

def test_settle_seizes_deficiency_goods_from_losing_short(world):
    """Signal rockets up: short's margin is exhausted. The exchange seizes the
    short's GRAIN (worth the deficiency) and redirects it to the long."""
    session, exchange, long_ent, short_ent = world
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=5, margin=Decimal("200"))
    long_grain0 = _grain(session, long_ent)
    short_grain0 = _grain(session, short_ent)
    long0 = _cash(session, long_ent)
    # Signal 9.00: long +400 -> credit 600; short -400 -> credit -200 (deficiency).
    set_price(session, "GRAIN", Decimal("9.00"))
    run_tick(session)

    summary = settle(session, exchange, 1)

    assert summary["seized"] is not None
    seized = Decimal(summary["seized"]["quantity"])
    # Deficiency = 200; at 9.00/unit -> 22.2222 GRAIN seized from short to long.
    assert seized == Decimal("22.2222")
    assert _grain(session, short_ent) == short_grain0 - seized
    assert _grain(session, long_ent) == long_grain0 + seized
    # Long gets the whole pool (400 cash) + the seized goods.
    assert _cash(session, long_ent) == long0 + Decimal("400")
    assert _pool(exchange) == Decimal("0")
    assert position_status(exchange, 1) == "settled"
    assert _no_issuance(session)


def test_settle_seizes_deficiency_goods_from_losing_long(world):
    """Symmetric: signal crashes, the long defaults, goods seized from long."""
    session, exchange, long_ent, short_ent = world
    markets.adjust_holding(session, long_ent, "GRAIN", Decimal("500"))  # long has goods
    session.flush()
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=5, margin=Decimal("200"))
    long_grain0 = _grain(session, long_ent)
    # Signal 1.00: long -400 -> credit -200 (deficiency); short +400 -> credit 600.
    set_price(session, "GRAIN", Decimal("1.00"))
    run_tick(session)

    summary = settle(session, exchange, 1)

    assert summary["seized"] is not None
    seized = Decimal(summary["seized"]["quantity"])
    assert seized == Decimal("200.0000")  # 200 / 1.00
    assert _grain(session, long_ent) == long_grain0 - seized


def test_settle_haircut_when_defaulter_has_no_goods(world):
    """The long defaults but holds no GRAIN: the seize fails, the short takes
    a haircut (gets the pool, not the deficiency)."""
    session, exchange, long_ent, short_ent = world
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=5, margin=Decimal("200"))
    short0 = _cash(session, short_ent)
    # Signal 1.00: long -400 -> credit -200 (deficiency); long holds 0 GRAIN.
    set_price(session, "GRAIN", Decimal("1.00"))
    run_tick(session)

    summary = settle(session, exchange, 1)

    assert summary["seized"] is None               # seize failed (no goods)
    assert _cash(session, short_ent) == short0 + Decimal("400")  # the pool only
    assert _pool(exchange) == Decimal("0")
    assert position_status(exchange, 1) == "settled"


def test_settle_haircut_without_seize_capability(world):
    """No SEIZE: the deficiency seizure is impossible; the winner takes a
    haircut. The seize is caught (MissingCapabilityError), not crashed."""
    session, exchange, long_ent, short_ent = world
    exchange.entity.capabilities = []   # SEIZE revoked
    session.flush()
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=5, margin=Decimal("200"))
    long0 = _cash(session, long_ent)
    set_price(session, "GRAIN", Decimal("9.00"))  # short deficiency
    run_tick(session)

    summary = settle(session, exchange, 1)

    assert summary["seized"] is None               # no SEIZE -> haircut
    assert _cash(session, long_ent) == long0 + Decimal("400")  # pool only
    assert _grain(session, short_ent) == Decimal("1000")       # untouched


# ---------------------------------------------------------------------------
# the margin-sufficiency VALIDATOR (the constitutional backstop)

def test_margin_sufficiency_allows_documented_seize(world):
    """With the validator installed, a documented deficiency seizure proceeds."""
    session, exchange, long_ent, short_ent = world
    _add_validator(session, exchange)
    open_future(session, exchange, long_ent, short_ent, "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=5, margin=Decimal("200"))
    short_grain0 = _grain(session, short_ent)
    set_price(session, "GRAIN", Decimal("9.00"))  # short deficiency
    run_tick(session)

    summary = settle(session, exchange, 1)

    assert summary["seized"] is not None           # documented -> allowed
    assert _grain(session, short_ent) < short_grain0


def test_margin_sufficiency_vetoes_undocumented_seize(world):
    """A naked seize (no deficiency oracle) is vetoed fail-closed: no goods
    move. Tested directly against ``services.seize`` (bypassing settle, which
    always writes the oracle first)."""
    from econengine import services
    from econengine.scripting import OperationVetoedError
    session, exchange, long_ent, short_ent = world
    _add_validator(session, exchange)
    short_grain0 = _grain(session, short_ent)
    # A naked seize with no deficiency oracle -> vetoed.
    with pytest.raises(OperationVetoedError):
        services.seize(session, exchange.entity, short_ent,
                       symbol="GRAIN", quantity=Decimal("10"),
                       rule_ref="futures:rogue",
                       reference="undocumented-seize")
    assert _grain(session, short_ent) == short_grain0   # nothing moved
