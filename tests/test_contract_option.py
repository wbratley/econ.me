"""Options reference contract (Step 5d) -- engine validation.

These tests prove the contract's headline asymmetry vs futures: settlement pays
the buyer **only if the option is in the money**. An exchange matches a buyer
(the holder of a right) and a writer (the obligated party); the buyer pays a
one-time **premium** (the price of the right, posts no margin), the writer
posts **margin** (collateral). ``option.lua`` marks to market each tick; at
settlement the buyer gets the intrinsic value if in the money, otherwise
nothing flows from the pool (the writer's margin returns whole). The deficiency
case (payout exceeds margin) reuses the futures ``seize``→``to_entity`` pattern.

Contrast with futures (``test_contract_futures``): a future is a *symmetric*
pair — both sides obligated, both post margin, settlement pays both. An option
is *asymmetric* — the buyer has a *right*, the writer has an *obligation*; the
premium (not margin) is what the buyer pays; settlement pays the long only if
in the money. The ``seize`` primitive is shared; the enforcement role (writer's
margin call) is the option's.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine import capabilities, markets
from econengine.models import (Base, EntityType, Transaction, TransactionType)
from econengine.services import (MissingCapabilityError, create_account,
                                create_entity)
from econengine.tick import run_tick

from contracts.option.option import (buyer_value, open_exchange, open_option,
                                     position_status, settle, total_open_interest,
                                     writer_credit)

SUFFICIENCY_VALIDATOR = (Path(__file__).resolve().parent.parent
                         / "contracts" / "option" / "option_sufficiency.lua").read_text()


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _add_validator(session, exchange):
    """Install the option-sufficiency VALIDATOR on the exchange (optional)."""
    from econengine.models import Script, ScriptType
    session.add(Script(
        name="option-sufficiency", source=SUFFICIENCY_VALIDATOR,
        script_type=ScriptType.VALIDATOR, entity_id=exchange.entity.id,
        is_active=True, state={}))
    session.flush()


def set_price(session, symbol, price):
    """Stand in for the platform oracle that posts signal prices between ticks.
    Shares the futures:price: namespace -- the underlying's price is one oracle."""
    key = f"futures:price:{symbol}"
    setting = session.get(__import__("econengine.models", fromlist=["WorldSetting"]).WorldSetting, key)
    if setting is None:
        from econengine.models import WorldSetting
        session.add(WorldSetting(key=key, value={"price": str(price)}))
    else:
        setting.value = {"price": str(price)}
    session.flush()


@pytest.fixture
def world(session):
    """A funded exchange (with state-granted SEIZE), a buyer and a writer (both
    with cash; the writer also holds goods), and a starting signal of 5.00."""
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    create_account(session, gov, "USD", initial_balance=Decimal("1000000"))
    exchange = open_exchange(session, "Desk", "USD")
    exchange.entity.capabilities = [capabilities.SEIZE]
    session.flush()

    def party(name, cash, grain):
        ent = create_entity(session, name, EntityType.INDIVIDUAL)
        create_account(session, ent, "USD", initial_balance=cash)
        if grain > 0:
            markets.adjust_holding(session, ent, "GRAIN", grain)
        session.flush()
        return ent

    buyer = party("Buyer", Decimal("10000"), Decimal("0"))
    writer = party("Writer", Decimal("10000"), Decimal("1000"))
    set_price(session, "GRAIN", Decimal("5.00"))
    session.flush()
    return session, exchange, buyer, writer


def _cash(entity):
    return [a for a in entity.accounts if a.currency == "USD"][0].balance


def _grain(session, entity):
    h = markets.get_holding(session, entity.id, "GRAIN")
    return h.quantity if h else Decimal("0")


def _pool(exchange):
    return exchange.account.balance


def _no_issuance(session):
    n = session.execute(
        select(func.count()).select_from(Transaction)
        .where(Transaction.tx_type == TransactionType.ISSUANCE)
    ).scalar()
    return n == 0


# ---------------------------------------------------------------------------
# origination: premium buyer->writer, margin writer->pool, no money created

def test_open_option_premium_and_margin(world):
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    # Premium flows buyer -> writer; margin flows writer -> pool.
    assert _cash(buyer) == Decimal("10000") - Decimal("50")
    assert _cash(writer) == Decimal("10000") + Decimal("50") - Decimal("200")
    assert _pool(exchange) == Decimal("200")
    assert _no_issuance(session)


def test_open_option_stamps_open_interest(world):
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    run_tick(session)  # the BEHAVIOUR script stamps open interest
    assert total_open_interest(exchange) == Decimal("100")


def test_open_option_rejects_past_expiry(world):
    session, exchange, buyer, writer = world
    run_tick(session)  # now at tick 1
    with pytest.raises(ValueError, match="future"):
        open_option(session, exchange, buyer, writer, "call", "GRAIN",
                    Decimal("100"), Decimal("5.00"), expiry=1,
                    premium=Decimal("50"), margin=Decimal("200"))


