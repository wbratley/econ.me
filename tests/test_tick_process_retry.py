"""Where production sits relative to the auction in a tick.

A start_process rejected only for want of inputs is retried after the auction,
so a process can be fed by what its entity bought that same tick. The retry is
deliberately not a wholesale "run production after clearing": these tests pin
the three things that would quietly break if it were, alongside the fix itself.
"""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.goods import create_good
from econengine.markets import adjust_holding, create_market, get_holding
from econengine.models import Base, EntityType, Script, ScriptType
from econengine.production import create_recipe
from econengine.services import create_account, create_entity
from econengine.tick import run_tick


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


def starts(tick, recipe=None):
    out = [e for e in tick.events if e["type"] == "start_process"]
    if recipe is not None:
        out = [e for e in out if (e.get("params") or {}).get("recipe") == recipe]
    return out


@pytest.fixture
def market_world(session):
    """A baker who can afford flour but holds none, and a miller selling it."""
    create_good(session, "FLOUR")
    create_good(session, "BREAD")
    create_market(session, "FLOUR", "USD")

    baker = create_entity(session, "Baker", EntityType.INDIVIDUAL)
    baker_acct = create_account(session, baker, "USD")
    baker_acct.balance = Decimal("1000")

    miller = create_entity(session, "Miller", EntityType.INDIVIDUAL)
    miller_acct = create_account(session, miller, "USD")
    adjust_holding(session, miller, "FLOUR", Decimal("10"))

    create_recipe(session, "BAKE_BREAD", inputs={"FLOUR": Decimal("2")},
                  outputs={"BREAD": Decimal("3")}, duration_ticks=1)
    session.flush()
    return session, baker, baker_acct, miller, miller_acct


def test_process_is_fed_by_what_its_entity_bought_this_tick(market_world):
    """The fix. Baker holds no FLOUR when its script runs, buys 2 in this
    tick's auction, and the retry lets BAKE_BREAD start the same tick."""
    session, baker, baker_acct, miller, miller_acct = market_world

    make_script(session, "buy-and-bake",
                "if ctx.state.done == nil then"
                "  ctx.action.place_order('FLOUR', 'buy', '2', '10', '%s')"
                "  ctx.action.start_process('BAKE_BREAD')"
                "  ctx.state.done = true "
                "end" % baker_acct.id, baker)
    make_script(session, "sell-flour",
                "if ctx.state.done == nil then"
                "  ctx.action.place_order('FLOUR', 'sell', '2', '1', '%s')"
                "  ctx.state.done = true "
                "end" % miller_acct.id, miller)

    assert get_holding(session, baker.id, "FLOUR") is None

    tick1 = run_tick(session)
    applied = starts(tick1, "BAKE_BREAD")
    assert len(applied) == 1, "exactly one event per intent, not one per attempt"
    assert applied[0]["status"] == "applied"
    # Bought 2, consumed 2 by the process that same tick.
    holding = get_holding(session, baker.id, "FLOUR")
    assert (holding.quantity if holding else Decimal("0")) == Decimal("0")

    run_tick(session)
    assert get_holding(session, baker.id, "BREAD").quantity == Decimal("3")


def test_rejection_for_another_reason_is_not_retried(market_world):
    """Only the shortfall a trade could cure is retried, and a genuine failure
    is still reported exactly once."""
    session, baker, baker_acct, miller, miller_acct = market_world

    make_script(session, "bake-nonsense",
                "if ctx.state.done == nil then"
                "  ctx.action.start_process('NO_SUCH_RECIPE')"
                "  ctx.state.done = true "
                "end", baker)

    tick1 = run_tick(session)
    rejected = starts(tick1)
    assert len(rejected) == 1
    assert rejected[0]["status"] == "rejected"
    assert "short_of_holdings" not in rejected[0]


