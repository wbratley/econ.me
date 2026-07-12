import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econ.models import Base, EntityType, Script, ScriptType
from econ.scripting import OperationVetoedError
from econ.services import create_account, create_entity, deposit, transfer
from econ.tick import run_tick


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


LIMIT_100 = """
if tonumber(ctx.op.amount) > 100 then
    return {allow=false, reason="amount over limit"}
end
"""


# --- validators ---

def test_validator_vetoes_over_limit(world):
    session, alice, bob, gov, a, b, g = world
    make_script(session, "limit", LIMIT_100, ScriptType.VALIDATOR)

    with pytest.raises(OperationVetoedError, match="amount over limit"):
        transfer(session, a, b, Decimal("500"), "big")
    assert a.balance == Decimal("1000")


def test_validator_allows_under_limit(world):
    session, alice, bob, gov, a, b, g = world
    make_script(session, "limit", LIMIT_100, ScriptType.VALIDATOR)

    transfer(session, a, b, Decimal("50"), "small")
    assert a.balance == Decimal("950")


def test_validator_no_return_allows(world):
    session, alice, bob, gov, a, b, g = world
    make_script(session, "noop", "-- observes, never objects", ScriptType.VALIDATOR)

    transfer(session, a, b, Decimal("500"), "fine")
    assert b.balance == Decimal("1500")


def test_validator_bare_false_denies(world):
    session, alice, bob, gov, a, b, g = world
    make_script(session, "freeze", "return false", ScriptType.VALIDATOR)

    with pytest.raises(OperationVetoedError):
        deposit(session, a, Decimal("1"), "x")


def test_validator_error_fails_closed(world):
    session, alice, bob, gov, a, b, g = world
    make_script(session, "broken", "error('bug in validator')", ScriptType.VALIDATOR)

    with pytest.raises(OperationVetoedError, match="failed"):
        transfer(session, a, b, Decimal("1"), "x")


def test_inactive_validator_ignored(world):
    session, alice, bob, gov, a, b, g = world
    make_script(session, "off", "return false", ScriptType.VALIDATOR, is_active=False)

    transfer(session, a, b, Decimal("10"), "x")
    assert b.balance == Decimal("1010")


def test_entity_scoped_validator(world):
    session, alice, bob, gov, a, b, g = world
    make_script(session, "alice-frozen", "return false", ScriptType.VALIDATOR, entity=alice)

    with pytest.raises(OperationVetoedError):
        transfer(session, a, b, Decimal("10"), "x")
    # Bob is not covered by Alice's validator
    transfer(session, b, a, Decimal("10"), "x")
    assert a.balance == Decimal("1010")


def test_validator_side_effects_ignored(world):
    session, alice, bob, gov, a, b, g = world
    script = make_script(
        session, "sneaky",
        f"""
ctx.action.transfer('{a.id}', '{b.id}', '999', 'smuggled')
ctx.state.ran = true
""",
        ScriptType.VALIDATOR,
    )

    transfer(session, a, b, Decimal("10"), "x")

    assert a.balance == Decimal("990")   # only the real transfer happened
    assert script.state == {}            # validator state never persisted


def test_validator_gates_tick_intents(world):
    session, alice, bob, gov, a, b, g = world
    make_script(session, "limit", LIMIT_100, ScriptType.VALIDATOR)
    make_script(
        session, "greedy",
        f"ctx.action.transfer('{a.id}', '{b.id}', '500', 'big')",
        ScriptType.BEHAVIOUR, entity=alice,
    )

    tick = run_tick(session)

    assert tick.events[0]["status"] == "rejected"
    assert "vetoed" in tick.events[0]["reason"]
    assert a.balance == Decimal("1000")


# --- hooks ---

def test_hook_fires_after_transfer(world):
    session, alice, bob, gov, a, b, g = world
    script = make_script(
        session, "audit",
        """
ctx.state.count = (ctx.state.count or 0) + 1
ctx.state.last_type = ctx.op.type
ctx.state.last_amount = ctx.op.amount
""",
        ScriptType.HOOK,
    )

    transfer(session, a, b, Decimal("25"), "one")
    transfer(session, a, b, Decimal("30"), "two")

    assert script.state["count"] == 2
    assert script.state["last_type"] == "transfer"
    assert script.state["last_amount"] == "30"


