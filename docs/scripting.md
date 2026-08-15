# Scripting libraries — the three tiers under every script

Status: *design — settled 2026-08-15 after the first live-world demo
surfaced the seam below. **Phase 1 is shipped** (§6): the injection
mechanism, the engine `std`, the `world` namespace + `scripting.world_lib`
wiring (tick, VALIDATOR/HOOK dispatch, and the dry-run endpoint all inject
through one accessor), the admin `GET/PUT/DELETE /admin/world-lib` surface,
and the demo world migrated as the reference implementation. This note is
the spec for the tiered library model. It spans engine (the injection
mechanism), content (world libs and packs), and platform (join composition)
— wherever each owns its seam. Nothing here changes the mechanism/data/
policy doctrine (`design.md` §2); it applies it to script vocabulary.*

## 1. The seam this closes

The engine's script contract is: **a script is one self-contained source
string.** The sandbox nils `require`/`dofile`/`loadfile`/`load`
(`lua_engine._SANDBOX_BLACKLIST` — untrusted code gets no load surface),
and each tick constructs a fresh `LuaRuntime`, so nothing persists except
`ctx.state`. The only injected API is `ctx`: data
(`ctx.holdings`, `ctx.needs`, …), read-only queries (`ctx.query.*`), and
intent queueing (`ctx.action.*`). Deliberate, and correct.

Worlds therefore ship their own helper library as **text**: the
`experiments/world` convention prepends `lua/prelude.lua` verbatim to each
role script (`scenario._behaviour`). The join path, meanwhile, stores
`join.config.starter_behaviour` **verbatim** and applies it as the
founder's entire script. Each side honours its own contract; nothing owns
the seam between them. The obvious operator move — put `starter.lua` in
the join config — produces founders whose every tick throws
`script_error: attempt to call a nil value (global 'settle_last_orders')`.

The failure mode is quiet and compounding (observed in the demo): a
script error records the event and skips the entity — no intents, no
alert. The founder farms nothing, sells nothing, buys no food; HUNGER
accumulates. With this world's tuning (grant 1, decay 0.05 → equilibrium
≈20 vs the kill threshold 30) the dynasty never even dies — a permanently
hungry zombie, uneliminated and unrejoinable. A tighter threshold would
eliminate every player at join time. The only traces are one counter in
`RoundSummary.events_by_type` and an error message naming a function that
exists nowhere the player can look.

The fix is structural, not a docs note: give scripts a real library model.

## 2. The tier model

Three tiers, one boundary test: **does it encode an opinion about how to
play?** No → engine or world tier. Yes → content pack. A script's source
is only ever its own logic; vocabulary arrives from the tiers beneath it.

