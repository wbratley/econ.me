"""
Sandboxed Lua execution engine.

Each call to LuaEngine.run() gets a fresh LuaRuntime — no shared state between executions.
Scripts interact with the simulation via a `ctx` object injected as a Lua global:

  ctx.entity        read-only entity info
  ctx.accounts      read-only account list
  ctx.holdings      read-only commodity holdings list
  ctx.processes     read-only running-production list
  ctx.needs         read-only need list with current satisfaction scores
  ctx.unlocks       read-only technology codes the entity can use (own + world)
  ctx.events        outcomes from the previous tick
  ctx.state         persistent dict; mutations are returned to the caller
  ctx.op            the service operation being validated/hooked (HOOK and
                    VALIDATOR runs only)
  ctx.query.*       read-only economy queries — backed by callables the caller
                    passes in ctx["queries"]; missing ones return nil
  ctx.action.*      queue an intent for Python to resolve after all scripts run

Scripts run as a top-level chunk; the chunk's return value is captured in
RunResult.return_value. VALIDATOR scripts use it for their verdict:
  return {allow=false, reason="too large"}   -- deny
  return false                               -- deny
  return {allow=true}  /  no return          -- allow
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
    return_value: object = None  # the chunk's return value (validator verdicts)
    elapsed_ms: float = 0.0  # wall-clock cost of this call, for compute budgets


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
        result: dict = {"state_updates": {}, "error": None, "return_value": None}

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

                result["return_value"] = _lua_to_python(lua.execute(source))

                # Capture any mutations the script made to ctx.state
                result["state_updates"] = _read_lua_table(state_tbl)
            except Exception as exc:
                if _TIMEOUT_SENTINEL in str(exc):
                    result["error"] = f"script timed out after {timeout_ms}ms"
                else:
                    # str() can be empty (e.g. LuaMemoryError) — fall back to
                    # the exception type so the error check stays truthy.
                    result["error"] = str(exc) or type(exc).__name__

        call_start = time.monotonic()
        thread = threading.Thread(target=_execute, daemon=True)
        thread.start()
        # The debug hook kills the script at the deadline from inside the VM.
        # The join grace only covers time spent in C calls, where hooks don't
        # fire; if we hit it the thread is abandoned as a last resort.
        thread.join(timeout=timeout_ms / 1000.0 + 1.0)
        finished["flag"] = True
        elapsed_ms = (time.monotonic() - call_start) * 1000

        if thread.is_alive():
            intents.clear()
            return RunResult(intents=[], state_updates={}, error=f"script timed out after {timeout_ms}ms", elapsed_ms=elapsed_ms)

        if result["error"]:
            return RunResult(intents=[], state_updates={}, error=result["error"], elapsed_ms=elapsed_ms)

        return RunResult(
            intents=list(intents),
            state_updates=result["state_updates"],
            error=None,
            return_value=result["return_value"],
            elapsed_ms=elapsed_ms,
        )


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
    holdings_tbl = _to_lua_list(ctx.get("holdings", []))
    processes_tbl = _to_lua_list(ctx.get("processes", []))
    parcels_tbl = _to_lua_list(ctx.get("parcels", []))
    needs_tbl   = _to_lua_list(ctx.get("needs", []))
    unlocks_tbl = _to_lua_list(ctx.get("unlocks", []))
    events_tbl  = _to_lua_list(ctx.get("events", []))
    state_tbl   = _to_lua_table(ctx.get("state", {}))

    # --- queries (caller-provided callables; missing ones return nil) ---
    #
    # Results are converted on the way out: a query returning a Python list or
    # dict would otherwise arrive in Lua as an opaque Python object, where `#`
    # and ipairs() do not work and indexing is 0-based — a trap for any script
    # author, and the reason scalar-only was the de facto rule here. Converting
    # lets a query return a row set (ctx.query.holders) and have it behave like
    # any other Lua table.
    def _wrap_result(fn):
        def wrapper(*args):
            value = fn(*args)
            if isinstance(value, dict):
                return _to_lua_table(value)
            if isinstance(value, list):
                return _to_lua_list(value)
            return value
        return wrapper

    query_tbl = lua.table()
    for name in ("balance", "total_supply", "market_price", "holding"):
        query_tbl[name] = _wrap_result(queries[name]) if name in queries else (lambda *a: None)
    for name, fn in queries.items():
        query_tbl[name] = _wrap_result(fn)

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

    def _place_order(symbol, side, quantity, limit_price, account_id, priority=100):
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="place_order",
            params={"symbol": str(symbol), "side": str(side), "quantity": str(quantity),
                    "limit_price": str(limit_price), "account_id": str(account_id)},
            resource_ids=[str(account_id), str(symbol)],
            priority=int(priority),
        ))

    def _cancel_order(order_id, priority=100):
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="cancel_order",
            params={"order_id": str(order_id)},
            resource_ids=[str(order_id)],
            priority=int(priority),
        ))

    def _start_process(recipe, parcel_id=None, priority=100):
        params = {"recipe": str(recipe)}
        if parcel_id is not None:
            params["parcel_id"] = str(parcel_id)
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="start_process",
            params=params,
            resource_ids=[str(recipe)],
            priority=int(priority),
        ))

    def _cancel_process(process_id, priority=100):
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="cancel_process",
            params={"process_id": str(process_id)},
            resource_ids=[str(process_id)],
            priority=int(priority),
        ))

    def _transfer_parcel(parcel_id, to_entity_id, priority=100):
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="transfer_parcel",
            params={"parcel_id": str(parcel_id), "to_entity_id": str(to_entity_id)},
            resource_ids=[str(parcel_id)],
            priority=int(priority),
        ))

    def _levy(from_id, to_id, amount, rule_ref, priority=100):
        # The privileged transfer: money leaves `from_id` (an account the
        # entity does NOT own) and enters the entity's own `to_id`, under a
        # declared `rule_ref`. resolve_intent gates this on the `levy`
        # capability; a VALIDATOR may veto it (e.g. amount over schedule).
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="levy",
            params={"from_account_id": str(from_id), "to_account_id": str(to_id),
                    "amount": str(amount), "rule_ref": str(rule_ref),
                    "reference": ""},
            resource_ids=[str(from_id), str(to_id)],
            priority=int(priority),
        ))

    def _seize(from_entity_id, spec, rule_ref, priority=100):
        # Privileged expropriation — the goods/parcels analogue of levy.
        # Move goods and/or parcels out of `from_entity_id` (an entity the
        # actor does NOT own) into a declared recipient (the authority
        # itself by default). resolve_intent gates this on the `seize`
        # capability; a VALIDATOR may veto it under `rule_ref`.
        # `spec` is a Lua table:
        #   {symbol="GRAIN", quantity=50}        goods
        #   {parcel_ids={id1, id2}}              parcels
        #   {to_entity_id=coop_id, ...}          redirect (redistribution)
        import json
        spec = _lua_to_python(spec) or {}
        params = {"from_entity_id": str(from_entity_id), "rule_ref": str(rule_ref)}
        if "symbol" in spec:
            params["symbol"] = str(spec["symbol"])
        if "quantity" in spec:
            params["quantity"] = str(spec["quantity"])
        if "to_entity_id" in spec:
            params["to_entity_id"] = str(spec["to_entity_id"])
        if spec.get("parcel_ids"):
            params["parcel_ids"] = json.dumps([str(p) for p in spec["parcel_ids"]])
        resource_ids = [str(from_entity_id)]
        if "to_entity_id" in spec:
            resource_ids.append(str(spec["to_entity_id"]))
        for p in (spec.get("parcel_ids") or []):
            resource_ids.append(str(p))
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="seize",
            params=params,
            resource_ids=resource_ids,
            priority=int(priority),
        ))

    def _set_fiscal_policy(policy, priority=100):
        # Replace the votable fiscal-policy dict. `policy` is a Lua table
        # (the natural form for a script author); it is converted to a
        # Python dict and serialised to a JSON string because intent params
        # are stringly typed. resolve_intent re-checks the
        # `set_fiscal_policy` capability and fires a VALIDATOR, which can
        # veto the change as a constitutional constraint on the rate.
        import json
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="set_fiscal_policy",
            params={"policy": json.dumps(_lua_to_python(policy))},
            resource_ids=[],
            priority=int(priority),
        ))

    def _set_script(script_type, lineage_id, source, bound_entity_id=None, priority=100):
        # Governed lawmaking (step 4a-1): retire the active version of
        # `lineage_id` and activate `source` as a new version. The entity
        # is the legislating authority; resolve_intent gates this on the
        # `legislate` capability. `bound_entity_id` binds a BEHAVIOUR
        # script to run as a specific entity each tick.
        params = {
            "script_type": str(script_type),
            "lineage_id": str(lineage_id),
            "source": str(source),
        }
        if bound_entity_id is not None:
            params["entity_id"] = str(bound_entity_id)
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="set_script",
            params=params,
            resource_ids=[str(lineage_id)],
            priority=int(priority),
        ))

    def _set_validator(lineage_id, source, bound_entity_id=None, priority=100):
        # Constitutional amendment (step 4b): the governed lifecycle for a
        # VALIDATOR — retire-old + activate-new, gated by
        # `amend_constitution`. The ONLY script path that writes a
        # validator; set_script is kept away from them. A script driving a
        # constitutional cycle would queue this as a mutation on a
        # constitutional proposal, then enact as a government holding
        # amend_constitution.
        params = {
            "lineage_id": str(lineage_id),
            "source": str(source),
        }
        if bound_entity_id is not None:
            params["entity_id"] = str(bound_entity_id)
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="set_validator",
            params=params,
            resource_ids=[str(lineage_id)],
            priority=int(priority),
        ))

    def _set_constitution(params, priority=100):
        # Constitutional amendment (step 4b): replace the voting-system
        # floor (supermajority threshold/quorum). `params` is a Lua table
        # (the natural form for a script author); converted to a dict and
        # serialised to JSON because intent params are stringly typed.
        # resolve_intent re-checks `amend_constitution` and fires a
        # VALIDATOR, so the constitution can constrain its own amendment.
        import json
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="set_constitution",
            params={"constitution": json.dumps(_lua_to_python(params))},
            resource_ids=[],
            priority=int(priority),
        ))

    def _create_proposal(target_id, mutations, weight_model="citizen", threshold="0.5", quorum="0", title="", proposal_type="ordinary", priority=100):
        # Open a proposal for vote (step 4a-ii). `mutations` is a Lua table
        # (a list of {type=..., params={...}}); converted to Python and
        # serialised to JSON because intent params are stringly typed. No
        # capability gates this — the proposer's electorate membership is
        # the gate, checked in resolve_intent via the weight model.
        # `proposal_type` is "ordinary" (default) or "constitutional";
        # the latter's mutations are set_validator / set_constitution and
        # its enactment needs amend_constitution + a supermajority.
        import json
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="create_proposal",
            params={
                "target_id": str(target_id),
                "mutations": json.dumps(_lua_to_python(mutations)),
                "weight_model": str(weight_model),
                "threshold": str(threshold),
                "quorum": str(quorum),
                "title": str(title),
                "proposal_type": str(proposal_type),
            },
            resource_ids=[str(target_id)],
            priority=int(priority),
        ))

    def _vote(proposal_id, choice, priority=100):
        # Cast a for/against on a proposal. Gated by electorate membership
        # in resolve_intent; the weight is snapshotted by the resolver.
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="vote",
            params={"proposal_id": str(proposal_id), "choice": str(choice)},
            resource_ids=[str(proposal_id)],
            priority=int(priority),
        ))

    def _enact(proposal_id, priority=100):
        # Tally and apply a proposal as the target government. resolve_intent
        # gates this on the legislate capability (the enactor must be the
        # proposal's target). On pass, the mutations apply atomically; a
        # VALIDATOR veto on any mutation fails the whole enactment.
        intents.append(Intent(
            entity_id=entity_id,
            intent_type="enact",
            params={"proposal_id": str(proposal_id)},
            resource_ids=[str(proposal_id)],
            priority=int(priority),
        ))

    action_tbl["transfer"]    = _transfer
    action_tbl["issue_money"] = _issue_money
    action_tbl["retire_money"] = _retire_money
    action_tbl["levy"]        = _levy
    action_tbl["seize"]       = _seize
    action_tbl["set_fiscal_policy"] = _set_fiscal_policy
    action_tbl["set_script"]  = _set_script
    action_tbl["set_validator"] = _set_validator
    action_tbl["set_constitution"] = _set_constitution
    action_tbl["create_proposal"] = _create_proposal
    action_tbl["vote"]            = _vote
    action_tbl["enact"]           = _enact
    action_tbl["place_order"] = _place_order
    action_tbl["cancel_order"] = _cancel_order
    action_tbl["start_process"] = _start_process
    action_tbl["cancel_process"] = _cancel_process
    action_tbl["transfer_parcel"] = _transfer_parcel

    ctx_tbl = lua.table()
    ctx_tbl["entity"]   = entity_tbl
    ctx_tbl["accounts"] = accounts_tbl
    ctx_tbl["holdings"] = holdings_tbl
    ctx_tbl["processes"] = processes_tbl
    ctx_tbl["parcels"]  = parcels_tbl
    ctx_tbl["needs"]    = needs_tbl
    ctx_tbl["unlocks"]  = unlocks_tbl
    ctx_tbl["events"]   = events_tbl
    ctx_tbl["state"]    = state_tbl
    ctx_tbl["query"]    = query_tbl
    ctx_tbl["action"]   = action_tbl
    if ctx.get("op"):
        ctx_tbl["op"] = _to_lua_table(ctx["op"])

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