def test_open_option_rejects_bad_kind(world):
    session, exchange, buyer, writer = world
    with pytest.raises(ValueError, match="kind"):
        open_option(session, exchange, buyer, writer, "straddle", "GRAIN",
                    Decimal("100"), Decimal("5.00"), expiry=10,
                    premium=Decimal("50"), margin=Decimal("200"))


def test_open_option_zero_premium(world):
    """A zero premium is allowed (a gift option); only margin must be positive."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("0"), margin=Decimal("200"))
    assert _cash(buyer) == Decimal("10000")        # no premium paid
    assert _cash(writer) == Decimal("10000") - Decimal("200")
    assert _pool(exchange) == Decimal("200")


# ---------------------------------------------------------------------------
# mark-to-market: intrinsic value from the signal

def test_call_mark_to_market(world):
    """A call in the money: buyer_value rises, writer_credit falls (zero-sum)."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("6.00"))    # +1 above strike
    run_tick(session)
    # Intrinsic = (6 - 5) * 100 = 100.
    assert buyer_value(exchange, 1) == Decimal("100.0000")
    assert writer_credit(exchange, 1) == Decimal("100.0000")   # 200 - 100
    assert position_status(exchange, 1) == "open"


def test_put_mark_to_market(world):
    """A put in the money: buyer_value rises as price falls below strike."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "put", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("300"))
    set_price(session, "GRAIN", Decimal("3.00"))    # -2 below strike
    run_tick(session)
    # Intrinsic = (5 - 3) * 100 = 200.
    assert buyer_value(exchange, 1) == Decimal("200.0000")
    assert writer_credit(exchange, 1) == Decimal("100.0000")   # 300 - 200


def test_out_of_the_money_stays_zero(world):
    """A call below strike: intrinsic stays 0, writer keeps full credit."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("4.00"))    # below strike
    run_tick(session)
    assert buyer_value(exchange, 1) == Decimal("0")
    assert writer_credit(exchange, 1) == Decimal("200.0000")


def test_breach_detection(world):
    """Writer's credit below maintenance -> breached (early close-out trigger)."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=100,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("6.50"))    # intrinsic = 150; credit = 50 < 100 (50%)
    run_tick(session)
    assert position_status(exchange, 1) == "breached"


def test_expiry_flag(world):
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=3,
                premium=Decimal("50"), margin=Decimal("200"))
    for _ in range(3):
        run_tick(session)
    assert position_status(exchange, 1) == "expired"


def test_dark_feed_no_mark(world):
    """No signal price -> no mark this tick (skip-safe; values hold stale)."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    # Clear the signal so there is no price this tick.
    from econengine.models import WorldSetting
    session.delete(session.get(WorldSetting, "futures:price:GRAIN"))
    session.flush()
    run_tick(session)
    assert buyer_value(exchange, 1) == Decimal("0")           # not marked
    assert writer_credit(exchange, 1) == Decimal("200")       # still full margin
    assert position_status(exchange, 1) == "open"             # status unchanged


# ---------------------------------------------------------------------------
# settlement: the headline asymmetry -- pays the buyer only if in the money

def test_settle_call_in_the_money(world):
    """Call ITM, fully collateralized: buyer gets intrinsic, writer the rest."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("6.00"))    # intrinsic = 100 <= 200
    summary = settle(session, exchange, 1)
    assert Decimal(summary["payout"]) == Decimal("100")
    assert summary["seized"] is None
    # Buyer: paid 50 premium, gets 100 payout = net +50 from start.
    assert _cash(buyer) == Decimal("10000") - Decimal("50") + Decimal("100")
    # Writer: got 50 premium, posted 200, gets 100 back = net -50 from start.
    assert _cash(writer) == Decimal("10000") + Decimal("50") - Decimal("200") + Decimal("100")
    assert _pool(exchange) == Decimal("0")
    assert position_status(exchange, 1) == "settled"
    assert _no_issuance(session)


def test_settle_call_out_of_the_money(world):
    """Call OTM: buyer gets NOTHING, writer's margin returns whole. This is the
    headline difference from futures -- settlement pays the long only if ITM."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("4.00"))    # intrinsic = 0
    summary = settle(session, exchange, 1)
    assert summary["payout"] is None               # no payout
    assert summary["seized"] is None
    # Buyer: lost the premium, nothing else.
    assert _cash(buyer) == Decimal("10000") - Decimal("50")
    # Writer: kept the premium, margin returned in full.
    assert _cash(writer) == Decimal("10000") + Decimal("50")
    assert _pool(exchange) == Decimal("0")


def test_settle_put_in_the_money(world):
    """Put ITM: buyer profits when price falls below strike."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "put", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("300"))
    set_price(session, "GRAIN", Decimal("3.00"))    # intrinsic = (5-3)*100 = 200
    summary = settle(session, exchange, 1)
    assert Decimal(summary["payout"]) == Decimal("200")
    assert summary["seized"] is None
    assert _cash(buyer) == Decimal("10000") - Decimal("50") + Decimal("200")
    assert _cash(writer) == Decimal("10000") + Decimal("50") - Decimal("300") + Decimal("100")
    assert _pool(exchange) == Decimal("0")


def test_settle_put_out_of_the_money(world):
    """Put OTM (price above strike): expires worthless, writer keeps premium."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "put", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("6.00"))    # intrinsic = 0
    summary = settle(session, exchange, 1)
    assert summary["payout"] is None
    assert _cash(buyer) == Decimal("10000") - Decimal("50")       # lost premium
    assert _cash(writer) == Decimal("10000") + Decimal("50")      # kept premium


