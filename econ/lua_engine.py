"""
Sandboxed Lua execution engine.

Each call to LuaEngine.run() gets a fresh LuaRuntime — no shared state between executions.
Scripts interact with the simulation via a `ctx` object injected as a Lua global:

  ctx.entity        read-only entity info
  ctx.accounts      read-only account list
  ctx.events        outcomes from the previous tick
  ctx.state         persistent dict; mutations are returned to the caller
  ctx.query.*       read-only economy queries — backed by callables the caller
                    passes in ctx["queries"]; missing ones return nil
  ctx.action.*      queue an intent for Python to resolve after all scripts run

Scripts must define an entry-point function matching their ScriptType:
  BEHAVIOUR  →  on_tick(ctx)
  HOOK       →  on_hook(ctx)
  VALIDATOR  →  on_validate(ctx)  -- must return {allow=bool, reason=string}
  POLICY     →  on_policy(ctx)
"""

import threading
import time
import uuid
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Intent — the unit of work a script submits to the Python resolver
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    entity_id: str
    intent_type: str    # "transfer", "issue_money", "retire_money", ...
    params: dict
    resource_ids: list  # account/resource IDs touched — used to build dependency graph
    priority: int = 100 # lower = higher priority
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# RunResult — what a single script execution returns to the caller
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    intents: list       # list[Intent]
    state_updates: dict # mutations the script made to ctx.state
    error: str | None   # set if the script raised an error or timed out


# ---------------------------------------------------------------------------
# LuaEngine
# ---------------------------------------------------------------------------

_SANDBOX_BLACKLIST = [
    "io", "os", "require", "dofile", "loadfile", "load",
    "rawget", "rawset", "rawequal", "rawlen",
    "package", "debug", "coroutine",
    "collectgarbage", "getmetatable", "setmetatable",
    "newproxy",
]

_MAX_MEMORY_BYTES = 16 * 1024 * 1024

_TIMEOUT_SENTINEL = "__script_timeout__"

# Installed before the sandbox nils out `debug`, so the script can never
# reach sethook. The hook checks the deadline every 1000 VM instructions;
# once exceeded it re-arms to fire on *every* instruction, so even a script
# that wraps its loop in pcall cannot make progress — each unwind lands on
# an instruction that immediately raises again until the chunk exits.
_HOOK_INSTALLER = """
function(deadline_exceeded)
  local sethook = debug.sethook
  sethook(function()
    if deadline_exceeded() then
      sethook(function() error("%s", 2) end, "", 1)
      error("%s", 2)
    end
  end, "", 1000)
end
""" % (_TIMEOUT_SENTINEL, _TIMEOUT_SENTINEL)


