import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.models import Base, EntityType, Script, ScriptType, WorldSetting
from econengine.scripting import (
    OperationVetoedError, PRIVATE_HOLDINGS_KEY,
)
from econengine.markets import adjust_holding
from econengine.services import create_account, create_entity, deposit, transfer
from econengine.tick import run_tick


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


# ---------------------------------------------------------------------------
# ctx.query.holders — the share register (see build_queries)
# ---------------------------------------------------------------------------

def test_holders_lists_entities_with_positive_quantity(world):
    session, alice, bob, gov, a, b, g = world
    adjust_holding(session, alice, "SHARE-ACME", Decimal("30"))
    adjust_holding(session, bob, "SHARE-ACME", Decimal("70"))

    script = make_script(
        session, "reader",
        """
        local hs = ctx.query.holders('SHARE-ACME')
        local total = 0
        for _, h in ipairs(hs) do total = total + tonumber(h.quantity) end
        ctx.state.n = #hs
        ctx.state.total = total
        ctx.state.has_account = hs[1].account_id ~= nil
        """,
        ScriptType.BEHAVIOUR, entity=gov,
    )
    run_tick(session)

    # ipairs/# work, so the result really is a Lua table and not an opaque
    # Python object — the thing _wrap_result exists to guarantee.
    assert script.state["n"] == 2
    assert script.state["total"] == 100
    assert script.state["has_account"] is True


def test_holders_excludes_zero_and_unknown_symbols(world):
    session, alice, bob, gov, a, b, g = world
    adjust_holding(session, alice, "SHARE-ACME", Decimal("5"))
    adjust_holding(session, bob, "SHARE-ACME", Decimal("5"))
    adjust_holding(session, bob, "SHARE-ACME", Decimal("-5"))  # back to zero

    script = make_script(
        session, "reader",
        """
        ctx.state.n = #ctx.query.holders('SHARE-ACME')
        ctx.state.none = #ctx.query.holders('SHARE-NOSUCH')
        """,
        ScriptType.BEHAVIOUR, entity=gov,
    )
    run_tick(session)

    assert script.state["n"] == 1     # Bob is at zero, so not a holder
    assert script.state["none"] == 0  # unknown symbol is empty, not an error


def test_holders_is_deterministically_ordered(world):
    session, alice, bob, gov, a, b, g = world
    adjust_holding(session, alice, "SHARE-ACME", Decimal("1"))
    adjust_holding(session, bob, "SHARE-ACME", Decimal("1"))

    script = make_script(
        session, "reader",
        """
        local ids = {}
        for _, h in ipairs(ctx.query.holders('SHARE-ACME')) do
          ids[#ids + 1] = h.entity_id
        end
        ctx.state.ids = table.concat(ids, ',')
        """,
        ScriptType.BEHAVIOUR, entity=gov,
    )
    run_tick(session)

    expected = ",".join(sorted([alice.id, bob.id]))
    assert script.state["ids"] == expected


def test_holders_supports_paying_a_dividend(world):
    """The actual use: an issuer pays its register pro rata, in one tick."""
    session, alice, bob, gov, a, b, g = world
    adjust_holding(session, alice, "SHARE-ACME", Decimal("25"))
    adjust_holding(session, bob, "SHARE-ACME", Decimal("75"))
    firm = create_entity(session, "Acme", EntityType.BUSINESS)
    f = create_account(session, firm, "USD", initial_balance=Decimal("400"))

    make_script(
        session, "dividend",
        f"""
        local hs = ctx.query.holders('SHARE-ACME')
        local total = 0
        for _, h in ipairs(hs) do total = total + tonumber(h.quantity) end
        for _, h in ipairs(hs) do
          local share = 400 * tonumber(h.quantity) / total
          ctx.action.transfer('{f.id}', h.account_id, string.format('%.4f', share), 'dividend')
        end
        """,
        ScriptType.BEHAVIOUR, entity=firm,
    )
    run_tick(session)

    session.refresh(a); session.refresh(b); session.refresh(f)
    assert a.balance == Decimal("1100.0000")  # 1000 + 25% of 400
    assert b.balance == Decimal("1300.0000")  # 1000 + 75% of 400
    assert f.balance == Decimal("0.0000")


