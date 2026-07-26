"""Production through the tick engine: script-driven start/cancel, multi-tick
completion, same-tick sale of fresh outputs, and rejection paths."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.markets import adjust_holding, create_market, get_holding
from econengine.models import Base, EntityType, OrderStatus, ProcessStatus, Script, ScriptType
from econengine.production import create_recipe, start_process
from econengine.services import create_account, create_entity
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_script(session, name, source, entity, *, script_type=ScriptType.BEHAVIOUR):
    script = Script(name=name, source=source, script_type=script_type,
                    entity_id=entity.id if entity else None)
    session.add(script)
    session.flush()
    return script


@pytest.fixture
def world(session):
    """Baker with 10 FLOUR; BAKE_BREAD = 2 FLOUR -> 3 BREAD over 2 ticks."""
    baker = create_entity(session, "Baker", EntityType.INDIVIDUAL)
    adjust_holding(session, baker, "FLOUR", Decimal("10"))
    create_recipe(session, "BAKE_BREAD", inputs={"FLOUR": Decimal("2")},
                  outputs={"BREAD": Decimal("3")}, duration_ticks=2)
    return session, baker


def test_script_starts_process_and_completes_over_ticks(world):
    session, baker = world
    script = make_script(session, "bake-once",
                         "if ctx.state.done == nil then "
                         "  ctx.action.start_process('BAKE_BREAD') "
                         "  ctx.state.done = true "
                         "end", baker)

    tick1 = run_tick(session)  # intent resolves during tick 1 -> completes tick 3
    applied = [e for e in tick1.events if e["type"] == "start_process"]
    assert len(applied) == 1 and applied[0]["status"] == "applied"
    assert "process_id" in applied[0]
    assert get_holding(session, baker.id, "FLOUR").quantity == Decimal("8")

    run_tick(session)  # tick 2: still baking
    assert get_holding(session, baker.id, "BREAD") is None

    tick3 = run_tick(session)
    assert get_holding(session, baker.id, "BREAD").quantity == Decimal("3")
    completed = [e for e in tick3.events if e["type"] == "process_completed"]
    assert completed == [{
        "type": "process_completed",
        "entity_id": baker.id,
        "process_id": applied[0]["process_id"],
        "recipe": "BAKE_BREAD",
        "outputs": {"BREAD": "3.0000"},
    }]


def test_ctx_processes_and_completion_event_feed(world):
    session, baker = world
    start_process(session, baker, "BAKE_BREAD")  # started_tick 1, completes 3

    # A watcher script records what it sees in its state.
    make_script(session, "watcher",
                "ctx.state.n_processes = #ctx.processes "
                "for _, e in ipairs(ctx.events) do "
                "  if e.type == 'process_completed' then ctx.state.saw = e.recipe end "
                "end", baker)

    run_tick(session)
    watcher = session.query(Script).filter_by(name="watcher").one()
    assert watcher.state["n_processes"] == 1  # running process visible in ctx

    run_tick(session)
    run_tick(session)  # tick 3 completes; event lands on tick 3
    run_tick(session)  # tick 4: watcher sees it via ctx.events
    session.refresh(watcher)
    assert watcher.state["n_processes"] == 0
    assert watcher.state["saw"] == "BAKE_BREAD"


def test_fresh_outputs_sellable_same_tick(world):
    """Completion runs before scripts, so tick-3 bread can be sold in the
    tick-3 auction."""
    session, baker = world
    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    baker_acct = create_account(session, baker, "USD")
    buyer_acct = create_account(session, buyer, "USD", initial_balance=Decimal("100"))
    create_market(session, "BREAD", "USD")

    start_process(session, baker, "BAKE_BREAD")  # completes tick 3
    make_script(session, "sell-bread",
                "for _, h in ipairs(ctx.holdings) do "
                "  if h.symbol == 'BREAD' then "
                f"    ctx.action.place_order('BREAD', 'sell', h.quantity, '10', '{baker_acct.id}') "
                "  end "
                "end", baker)
    make_script(session, "buy-bread",
                f"ctx.action.place_order('BREAD', 'buy', '3', '10', '{buyer_acct.id}')", buyer)

    run_tick(session)  # 1: bread still baking, only buy order rests
    run_tick(session)  # 2
    tick3 = run_tick(session)

    assert any(e["type"] == "trade" for e in tick3.events)
    assert get_holding(session, buyer.id, "BREAD").quantity == Decimal("3")
    assert baker_acct.balance == Decimal("30")


def test_script_cancel_process(world):
    session, baker = world
    process = start_process(session, baker, "BAKE_BREAD")
    make_script(session, "abort", f"ctx.action.cancel_process('{process.id}')", baker)

    tick = run_tick(session)
    assert process.status == ProcessStatus.CANCELLED
    assert [e["status"] for e in tick.events if e["type"] == "cancel_process"] == ["applied"]
    run_tick(session)
    run_tick(session)
    assert get_holding(session, baker.id, "BREAD") is None


def test_script_cannot_cancel_others_process(world):
    session, baker = world
    rival = create_entity(session, "Rival", EntityType.INDIVIDUAL)
    process = start_process(session, baker, "BAKE_BREAD")
    make_script(session, "sabotage", f"ctx.action.cancel_process('{process.id}')", rival)

    tick = run_tick(session)
    event = next(e for e in tick.events if e["type"] == "cancel_process")
    assert event["status"] == "rejected"
    assert "own" in event["reason"]
    assert process.status == ProcessStatus.RUNNING


def test_insufficient_inputs_rejected_not_partially_consumed(world):
    """A two-input recipe short on the second input must roll back the first."""
    session, baker = world
    create_recipe(session, "FANCY", inputs={"FLOUR": Decimal("1"), "GOLD": Decimal("1")},
                  outputs={"CAKE": Decimal("1")}, duration_ticks=1)
    make_script(session, "overreach", "ctx.action.start_process('FANCY')", baker)

    tick = run_tick(session)
    event = next(e for e in tick.events if e["type"] == "start_process")
    assert event["status"] == "rejected"
    assert get_holding(session, baker.id, "FLOUR").quantity == Decimal("10")  # rolled back