| tier | ships with | scope | v0 contents (from the current prelude) |
|---|---|---|---|
| **engine stdlib** (`std`) | the engine + its test suite | pure functions over injected data; zero opinions; no world knowledge | `holding_qty`, `market_price`, `has_unlock`, `need_by_code`, `running_recipe`, `facility_parcel`, `deposit_parcel`, `amount_str` (+ thin `math`/`string` conveniences) |
| **world lib** (`world`) | the world, at creation | engine idioms shared by every script in *this* world; no play opinions | `settle_last_orders` — the GTC order lifecycle (cancel last tick's generation, read fills from `place_order`/`trade` events); the duration-1 process timing |
| **content pack** | the pack manifest | role behaviours, starters, anything with an opinion | `concede`, `sell_surplus`, `buy_food` (reservation-and-adapt pricing, clamps 0.3–3.0, steps −0.15/+0.03, hardcoded GRAIN, 3× bid cap, order priorities) |

Notes on the model:

- The strata are real: the current prelude splits along exactly this
  line. `buy_food` bids 3× and keeps a pantry of 3 — that is a strategy
  wearing a function name; it must never seep into `std`.
- **The engine tier stays small and boring.** Its failure mode is
  accreting policy one "handy" function at a time; the opinion test is
  the review bar for every addition.
- Content packs are the unit of content management going forward
  (bundles of goods/tech/recipes/cast/scripts); their manifest (§5) pins
  versions. Pack scripts compose over `std` + `world` like any other.

## 3. Injection, not concatenation

Tiers are injected as **namespace tables** the engine sets up before the
sandbox is applied — the same mechanism `ctx` already uses. A script sees:

```lua
local fills  = world.settle_last_orders()
local price  = std.market_price("GRAIN", 1.0)
ctx.action.start_process("FARM_GRAIN", std.facility_parcel("FARM"), 20)
```

- **`get_behaviour` shows only the player's own source.** Today a
  founder reads 183 lines of which 154 are library; the fetch→edit→set
  loop becomes real. Same for agent-authored behaviours: writing from
  scratch against `std`/`world` docs is possible; nil-calling a phantom
  prelude is not.
- **Honest error line numbers** — the player's line 10 is their line 10,
  not offset by the prelude length.
- **Read-only namespaces**: the injection installs read-only metatables
  at the same moment the sandbox nils `setmetatable` for scripts; a
  script assigning `std = {}` clobbers only its own view (and each tick
  is a fresh runtime anyway, so no pollution).
- **No shadowing hazards**: tier names are engine-set globals, never
  locals the script could capture.

Wiring (engine, unchanged in doctrine — `lua_engine` stays pure):

1. `std` ships with the engine as a constant source (or precompiled
   chunk); `lua_engine.run()` gains an optional `libraries` argument and
   executes each tier into its namespace table in the natural insertion
   point that already exists: construct runtime → install timeout hook →
   **inject `std`/`world`** → apply sandbox → run the script.
2. The **world lib** is a `WorldSetting` (`scripting.world_lib`) written
   once at world bootstrap; `run_tick` (which owns the session) reads it
   per tick and passes it down. `lua_engine` never touches the DB.
3. The **join path** stores only the player-editable part in
   `join.config.starter_behaviour`; the platform no longer needs to know
   preludes exist at all. The gap in §1 dissolves rather than getting
   patched.

## 4. Safety model

Two axes — environment hardening (mostly exists) and library provenance
(new).

**Already in place** (the reason tiers are safe to inject at all):
fresh `LuaRuntime` per tick; module system and `io`/`os`/`debug` nil'd;
16MB memory cap; in-VM instruction watchdog enforcing `timeout_ms`;
query callables wrapped and inerted the moment `run()` returns (an
abandoned script cannot reach the session); and the deep one — scripts
only ever *request*. Every intent is validated and applied by Python
with the money-scope invariant, so no Lua can spend money its entity
does not have, whatever it calls.

**What the tier model adds**:

- **One choke point.** `lua_engine` injects exactly `ctx`, `std`,
  `world` and nothing else; scripts cannot add tiers or reach a tier
  that was not injected.
- **The pure-Lua rule.** Engine-stdlib members are pure Lua functions
  over `ctx` — never Python-backed callables. A library member that
  wraps a session handle would be a hole the script sandbox cannot see;
  pure Lua makes the class of bug structurally impossible. (If a future
  stdlib member ever needs Python, it goes through the same
  wrapping-and-inerting path as `ctx.query`, with that stated in review.)
- **Provenance gradient.** Engine stdlib: reviewed with the engine
  suite, unconditional. World lib and pack scripts: strict at *install
  time* — syntax check (compile, don't execute), a smoke-run against a
  synthetic `ctx`, and a strictness lint (reject assignment to
  `ctx`/`std`/`world`, reject undeclared globals). Broken or hostile
  content is refused before any player runs on it. Install-time cost is
  irrelevant; per-tick cost stays zero because injection is the same
  cheap path `ctx` uses.

## 5. Settled decisions

1. **Determinism pinning — yes.** Script output feeds `events_hash`, the
   RNG entropy commitment. A world records the stdlib version and the
   world-lib source (it is a WorldSetting, so it is already state — but
   it must be treated as replay-input, not incidental config); content
   pack manifests declare the engine + lib versions they target, and a
   world running a pack refuses a mismatch. Replays check all of it.
2. **World-lib authoring — operator at world creation, for now.**
   Deliberately open: whether world-lib changes can become votable (they
   are effectively rules-of-scripting changes mid-game, adjacent to the
   constitution's validator tier) is deferred, not decided. Nothing in
   the mechanism forecloses either answer.
3. **Migration — yes.** `experiments/world` moves from concatenation to
   namespaces as the reference implementation of the model (its three
   strata split into §2's three tiers); concatenation keeps working
   until every script has migrated, then is deleted.
4. **Engine-tier scope discipline — yes.** Small, boring, opinion-free;
   additions pass the boundary test in review. The stdlib is vocabulary,
   not strategy.

## 6. Phasing

1. **Phase 1 — the mechanism + the stdlib + the join fix — ✅ shipped.**
   `libraries` injection in `lua_engine` (namespaces frozen read-only
   pre-sandbox); engine stdlib (`std`); `scripting.world_lib` WorldSetting
   + wiring through one accessor (`get_world_libraries`) used by `run_tick`,
   the VALIDATOR/HOOK dispatch, and the `/admin/scripts/{id}/validate`
   dry-run; admin `GET/PUT/DELETE /admin/world-lib`; `experiments/world`
   migrated as the reference implementation (survival tests unchanged and
   green). Join needs no code change: a starter stored WITHOUT library
   text now works, because vocabulary arrives from the tiers at run time
   — the §1 gap is closed. *As-built deviation:* the pack stratum
   (`concede`/`sell_surplus`/`buy_food`) still rides in script source,
   concatenated by the scenario — deliberately, since the pack namespace
   is Phase 2's; the helpers being visible in `get_behaviour` is the point.
2. **Phase 2 — the world tier + the gate**: `world` namespace populated
   from `scripting.world_lib`; install-time validation (syntax,
   smoke-run, lint) for world lib and pack scripts; pack manifest
   version pinning.
3. **Phase 3 — strictness + the open question**: the strictness lint
   hardened into the default for player-authored `set_behaviour`
   (catches nil-call class errors at *submit* time, closing the same
   trap one level down); the votable-world-lib design note.

## 7. Migration detail (for Phase 1–2)

- `scenario._behaviour` concatenation → per-role scripts written against
  `std`/`world`; the prelude's three strata move to their tiers.
- The join-config starter becomes the editable remainder (roughly
  today's `starter.lua` minus everything the tiers now provide).
- `experiments/world` tests (the `test_world.py` survival proofs) run
  unchanged against the tiered world — they are the acceptance gate for
  the migration.
