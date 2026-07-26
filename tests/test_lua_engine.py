import pytest
from econengine.lua_engine import LuaEngine, Intent

engine = LuaEngine()

_CTX = {
    "entity": {"id": "ent-1", "name": "Central Bank", "entity_type": "bank", "is_monetary_authority": True},
    "accounts": [{"id": "acct-1", "currency": "USD", "balance": "10000.0000"}],
    "events": [],
    "state": {"counter": 0},
}


def test_valid_script_returns_no_error():
    result = engine.run("-- no-op", _CTX)
    assert result.error is None


def test_script_queues_transfer_intent():
    src = """
ctx.action.transfer("acct-1", "acct-2", "100", "payment")
"""
    result = engine.run(src, _CTX)
    assert result.error is None
    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.intent_type == "transfer"
    assert intent.params["amount"] == "100"
    assert set(intent.resource_ids) == {"acct-1", "acct-2"}


def test_script_queues_issue_money_intent():
    src = """
ctx.action.issue_money("acct-1", "500", "QE")
"""
    result = engine.run(src, _CTX)
    assert result.error is None
    assert len(result.intents) == 1
    assert result.intents[0].intent_type == "issue_money"


def test_script_queues_retire_money_intent():
    src = """
ctx.action.retire_money("acct-1", "200", "drain")
"""
    result = engine.run(src, _CTX)
    assert result.error is None
    assert result.intents[0].intent_type == "retire_money"


def test_intent_captures_entity_id():
    src = "ctx.action.issue_money('acct-1', '1', 'test')"
    result = engine.run(src, _CTX)
    assert result.intents[0].entity_id == "ent-1"


def test_intent_default_priority():
    src = "ctx.action.transfer('a', 'b', '1', 'ref')"
    result = engine.run(src, _CTX)
    assert result.intents[0].priority == 100


def test_intent_custom_priority():
    src = "ctx.action.transfer('a', 'b', '1', 'ref', 10)"
    result = engine.run(src, _CTX)
    assert result.intents[0].priority == 10


def test_state_mutations_returned():
    src = """
ctx.state.counter = 42
ctx.state.new_key = "hello"
"""
    result = engine.run(src, _CTX)
    assert result.error is None
    assert result.state_updates.get("counter") == 42
    assert result.state_updates.get("new_key") == "hello"


def test_query_stubs_return_none():
    src = """
local bal = ctx.query.balance("acct-1")
local sup = ctx.query.total_supply("USD")
local prc = ctx.query.market_price("WHEAT")
-- no error expected even though they return nil
"""
    result = engine.run(src, _CTX)
    assert result.error is None


def test_syntax_error_reported():
    result = engine.run("this is not valid lua !!!", _CTX)
    assert result.error is not None
    assert result.intents == []


def test_runtime_error_reported():
    result = engine.run("error('something went wrong')", _CTX)
    assert result.error is not None


def test_sandbox_blocks_io():
    result = engine.run("io.open('somefile', 'r')", _CTX)
    assert result.error is not None


def test_sandbox_blocks_os():
    result = engine.run("os.execute('ls')", _CTX)
    assert result.error is not None


def test_sandbox_blocks_require():
    result = engine.run("require('os')", _CTX)
    assert result.error is not None


def test_timeout_kills_infinite_loop():
    src = "while true do end"
    result = engine.run(src, _CTX, timeout_ms=100)
    assert result.error is not None
    assert "timed out" in result.error
    assert result.intents == []


def test_timeout_actually_stops_the_vm():
    # The hook must abort execution shortly after the deadline — run() should
    # not need the watchdog grace period, and no thread should be left running.
    import time
    import threading

    before = threading.active_count()
    start = time.monotonic()
    result = engine.run("while true do end", _CTX, timeout_ms=100)
    elapsed = time.monotonic() - start

    assert "timed out" in result.error
    assert elapsed < 0.5
    time.sleep(0.1)
    assert threading.active_count() <= before


def test_timeout_cannot_be_swallowed_by_pcall():
    src = """
while true do
    pcall(function() while true do end end)
end
"""
    result = engine.run(src, _CTX, timeout_ms=100)
    assert result.error is not None
    assert "timed out" in result.error


def test_memory_bomb_is_capped():
    src = """
local s = "xxxxxxxxxxxxxxxx"
while true do s = s .. s end
"""
    result = engine.run(src, _CTX, timeout_ms=5000)
    assert result.error is not None
    assert "timed out" not in result.error


def test_elapsed_ms_recorded_on_success():
    result = engine.run("-- no-op", _CTX)
    assert result.error is None
    assert result.elapsed_ms >= 0
    assert result.elapsed_ms < 500


def test_elapsed_ms_recorded_on_timeout():
    result = engine.run("while true do end", _CTX, timeout_ms=100)
    assert result.error is not None
    assert result.elapsed_ms > 0


def test_intents_discarded_on_timeout():
    src = """
ctx.action.issue_money("acct-1", "999", "before loop")
while true do end
"""
    result = engine.run(src, _CTX, timeout_ms=100)
    assert result.error is not None
    assert result.intents == []


def test_multiple_intents_collected():
    src = """
ctx.action.transfer("acct-1", "acct-2", "10", "p1")
ctx.action.transfer("acct-1", "acct-3", "20", "p2")
ctx.action.issue_money("acct-1", "100", "issue")
"""
    result = engine.run(src, _CTX)
    assert result.error is None
    assert len(result.intents) == 3
