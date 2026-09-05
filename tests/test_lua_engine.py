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


def test_intent_extra_arguments_are_refused():
    # Lua silently discards extra call arguments; the action surface
    # must not (run 15: invented trailing args queued intents that
    # meant something else, with no error anywhere). The submit gate
    # smoke-runs every script, so this surfaces at submit time --
    # in the author's hands -- not at the entity's next tick.
    src = "ctx.action.transfer('a', 'b', '1', 'ref', 10)"
    result = engine.run(src, _CTX)
    assert result.error and "ctx.action.transfer takes" in result.error

    src = "ctx.action.start_process('GATHER', nil, 20)"
    result = engine.run(src, _CTX)
    assert result.error and "ctx.action.start_process takes (recipe, parcel_id)" in result.error


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


# ===========================================================================
# Library tiers (docs/scripting.md): std injected always, libraries= adds
# read-only namespaces (the per-world `world` lib in production).
# ===========================================================================

_LIB_CTX = {
    "tick": 25,
    "clock": {"tick": 25, "day": 2, "hour": 1, "is_day": False, "is_night": True,
               "daylight_hours": "06:00-19:00"},
    "entity": {"id": "ent-1", "name": "T", "entity_type": "individual"},
    "accounts": [{"id": "acct-1", "currency": "USD", "balance": "100.0000"}],
    "holdings": [{"symbol": "GRAIN", "quantity": "3.5000"}],
    "unlocks": ["FARMING"],
    "needs": [{"code": "FOOD", "satisfaction": 1.0}],
    "processes": [{"recipe": "FARM_GRAIN"}],
    "parcels": [{"id": "p1", "facilities": ["FARM"], "deposits": {"ORE": 5}}],
    "events": [],
    "state": {},
    "queries": {
        "market_price": lambda symbol: "0.75" if symbol == "GRAIN" else None,
        "best_bid": lambda symbol: "0.70" if symbol == "GRAIN" else None,
        "best_ask": lambda symbol: "0.80" if symbol == "GRAIN" else None,
    },
}


def test_stdlib_injected_by_default():
    result = engine.run("return std.holding_qty('GRAIN')", _LIB_CTX)
    assert result.error is None
    assert abs(result.return_value - 3.5) < 1e-9


def test_stdlib_pure_helpers_work():
    checks = {
        "missing_holding": "return std.holding_qty('ORE')",
        "market_price_query": "return std.market_price('GRAIN', 9.9)",
        "market_price_fallback": "return std.market_price('NOPE', 1.25)",
        "best_bid_query": "return std.best_bid('GRAIN', 9.9)",
        "best_ask_query": "return std.best_ask('GRAIN', 9.9)",
        "book_fallback": "return std.best_bid('NOPE', 1.25)",
        "has_unlock": "return std.has_unlock('FARMING')",
        "need_by_code": "return std.need_by_code('FOOD').code",
        "running_recipe": "return std.running_recipe('FARM_GRAIN')",
        "facility_parcel": "return std.facility_parcel('FARM')",
        "deposit_parcel": "return std.deposit_parcel('ORE')",
        "amount_str": "return std.amount_str(1.5)",
        "hour": "return std.hour()",
        "is_night": "return std.is_night()",
        "day": "return std.day()",
    }
    expected = {
        "missing_holding": 0,
        "market_price_query": 0.75,
        "market_price_fallback": 1.25,
        "best_bid_query": 0.70,
        "best_ask_query": 0.80,
        "book_fallback": 1.25,
        "has_unlock": True,
        "need_by_code": "FOOD",
        "running_recipe": True,
        "facility_parcel": "p1",
        "deposit_parcel": "p1",
        "amount_str": "1.5000",
        "hour": 1,
        "is_night": True,
        "day": 2,
    }
    for name, src in checks.items():
        result = engine.run(src, _LIB_CTX)
        assert result.error is None, f"{name}: {result.error}"
        assert result.return_value == expected[name], name


def test_stdlib_sugar_helpers():
    """std.at / std.is_day / std.need_level / std.balance (run 30
    postmortem): the idioms every house rewrote by hand, with the
    hand-rolled versions failing silently."""
    at_ctx = dict(_LIB_CTX, entity={**_LIB_CTX["entity"], "place": "HEARTH"})
    need_ctx = dict(_LIB_CTX,
                    needs=[{"code": "WARMTH", "satisfaction": "0.5000"}])

    def run(src, ctx):
        result = engine.run(src, ctx)
        assert result.error is None, result.error
        return result.return_value

    # std.at: true only at the key you stand on (entity.place is a STRING)
    assert run("return std.at('HEARTH')", at_ctx) is True
    assert run("return std.at('THICKET')", at_ctx) is False
    assert run("return std.at('HEARTH')", _LIB_CTX) is False  # unplaced
    assert run("return std.at(nil)", at_ctx) is False
    # std.is_day mirrors is_night: boolean with a clock, nil without
    assert run("return std.is_day()", _LIB_CTX) is False      # clock: night
    assert run("return std.is_day()", dict(_LIB_CTX, clock=None)) is None
    # std.need_level: the numeric read of a string satisfaction
    assert abs(run("return std.need_level('WARMTH')", need_ctx) - 0.5) < 1e-9
    assert abs(run("return std.need_level('FOOD')", _LIB_CTX) - 1.0) < 1e-9
    assert run("return std.need_level('NOPE')", _LIB_CTX) is None
    # std.balance: first account (or first in a currency) as a number
    assert abs(run("return std.balance()", _LIB_CTX) - 100.0) < 1e-9
    assert abs(run("return std.balance('USD')", _LIB_CTX) - 100.0) < 1e-9
    assert run("return std.balance('EUR')", _LIB_CTX) == 0