# ---------------------------------------------------------------------------
# deficiency: payout exceeds writer's margin -> seize goods (the futures pattern)

def test_settle_deficiency_seizure(world):
    """ITM but margin exhausted: buyer gets the pool + seized goods from the
    writer (redirected via to_entity), exactly as futures settles a deficiency."""
    session, exchange, buyer, writer = world
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("8.00"))    # intrinsic = 300 > 200 margin
    summary = settle(session, exchange, 1)
    assert Decimal(summary["payout"]) == Decimal("200")          # the full pool
    assert summary["seized"] is not None
    assert Decimal(summary["seized"]["quantity"]) == Decimal("12.5")   # 100/8
    assert summary["seized"]["from"] == writer.id
    assert summary["seized"]["to"] == buyer.id
    # Buyer: cash + the seized grain.
    assert _cash(buyer) == Decimal("10000") - Decimal("50") + Decimal("200")
    assert _grain(session, buyer) == Decimal("12.5")
    # Writer: grain reduced by the seized quantity.
    assert _grain(session, writer) == Decimal("1000") - Decimal("12.5")
    assert _pool(exchange) == Decimal("0")
    assert _no_issuance(session)


def test_settle_deficiency_haircut_no_goods(session):
    """Deficiency but the writer holds no goods: buyer takes a haircut (pool only)."""
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    create_account(session, gov, "USD", Decimal("1000000"))
    exchange = open_exchange(session, "Desk", "USD")
    exchange.entity.capabilities = [capabilities.SEIZE]
    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    create_account(session, buyer, "USD", Decimal("10000"))
    writer = create_entity(session, "Writer", EntityType.INDIVIDUAL)
    create_account(session, writer, "USD", Decimal("10000"))
    # Writer holds NO goods.
    set_price(session, "GRAIN", Decimal("5.00"))
    session.flush()
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("8.00"))    # intrinsic = 300 > 200
    summary = settle(session, exchange, 1)
    assert summary["seized"] is None                # seizure failed -> haircut
    # Buyer gets the pool only (200), not the full 300.
    assert _cash(buyer) == Decimal("10000") - Decimal("50") + Decimal("200")
    assert _grain(session, buyer) == Decimal("0")   # nothing seized (writer has none)


def test_settle_deficiency_haircut_no_seize(session):
    """Deficiency but the exchange lacks SEIZE: buyer takes a haircut."""
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    create_account(session, gov, "USD", Decimal("1000000"))
    exchange = open_exchange(session, "Desk", "USD")
    # NO capabilities granted -- the exchange cannot seize.
    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    create_account(session, buyer, "USD", Decimal("10000"))
    writer = create_entity(session, "Writer", EntityType.INDIVIDUAL)
    create_account(session, writer, "USD", Decimal("10000"))
    markets.adjust_holding(session, writer, "GRAIN", Decimal("1000"))
    set_price(session, "GRAIN", Decimal("5.00"))
    session.flush()
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("8.00"))    # intrinsic = 300 > 200
    summary = settle(session, exchange, 1)
    assert summary["seized"] is None                # no SEIZE -> haircut
    assert _cash(buyer) == Decimal("10000") - Decimal("50") + Decimal("200")
    assert _grain(session, buyer) == Decimal("0")   # nothing seized
    assert _grain(session, writer) == Decimal("1000")  # writer keeps goods


# ---------------------------------------------------------------------------
# the option-sufficiency VALIDATOR (the constitutional backstop)

def test_sufficiency_allows_documented_deficiency(world):
    """With the validator installed, settle()'s documented seizure proceeds."""
    session, exchange, buyer, writer = world
    _add_validator(session, exchange)
    open_option(session, exchange, buyer, writer, "call", "GRAIN",
                Decimal("100"), Decimal("5.00"), expiry=10,
                premium=Decimal("50"), margin=Decimal("200"))
    set_price(session, "GRAIN", Decimal("8.00"))    # intrinsic = 300 > 200
    summary = settle(session, exchange, 1)
    assert summary["seized"] is not None            # documented -> allowed
    assert _grain(session, buyer) == Decimal("12.5")


def test_sufficiency_vetoes_naked_seizure(world):
    """A seize without the deficiency oracle is vetoed fail-closed."""
    from econengine.scripting import OperationVetoedError
    from econengine import services
    session, exchange, buyer, writer = world
    _add_validator(session, exchange)
    # A naked seize (no oracle written) -> vetoed.
    with pytest.raises(OperationVetoedError):
        services.seize(session, exchange.entity, writer,
                       symbol="GRAIN", quantity=Decimal("10"),
                       rule_ref="option:rogue",
                       reference="option-naked")
    assert _grain(session, writer) == Decimal("1000")   # nothing seized