def test_still_short_after_the_auction_is_reported_once(market_world):
    """Retried and still unfed: one rejection event, not two."""
    session, baker, baker_acct, miller, miller_acct = market_world

    # Baker tries to bake with no flour and nobody selling any.
    make_script(session, "bake-dry",
                "if ctx.state.done == nil then"
                "  ctx.action.start_process('BAKE_BREAD')"
                "  ctx.state.done = true "
                "end", baker)

    tick1 = run_tick(session)
    rejected = starts(tick1, "BAKE_BREAD")
    assert len(rejected) == 1
    assert rejected[0]["status"] == "rejected"
    assert rejected[0].get("short_of_holdings") is True


def test_process_keeps_first_claim_over_a_sell_order_of_the_same_good(session):
    """Orders do not escrow -- holdings are checked live at settlement -- so
    whichever runs first wins. Production runs first (line order, the stable tie-break). If
    production were deferred wholesale past the auction, the sale below would
    take the flour out from under the process and it would be rejected."""
    create_good(session, "FLOUR")
    create_good(session, "BREAD")
    create_market(session, "FLOUR", "USD")

    baker = create_entity(session, "Baker", EntityType.INDIVIDUAL)
    baker_acct = create_account(session, baker, "USD")
    adjust_holding(session, baker, "FLOUR", Decimal("2"))

    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    buyer_acct = create_account(session, buyer, "USD")
    buyer_acct.balance = Decimal("1000")

    create_recipe(session, "BAKE_BREAD", inputs={"FLOUR": Decimal("2")},
                  outputs={"BREAD": Decimal("3")}, duration_ticks=1)
    session.flush()

    # Baker holds exactly 2 FLOUR and both uses AND offers all of it. Line
    # order puts the bake first; the bake must win.
    make_script(session, "bake-then-offer",
                "if ctx.state.done == nil then"
                "  ctx.action.start_process('BAKE_BREAD')"
                "  ctx.action.place_order('FLOUR', 'sell', '2', '1', '%s')"
                "  ctx.state.done = true "
                "end" % baker_acct.id, baker)
    make_script(session, "buy-flour",
                "if ctx.state.done == nil then"
                "  ctx.action.place_order('FLOUR', 'buy', '2', '10', '%s')"
                "  ctx.state.done = true "
                "end" % buyer_acct.id, buyer)

    tick1 = run_tick(session)
    applied = starts(tick1, "BAKE_BREAD")
    assert len(applied) == 1 and applied[0]["status"] == "applied", \
        "a sell order must not take inputs out from under this entity's own process"
    # The sale could not settle: the flour was already consumed by the bake.
    assert get_holding(session, buyer.id, "FLOUR") is None


def test_duration_zero_output_is_sellable_in_the_same_tick(session):
    """A duration-0 recipe completes inline inside start_process, so its output
    must reach this tick's auction. Deferring production past the auction would
    push that output into the decay pass instead -- the mirror of the bug the
    retry exists to fix."""
    create_good(session, "GRAIN")
    create_good(session, "FLOUR")
    create_market(session, "FLOUR", "USD")

    miller = create_entity(session, "Miller", EntityType.INDIVIDUAL)
    miller_acct = create_account(session, miller, "USD")
    adjust_holding(session, miller, "GRAIN", Decimal("5"))

    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    buyer_acct = create_account(session, buyer, "USD")
    buyer_acct.balance = Decimal("1000")

    create_recipe(session, "MILL", inputs={"GRAIN": Decimal("2")},
                  outputs={"FLOUR": Decimal("2")}, duration_ticks=0)
    session.flush()

    # Mill first, then offer the fresh flour (line order = intent order).
    make_script(session, "mill-and-sell",
                "if ctx.state.done == nil then"
                "  ctx.action.start_process('MILL')"
                "  ctx.action.place_order('FLOUR', 'sell', '2', '1', '%s')"
                "  ctx.state.done = true "
                "end" % miller_acct.id, miller)
    make_script(session, "buy-flour",
                "if ctx.state.done == nil then"
                "  ctx.action.place_order('FLOUR', 'buy', '2', '10', '%s')"
                "  ctx.state.done = true "
                "end" % buyer_acct.id, buyer)

    run_tick(session)
    assert get_holding(session, buyer.id, "FLOUR").quantity == Decimal("2"), \
        "flour milled this tick must be sellable in this tick's auction"