def test_hook_receives_transaction_ids(world):
    session, alice, bob, gov, a, b, g = world
    script = make_script(
        session, "tx-ids", "ctx.state.n_txs = #ctx.op.transaction_ids", ScriptType.HOOK,
    )

    transfer(session, a, b, Decimal("5"), "x")
    assert script.state["n_txs"] == 2  # debit + credit


def test_hook_tax_collects_without_recursion(world):
    session, alice, bob, gov, a, b, g = world
    make_script(
        session, "tax",
        f"""
if ctx.op.type == 'transfer' then
    ctx.action.transfer(ctx.op.from_account_id, '{g.id}', tostring(tonumber(ctx.op.amount) * 0.1), 'tax')
end
""",
        ScriptType.HOOK,
    )

    transfer(session, a, b, Decimal("100"), "purchase")

    assert b.balance == Decimal("1100")
    assert g.balance == Decimal("10")            # 10% of 100, exactly once
    assert a.balance == Decimal("890")           # no tax-on-tax recursion


def test_hook_intents_bypass_validators(world):
    session, alice, bob, gov, a, b, g = world
    # A validator that would veto the tax transfer if it were re-dispatched
    make_script(session, "no-tax", "if ctx.op.reference == 'tax' then return false end", ScriptType.VALIDATOR)
    make_script(
        session, "tax",
        f"ctx.action.transfer(ctx.op.from_account_id, '{g.id}', '10', 'tax')",
        ScriptType.HOOK,
    )

    transfer(session, a, b, Decimal("100"), "purchase")
    assert g.balance == Decimal("10")


def test_hook_error_does_not_fail_operation(world):
    session, alice, bob, gov, a, b, g = world
    make_script(session, "broken", "error('hook bug')", ScriptType.HOOK)

    transfer(session, a, b, Decimal("10"), "x")
    assert b.balance == Decimal("1010")


def test_entity_scoped_hook(world):
    session, alice, bob, gov, a, b, g = world
    script = make_script(
        session, "alice-only", "ctx.state.count = (ctx.state.count or 0) + 1",
        ScriptType.HOOK, entity=alice,
    )

    transfer(session, a, b, Decimal("10"), "by alice")
    transfer(session, b, a, Decimal("10"), "by bob")

    assert script.state["count"] == 1


# --- policies ---

def test_policy_runs_each_tick(world):
    session, alice, bob, gov, a, b, g = world
    make_script(
        session, "print-money",
        f"ctx.action.issue_money('{g.id}', '100', 'stimulus')",
        ScriptType.POLICY, entity=gov,
    )

    run_tick(session)
    run_tick(session)

    assert g.balance == Decimal("200")


def test_policy_sees_all_events(world):
    session, alice, bob, gov, a, b, g = world
    make_script(
        session, "alice-pays",
        f"ctx.action.transfer('{a.id}', '{b.id}', '10', 'x')",
        ScriptType.BEHAVIOUR, entity=alice,
    )
    policy = make_script(
        session, "observer", "ctx.state.seen = #ctx.events", ScriptType.POLICY, entity=gov,
    )

    run_tick(session)
    run_tick(session)

    # Alice's transfer event is visible to the policy despite belonging to Alice
    assert policy.state["seen"] == 1


def test_policy_intents_resolve_before_behaviour_on_tie(world):
    session, alice, bob, gov, a, b, g = world
    make_script(
        session, "behaviour",
        f"ctx.action.transfer('{a.id}', '{b.id}', '10', 'behaviour')",
        ScriptType.BEHAVIOUR, entity=alice,
    )
    make_script(
        session, "policy",
        f"ctx.action.issue_money('{g.id}', '10', 'policy')",
        ScriptType.POLICY, entity=gov,
    )

    tick = run_tick(session)

    refs = [e["params"]["reference"] for e in tick.events]
    assert refs == ["policy", "behaviour"]
