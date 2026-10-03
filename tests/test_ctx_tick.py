"""ctx.tick — the time primitive (Step 5a).

A script reads the current tick as ctx.tick rather than counting its own
runs in state. Counting runs desynchronises from wall-tick the moment a
script is budget-skipped; ctx.tick always tells the truth.
"""
import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.models import Base, EntityType, Script, ScriptType
from econengine.services import create_account, create_entity, transfer
from econengine.tick import run_tick, set_compute_budget_ms


@pytest.fixture
def session():
    # check_same_thread off: ctx.query.* callbacks run on the script thread
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    a = create_account(session, alice, "USD", initial_balance=Decimal("1000"))
    b = create_account(session, bob, "USD", initial_balance=Decimal("1000"))
    g = create_account(session, gov, "USD")
    return session, alice, bob, gov, a, b, g


def make_script(session, name, source, script_type, entity=None, **kwargs):
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


# --- tick-run scripts (POLICY/BEHAVIOUR): the tick currently executing ---

def test_ctx_tick_reflects_current_tick(world):
    session, alice, bob, gov, a, b, g = world
    script = make_script(
        session, "clock", "ctx.state.last = ctx.tick",
        ScriptType.POLICY, entity=gov,
    )

    run_tick(session)
    assert script.state["last"] == 1

    run_tick(session)
    assert script.state["last"] == 2


def test_ctx_tick_survives_budget_skip(world):
    """The acceptance test for 5a. A self-counter desyncs when a script is
    budget-skipped; ctx.tick tracks wall-tick and recovers.

    tick 1: script runs        -> state.last == 1
    tick 2: budget 0 -> skipped -> state.last unchanged (1)
    tick 3: budget lifted       -> state.last == 3   (NOT 2, as a
                                                run-counter would give)
    """
    session, alice, bob, gov, a, b, g = world
    script = make_script(
        session, "clock", "ctx.state.last = ctx.tick",
        ScriptType.POLICY, entity=gov,
    )

    run_tick(session)
    assert script.state["last"] == 1

    set_compute_budget_ms(session, 0)          # skip all scripts this tick
    tick2 = run_tick(session)
    assert script.state["last"] == 1           # unchanged: body never ran
    assert any(
        e["type"] == "compute_budget_exceeded" for e in tick2.events
    )

    set_compute_budget_ms(session, None)        # lift the budget
    run_tick(session)
    assert script.state["last"] == 3           # wall-tick, not run-count


def test_ctx_tick_in_behaviour_script(world):
    """BEHAVIOUR scripts see the same ctx.tick as POLICY scripts in the same
    tick (both threaded from the one tick number)."""
    session, alice, bob, gov, a, b, g = world
    seen = make_script(
        session, "b", "ctx.state.t = ctx.tick",
        ScriptType.BEHAVIOUR, entity=alice,
    )

    run_tick(session)
    run_tick(session)
    assert seen.state["t"] == 2


# --- validators/hooks: the latest committed tick ---

def test_ctx_tick_in_hook_before_any_tick(world):
    """Before tick 1 commits, the latest tick is 0."""
    session, alice, bob, gov, a, b, g = world
    hook = make_script(
        session, "audit", "ctx.state.seen = ctx.tick", ScriptType.HOOK,
    )

    transfer(session, a, b, Decimal("10"), "x")

    assert hook.state["seen"] == 0


def test_ctx_tick_in_hook_reads_latest_committed(world):
    """A hook fired between ticks reads the latest committed tick. A hook
    fired mid-tick (the op resolves before the current tick commits) reads
    the last-completed tick — the world as it stood when the op applied."""
    session, alice, bob, gov, a, b, g = world
    run_tick(session)          # commit tick 1
    hook = make_script(
        session, "audit", "ctx.state.seen = ctx.tick", ScriptType.HOOK,
    )

    transfer(session, a, b, Decimal("10"), "x")   # direct op, not in a tick

    assert hook.state["seen"] == 1               # tick 1, the latest committed


# --- ctx.accounts order (F1): the "first account" contract ---

def test_ctx_accounts_ordered_currency_then_id(world):
    """std.balance reads "the first account in a currency" and the
    codebase convention is ctx.accounts[1] -- but nothing ordered the
    relationship, so an entity holding two accounts of one currency
    resolved arbitrarily (whatever row the DB returned first). The
    relationship orders (currency, id): currencies grouped, and the
    lowest id -- the account genesis opened -- wins within one."""
    from econengine.lua_engine import LuaEngine
    from econengine.models import Account
    from econengine.tick import _build_script_ctx

    session, alice, *_ = world
    genesis_usd = [a for a in alice.accounts if a.currency == "USD"][0]
    # duplicates inserted AFTER genesis, with ids that sort after any
    # uuid4 the fixture could have minted ("mmm"/"zzz" > hex; "000-" < hex)
    session.add_all([
        Account(entity=alice, currency="USD", id="mmm-second", balance=Decimal("1")),
        Account(entity=alice, currency="EUR", id="000-eur", balance=Decimal("5")),
        Account(entity=alice, currency="USD", id="zzz-third", balance=Decimal("2")),
    ])
    session.flush()

    # the order is the LOAD-time contract: expire the collection so the
    # read goes back to the DB (per-tick, a fresh session does this)
    session.expire(alice, ["accounts"])
    keys = [(a.currency, a.id) for a in alice.accounts]
    assert keys == [("EUR", "000-eur"), ("USD", genesis_usd.id),
                    ("USD", "mmm-second"), ("USD", "zzz-third")]

    script = make_script(session, "bal", "return std.balance('USD')",
                         ScriptType.POLICY, entity=alice)
    ctx = _build_script_ctx(session, alice, script, [], 1)
    assert [a["id"] for a in ctx["accounts"]] == [
        "000-eur", genesis_usd.id, "mmm-second", "zzz-third"]

    engine = LuaEngine()
    result = engine.run("return std.balance('USD')", ctx)
    assert result.error is None, result.error
    assert abs(result.return_value - 1000.0) < 1e-9  # the genesis account
    result = engine.run("return std.balance()", ctx)
    assert result.error is None, result.error
    assert abs(result.return_value - 5.0) < 1e-9      # first row = the EUR
