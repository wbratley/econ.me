"""
Script dispatch — wires HOOK and VALIDATOR scripts into the service layer,
and holds the intent resolver shared with the tick engine.

Also owns the tiered script libraries (docs/scripting.md): the per-world
`world` lib and the content-pack `pack` lib (WorldSettings), the install-
time validation gate every library and pack script passes, and the stdlib
version pinning behind manifest checks.

VALIDATOR  runs before every money operation with the operation as ctx.op;
           the chunk's return value is the verdict ({allow=false, reason=...}
           or a bare `false` denies; nil or {allow=true} allows). Fail-closed:
           an erroring or timed-out validator vetoes the operation. Validators
           are pure — any intents or state mutations they produce are ignored.

HOOK       runs after a successful operation with ctx.op (including the
           resulting transaction ids). Hooks persist ctx.state and may queue
           intents, which are resolved immediately with dispatch suppressed —
           a hook-triggered operation never re-fires hooks or validators, so
           recursion is impossible. A failing hook never fails the operation.

Scoping: a script with entity_id NULL applies to every operation; with
entity_id set it only fires for operations acted by that entity.
"""



import hashlib
import json
import threading
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .lua_engine import Intent, LuaEngine, stdlib_fingerprint
from . import capabilities as _capabilities
from .models import Account, Entity, Holding, Script, ScriptType, Proposal, ProposalStatus, VoteChoice, ProposalType, Tick, WorldSetting
from .models.entity import EntityType


class OperationVetoedError(ValueError):
    pass


class LibraryRejected(ValueError):
    """A library or pack script failed the install-time gate
    (docs/scripting.md section 4): syntax check, strict smoke-run against
    a synthetic ctx, purity. `.problems` lists the findings; broken or
    hostile content is refused BEFORE any player runs on it."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class ScriptRejected(ValueError):
    """A player-authored behaviour failed the submit-time lint
    (docs/scripting.md section 4, Phase 3): the same strict standard the
    install gate applies to operator content, wired into the autonomy
    path (`set_entity_behaviour`). `.problems` lists the findings.

    Refusal class = vocabulary the script cannot have: syntax errors,
    undeclared-global reads (the nil-call trap: a helper that was never
    injected, which would silently zombie the entity every tick),
    undeclared-global writes, and reassigning injected names. Everything
    else the smoke-run hits (generic errors on the synthetic ctx -- a
    script may legitimately depend on real ctx.state, which the synthetic
    ctx does not provide) is a WARNING, not a refusal: the script is
    accepted and the finding is returned alongside it."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


_engine = LuaEngine()
_local = threading.local()


# ---------------------------------------------------------------------------
# The per-world script library (docs/scripting.md section 3)
# ---------------------------------------------------------------------------

#: WorldSetting key holding the world lib: a Lua chunk that RETURNS its
#: namespace table, injected as `world` into every script run in this world
#: (BEHAVIOUR, POLICY, VALIDATOR, HOOK alike) alongside the engine `std`.
#: Engine idioms shared by the world's scripts -- no play opinions; those
#: live in content packs. Operator-authored at world bootstrap (settled
#: decision #2: whether world-lib changes can become votable is open, and
#: nothing here forecloses it). Determinism (settled decision #1): this is
#: replay INPUT -- a snapshot/fork carries it like any other row, and `std`
#: is pinned by the engine version.
WORLD_LIB_KEY = "scripting.world_lib"

#: WorldSetting key holding the content-pack lib: the play OPINIONS the
#: pack ships (pricing policies, pantry rules) as a Lua chunk returning its
#: namespace table, injected as `pack`. Tier three of docs/scripting.md
#: section 2 -- unlike std/world it encodes opinions about how to play, so
#: it travels with the content pack and its manifest, not the engine.
PACK_LIB_KEY = "scripting.pack_lib"

#: WorldSetting key recording the engine-stdlib fingerprint this world's
#: scripts were installed against (settled decision #1: determinism
#: pinning). Written once at world bootstrap; drift -- an engine upgrade
#: changing `std` under a running world -- is visible in scripting_report
#: and refuses pack re-installs until the manifest is re-pinned.
STD_PIN_KEY = "scripting.std_version"

#: WorldSetting key: when truthy, entity-scoped scripts see only their
#: OWN holdings through ctx.query -- `holding` of another entity returns
#: nil and `holders` comes back empty. The share-register reads stay
#: available in worlds that leave this unset (real share registers are
#: public); a private-holdings world sets it at content time.
PRIVATE_HOLDINGS_KEY = "world.private_holdings"


def get_world_lib(session: Session) -> str | None:
    """The world lib source, or None if unset/blank."""
    return _get_lib(session, WORLD_LIB_KEY)


def set_world_lib(session: Session, source: str | None) -> str | None:
    """Set (non-blank str) or clear (None/blank) the world lib.

    Gated: a non-blank source must pass the install-time validation gate
    (LibraryRejected on failure) -- nothing broken reaches a tick."""
    return _set_lib(session, WORLD_LIB_KEY, source)


def get_pack_lib(session: Session) -> str | None:
    """The content-pack lib source, or None if unset/blank."""
    return _get_lib(session, PACK_LIB_KEY)


def set_pack_lib(session: Session, source: str | None) -> str | None:
    """Set or clear the pack lib (same gate as the world lib)."""
    return _set_lib(session, PACK_LIB_KEY, source)


def get_world_libraries(session: Session) -> dict[str, str] | None:
    """The `libraries` dict every LuaEngine.run() call in this world passes.

    One accessor so the tick path, the VALIDATOR/HOOK dispatch below, and
    the platform's dry-run endpoint all inject exactly the same tiers --
    the one-choke-point rule (docs/scripting.md section 4). Currently the
    per-world tiers: `world` and `pack`, whichever are set."""
    libs: dict[str, str] = {}
    world_lib = get_world_lib(session)
    if world_lib:
        libs["world"] = world_lib
    pack_lib = get_pack_lib(session)
    if pack_lib:
        libs["pack"] = pack_lib
    return libs or None


# ---------------------------------------------------------------------------
# The install-time gate (docs/scripting.md section 4: provenance gradient)
# ---------------------------------------------------------------------------

#: Synthetic ctx for gate smoke-runs: the shape _build_ctx expects, with no
#: session behind it. Pure-Lua library members can only touch what is
#: injected, so running them here is safe by construction -- and catches
#: the nil-call class at install time instead of at a player's tick.
_GATE_TIMEOUT_MS = 1000  # install-time cost is irrelevant (docs/scripting.md)

