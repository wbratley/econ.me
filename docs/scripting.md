# Scripting libraries — the three tiers under every script

Status: *design — settled 2026-08-15 after the first live-world demo
surfaced the seam below. **Phases 1–3 are shipped** (§6): the
injection mechanism, the engine `std`, the `world` namespace +
`scripting.world_lib` wiring, the admin library surfaces, the demo world
migrated as the reference implementation — then the content-pack tier
(`pack`), the install-time validation gate, manifest version pinning,
and strictness on the dry-run path — and finally the same standard as
the default at *player submit* time (`set_behaviour` lint, §4) plus the
votable-world-lib analysis (§8). This note is the spec for the tiered
library model. It spans engine (the injection mechanism), content (world
libs and packs), and platform (join composition) — wherever each owns its
seam. Nothing here changes the mechanism/data/policy doctrine
(`design.md` §2); it applies it to script vocabulary.*

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
  cheap path `ctx` uses. Player-authored behaviours: the same strict
  standard at *submit* time (Phase 3) -- a script citing vocabulary
  that is not injected is refused with the finding in hand and the
  entity keeps its current behaviour; synthetic-ctx errors a healthy,
  state-dependent script can still produce come back as warnings, not
  refusals. One vocabulary source throughout (`get_world_libraries`):
  the install gate, the dry-run, and the player lint cannot drift from
  what the tick actually runs.

## 5. Settled decisions

1. **Determinism pinning — yes.** Script output feeds `events_hash`, the
   RNG entropy commitment. A world records the stdlib version and the
   world-lib source (it is a WorldSetting, so it is already state — but
   it must be treated as replay-input, not incidental config); content
   pack manifests declare the engine + lib versions they target, and a
   world running a pack refuses a mismatch. Replays check all of it.
2. **World-lib authoring — operator at world creation, for now.**
   Deliberately open: whether world-lib changes can become votable is
   analyzed in §8 (recommendation: additive-only, at the constitutional
   amendment bar, gated on an enactment-time compatibility sweep) but
   **not built** — the mechanism ships nothing that forecloses either
   answer.
