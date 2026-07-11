import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econ.models import Base, EntityType, Script, ScriptType
from econ.services import create_account, create_entity
from econ.tick import run_tick


@pytest.fixture
def session():
    # check_same_thread off: ctx.query.* callbacks run on the script thread
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_script(session, name, source, entity, *, script_type=ScriptType.BEHAVIOUR, **kwargs):
    script = Script(
        name=name,
        source=source,
        script_type=script_type,
        entity_id=entity.id if entity else None,
        **kwargs,
    )
    session.add(script)
    session.flush()
    return script


@pytest.fixture
def world(session):
    """Alice (1000 USD), Bob (0 USD), and a central bank (monetary authority)."""
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    cb = create_entity(session, "Central Bank", EntityType.BANK)
    cb.is_monetary_authority = True
    a = create_account(session, alice, "USD", initial_balance=Decimal("1000"))
    b = create_account(session, bob, "USD")
    c = create_account(session, cb, "USD")
    return session, alice, bob, cb, a, b, c


def test_tick_numbers_increment(session):
    assert run_tick(session).number == 1
    assert run_tick(session).number == 2


def test_behaviour_script_transfers_money(world):
    session, alice, bob, cb, a, b, c = world
    make_script(session, "pay-bob", f"ctx.action.transfer('{a.id}', '{b.id}', '100', 'rent')", alice)

    tick = run_tick(session)

    assert a.balance == Decimal("900")
    assert b.balance == Decimal("100")
    assert tick.events[0]["status"] == "applied"
    assert tick.events[0]["type"] == "transfer"


def test_monetary_authority_script_issues_money(world):
    session, alice, bob, cb, a, b, c = world
    make_script(session, "qe", f"ctx.action.issue_money('{c.id}', '5000', 'QE')", cb)

    tick = run_tick(session)

    assert c.balance == Decimal("5000")
    assert tick.events[0]["status"] == "applied"


def test_non_authority_issue_rejected(world):
    session, alice, bob, cb, a, b, c = world
    make_script(session, "counterfeit", f"ctx.action.issue_money('{a.id}', '5000', 'ha')", alice)

    tick = run_tick(session)

    assert a.balance == Decimal("1000")
    assert tick.events[0]["status"] == "rejected"
    assert "monetary authority" in tick.events[0]["reason"]


def test_cannot_transfer_from_unowned_account(world):
    session, alice, bob, cb, a, b, c = world
    # Bob's script tries to move Alice's money to Bob
    make_script(session, "theft", f"ctx.action.transfer('{a.id}', '{b.id}', '500', 'steal')", bob)

    tick = run_tick(session)

    assert a.balance == Decimal("1000")
    assert b.balance == Decimal("0")
    assert tick.events[0]["status"] == "rejected"
    assert "does not own" in tick.events[0]["reason"]


def test_failed_intent_does_not_block_others(world):
    session, alice, bob, cb, a, b, c = world
    make_script(
        session, "overdraw-then-pay",
        f"""
ctx.action.transfer('{a.id}', '{b.id}', '999999', 'too much', 10)
ctx.action.transfer('{a.id}', '{b.id}', '50', 'ok', 20)
""",
        alice,
    )

    tick = run_tick(session)

    statuses = [e["status"] for e in tick.events]
    assert statuses == ["rejected", "applied"]
    assert a.balance == Decimal("950")
    assert b.balance == Decimal("50")


def test_priority_orders_intents(world):
    session, alice, bob, cb, a, b, c = world
    # Only 1000 available: the lower-priority-number intent must win
    make_script(
        session, "compete",
        f"""
ctx.action.transfer('{a.id}', '{b.id}', '800', 'low priority', 200)
ctx.action.transfer('{a.id}', '{c.id}', '800', 'high priority', 1)
""",
        alice,
    )

    tick = run_tick(session)

    by_ref = {e["params"]["reference"]: e["status"] for e in tick.events}
    assert by_ref["high priority"] == "applied"
    assert by_ref["low priority"] == "rejected"
    assert c.balance == Decimal("800")


def test_state_persists_across_ticks(world):
    session, alice, bob, cb, a, b, c = world
    script = make_script(session, "counter", "ctx.state.n = (ctx.state.n or 0) + 1", alice)

    run_tick(session)
    assert script.state["n"] == 1
    run_tick(session)
    assert script.state["n"] == 2


def test_events_fed_to_next_tick(world):
    session, alice, bob, cb, a, b, c = world
    script = make_script(
        session, "observer",
        f"""
ctx.state.seen = #ctx.events
if ctx.state.seen == 0 then
    ctx.action.transfer('{a.id}', '{b.id}', '10', 'first')
end
""",
        alice,
    )

    run_tick(session)
    assert script.state["seen"] == 0
    run_tick(session)
    assert script.state["seen"] == 1  # sees its own applied transfer from tick 1


def test_events_filtered_by_entity(world):
    session, alice, bob, cb, a, b, c = world
    make_script(session, "alice-pays", f"ctx.action.transfer('{a.id}', '{b.id}', '10', 'x')", alice)
    bob_script = make_script(session, "bob-watches", "ctx.state.seen = #ctx.events", bob)

    run_tick(session)
    run_tick(session)
    # Alice's transfer event belongs to Alice, not Bob
    assert bob_script.state["seen"] == 0


def test_script_error_recorded_as_event(world):
    session, alice, bob, cb, a, b, c = world
    make_script(session, "broken", "error('boom')", alice)
    make_script(session, "working", f"ctx.action.transfer('{a.id}', '{b.id}', '10', 'ok')", alice)

    tick = run_tick(session)

    types = [e["type"] for e in tick.events]
    assert "script_error" in types
    assert b.balance == Decimal("10")


def test_inactive_scripts_skipped(world):
    session, alice, bob, cb, a, b, c = world
    make_script(session, "off", f"ctx.action.transfer('{a.id}', '{b.id}', '10', 'x')", alice, is_active=False)

    tick = run_tick(session)

    assert tick.events == []
    assert a.balance == Decimal("1000")


def test_query_balance_and_total_supply(world):
    session, alice, bob, cb, a, b, c = world
    script = make_script(
        session, "reader",
        f"""
ctx.state.my_balance = ctx.query.balance('{a.id}')
ctx.state.supply = ctx.query.total_supply('USD')
ctx.state.price = ctx.query.market_price('WHEAT')
""",
        alice,
    )

    run_tick(session)

    assert Decimal(script.state["my_balance"]) == Decimal("1000")
    assert Decimal(script.state["supply"]) == Decimal("1000")
    assert script.state.get("price") is None


def test_unattached_behaviour_script_skipped(session):
    script = Script(name="orphan", source="error('should not run')", script_type=ScriptType.BEHAVIOUR)
    session.add(script)
    session.flush()

    tick = run_tick(session)
    assert tick.events == []