# --- rival privacy (world.private_holdings) ---------------------------------

def test_private_flag_blinds_queries_to_rivals(world):
    """With the flag set, an entity's behaviour sees its own holdings
    and NOTHING else: holding(rival) is nil, holders() is empty."""
    session, alice, bob, gov, a, b, g = world
    adjust_holding(session, alice, "SHARE-ACME", Decimal("5"))
    adjust_holding(session, bob, "SHARE-ACME", Decimal("7"))
    session.add(WorldSetting(key=PRIVATE_HOLDINGS_KEY, value={"enabled": True}))

    script = make_script(
        session, "nosy",
        f"""
        ctx.state.mine = ctx.query.holding(ctx.entity.id, 'SHARE-ACME') or 'none'
        ctx.state.theirs = ctx.query.holding('{bob.id}', 'SHARE-ACME') or 'none'
        ctx.state.register = #ctx.query.holders('SHARE-ACME')
        """,
        ScriptType.BEHAVIOUR, entity=alice,
    )
    run_tick(session)

    assert script.state == {"mine": "5.0000", "theirs": "none", "register": 0}


def test_private_flag_leaves_the_referee_sighted(world):
    """Op-context scripts (validators, hooks — _op_ctx) call
    build_queries unscoped: the flag blinds entity behaviours, not the
    referee. Probed at the seam, without a tick."""
    from econengine.scripting import build_queries

    session, alice, bob, gov, a, b, g = world
    adjust_holding(session, alice, "SHARE-ACME", Decimal("5"))
    adjust_holding(session, bob, "SHARE-ACME", Decimal("7"))
    session.add(WorldSetting(key=PRIVATE_HOLDINGS_KEY, value={"enabled": True}))

    referee = build_queries(session)          # the _op_ctx form: no owner
    assert referee["holding"](bob.id, "SHARE-ACME") == "7.0000"
    assert len(referee["holders"]("SHARE-ACME")) == 2

    scoped = build_queries(session, owner_id=alice.id)   # _entity_ctx form
    assert scoped["holding"](bob.id, "SHARE-ACME") is None
    assert scoped["holding"](alice.id, "SHARE-ACME") == "5.0000"
    assert scoped["holders"]("SHARE-ACME") == []


def test_unset_flag_keeps_queries_global(world):
    """The default world stays public: the flag is opt-in per pack."""
    session, alice, bob, gov, a, b, g = world
    adjust_holding(session, bob, "SHARE-ACME", Decimal("7"))

    script = make_script(
        session, "nosy",
        f"""
        ctx.state.theirs = ctx.query.holding('{bob.id}', 'SHARE-ACME') or 'none'
        ctx.state.register = #ctx.query.holders('SHARE-ACME')
        """,
        ScriptType.BEHAVIOUR, entity=alice,
    )
    run_tick(session)

    assert script.state == {"theirs": "7.0000", "register": 1}


def test_unreserved_query_exposes_the_spendable_side(world):
    """Run 15: 144 'insufficient unreserved LABOR' refusals against a
    holdings read that looked fine -- nothing showed held-minus-reserved.
    ctx.query.unreserved (std.unreserved over it) is that surface."""
    from econengine.scripting import build_queries
    from econengine.production import create_recipe, start_process
    session, alice, bob, gov, a, b, g = world
    adjust_holding(session, alice, "OVEN", Decimal("1"))
    adjust_holding(session, alice, "FLOUR", Decimal("10"))
    create_recipe(session, "BAKE", inputs={"FLOUR": Decimal("1")},
                  outputs={"BREAD": Decimal("2")}, duration_ticks=2,
                  good_requirements={"OVEN": Decimal("0.5")})
    queries = build_queries(session, owner_id=alice.id)
    assert queries["unreserved"](alice.id, "OVEN") == "1.0000"
    start_process(session, alice, "BAKE")  # RUNNING: reserves half the oven
    assert queries["holding"](alice.id, "OVEN") == "1.0000"       # the pantry
    assert queries["unreserved"](alice.id, "OVEN") == "0.5000"  # spendable