class LuaEngine:
    def run(self, source: str, ctx: dict, timeout_ms: int = 100) -> RunResult:
        """
        Execute Lua source with the given ctx dict. Returns a RunResult.
        Each call gets a fresh LuaRuntime capped at _MAX_MEMORY_BYTES.
        A debug hook aborts execution inside the VM once timeout_ms elapses
        (pcall cannot swallow it); RunResult.error is set on timeout.
        """
        try:
            from lupa import LuaRuntime
        except ImportError as exc:
            return RunResult(intents=[], state_updates={}, error=f"lupa not installed: {exc}")

        intents: list[Intent] = []
        result: dict = {"state_updates": {}, "error": None}

        # Query callables may hold a DB session owned by the caller's thread.
        # They go inert once run() returns, so a script abandoned by the
        # watchdog can never touch the session afterwards.
        finished = {"flag": False}
        queries = {
            name: _wrap_query(fn, finished)
            for name, fn in (ctx.get("queries") or {}).items()
        }

        def _execute() -> None:
            try:
                lua = LuaRuntime(unpack_returned_tuples=True, max_memory=_MAX_MEMORY_BYTES)

                deadline = time.monotonic() + timeout_ms / 1000.0
                install_hook = lua.eval(_HOOK_INSTALLER)
                install_hook(lambda: time.monotonic() > deadline)

                _apply_sandbox(lua)

                entity_id = ctx.get("entity", {}).get("id", "")
                state_tbl = _build_ctx(lua, ctx, entity_id, intents, queries)

                lua.execute(source)

                # Capture any mutations the script made to ctx.state
                result["state_updates"] = _read_lua_table(state_tbl)
            except Exception as exc:
                if _TIMEOUT_SENTINEL in str(exc):
                    result["error"] = f"script timed out after {timeout_ms}ms"
                else:
                    # str() can be empty (e.g. LuaMemoryError) — fall back to
                    # the exception type so the error check stays truthy.
                    result["error"] = str(exc) or type(exc).__name__

        thread = threading.Thread(target=_execute, daemon=True)
        thread.start()
        # The debug hook kills the script at the deadline from inside the VM.
        # The join grace only covers time spent in C calls, where hooks don't
        # fire; if we hit it the thread is abandoned as a last resort.
        thread.join(timeout=timeout_ms / 1000.0 + 1.0)
        finished["flag"] = True

        if thread.is_alive():
            intents.clear()
            return RunResult(intents=[], state_updates={}, error=f"script timed out after {timeout_ms}ms")

        if result["error"]:
            return RunResult(intents=[], state_updates={}, error=result["error"])

        return RunResult(intents=list(intents), state_updates=result["state_updates"], error=None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _apply_sandbox(lua) -> None:
    g = lua.globals()
    for name in _SANDBOX_BLACKLIST:
        setattr(g, name, None)


def _wrap_query(fn, finished: dict):
    def wrapper(*args):
        if finished["flag"]:
            return None
        try:
            return fn(*args)
        except Exception:
            return None
    return wrapper


def _build_ctx(lua, ctx: dict, entity_id: str, intents: list, queries: dict):
    """
    Build the ctx Lua table injected into each script.
    Returns the state_tbl so the caller can read mutations after execution.
    """
    def _to_lua_table(d: dict):
        t = lua.table()
        for k, v in d.items():
            if isinstance(v, dict):
                t[k] = _to_lua_table(v)
            elif isinstance(v, list):
                t[k] = _to_lua_list(v)
            else:
                t[k] = v
        return t

    def _to_lua_list(lst: list):
        t = lua.table()
        for i, item in enumerate(lst, start=1):
            if isinstance(item, dict):
                t[i] = _to_lua_table(item)
            elif isinstance(item, list):
                t[i] = _to_lua_list(item)
            else:
                t[i] = item
        return t

    entity_tbl  = _to_lua_table(ctx.get("entity", {}))
    accounts_tbl = _to_lua_list(ctx.get("accounts", []))
    events_tbl  = _to_lua_list(ctx.get("events", []))
    state_tbl   = _to_lua_table(ctx.get("state", {}))

    # --- queries (caller-provided callables; missing ones return nil) ---
    query_tbl = lua.table()
    for name in ("balance", "total_supply", "market_price"):
        query_tbl[name] = queries.get(name) or (lambda *args: None)
    for name, fn in queries.items():
        query_tbl[name] = fn

    # --- action stubs (collect intents) ---
    action_tbl = lua.table()

    def _transfer(from_id, to_id, amount, ref, priority=100):
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="transfer",
            params={"from_account_id": str(from_id), "to_account_id": str(to_id),
                    "amount": str(amount), "reference": str(ref)},
            resource_ids=[str(from_id), str(to_id)],
            priority=int(priority),
        ))

    def _issue_money(account_id, amount, ref, priority=100):
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="issue_money",
            params={"account_id": str(account_id), "amount": str(amount), "reference": str(ref)},
            resource_ids=[str(account_id)],
            priority=int(priority),
        ))

    def _retire_money(account_id, amount, ref, priority=100):
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="retire_money",
            params={"account_id": str(account_id), "amount": str(amount), "reference": str(ref)},
            resource_ids=[str(account_id)],
            priority=int(priority),
        ))

    action_tbl["transfer"]    = _transfer
    action_tbl["issue_money"] = _issue_money
    action_tbl["retire_money"] = _retire_money

    ctx_tbl = lua.table()
    ctx_tbl["entity"]   = entity_tbl
    ctx_tbl["accounts"] = accounts_tbl
    ctx_tbl["events"]   = events_tbl
    ctx_tbl["state"]    = state_tbl
    ctx_tbl["query"]    = query_tbl
    ctx_tbl["action"]   = action_tbl

    lua.globals().ctx = ctx_tbl

    return state_tbl  # returned so caller can read mutations after execution


def _read_lua_table(tbl) -> dict:
    result = {}
    try:
        for k in tbl:
            result[k] = _lua_to_python(tbl[k])
    except Exception:
        pass
    return result


def _lua_to_python(value):
    """Recursively convert Lua tables so state stays JSON-serializable."""
    try:
        from lupa import lua_type
    except ImportError:
        return value
    if lua_type(value) != "table":
        return value
    keys = list(value.keys())
    if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [_lua_to_python(value[k]) for k in sorted(keys)]
    return {k: _lua_to_python(value[k]) for k in keys}
