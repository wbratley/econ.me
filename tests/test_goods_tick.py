"""Goods through the tick engine: pass ordering (auto-issue before scripts,
decay after the auction) and the flagship fully circular economy."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econ.goods import create_good
from econ.markets import adjust_holding, create_market, get_holding
from econ.models import Base, EntityType, Script, ScriptType
from econ.production import create_recipe
from econ.services import create_account, create_entity
from econ.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_script(session, name, source, entity):
    script = Script(name=name, source=source, script_type=ScriptType.BEHAVIOUR,
                    entity_id=entity.id)
    session.add(script)
    session.flush()
    return script


def test_auto_issued_labor_sellable_same_tick(session):
    """Auto-issue runs before scripts, so tick-1 labor trades in the tick-1
    auction — a person with nothing can earn a wage immediately."""
    create_good(session, "LABOR", auto_issue_quantity=Decimal("8"),
                auto_issue_entity_type=EntityType.INDIVIDUAL)
    create_market(session, "LABOR", "USD")
    person = create_entity(session, "Person", EntityType.INDIVIDUAL)
    firm = create_entity(session, "Firm", EntityType.BUSINESS)
    person_acct = create_account(session, person, "USD")
    firm_acct = create_account(session, firm, "USD", initial_balance=Decimal("100"))

    make_script(session, "work",
                "for _, h in ipairs(ctx.holdings) do "
                "  if h.symbol == 'LABOR' then "
                f"    ctx.action.place_order('LABOR', 'sell', h.quantity, '1', '{person_acct.id}') "
                "  end "
                "end", person)
    make_script(session, "hire",
                f"ctx.action.place_order('LABOR', 'buy', '8', '1', '{firm_acct.id}')", firm)

    tick = run_tick(session)

    assert any(e["type"] == "trade" for e in tick.events)
    assert person_acct.balance == Decimal("8")
    assert get_holding(session, firm.id, "LABOR").quantity == Decimal("8")


def test_decay_runs_after_auction(session):
    """A seller's entire perishable stock clears at auction before decay, so
    the buyer takes delivery of the full quantity and the rot hits them."""
    create_good(session, "BREAD", decay_per_tick=Decimal("0.5"))
    create_market(session, "BREAD", "USD")
    seller = create_entity(session, "Seller", EntityType.BUSINESS)
    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    seller_acct = create_account(session, seller, "USD")
    buyer_acct = create_account(session, buyer, "USD", initial_balance=Decimal("10"))
    adjust_holding(session, seller, "BREAD", Decimal("2"))

    make_script(session, "sell",
                f"ctx.action.place_order('BREAD', 'sell', '2', '2', '{seller_acct.id}')", seller)
    make_script(session, "buy",
                f"ctx.action.place_order('BREAD', 'buy', '2', '2', '{buyer_acct.id}')", buyer)

    tick = run_tick(session)

    # Had decay run before the auction, the seller would have held only 1
    # BREAD at settlement and the order would have been cancelled short.
    trades = [e for e in tick.events if e["type"] == "trade" and e["side"] == "buy"]
    assert [t["quantity"] for t in trades] == ["2.0000"]
    assert get_holding(session, buyer.id, "BREAD").quantity == Decimal("1")  # then it rots
    assert get_holding(session, seller.id, "BREAD").quantity == Decimal("0")


def test_unsold_perishables_rot(session):
    create_good(session, "BREAD", decay_per_tick=Decimal("0.5"))
    create_market(session, "BREAD", "USD")
    seller = create_entity(session, "Seller", EntityType.BUSINESS)
    seller_acct = create_account(session, seller, "USD")
    adjust_holding(session, seller, "BREAD", Decimal("2"))

    make_script(session, "sell",
                f"ctx.action.place_order('BREAD', 'sell', '2', '2', '{seller_acct.id}')", seller)

    tick = run_tick(session)  # no buyer: auction clears nothing

    assert not any(e["type"] == "trade" for e in tick.events)
    assert get_holding(session, seller.id, "BREAD").quantity == Decimal("1")
    decay = next(e for e in tick.events if e["type"] == "decay")
    assert decay == {"type": "decay", "entity_id": None, "symbol": "BREAD",
                     "decayed": "1.0000", "holders": 1}


def test_circular_economy(session):
    """The flagship: over 3 ticks money and goods complete a full circle.
    Person sells auto-issued labor to the baker, the baker bakes bread with
    it, the person buys the bread back with their wages — the baker's cash
    returns exactly to its starting balance, and the person's bread rots."""
    create_good(session, "LABOR", auto_issue_quantity=Decimal("8"),
                auto_issue_entity_type=EntityType.INDIVIDUAL)
    create_good(session, "BREAD", decay_per_tick=Decimal("0.5"))
    create_market(session, "LABOR", "USD")
    create_market(session, "BREAD", "USD")
    create_recipe(session, "BAKE", inputs={"LABOR": Decimal("4")},
                  outputs={"BREAD": Decimal("2")}, duration_ticks=1)

    person = create_entity(session, "Person", EntityType.INDIVIDUAL)
    baker = create_entity(session, "Baker", EntityType.BUSINESS)
    person_acct = create_account(session, person, "USD")
    baker_acct = create_account(session, baker, "USD", initial_balance=Decimal("4"))

    tick_counter = "local t = (ctx.state.t or 0) + 1 ctx.state.t = t "
    make_script(session, "person", tick_counter +
                f"if t == 1 then ctx.action.place_order('LABOR', 'sell', '4', '1', '{person_acct.id}') end "
                f"if t == 3 then ctx.action.place_order('BREAD', 'buy', '2', '2', '{person_acct.id}') end",
                person)
    make_script(session, "baker", tick_counter +
                f"if t == 1 then ctx.action.place_order('LABOR', 'buy', '4', '1', '{baker_acct.id}') end "
                "if t == 2 then ctx.action.start_process('BAKE') end "
                f"if t == 3 then ctx.action.place_order('BREAD', 'sell', '2', '2', '{baker_acct.id}') end",
                baker)

    # Tick 1: person is issued 8 LABOR, sells 4 @ 1 to the baker.
    tick1 = run_tick(session)
    issue1 = next(e for e in tick1.events if e["type"] == "auto_issue")
    assert issue1["symbol"] == "LABOR" and issue1["issued"] == "8.0000"
    assert person_acct.balance == Decimal("4")  # wages
    assert baker_acct.balance == Decimal("0")
    assert get_holding(session, baker.id, "LABOR").quantity == Decimal("4")

    # Tick 2: person is topped back up (not doubled); baker starts baking.
    tick2 = run_tick(session)
    issue2 = next(e for e in tick2.events if e["type"] == "auto_issue")
    assert issue2["issued"] == "4.0000"  # top-up from 4 to 8
    assert get_holding(session, person.id, "LABOR").quantity == Decimal("8")
    assert get_holding(session, baker.id, "LABOR").quantity == Decimal("0")  # consumed

    # Tick 3: bread completes before scripts, sells the same tick, and the
    # person's uneaten bread rots after the auction.
    tick3 = run_tick(session)
    assert any(e["type"] == "process_completed" for e in tick3.events)
    assert not any(e["type"] == "auto_issue" for e in tick3.events)  # already at 8
    assert baker_acct.balance == Decimal("4")  # full circle
    assert person_acct.balance == Decimal("0")
    assert get_holding(session, person.id, "BREAD").quantity == Decimal("1")
    assert get_holding(session, baker.id, "BREAD").quantity == Decimal("0")
    decay = next(e for e in tick3.events if e["type"] == "decay")
    assert decay["symbol"] == "BREAD" and decay["decayed"] == "1.0000"
