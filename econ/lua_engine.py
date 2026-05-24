"""
Sandboxed Lua execution engine.

Each call to LuaEngine.run() gets a fresh LuaRuntime — no shared state between executions.
Scripts interact with the simulation via a `ctx` object injected as a Lua global:

  ctx.entity        read-only entity info
  ctx.accounts      read-only account list
  ctx.events        outcomes from the previous tick
  ctx.state         persistent dict; mutations are returned to the caller
  ctx.query.*       read-only economy queries (stubbed until Step 3)
  ctx.action.*      queue an intent for Python to resolve after all scripts run

Scripts must define an entry-point function matching their ScriptType:
  BEHAVIOUR  →  on_tick(ctx)
  HOOK       →  on_hook(ctx)
  VALIDATOR  →  on_validate(ctx)  -- must return {allow=bool, reason=string}
  POLICY     →  on_policy(ctx)
"""

import threading
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


class LuaEngine:
    def run(self, source: str, ctx: dict, timeout_ms: int = 100) -> RunResult:
        """
        Execute Lua source with the given ctx dict. Returns a RunResult.
        Each call gets a fresh LuaRuntime. If execution exceeds timeout_ms
        the thread is abandoned and RunResult.error is set.
        """
        try:
            from lupa import LuaRuntime
        except ImportError as exc:
            return RunResult(intents=[], state_updates={}, error=f"lupa not installed: {exc}")

        intents: list[Intent] = []
        result: dict = {"state_updates": {}, "error": None}

        def _execute() -> None:
            try:
                lua = LuaRuntime(unpack_returned_tuples=True)
                _apply_sandbox(lua)

                entity_id = ctx.get("entity", {}).get("id", "")
                state_tbl = _build_ctx(lua, ctx, entity_id, intents)

                lua.execute(source)

                # Capture any mutations the script made to ctx.state
                result["state_updates"] = _read_lua_table(state_tbl)
            except Exception as exc:
                result["error"] = str(exc)

        thread = threading.Thread(target=_execute, daemon=True)
        thread.start()
        thread.join(timeout=timeout_ms / 1000.0)

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


def _build_ctx(lua, ctx: dict, entity_id: str, intents: list):
    """
    Build the ctx Lua table injected into each script.
    Returns the state_tbl so the caller can read mutations after execution.
    """
    def _to_lua_table(d: dict):
        t = lua.table()
        for k, v in d.items():
            t[k] = v
        return t

    def _to_lua_list(lst: list):
        t = lua.table()
        for i, item in enumerate(lst, start=1):
            t[i] = _to_lua_table(item) if isinstance(item, dict) else item
        return t

    entity_tbl  = _to_lua_table(ctx.get("entity", {}))
    accounts_tbl = _to_lua_list(ctx.get("accounts", []))
    events_tbl  = _to_lua_list(ctx.get("events", []))
    state_tbl   = _to_lua_table(ctx.get("state", {}))

    # --- query stubs (return nil until Step 3) ---
    query_tbl = lua.table()
    query_tbl["balance"]       = lambda account_id: None
    query_tbl["total_supply"]  = lambda currency: None
    query_tbl["market_price"]  = lambda symbol: None

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
            result[k] = tbl[k]
    except Exception:
        pass
    return result