3. **Migration — yes.** `experiments/world` moves from concatenation to
   namespaces as the reference implementation of the model (its three
   strata split into §2's three tiers); concatenation keeps working
   until every script has migrated, then is deleted.
4. **Engine-tier scope discipline — yes.** Small, boring, opinion-free;
   additions pass the boundary test in review. The stdlib is vocabulary,
   not strategy.
5. **Rival privacy — a world flag, not a rewrite.** `ctx.query.*` reads
   were global by default (right for public share registers, wrong for
   pantries). The cut that shipped: a world sets `world.private_holdings`
   and every entity-scoped script — `holding` of another entity returns
   nil, `holders` comes back empty — while op-context scripts
   (validators, hooks: the referee) keep the global read. Per-symbol
   votable visibility stays unbuilt (§8's analysis pattern applies).

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
2. **Phase 2 — the world tier + the gate — ✅ shipped.** The `pack`
   namespace (`scripting.pack_lib` WorldSetting; the play opinions left
   concatenation and are now an injected tier, so a starter's source is
   only its own logic); the install-time gate — `validate_library_source`
   (syntax / strict smoke-run / purity: members are functions or nested
   tables only, per the pure-Lua rule / a member sweep that calls each
   function under strict globals, best-effort by construction) and
   `validate_script_source` (strict smoke-run; used by the reference
   world's `_behaviour`, so NO pack script reaches a Script row ungated);
   the setters (`set_world_lib`/`set_pack_lib`) and the admin endpoints
   refuse broken sources with 400 (`LibraryRejected`); manifest version
   pinning — `experiments/world/pack.json` records the engine-stdlib
   fingerprint + a sha per lua/ file, `create_content` refuses drift
   (`PackManifestMismatch`), regeneration is the deliberate
   `python -m experiments.world.manifest`; the `/admin/scripting-tiers`
   report (fingerprints, shas, gate verdicts, `matches_pinned`); and the
   MCP `get_script_libraries` tool (authoring from scratch requires
   reading the tiers, not guessing). *As-built deviations from the note:*
   the lint is enforced by a strict-globals run (`run(strict_globals=...)`
   in the engine: undeclared-global reads/writes error, a post-run probe
   rejects reassigning injected names) rather than a separate linter; the
   platform dry-run endpoint now runs strict too, so it cannot bless what
   the gate rejects; the synthetic ctx carries the full no-op query
   surface (a synthetic ctx that lies about `ctx.query` vocabulary is the
   same bug as one that lies about tiers).
3. **Phase 3 — strictness + the open question — ✅ shipped.** The
   strictness lint hardened into the default for player-authored
   `set_behaviour`: `set_entity_behaviour` lints the source against the
   injected tiers BEFORE anything is retired or stored — a refusal
   leaves the entity's current behaviour untouched — with the
   fatal/warning split of §4 (`ScriptRejected` for syntax /
   undeclared-global reads+writes / tier reassignment; synthetic-ctx
   findings ride back as warnings on the accepted script: the REST
   `warnings` field, MCP `lint_warnings`). The join paths lint too (a
   broken starter fails join loudly rather than handing out zombies;
   operator content is pre-gated, so this never fires in practice).
   The votable-world-lib analysis is §8 — analysis only, deliberately:
   decision #2 stays deferred, and nothing built forecloses it.
   *Scope boundary:* the legislation path (`set_script`) is not linted
   — a polity legislating a nil-calling POLICY zombies its own
   machinery, which is the polity's reviewed choice with the dry-run
   endpoint at hand; extending the lint there changes governance
   behavior and is its own decision, taken when a world asks for it.

## 7. Migration detail (for Phase 1–2)

- `scenario._behaviour` concatenation → per-role scripts written against
  `std`/`world`; the prelude's three strata move to their tiers.
- The join-config starter becomes the editable remainder (roughly
  today's `starter.lua` minus everything the tiers now provide).
- `experiments/world` tests (the `test_world.py` survival proofs) run
  unchanged against the tiered world — they are the acceptance gate for
  the migration.

## 8. The open question: a votable world lib (Phase 3 analysis)

Settled decision #2 deferred this on purpose. This section is the
analysis that was owed; it recommends, but ships nothing — the mechanism
forecloses neither answer, and that is a feature until a world actually
needs to grow its vocabulary mid-game.

**What the world lib is, tier-wise.** The world lib sits *beneath* the
constitution, not inside it: VALIDATORs — the constitutional tier — are
written *using* `world.*` helpers. A world-lib edit is therefore an
implicit edit of every script that cites its helpers, including the
validators that gate law itself, without touching a single script's
source. That is the crux the tier question has to answer: an ordinary
proposal (simple majority, `legislate`) cannot amend a validator — the
proposal tiers exist precisely to prevent that — yet an ordinary-tier
world-lib edit would rewrite what the validators *mean*. The tier the
lib effectively occupies is constitutional, whatever surface edits it.

**Why not leave it at operator fiat forever (status quo).** The lib is
the world's API surface; play generates vocabulary needs the operator
cannot foresee (a clearing idiom, a new market's quoting helper).
Fiat-only means every world's vocabulary is frozen at the imagination of
one author at t=0. That is survivable and safe — it is the shipped
state — but it makes the lib the only rule-shaped thing a polity can
never touch.

**The zombie trap at world scale.** Phase 1–3 exist because a missing
helper is silent death: `settle_last_orders()` nil-calls, the script
errors every tick, the entity zombied. A votable *removal or rename*
reopens that trap for every dependent script at once — and Phase 3's
submit-time lint cannot catch it, because the submissions were legal
when made. Whatever the governance answer, one mechanical precondition
is non-negotiable: **an enactment-time compatibility sweep.** Before a
lib change can take effect, every active script is run through
`check_player_script` against the *new* lib; a change that would break
any dependent script is refused (or quarantines the change until scripts
migrate). The standard that guards one player's submit guards the
polity's amendment — same lint, same tiers, same choke point.

**Determinism (settled decision #1).** The lib is replay-input state.
Today's admin PUT writes it outside the event log — operator fiat is
replay-invisible, which is tolerable only because it is confined to
bootstrap. A votable change MUST ride the intent/enactment path like
every other mutation, and the enactment record must carry the lib's sha
before/after — a lib change is a new manifest epoch, not an edit in
place.

**The options, honestly:**

1. **Operator fiat forever.** Safe, static, boring. Vocabulary frozen
   at world creation.
2. **Ordinary law.** Rejected on the tier argument above: it lets a
   simple majority rewrite the meaning of constitutional validators by
   editing the helpers under them.
3. **Constitutional amendment, additive-only, sweep-gated.** Additions
   (new helpers; old signatures untouched) are votable at the amendment
   bar (`amend_constitution`, the supermajority floor in the
   `constitution` setting). Removals and renames are not offered as a
   vote at all — effectively they remain operator-only at world
   creation, because a removal that nothing depends on is rare and one
   that something depends on is exactly the mass-zombie case the sweep
   exists to stop. Additive-only bounds the blast radius mechanically:
   a new name cannot nil-call anyone.
4. **Lib changes only between worlds.** The strongest determinism
   story; the most static. Equivalent to (1) with extra ceremony.

**Recommendation: (3), when a world needs it.** It matches the tier the
lib occupies, it keeps replay honest, and its safety is mechanical
rather than procedural — additive-only plus the sweep means the trap
this whole document exists to close stays closed even when the polity
holds the pen. Until a real world pressures the seam, (1) ships today
and nothing above needs to exist as code.

Two adjacent notes. The `std` tier is engine mechanism — un-votable by
the doctrine table (`design.md` §2), pinned by fingerprint, versioned
with the engine. The `pack` tier is *content*, not rules: it is the
starter's inheritance, and players already own the right to keep, drop,
or replace every opinion in it by rewriting their behaviour — a polity
voting on the pack would be legislating taste, which autonomy already
answers script-by-script.