def test_stdlib_namespace_is_read_only():
    result = engine.run("std.holding_qty = nil", _LIB_CTX)
    assert result.error is not None
    assert "read-only namespace" in result.error


def test_stdlib_global_shadow_is_local_to_the_run():
    # Assigning the NAME only clobbers that run's view (fresh runtime per
    # run; the engine re-injects next time) -- allowed, unlike member writes.
    result = engine.run("std = {}; return type(std.holding_qty)", _LIB_CTX)
    assert result.error is None
    assert result.return_value == "nil"


def test_libraries_argument_injects_namespaces():
    world_lib = "return { tag = 'ok', doubled = function(x) return x * 2 end }"
    result = engine.run(
        "return world.tag .. ':' .. world.doubled(21)",
        _LIB_CTX, libraries={"world": world_lib},
    )
    assert result.error is None
    assert result.return_value == "ok:42"


def test_libraries_namespaces_are_read_only():
    world_lib = "return { tag = 'ok' }"
    result = engine.run("world.tag = 'nope'", _LIB_CTX, libraries={"world": world_lib})
    assert result.error is not None
    assert "read-only namespace 'world" in result.error


def test_std_name_cannot_be_overridden_via_libraries():
    with pytest.raises(ValueError, match="std"):
        engine.run("return 1", _LIB_CTX, libraries={"std": "return {}"})


def test_library_must_return_a_table():
    result = engine.run("return 1", _LIB_CTX, libraries={"world": "return 42"})
    assert result.error is not None
    assert "did not return a namespace table" in result.error


def test_library_compile_error_surfaces_on_run_result():
    result = engine.run("return 1", _LIB_CTX, libraries={"world": "this is not lua ("})
    assert result.error is not None


def test_sandbox_unchanged_with_libraries():
    world_lib = "return { tag = 'ok' }"
    result = engine.run(
        "return tostring(require) .. '/' .. tostring(world.tag)",
        _LIB_CTX, libraries={"world": world_lib},
    )
    assert result.error is None
    assert result.return_value == "nil/ok"


# ---------------------------------------------------------------------------
# strict_globals: the lint half of the install-time gate (docs/scripting.md
# section 4; Phase 2). Production runs stay permissive-but-loud -- only
# validation paths (the gate, the dry-run endpoint) turn this on.
# ---------------------------------------------------------------------------

def test_strict_rejects_undeclared_global_read():
    # The original zombie-trap, verbatim class: a helper that was never
    # injected. Permissive mode = nil-call at the player's tick; strict
    # mode = loud at validation time.
    result = engine.run("settle_last_orders()", _CTX, strict_globals=True)
    assert result.error is not None
    assert "undeclared global 'settle_last_orders'" in result.error


def test_strict_rejects_undeclared_global_write():
    result = engine.run("fctors = {}", _CTX, strict_globals=True)
    assert result.error is not None
    assert "assignment to undeclared global 'fctors'" in result.error


def test_strict_allows_sandbox_nil_reads():
    # The blacklisted names are pre-declared nil: reading `require` yields
    # nil exactly as production, not a lint error.
    result = engine.run("return tostring(require)", _CTX, strict_globals=True)
    assert result.error is None
    assert result.return_value == "nil"


def test_strict_rejects_reassigning_injected_names():
    result = engine.run("std = {}", _CTX, strict_globals=True)
    assert result.error is not None
    assert "reassigned injected name 'std'" in result.error
    result = engine.run("ctx = 5", _CTX, strict_globals=True)
    assert result.error is not None
    assert "reassigned injected name 'ctx'" in result.error


def test_strict_clean_script_passes_with_all_tiers():
    lib = "local t = {} function t.f() return 7 end return t"
    result = engine.run(
        "ctx.state.v = std.amount_str(world.f())",
        _CTX, libraries={"world": lib}, strict_globals=True,
    )
    assert result.error is None
    assert result.state_updates["v"] == "7.0000"


def test_default_run_stays_permissive():
    result = engine.run("quietly_typo = 1", _CTX)
    assert result.error is None


def test_stdlib_fingerprint_is_stable_identity():
    from econengine.lua_engine import stdlib_fingerprint, stdlib_source
    fp = stdlib_fingerprint()
    assert len(fp) == 16 and int(fp, 16) >= 0
    assert stdlib_fingerprint() == fp
    # The fingerprint tracks the source: the pinned vocabulary IS this text.
    import hashlib
    assert fp == hashlib.sha256(stdlib_source().encode()).hexdigest()[:16]


def test_std_unreserved_smoke():
    # std.unreserved over the synthetic query (None) is nil, not an
    # error: the same nil-safe discipline as std.best_bid.
    src = "ctx.state.x = std.unreserved('LABOR') or 'nil'"
    result = engine.run(src, _CTX)
    assert result.error is None
    assert result.state_updates["x"] == "nil"


def test_std_clock_queries_nil_without_ctx_clock():
    # An engine world with no clock facts (pre-run-18) still runs: the
    # queries return nil, they never raise.
    src = "ctx.state.h = std.hour(); ctx.state.n = std.is_night() " \
          "; ctx.state.d = std.day()"
    bare = dict(_LIB_CTX)
    bare.pop("clock", None)
    result = engine.run("return std.hour()", bare)
    assert result.error is None
    assert result.return_value is None
    result = engine.run("return std.is_night()", bare)
    assert result.error is None
    assert result.return_value is None
