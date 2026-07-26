"""Needs through the tick engine: pass ordering (consumption after the
auction, before decay) and behaviour scripts reacting to satisfaction."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econ.goods import create_good
from econ.markets import adjust_holding, create_market, get_holding
from econ.models import Base, EntityType, Script, ScriptType
from econ.needs import create_need
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


def test_goods_bought_this_tick_are_eaten_this_tick(session):
    """Consumption runs after the auction: a person can earn nothing, buy
    bread, and eat it within a single tick — the demand loop is one tick
    tight. The seller's declared supply settles before anything is eaten."""
    create_need(session, "FOOD", Decimal("1"), ["BREAD"], entity_type=EntityType.INDIVIDUAL)
    create_market(session, "BREAD", "USD")
    seller = create_entity(session, "Seller", EntityType.BUSINESS)
    eater = create_entity(session, "Eater", EntityType.INDIVIDUAL)
    seller_acct = create_account(session, seller, "USD")
    eater_acct = create_account(session, eater, "USD", initial_balance=Decimal("2"))
    adjust_holding(session, seller, "BREAD", Decimal("1"))

    make_script(session, "sell",
                f"ctx.action.place_order('BREAD', 'sell', '1', '2', '{seller_acct.id}')", seller)
    make_script(session, "buy",
                f"ctx.action.place_order('BREAD', 'buy', '1', '2', '{eater_acct.id}')", eater)

    tick = run_tick(session)

    assert any(e["type"] == "trade" for e in tick.events)
    satisfied = next(e for e in tick.events if e["type"] == "need_satisfied")
    assert satisfied["entity_id"] == eater.id and satisfied["need"] == "FOOD"
    assert get_holding(session, eater.id, "BREAD").quantity == Decimal("0")  # eaten


def test_consumption_runs_before_decay(session):
    """You eat fresh: the need is met from full stock, and only the uneaten
    remainder rots. Decay-first would rot the bread before dinner."""
    create_good(session, "BREAD", decay_per_tick=Decimal("0.5"))
    create_need(session, "FOOD", Decimal("1"), ["BREAD"], entity_type=EntityType.INDIVIDUAL)
    eater = create_entity(session, "Eater", EntityType.INDIVIDUAL)
    adjust_holding(session, eater, "BREAD", Decimal("2"))

    tick = run_tick(session)

    # Eat 1 of 2, then half of the leftover loaf rots.
    assert any(e["type"] == "need_satisfied" for e in tick.events)
    assert get_holding(session, eater.id, "BREAD").quantity == Decimal("0.5")
    decay = next(e for e in tick.events if e["type"] == "decay")
    assert decay["decayed"] == "0.5000"
    # Consumption events precede decay events within the tick.
    types = [e["type"] for e in tick.events]
    assert types.index("need_satisfied") < types.index("decay")


def test_selling_your_food_is_your_own_choice(session):
    """The auction settles declared sell orders before the consumption pass:
    the engine never withholds stock an entity chose to sell — going hungry
    to make a sale is the script's policy, and the unmet need records it."""
    create_need(session, "FOOD", Decimal("1"), ["BREAD"], entity_type=EntityType.INDIVIDUAL)
    create_market(session, "BREAD", "USD")
    farmer = create_entity(session, "Farmer", EntityType.INDIVIDUAL)
    buyer = create_entity(session, "Buyer", EntityType.BUSINESS)
    farmer_acct = create_account(session, farmer, "USD")
    buyer_acct = create_account(session, buyer, "USD", initial_balance=Decimal("2"))
    adjust_holding(session, farmer, "BREAD", Decimal("1"))

    make_script(session, "sell all",
                f"ctx.action.place_order('BREAD', 'sell', '1', '2', '{farmer_acct.id}')", farmer)
    make_script(session, "buy",
                f"ctx.action.place_order('BREAD', 'buy', '1', '2', '{buyer_acct.id}')", buyer)

    tick = run_tick(session)

    assert any(e["type"] == "trade" for e in tick.events)
    unmet = next(e for e in tick.events if e["type"] == "need_unmet")
    assert unmet["entity_id"] == farmer.id and unmet["satisfaction"] == "0.0000"
    assert get_holding(session, buyer.id, "BREAD").quantity == Decimal("1")


def test_behaviour_script_reacts_to_need_unmet_event(session):
    """The demand signal drives behaviour: a hungry person sees last tick's
    need_unmet event (their own entity's events only) and bids for bread."""
    create_need(session, "FOOD", Decimal("1"), ["BREAD"], entity_type=EntityType.INDIVIDUAL)
    create_market(session, "BREAD", "USD")
    eater = create_entity(session, "Eater", EntityType.INDIVIDUAL)
    baker = create_entity(session, "Baker", EntityType.BUSINESS)
    eater_acct = create_account(session, eater, "USD", initial_balance=Decimal("2"))
    baker_acct = create_account(session, baker, "USD")
    adjust_holding(session, baker, "BREAD", Decimal("1"))

    make_script(session, "eat when hungry",
                "for _, e in ipairs(ctx.events) do "
                "  if e.type == 'need_unmet' and e.need == 'FOOD' then "
                f"    ctx.action.place_order('BREAD', 'buy', '1', '2', '{eater_acct.id}') "
                "  end "
                "end", eater)
    make_script(session, "always selling",
                f"ctx.action.place_order('BREAD', 'sell', '1', '2', '{baker_acct.id}')", baker)

    tick1 = run_tick(session)  # nothing to eat yet: unmet, no bid placed
    assert any(e["type"] == "need_unmet" and e["entity_id"] == eater.id for e in tick1.events)
    assert not any(e["type"] == "trade" for e in tick1.events)

    tick2 = run_tick(session)  # reacts to tick 1's hunger, buys, eats
    assert any(e["type"] == "trade" for e in tick2.events)
    assert any(e["type"] == "need_satisfied" and e["entity_id"] == eater.id
               for e in tick2.events)


def test_ctx_needs_exposes_definitions_and_satisfaction(session):
    """Scripts see their applicable needs — quantities to plan purchases
    before any event has fired, and the current satisfaction score after."""
    create_need(session, "FOOD", Decimal("2"), ["BREAD", "FISH"],
                entity_type=EntityType.INDIVIDUAL, priority=1)
    create_need(session, "WARMTH", Decimal("1"), ["COAL"])  # applies to everyone
    person = create_entity(session, "Person", EntityType.INDIVIDUAL)
    adjust_holding(session, person, "BREAD", Decimal("1"))

    make_script(session, "introspect",
                "local n = ctx.needs[1] "
                "ctx.state.first = n.code "
                "ctx.state.count = #ctx.needs "
                "ctx.state.qty = n.quantity_per_tick "
                "ctx.state.sat = n.satisfaction "
                "ctx.state.satisfier = n.satisfiers[1]", person)
    script = session.query(Script).one()

    run_tick(session)
    session.refresh(script)
    # Ordered by priority: WARMTH (0) before FOOD (1); no pass has run yet.
    assert script.state["first"] == "WARMTH"
    assert script.state["count"] == 2
    assert script.state["sat"] == "0"
    assert script.state["satisfier"] == "COAL"

    run_tick(session)
    session.refresh(script)
    # Tick 2 sees tick 1's scores: WARMTH had no COAL to draw.
    assert script.state["sat"] == "0.0000"