#: No-op stand-ins for the full production query surface (scripting.py
#: build_queries). Collection queries return empty lists (converted to Lua
#: tables by _build_ctx) so ipairs-based scripts smoke-run cleanly; scalar
#: queries return None. Without these, a synthetic ctx LIES about vocabulary
#: -- ctx.query.world_setting would be a nil field and every POLICY script
#: using it would fail validation for a reason that cannot occur in
#: production. The platform dry-run uses the same dict.
def synthetic_queries() -> dict:
    return {
        "balance": lambda account_id: None,
        "total_supply": lambda currency: None,
        "market_price": lambda symbol: None,
        "best_bid": lambda symbol: None,
        "best_ask": lambda symbol: None,
        "holding": lambda entity_id, symbol: None,
        "unreserved": lambda entity_id, symbol: None,
        "has_unlock": lambda entity_id, code: False,
        "holders": lambda symbol: [],
        "world_setting": lambda key: None,
        "fiscal_policy": lambda: None,
        "constitution": lambda: None,
        "active_script": lambda lineage_id: None,
        "script_history": lambda lineage_id: [],
        "proposal": lambda proposal_id: None,
        "proposals": lambda status=None: [],
        "tally": lambda proposal_id: None,
        "age": lambda entity_id: None,
        "lifespan": lambda entity_id: None,
        "population": lambda: [],
        "parents": lambda entity_id: [],
        "children": lambda entity_id: [],
    }


#: The synthetic ctx itself: the shape _build_ctx expects, with no session
#: behind it. Pure-Lua library members can only touch what is injected, so
#: running them here is safe by construction -- and catches the nil-call
#: class at install time instead of at a player's tick.
SYNTHETIC_CTX = {
    "entity": {"id": "gate-entity", "name": "Gate", "entity_type": "individual",
               "is_monetary_authority": False},
    "accounts": [{"id": "gate-account", "currency": "USD", "balance": "1000.0000"}],
    "holdings": [], "processes": [], "parcels": [], "needs": [],
    "unlocks": [], "events": [], "state": {},
    # _build_ctx exposes the executing tick as ctx.tick (scripting.py
    # "tick"); behaviours schedule off it (the post peddles every 10th
    # tick), so the gate ctx carries a scalar too -- a nil here would
    # be a vocabulary lie, not a clean smoke run.
    "tick": 1,
    # POLICY/VALIDATOR/HOOK scripts read ctx.op
    "op": {
        "type": "transfer", "entity_id": "gate-entity",
        "from_account_id": "gate-account", "to_account_id": "gate-account-2",
        "amount": "100.0000", "currency": "USD", "reference": "gate",
        "transaction_ids": ["gate-tx-1"],
    },
    "queries": None,  # filled per-call: synthetic_queries() (fresh callables)
}


def synthetic_ctx() -> dict:
    """A fresh synthetic ctx (fresh query callables -- they are inert
    closures, but never share mutable state across gate runs)."""
    ctx = dict(SYNTHETIC_CTX)
    ctx["queries"] = synthetic_queries()
    return ctx


# Gate runs use synthetic_ctx() (fresh per call).


def validate_library_source(source: str, libraries: dict[str, str] | None = None) -> list[str]:
    """Gate a library-tier source (world lib, pack lib). Returns problems.

    1. syntax  -- compile, don't execute;
    2. smoke   -- strict-run the chunk against the synthetic ctx (the lint:
                  undeclared-global reads/writes, tier shadowing);
    3. purity  -- the chunk must RETURN a namespace table whose members are
                  functions or nested tables only: vocabulary, never
                  strategy constants or anything Python-backed (the
                  pure-Lua rule -- a Python callable here would be a hole
                  the sandbox cannot see);
    4. sweep   -- each member is CALLED (zero-arg, pcall'd) under strict
                  globals and only `undeclared global` findings count: the
                  nil-call class inside member bodies surfaces here at
                  install time. Best-effort by construction (members whose
                  bad read hides behind an argument-dependent branch with
                  nil test args are not reached); top-level source and --
                  in Phase 3 -- player submissions are checked
                  exhaustively instead.
    """
    try:
        from lupa import LuaRuntime
        LuaRuntime().compile(source)
    except Exception as exc:
        return [f"syntax: {exc}"]

    result = _engine.run(source, synthetic_ctx(), timeout_ms=_GATE_TIMEOUT_MS,
                         libraries=libraries, strict_globals=True)
    if result.error:
        return [f"smoke-run: {result.error}"]
    if not isinstance(result.return_value, dict):
        return [f"must return a namespace table, got {type(result.return_value).__name__}"]

    problems: list[str] = []

    def _walk(prefix: str, table: dict) -> None:
        for key, member in table.items():
            name = f"{prefix}{key}"
            if callable(member):
                continue
            if isinstance(member, dict):
                _walk(f"{name}.", member)
                continue
            problems.append(
                f"member {name!r}: a namespace exposes functions (or nested "
                f"tables), got {type(member).__name__} -- constants live in "
                f"script source or ctx.state, not library tiers"
            )

    _walk("", result.return_value)
    if problems:
        return problems

    sweep = _engine.run(_member_sweep_source(source), synthetic_ctx(),
                        timeout_ms=_GATE_TIMEOUT_MS,
                        libraries=libraries, strict_globals=True)
    if sweep.error:
        return [f"member sweep: {sweep.error}"]
    findings = sweep.return_value
    if isinstance(findings, dict):
        findings = list(findings.values())
    return [f"member sweep: {f}" for f in (findings or [])]


def validate_script_source(source: str,
                           libraries: dict[str, str] | None = None) -> list[str]:
    """Gate a script source (pack role scripts, and in Phase 3 player
    submissions): a strict smoke-run against the synthetic ctx with the
    given tiers injected. The lint catches the nil-call class -- reads of
    helpers that were never injected -- at submit time instead of at the
    player's next tick."""
    result = _engine.run(source, synthetic_ctx(), timeout_ms=_GATE_TIMEOUT_MS,
                         libraries=libraries, strict_globals=True)
    if result.error:
        return [f"smoke-run: {result.error}"]
    return []


# Submit-time classification (Phase 3). The strict run reports ONE error --
# the first hit -- so findings are classified one at a time; fixing the
# reported problem and resubmitting surfaces the next, if any. Iterative
# linting, like every compiler.
_FATAL_MARKERS = (
    "read of undeclared global",
    "assignment to undeclared global",
    "reassigned injected name",
)


def check_player_script(source: str,
                        libraries: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Lint a player-authored behaviour at submit time. Returns
    ``(problems, warnings)``.

    * problems -- REFUSE. The script cannot behave as written under any
      ctx: it does not compile, or it references vocabulary that is not
      injected (the nil-call trap: `setle_last_orders()` where the world
      provides `world.settle_last_orders`). The entity keeps its current
      behaviour; the player gets the finding in hand to fix now, not a
      zombie next tick.
    * warnings -- ACCEPT. The smoke-run errored on the synthetic ctx for
      some other reason (nil arithmetic on ctx.state the synthetic ctx
      does not populate, a timeout). A state-dependent script can be
      perfectly healthy; the dry-run endpoint is the voluntary deeper
      check. The finding is surfaced so the player can look.

    Writes are refused alongside reads, matching the install gate's one
    standard for operator content and player content alike: a global write
    is at best a scratch variable that dies with the per-run runtime --
    `local` is the fix, always trivial, strictly safer.
    """
    try:
        from lupa import LuaRuntime
        LuaRuntime().compile(source)
    except Exception as exc:
        return ([f"syntax: {exc}"], [])

    result = _engine.run(source, synthetic_ctx(), timeout_ms=_GATE_TIMEOUT_MS,
                         libraries=libraries, strict_globals=True)
    if result.error:
        if any(marker in result.error for marker in _FATAL_MARKERS):
            return ([f"lint: {result.error}"], [])
        return ([], [f"smoke-run: {result.error}"])
    return ([], [])


# ---------------------------------------------------------------------------
# Version pinning (settled decision #1: determinism)
# ---------------------------------------------------------------------------

def pin_std_version(session: Session) -> str:
    """Record the current engine-stdlib fingerprint as a WorldSetting.
    Called at world bootstrap; content-pack manifests declare the
    fingerprint they target, and a pack refuses to install on drift."""
    fingerprint = stdlib_fingerprint()
    row = session.get(WorldSetting, STD_PIN_KEY)
    if row is None:
        session.add(WorldSetting(key=STD_PIN_KEY, value=fingerprint))
    else:
        row.value = fingerprint
    session.flush()
    return fingerprint


def scripting_report(session: Session) -> dict:
    """Operator diagnostic: the identity of every script tier in this world
    and whether anything has drifted. `std.matches_pinned` false means the
    engine's stdlib changed under a running world (an engine upgrade) --
    replay inputs are suspect until the world re-pins and its pack
    manifests catch up."""
    row = session.get(WorldSetting, STD_PIN_KEY)
    pinned = row.value if row else None
    fingerprint = stdlib_fingerprint()
    world_lib = get_world_lib(session)
    pack_lib = get_pack_lib(session)
    report = {
        "std": {
            "fingerprint": fingerprint,
            "pinned": pinned,
            "matches_pinned": pinned in (None, fingerprint),
        },
        "world_lib_sha": _lib_sha(world_lib),
        "pack_lib_sha": _lib_sha(pack_lib),
        "gate": {
            "world_lib": validate_library_source(world_lib) if world_lib else [],
            "pack_lib": validate_library_source(pack_lib) if pack_lib else [],
        },
    }
    return report


def _lib_sha(source: str | None) -> str | None:
    if not source:
        return None
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _member_sweep_source(source: str) -> str:
    """Wrap a library source so each member runs once (zero-arg, pcall'd)
    under strict globals; only `undeclared global` findings are reported --
    the sweep is fishing for the nil-call class, not for arity noise. Pure
    Lua over the synthetic ctx makes calling members safe by construction."""
    return (
        "local __ns = (function()\n" + source + "\nend)()\n"
        "local __bad = {}\n"
        "local function __check(prefix, t)\n"
        "  for k, v in pairs(t) do\n"
        "    local name = prefix .. tostring(k)\n"
        "    if type(v) == 'function' then\n"
        "      local ok, err = pcall(v)\n"
        "      if not ok and type(err) == 'string'\n"
        "         and err:find('undeclared global', 1, true) then\n"
        "        table.insert(__bad, name .. ': ' .. err)\n"
        "      end\n"
        "    elseif type(v) == 'table' then\n"
        "      __check(name .. '.', v)\n"
        "    end\n"
        "  end\n"
        "end\n"
        "__check('', __ns)\n"
        "return __bad\n"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_lib(session: Session, key: str) -> str | None:
    row = session.get(WorldSetting, key)
    source = row.value if row else None
    return source if isinstance(source, str) and source.strip() else None


def _set_lib(session: Session, key: str, source: str | None) -> str | None:
    if source is not None and not source.strip():
        source = None
    if source is not None:
        problems = validate_library_source(source)
        if problems:
            raise LibraryRejected(problems)
    row = session.get(WorldSetting, key)
    if source is None:
        if row is not None:
            session.delete(row)
            session.flush()
        return None
    if row is None:
        session.add(WorldSetting(key=key, value=source))
    else:
        row.value = source
    session.flush()
    return source


def _depth() -> int:
    return getattr(_local, "depth", 0)


@contextmanager
def _suppressed():
    _local.depth = _depth() + 1
    try:
        yield
    finally:
        _local.depth -= 1


# ---------------------------------------------------------------------------
# Dispatch — called by the service layer
# ---------------------------------------------------------------------------

def fire_validators(session: Session, op: dict) -> None:
    """Run every applicable VALIDATOR; raise OperationVetoedError on deny."""
    if _depth():
        return
    for script in _applicable_scripts(session, ScriptType.VALIDATOR, op):
        result = _engine.run(script.source, _op_ctx(session, script, op),
                             timeout_ms=script.timeout_ms,
                             libraries=get_world_libraries(session))
        if result.error:
            raise OperationVetoedError(f"validator {script.name!r} failed: {result.error}")
        verdict = result.return_value
        if verdict is False:
            raise OperationVetoedError(f"vetoed by validator {script.name!r}")
        if isinstance(verdict, dict) and not verdict.get("allow", True):
            reason = verdict.get("reason") or "denied"
            raise OperationVetoedError(f"vetoed by validator {script.name!r}: {reason}")


def fire_hooks(session: Session, op: dict) -> None:
    """Run every applicable HOOK after a successful operation."""
    if _depth():
        return
    for script in _applicable_scripts(session, ScriptType.HOOK, op):
        result = _engine.run(script.source, _op_ctx(session, script, op),
                             timeout_ms=script.timeout_ms,
                             libraries=get_world_libraries(session))
        if result.error:
            continue  # a broken hook must not fail the operation
        script.state = dict(result.state_updates)
        with _suppressed():
            for intent in sorted(result.intents, key=lambda i: i.priority):
                resolve_intent(session, intent)


def _applicable_scripts(session: Session, script_type: ScriptType, op: dict):
    scripts = session.execute(
        select(Script)
        .where(Script.script_type == script_type, Script.is_active.is_(True))
        .order_by(Script.created_at, Script.id)
    ).scalars().all()
    return [s for s in scripts if s.entity_id is None or s.entity_id == op.get("entity_id")]


def _op_ctx(session: Session, script: Script, op: dict) -> dict:
    entity = session.get(Entity, op.get("entity_id")) if op.get("entity_id") else None
    # The tick this op applies at: the latest committed (an op fires
    # before the current tick commits). age() computes against the same
    # value so it never disagrees with ctx.tick here.
    _tick = _latest_tick_number(session)
    return {
        "entity": {
            "id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type.value,
            "age": (_tick - entity.birth_tick) if (entity and entity.birth_tick is not None) else None,
            "is_monetary_authority": entity.is_monetary_authority,
            "capabilities": list(entity.capabilities or []),
        } if entity else {},
        "accounts": [
            {"id": a.id, "currency": a.currency, "balance": str(a.balance)}
            for a in entity.accounts
        ] if entity else [],
        "events": [],
        "state": dict(script.state or {}),
        "op": op,
        "queries": build_queries(session, _tick),
        # ctx.tick is the tick currently executing (threaded in). A validator
        # or hook fires mid-operation, before the current tick is committed,
        # so the honest value here is the last-completed tick — the world as
        # it stood when the op was applied. A direct API op (between ticks)
        # reads the same field and gets the true latest.
        "tick": _tick,
    }


def _latest_tick_number(session: Session) -> int:
    """Number of the most recently committed Tick, or 0 before tick 1."""
    row = session.execute(
        select(Tick.number).order_by(Tick.number.desc()).limit(1)
    ).scalar_one_or_none()
    return row if row is not None else 0


def _executing_tick(session: Session) -> int:
    """The tick currently executing, else the latest committed.

    ``run_tick`` sets a thread-local to the tick in progress around the
    intent-resolution phase, so a mid-tick ``spawn_entity`` stamps the very
    tick the spawner saw as ``ctx.tick`` (not the one before it -- the
    current Tick row is committed only at the *end* of run_tick). Outside a
    tick (the API/test path, which resolves intents between ticks) the
    thread-local is unset and this falls back to the latest committed tick,
    matching ``create_entity``. Either way a newborn records the tick it
    first exists at, so ``age()`` never disagrees with ``ctx.tick``.
    """
    t = getattr(_local, "tick", None)
    return t if t is not None else _latest_tick_number(session)


def set_executing_tick(number: int | None) -> None:
    """Mark the tick in progress (called by ``run_tick``); None clears it."""
    _local.tick = number


# ---------------------------------------------------------------------------
# Shared with the tick engine
# ---------------------------------------------------------------------------

def build_queries(session: Session, tick_number: int | None = None,
                  owner_id: str | None = None) -> dict:
    """ctx.query.* — read-only, string results so Lua sees exact decimals.

    ``tick_number`` is the tick age() computes against, threaded from the
    caller so it matches the ctx.tick that very script already sees
    (executing tick for POLICY/BEHAVIOUR, latest committed for
    VALIDATOR/HOOK). Unset (the bare ``build_queries(session)`` form used by
    tests) falls back to the latest committed tick.

    ``owner_id`` scopes the entity-visible reads: in a world that set
    ``world.private_holdings``, a script sees only its own entity's
    holdings (``holding`` of anyone else is nil, ``holders`` is empty);
    unset -- the op-context form validators use -- reads stay global, the
    monetary authority's view of the world it guards.
    """
    from . import markets, tech  # deferred: markets imports this module
    _tick = tick_number if tick_number is not None else _latest_tick_number(session)
    _private = False
    if owner_id is not None:
        _row = session.get(WorldSetting, PRIVATE_HOLDINGS_KEY)
        _private = bool(_row is not None and _row.value)

    def balance(account_id):
        acct = session.get(Account, str(account_id))
        return str(acct.balance) if acct else None

    def total_supply(currency):
        total = session.execute(
            select(func.coalesce(func.sum(Account.balance), 0))
            .where(Account.currency == str(currency).upper())
        ).scalar_one()
        return str(total)

    def market_price(symbol):
        market = markets.get_market(session, str(symbol))
        if market is None or market.last_price is None:
            return None
        return str(market.last_price)

    def _book_top(symbol, side):
        """Top of the public book: the best OPEN limit on `side` ("buy"/
        "sell"), or None when nothing rests. Price only -- depth beyond
        the touch stays private, last_price stays the historical record."""
        from .models.order import Order, OrderSide, OrderStatus
        market = markets.get_market(session, str(symbol))
        if market is None:
            return None
        side = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
        agg = func.max(Order.limit_price) if side == OrderSide.BUY \
            else func.min(Order.limit_price)
        price = session.execute(
            select(agg).where(
                Order.market_id == market.id,
                Order.side == side,
                Order.status == OrderStatus.OPEN,
                Order.remaining > 0,
            )
        ).scalar_one()
        return str(price) if price is not None else None

    def best_bid(symbol):
        return _book_top(symbol, "buy")

    def best_ask(symbol):
        return _book_top(symbol, "sell")

    def holding(entity_id, symbol):
        if _private and str(entity_id) != str(owner_id):
            return None
        h = markets.get_holding(session, str(entity_id), str(symbol).upper())
        return str(h.quantity) if h else "0"

    def unreserved(entity_id, symbol):
        """Held minus reserved-by-running-processes -- the spendable side.

        start_process and market settlement both draw on this balance
        (production._available_quantity), but until run 15's postmortem
        no script could READ it: holding_qty showed the pantry while the
        check saw the pantry minus what running work had reserved, and
        144 refusals bounced off the difference. Same privacy rule as
        `holding`: a private-holdings world shows only your own row.
        """
        if _private and str(entity_id) != str(owner_id):
            return None
        eid = str(entity_id)
        h = markets.get_holding(session, eid, str(symbol).upper())
        held = h.quantity if h else Decimal("0")
        return str(held - markets.reserved_quantity(session, eid, str(symbol).upper()))

    def has_unlock(entity_id, code):
        technology = tech.get_technology(session, str(code))
        if technology is None:
            return False
        return tech.has_unlock(session, str(entity_id), technology)

    def holders(symbol):
        """Every entity holding a positive quantity of the symbol, with the
        settlement account to pay them through.

        The register a share needs: an issuer cannot pay a dividend without
        knowing who its holders are, and once shares trade, a cap table
        cached in Script.state goes stale the first time one changes hands.

        Note this is a GLOBAL read — any script can enumerate holders of any
        symbol, which is right for a share register (real ones are public)
        and considerably more than that for, say, FOOD. Per-symbol
        visibility as votable data remains unimplemented; the coarse cut
        a world can make TODAY is world.private_holdings (see
        PRIVATE_HOLDINGS_KEY): with it set, entity-scoped scripts get nil
        and empty here — a private pantry — while op-context scripts (the
        referee) keep the global read.

        Ordered by entity id so a script iterating holders is deterministic.
        The account is the entity's first in `currency`, matching how
        ctx.accounts[1] is used everywhere else in this codebase.
        """
        if _private:
            return []
        rows = session.execute(
            select(Holding.entity_id, Holding.quantity)
            .where(Holding.symbol == str(symbol).upper(), Holding.quantity > 0)
            .order_by(Holding.entity_id)
        ).all()
        if not rows:
            return []

        # Accounts for the whole register in one query rather than one per
        # holder: a dividend reads this every payout period, and an N+1 here
        # would scale with the shareholder count on a hot path.
        holder_ids = [entity_id for entity_id, _ in rows]
        first_account: dict[str, str] = {}
        for account in session.execute(
            select(Account)
            .where(Account.entity_id.in_(holder_ids))
            .order_by(Account.entity_id, Account.currency, Account.id)
        ).scalars():
            first_account.setdefault(account.entity_id, account.id)

        return [
            {
                "entity_id": entity_id,
                "quantity": str(quantity),
                "account_id": first_account.get(entity_id),
            }
            for entity_id, quantity in rows
        ]

    def world_setting(key):
        """Any world-level votable datum by key, or None if unset.

        The generic read behind fiscal_policy() and constitution(): a
        governance layer writes WorldSettings, scripts read them. This is
        also the signal channel (Step 5c, Fork A) -- a price-feed POLICY
        posts ``signal:wheat`` each tick; consumers read it here instead
        of each keeping their own copy. A global read, like a published
        rate or register; the rules of the world are public.

        Returns the raw stored value (a dict) so a caller stores whatever
        shape it needs and reads those keys back. ``None`` means unset --
        a signal whose feed has gone dark, or a key never written.
        """
        setting = session.get(WorldSetting, str(key))
        return setting.value if setting is not None else None

    def fiscal_policy():
        """The government's votable fiscal-policy dict (or {} if unset).

        This is the read side a government POLICY script uses to turn
        enacted rates into levy calls: citizens vote on the *numbers*
        (set_fiscal_policy), the script reads them here, and the engine
        mechanism (services.levy) does the moving. Global read — any
        script may see the published policy, the way real tax schedules
        are public.
        """
        from . import fiscal
        return fiscal.get_fiscal_policy(session)

    def constitution():
        """The voting-system floor — the params a constitutional amendment
        must clear (supermajority threshold + quorum), with defaults.

        The read side a constitutional POLICY script (or the platform's
        amendment-day UI) uses to know the bar: citizens vote to amend the
        *constitution* (set_constitution), and this is the result. Like
        fiscal_policy, a global read — the rules of amendment are public.
        Always returns a full dict (threshold + quorum); there is always a
        constitution in force.
        """
        from . import constitution as _constitution
        return _constitution.get_constitution(session)

    def active_script(lineage_id):
        """The currently-active version of a law (lineage), or None.

        Returns the source so a POLICY script can read another law's text,
        and so the platform can render the live law. Resolves by
        lineage_id + is_active — the retire-old/activate-new identity.
        """
        s = session.execute(
            select(Script).where(
                Script.lineage_id == str(lineage_id),
                Script.is_active.is_(True),
            )
        ).scalars().first()
        if s is None:
            return None
        return {
            "id": s.id,
            "name": s.name,
            "script_type": s.script_type.value,
            "source": s.source,
            "entity_id": s.entity_id,
            "lineage_id": s.lineage_id,
        }

    def script_history(lineage_id):
        """Every version of a law (lineage), oldest first — the legislative
        record that retire-old/activate-new preserves. Metadata only (no
        source) to keep the audit view cheap; read a version's source by id
        where needed.
        """
        rows = session.execute(
            select(Script)
            .where(Script.lineage_id == str(lineage_id))
            .order_by(Script.created_at, Script.id)
        ).scalars().all()
        return [
            {
                "id": s.id,
                "name": s.name,
                "script_type": s.script_type.value,
                "is_active": s.is_active,
                "entity_id": s.entity_id,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in rows
        ]

    def _proposal_view(p):
        return {
            "id": p.id,
            "title": p.title,
            "proposal_type": p.proposal_type.value,
            "proposer_id": p.proposer_id,
            "target_id": p.target_id,
            "weight_model": p.weight_model,
            "threshold": p.threshold,
            "quorum": p.quorum,
            "mutations": list(p.mutations or []),
            "status": p.status.value,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "enacted_at": p.enacted_at.isoformat() if p.enacted_at else None,
            "tally_yes": p.tally_yes,
            "tally_no": p.tally_no,
            "tally_turnout": p.tally_turnout,
            "failure_reason": p.failure_reason,
        }

    def proposal(proposal_id):
        """One proposal by id — the record a voter or the platform reads to
        see status, threshold/quorum, and the snapshotted tally."""
        p = session.get(Proposal, str(proposal_id))
        return _proposal_view(p) if p is not None else None

    def proposals(status=None):
        """Every proposal (optionally filtered by status), newest last."""
        stmt = select(Proposal)
        if status is not None:
            try:
                wanted = ProposalStatus(str(status))
            except ValueError:
                return []
            stmt = stmt.where(Proposal.status == wanted)
        stmt = stmt.order_by(Proposal.created_at, Proposal.id)
        return [_proposal_view(p) for p in session.execute(stmt).scalars()]

    def tally(proposal_id):
        """Live tally — yes/no/turnout computed now from recorded votes and
        the current electorate. For a closed proposal this recomputes; the
        authoritative record is the proposal's snapshotted tally_* columns."""
        from . import weights
        p = session.get(Proposal, str(proposal_id))
        if p is None:
            return None
        yes = sum((Decimal(v.weight) for v in p.votes if v.choice == VoteChoice.FOR), Decimal(0))
        no = sum((Decimal(v.weight) for v in p.votes if v.choice == VoteChoice.AGAINST), Decimal(0))
        electorate_total = sum(weights.electorate(session, p.weight_model).values(), Decimal(0))
        cast = yes + no
        turnout = str(cast / electorate_total) if electorate_total > 0 else "0"
        return {
            "proposal_id": p.id,
            "yes": str(yes),
            "no": str(no),
            "cast": str(cast),
            "electorate": str(electorate_total),
            "turnout": turnout,
        }

    def age(entity_id):
        """The age of an entity in ticks (current tick minus birth_tick),
        or None if untracked.

        Age is the one entity attribute that is NOT a holding (it is
        monotonic and tick-derived, so storing and mutating it would be
        wasteful and wrong -- Step 6, docs/actors.md). The engine stamps
        birth_tick once at creation; this computes the derived value against
        the same tick the calling script already sees as ctx.tick (executing
        tick for POLICY/BEHAVIOUR, latest committed for VALIDATOR/HOOK), so
        age and ctx.tick never disagree.

        None means the entity has no birth_tick (it predates age-tracking,
        or does not exist) -- nil in Lua, a dark read rather than an error.
        A fail-closed age-gating script treats nil as "eligibility cannot
        be certified".
        """
        e = session.get(Entity, str(entity_id))
        if e is None or e.birth_tick is None:
            return None
        return _tick - e.birth_tick

    def lifespan(entity_id):
        """The age (in ticks) at which this entity dies of old age, or None if
        it is immortal.

        The invariant mortality floor of Step 6d (docs/actors.md): the
        engine's incapacity pass deactivates the entity once its derived
        age reaches this and applies the estate rule. None (nil in Lua)
        means immortal -- either the entity has no lifespan (the default;
        the feature is opt-in) or does not exist. Per-entity data, not a
        votable WorldSetting, and immutable once stamped at spawn.

        A pension or insurance POLICY reads this to compute remaining life
        (``lifespan - age``) without inventing a datum; a death-by-old-age
        script gates on it. Note this is the hard FLOOR: the *dynamic* face
        of mortality stays the shipped condition/incapacitates_at pass
        (food, medicine, needs, decay), which a script reads separately.
        """
        e = session.get(Entity, str(entity_id))
        if e is None:
            return None
        return e.lifespan

    def population():
        """Count of ACTIVE entities -- the living population.

        The world-facing number a votable population cap checks (Step 6c,
        docs/actors.md). Active only because the interesting quantity for a
        living-population rule is who is *alive*, not the cumulative row
        count (the dead still take storage but do not run scripts). The
        server-tier hard cap counts both active and total internally and is
        never script-visible; this is the policy-facing read.
        """
        from .models.entity import EntityStatus
        return session.execute(
            select(func.count()).select_from(Entity)
            .where(Entity.status == EntityStatus.ACTIVE)
        ).scalar_one()

    def parents(entity_id):
        """The parent ids of an entity (its provenance), or an empty list.

        The stored ``parents`` list stamped once by ``spawn_entity`` --
        engine-blind (the engine does not interpret it) but authoritative
        (immutable, so inheritance and consanguinity rules rest on honest
        data). Empty for an entity made at world setup or one spawned from
        no parents (spontaneous generation). A consanguinity validator
        walks this to decide "these two share a parent".
        """
        e = session.get(Entity, str(entity_id))
        return list(e.parents or []) if e is not None else []

    def children(entity_id):
        """Every entity whose ``parents`` lists ``entity_id`` -- the reverse
        of :func:`parents`.

        Scans all entities (any status): a fertility-quota rule may count
        living children, a genealogy rule may count all ever born. Either
        is policy; this returns the raw set and lets the caller filter.
        Bounded by the server's total-row cap, so the scan stays cheap.
        """
        pid = str(entity_id)
        rows = session.execute(select(Entity.id, Entity.parents)).all()
        return [eid for (eid, plist) in rows if plist and pid in plist]

    return {
        "balance": balance,
        "total_supply": total_supply,
        "market_price": market_price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "holding": holding,
        "unreserved": unreserved,
        "has_unlock": has_unlock,
        "holders": holders,
        "age": age,
        "lifespan": lifespan,
        "population": population,
        "parents": parents,
        "children": children,
        "world_setting": world_setting,
        "fiscal_policy": fiscal_policy,
        "constitution": constitution,
        "active_script": active_script,
        "script_history": script_history,
        "proposal": proposal,
        "proposals": proposals,
        "tally": tally,
    }


SAY_TEXT_CAP = 256


def resolve_intent(session: Session, intent: Intent,
                   said: set[str] | None = None) -> dict:
    from . import combat, markets, production, services  # deferred: all import this module

    event = {
        "type": intent.intent_type,
        "entity_id": intent.entity_id,
        "params": intent.params,
        "idempotency_key": intent.idempotency_key,
    }

    def rejected(reason: str) -> dict:
        return {**event, "status": "rejected", "reason": reason}

    def amount_of(key: str) -> Decimal:
        try:
            return Decimal(intent.params[key])
        except (InvalidOperation, KeyError, TypeError):
            raise ValueError(f"invalid {key}")

    reference = intent.params.get("reference", "")
    extra: dict = {}

    # Capability gate — the same boundary that enforces ownership also
    # enforces privilege. An intent type listed in INTENT_CAPABILITIES may
    # only be queued by an entity holding that capability; without it the
    # intent is rejected before any service is touched. Ordinary
    # self-directed action (trade, produce, move your own money) is listed
    # nowhere and requires only ownership, which each branch checks below.
    required_cap = _capabilities.required_for(intent.intent_type)
    if required_cap is not None:
        entity = session.get(Entity, intent.entity_id)
        if entity is None or not entity.has_capability(required_cap):
            return rejected(f"missing capability {required_cap!r}")

    try:
        if intent.intent_type == "transfer":
            from_account = session.get(Account, intent.params.get("from_account_id"))
            to_account = session.get(Account, intent.params.get("to_account_id"))
            if from_account is None or to_account is None:
                return rejected("unknown account")
            if from_account.entity_id != intent.entity_id:
                return rejected("entity does not own source account")
            with session.begin_nested():
                services.transfer(session, from_account, to_account, amount_of("amount"), reference)

        elif intent.intent_type == "levy":
            # Privileged transfer: the authority compels money out of an
            # account it does NOT own, into its own. The capability gate
            # above already proved `entity` holds LEVY; here we only check
            # the recipient side — the authority must own `to_account` —
            # and let services.levy bypass ownership on `from_account`.
            from_account = session.get(Account, intent.params.get("from_account_id"))
            to_account = session.get(Account, intent.params.get("to_account_id"))
            if from_account is None or to_account is None:
                return rejected("unknown account")
            if to_account.entity_id != intent.entity_id:
                return rejected("entity does not own recipient account")
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            with session.begin_nested():
                services.levy(
                    session, authority, from_account, to_account,
                    amount_of("amount"),
                    intent.params.get("rule_ref", ""),
                    reference,
                )

        elif intent.intent_type == "seize":
            # Privileged expropriation: the authority compels goods and/or
            # parcels out of an entity it does NOT own, into a declared
            # recipient (itself by default). The capability gate above
            # already proved `entity` holds SEIZE; services.seize re-checks
            # it and bypasses ownership on the victim's holdings/parcels.
            # The goods/parcels analogue of levy (the money half).
            from_entity = session.get(Entity, intent.params.get("from_entity_id"))
            if from_entity is None:
                return rejected("unknown source entity")
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            to_entity = None
            if intent.params.get("to_entity_id"):
                to_entity = session.get(Entity, intent.params.get("to_entity_id"))
                if to_entity is None:
                    return rejected("unknown recipient entity")
            symbol = intent.params.get("symbol") or None
            quantity = amount_of("quantity") if "quantity" in intent.params else None
            raw_parcels = intent.params.get("parcel_ids", "") or ""
            try:
                parcel_ids = json.loads(raw_parcels) if raw_parcels else None
            except ValueError:
                return rejected("invalid parcel_ids JSON")
            with session.begin_nested():
                summary = services.seize(
                    session, authority, from_entity,
                    symbol=symbol, quantity=quantity, parcel_ids=parcel_ids,
                    to_entity=to_entity,
                    rule_ref=intent.params.get("rule_ref", ""),
                    reference=reference,
                )
            extra["seized_goods"] = summary["goods_quantity"]
            extra["seized_symbol"] = summary["goods_symbol"]
            extra["seized_parcels"] = summary["parcels"]

        elif intent.intent_type == "attack":
            # One creature's attempt on another (run 20: wolves as
            # entities). Any entity may fight anything: the resolution --
            # daylight refusal, hearth deterrence, stat math, loot -- is
            # the pack's COMBAT_RULES, applied by combat.py. The target
            # may be None: a desperate prowl, and the engine picks the
            # noisiest speaker of the night.
            target = intent.params.get("target_id") or None
            with session.begin_nested():
                outcome = combat.resolve_attack(
                    session, intent.entity_id, target, _executing_tick(session))
            outcome["idempotency_key"] = intent.idempotency_key
            return outcome

        elif intent.intent_type == "spawn_entity":
            # Bring a new entity into being during a tick (Step 6c). The
            # capability gate above already proved `entity` (the CALLER --
            # a midwife, a factory, or one of the parents) holds SPAWN.
            # `parents` is the declared provenance, independent of the
            # caller: capability gates the caller, validators gate the
            # parents. services.spawn_entity enforces the server hard caps
            # (engine invariant) and fires a VALIDATOR (the world's votable
            # rules), then stamps birth_tick + immutable parents and opens
            # an empty account. It does NOT endow -- a transfer the
            # spawning script / HOOK makes after, not the mechanism.
            try:
                parents = json.loads(intent.params.get("parents", "[]"))
            except ValueError:
                return rejected("invalid parents JSON")
            if not isinstance(parents, list):
                return rejected("parents must be a JSON array")
            caller = entity  # loaded at the capability gate
            owner_id = intent.params.get("owner_id") or caller.owner_id
            currency = intent.params.get("currency")
            if not currency:
                # default to the caller's first account currency -- the
                # money the spawner itself uses (matches how ctx.accounts[1]
                # is read everywhere). A money-incapable caller must pass an
                               # explicit currency.
                acct = sorted(caller.accounts, key=lambda a: a.id)[0] if caller.accounts else None
                if acct is None:
                    return rejected("caller has no account to derive currency; pass currency")
                currency = acct.currency
            try:
                et = EntityType(intent.params.get("entity_type", "individual"))
            except ValueError:
                return rejected(f"unknown entity_type {intent.params.get('entity_type')!r}")
            name = intent.params.get("name") or "entity"
            lifespan_raw = intent.params.get("lifespan")
            lifespan: int | None = None
            if lifespan_raw not in (None, ""):
                try:
                    lifespan = int(lifespan_raw)
                except ValueError:
                    return rejected(f"invalid lifespan {lifespan_raw!r}; must be an integer")
                if lifespan < 0:
                    return rejected("lifespan must be non-negative")
            with session.begin_nested():
                summary = services.spawn_entity(
                    session, caller, parents=parents, owner_id=owner_id,
                    currency=currency, name=name, entity_type=et,
                    lifespan=lifespan, reference=reference,
                )
            extra["child_id"] = summary["child_id"]
            extra["child_account_id"] = summary["account_id"]

        elif intent.intent_type == "set_fiscal_policy":
            # Replace the votable fiscal-policy dict. The capability gate
            # above already proved `entity` holds SET_FISCAL_POLICY; the
            # policy rides as a JSON string (intent params are stringly
            # typed) and is parsed here so the service stays
            # transport-agnostic (it takes a dict, like levy takes a
            # Decimal). services.set_fiscal_policy re-checks the capability
            # and fires a VALIDATOR — a constitutional veto on the rate.
            try:
                policy = json.loads(intent.params.get("policy", "") or "{}")
            except ValueError:
                return rejected("invalid fiscal policy JSON")
            if not isinstance(policy, dict):
                return rejected("fiscal policy must be a JSON object")
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            with session.begin_nested():
                services.set_fiscal_policy(session, authority, policy, reference)

        elif intent.intent_type == "set_script":
            # Governed lawmaking (step 4a-1): enact a new version of a law.
            # The capability gate above already proved `entity` holds
            # LEGISLATE; services.set_script enforces it again and keeps
            # validators out of reach (they are the constitution). A law is
            # identified by lineage_id; the service retires the active
            # version and activates this source as a new one.
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            raw_type = intent.params.get("script_type", "")
            try:
                script_type = ScriptType(raw_type)
            except ValueError:
                return rejected(f"unknown script_type {raw_type!r}")
            lineage_id = intent.params.get("lineage_id", "")
            if not lineage_id:
                return rejected("lineage_id required")
            bound_entity_id = intent.params.get("entity_id") or None
            with session.begin_nested():
                script = services.set_script(
                    session, authority, script_type, lineage_id,
                    intent.params.get("source", ""),
                    entity_id=bound_entity_id,
                    description=intent.params.get("description", ""),
                    timeout_ms=int(intent.params.get("timeout_ms", "100")),
                    reference=reference,
                )
            extra["script_id"] = script.id
            extra["lineage_id"] = lineage_id

        elif intent.intent_type == "set_validator":
            # Constitutional amendment (step 4b): enact a new version of a
            # VALIDATOR — the only path that writes one (set_script is kept
            # away from validators). The capability gate above already
            # proved `entity` holds AMEND_CONSTITUTION; services.set_validator
            # enforces it again. The validator binds the very next op,
            # including the rest of this enactment's mutations.
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            lineage_id = intent.params.get("lineage_id", "")
            if not lineage_id:
                return rejected("lineage_id required")
            bound_entity_id = intent.params.get("entity_id") or None
            with session.begin_nested():
                script = services.set_validator(
                    session, authority, lineage_id,
                    intent.params.get("source", ""),
                    description=intent.params.get("description", ""),
                    timeout_ms=int(intent.params.get("timeout_ms", "100")),
                    entity_id=bound_entity_id,
                    reference=reference,
                )
            extra["script_id"] = script.id
            extra["lineage_id"] = lineage_id

        elif intent.intent_type == "set_constitution":
            # Constitutional amendment (step 4b): replace the voting-system
            # floor (the supermajority threshold/quorum). The capability
            # gate above already proved `entity` holds AMEND_CONSTITUTION;
            # services.set_constitution re-checks and fires a VALIDATOR — so
            # the constitution can constrain its own amendment. Params ride
            # as a JSON string (intent params are stringly typed).
            try:
                params = json.loads(intent.params.get("constitution", "") or "{}")
            except ValueError:
                return rejected("invalid constitution JSON")
            if not isinstance(params, dict):
                return rejected("constitution must be a JSON object")
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            with session.begin_nested():
                services.set_constitution(session, authority, params, reference)

        elif intent.intent_type in ("grant_capability", "revoke_capability"):
            # Governed capability transfer — the meta-privilege of changing
            # *who can exercise power*. The capability gate above already
            # proved `entity` holds GRANT_CAPABILITY; the service re-checks
            # it and fires a VALIDATOR (so the constitution can forbid
            # conferring a dangerous capability regardless of who
            # authorises it). The capability name is validated against the
            # declared vocabulary; the target must exist. As a proposal
            # mutation this is constitutional-tier (power transfer is meta).
            capability = intent.params.get("capability", "")
            target = session.get(Entity, intent.params.get("to_entity_id"))
            if target is None:
                return rejected("unknown target entity")
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            fn = (services.grant_capability
                  if intent.intent_type == "grant_capability"
                  else services.revoke_capability)
            with session.begin_nested():
                try:
                    fn(session, authority, target, capability, reference)
                except ValueError as exc:
                    return rejected(str(exc))

        elif intent.intent_type == "create_proposal":
            # Open a proposal for vote (step 4a-ii). No capability gates
            # this — participation *is* the electorate, defined by the
            # weight model (form of government as data), so the proposer
            # must be a member. The proposal is inert until enacted; the
            # target is the government whose capabilities the mutations
            # will exercise.
            from . import weights
            target = session.get(Entity, intent.params.get("target_id", ""))
            if target is None:
                return rejected("unknown target entity")
            weight_model = intent.params.get("weight_model", "")
            try:
                proposer_weight = weights.weight_of(session, weight_model, intent.entity_id)
            except ValueError as exc:
                return rejected(str(exc))
            if proposer_weight <= 0:
                return rejected("proposer is not in the electorate")
            try:
                mutations = json.loads(intent.params.get("mutations", "[]"))
            except ValueError:
                return rejected("invalid mutations JSON")
            raw_pt = intent.params.get("proposal_type", "ordinary")
            try:
                proposal_type = ProposalType(raw_pt)
            except ValueError:
                return rejected(f"unknown proposal_type {raw_pt!r}")
            try:
                with session.begin_nested():
                    proposal = services.create_proposal(
                        session, intent.entity_id, target.id,
                        intent.params.get("title", ""),
                        weight_model,
                        Decimal(intent.params.get("threshold", "0.5")),
                        Decimal(intent.params.get("quorum", "0")),
                        mutations,
                        proposal_type=proposal_type,
                        reference=reference,
                    )
            except ValueError as exc:
                return rejected(str(exc))
            extra["proposal_id"] = proposal.id

        elif intent.intent_type == "vote":
            # Cast a for/against. Gated by electorate membership (the
            # resolver): a non-member gets weight 0 and is rejected. The
            # weight is snapshotted at cast time. Idempotent per voter.
            from . import weights
            proposal = session.get(Proposal, intent.params.get("proposal_id", ""))
            if proposal is None:
                return rejected("unknown proposal")
            if proposal.status != ProposalStatus.OPEN:
                return rejected(f"proposal is {proposal.status.value}, not open")
            choice_raw = intent.params.get("choice", "")
            try:
                choice = VoteChoice(choice_raw)
            except ValueError:
                return rejected("choice must be 'for' or 'against'")
            try:
                voter_weight = weights.weight_of(session, proposal.weight_model, intent.entity_id)
            except ValueError as exc:
                return rejected(str(exc))
            if voter_weight <= 0:
                return rejected("voter is not in the electorate")
            try:
                with session.begin_nested():
                    vote = services.cast_vote(
                        session, proposal, intent.entity_id, choice, voter_weight, reference,
                    )
            except ValueError as exc:
                return rejected(str(exc))
            extra["vote_id"] = vote.id
            extra["choice"] = choice.value
            extra["weight"] = str(voter_weight)

        elif intent.intent_type == "enact":
            # Tally and apply. The required capability is data on the
            # proposal — ordinary -> legislate, constitutional ->
            # amend_constitution — so it is checked here (after the proposal
            # and its tier are loaded), not at the top-level capability
            # gate. We also confirm the enactor IS this proposal's target.
            # enact_proposal tallies and, on pass, applies the mutations
            # atomically as the target — re-running each through
            # resolve_intent, so caps and validators fire. A constitutional
            # proposal's threshold/quorum are also raised to the
            # supermajority floor inside enact_proposal. A failed tally is
            # still an "applied" intent (it did its job); the outcome rides
            # in extra.
            proposal = session.get(Proposal, intent.params.get("proposal_id", ""))
            if proposal is None:
                return rejected("unknown proposal")
            if proposal.status != ProposalStatus.OPEN:
                return rejected(f"proposal is {proposal.status.value}, not open")
            if proposal.target_id != intent.entity_id:
                return rejected("only the target government may enact this proposal")
            required = (_capabilities.AMEND_CONSTITUTION
                        if proposal.proposal_type == ProposalType.CONSTITUTIONAL
                        else _capabilities.LEGISLATE)
            entity = session.get(Entity, intent.entity_id)
            if entity is None or not entity.has_capability(required):
                return rejected(f"missing capability {required!r}")
            with session.begin_nested():
                outcome = services.enact_proposal(session, proposal, reference)
            extra["proposal_id"] = proposal.id
            extra["proposal_status"] = outcome["status"]
            extra["tally_yes"] = str(outcome["yes"])
            extra["tally_no"] = str(outcome["no"])
            extra["tally_turnout"] = str(outcome["turnout"])
            if outcome.get("reason"):
                extra["reason"] = outcome["reason"]

        elif intent.intent_type in ("issue_money", "retire_money"):
            account = session.get(Account, intent.params.get("account_id"))
            if account is None:
                return rejected("unknown account")
            if account.entity_id != intent.entity_id:
                return rejected("entity does not own account")
            op = services.issue_money if intent.intent_type == "issue_money" else services.retire_money
            with session.begin_nested():
                op(session, account, amount_of("amount"), reference)

        elif intent.intent_type == "place_order":
            with session.begin_nested():
                order = markets.place_order(
                    session,
                    intent.entity_id,
                    symbol=intent.params.get("symbol", ""),
                    side=intent.params.get("side", ""),
                    quantity=amount_of("quantity"),
                    limit_price=amount_of("limit_price"),
                    account_id=intent.params.get("account_id", ""),
                    reference=reference,
                )
            extra["order_id"] = order.id  # scripts need this to cancel later

        elif intent.intent_type == "cancel_order":
            with session.begin_nested():
                markets.cancel_order(session, intent.params.get("order_id", ""), intent.entity_id)

        elif intent.intent_type == "start_process":
            entity = session.get(Entity, intent.entity_id)
            if entity is None:
                return rejected("unknown entity")
            with session.begin_nested():
                process = production.start_process(
                    session, entity, intent.params.get("recipe", ""),
                    parcel_id=intent.params.get("parcel_id"),
                )
            extra["process_id"] = process.id  # scripts need this to cancel later

        elif intent.intent_type == "cancel_process":
            with session.begin_nested():
                production.cancel_process(
                    session, intent.params.get("process_id", ""), intent.entity_id
                )

        elif intent.intent_type == "transfer_parcel":
            from . import parcels
            to_entity = session.get(Entity, intent.params.get("to_entity_id"))
            if to_entity is None:
                return rejected("unknown recipient entity")
            with session.begin_nested():
                parcels.transfer_parcel(
                    session, intent.params.get("parcel_id", ""),
                    intent.entity_id, to_entity,
                )

        elif intent.intent_type == "say":
            # Speech (game.md 15.6): free but bounded. Identity is
            # structural -- the actor IS the entity, so spoofing is
            # impossible and the text carries no provenance to check.
            # Content is free-form: cheap talk, lies included -- that is
            # the experiment. The bounds are volume, not meaning: one
            # utterance per entity per tick, text capped. The event is
            # also the noise record: when distance lands, loud things
            # can key on it.
            text = intent.params.get("text", "")
            if not isinstance(text, str):
                return rejected("say text must be a string")
            text = text.strip()
            if not text:
                return rejected("say text is empty")
            if len(text) > SAY_TEXT_CAP:
                return rejected(f"say text exceeds {SAY_TEXT_CAP} characters")
            if said is not None and intent.entity_id in said:
                return rejected("one say per tick")
            event["params"] = {**intent.params, "text": text}
            if said is not None:
                said.add(intent.entity_id)

        else:
            return rejected(f"unknown intent type {intent.intent_type!r}")

    except ValueError as exc:
        # InsufficientFunds / CurrencyMismatch / NotMonetaryAuthority /
        # OperationVetoed / InsufficientHoldings / MarketInactive / bad amount
        outcome = rejected(str(exc))
        if isinstance(exc, markets.InsufficientHoldingsError):
            # The one rejection this tick's auction could still cure. Flagged by
            # exception type rather than left for the caller to pattern-match
            # out of `reason`, which is a human-readable string and not a
            # contract. run_tick uses this to retry the intent after clearing.
            outcome["short_of_holdings"] = True
        return outcome

    return {**event, "status": "applied", **extra}
