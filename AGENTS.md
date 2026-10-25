# AGENTS.md — navigation guide for econ.me

Compact orientation for agents working in this repo. Distilled from `docs/`
(~66K tokens — too big to hold in context at once; read the real docs in
slices for depth). This file tells you where things are and what is true.

## What this is

A deterministic **economy-simulation engine** plus a **FastAPI platform**
around it. A world is a set of persistent *entities* — individuals, firms,
banks, governments — that act once per **tick**. Every privileged action is
gated by the engine (capabilities), "laws" are **Lua scripts** owned by
entities and changeable only through governed votes, and the product goal is
a continuously-running world operated by AI agents (a "game" — see
`docs/game.md`).

### The governing principle (design.md §2)

**Mechanism / data / policy split.** The engine ships *mechanism* (primitives
that move money, goods, parcels, and power). Worlds declare *data* (Good,
Need, Recipe, Market, WorldSetting rows). *Policy* is Lua scripts that decide
what to do each tick. Anything that is "really just data + a script" is
deliberately NOT an engine feature (no `Contract` model, no `Tax` model, no
`Age` holding — see the rationale in `docs/actors.md` steps 5–6).

## Repo map

```
engine/econengine/   The engine package. Pure Python, no HTTP.
econ/                FastAPI platform layer (world server over one SQLite db).
  econ/api/main.py       App entry (round clock, heartbeats, world clock).
  econ/api/routers/      The current HTTP surface: intents, entities, goods,
                         markets, production, proposals, governance, scripts,
                         parcels, places, needs, tech, ticks, epochs,
                         leaderboard, catalog, admin, auth, join, mcp,
                         activity, transactions, rounds, world.
  econ/world/            World-layer projection/runtime: tile2d chunks,
                         scene assembly/deltas, W2 in-memory presence seats.
  (flat econ/api/*.py modules predate routers/ — routers/ is what the docs
   reference; treat flat files as legacy unless proven otherwise.)
  econ/db.py               SQLAlchemy engine/session setup.
web/                  W1 reference spectator client (TypeScript/Vite/Canvas;
                      generated map, camera/follow, public speech; no writes).
alembic/               Migrations.
contracts/             Reference financial instruments as data + Lua:
                         bond/, bank/, loan/, futures/, option/, insurance/.
                         Each = setup .py + behaviour .lua + a cap VALIDATOR
                         + tests/test_contract_*.py.
experiments/           Runnable worlds that prove roadmap steps:
                         world/ (stone_age), agent/ (multi-agent runs),
                         inequality/, lifecycle/ (age policy),
                         population/ (spawn), generations/ (death+inheritance).
tests/                 pytest. test_api_* = platform, test_contract_* =
                         contracts, rest = engine.
docs/                  The documentation (see index at bottom).
run.sh                 Convenience launcher (multi-agent stone-age run).
```

### Engine modules that matter (`engine/econengine/`)

| File | What it is |
|---|---|
| `tick.py` | The tick loop (`run_tick`), script ctx assembly, pass ordering. |
| `scripting.py` | `resolve_intent` — the single safe action surface; `build_queries` (ctx.query) and actions. |
| `lua_engine.py` | Sandboxed Lua runtime for scripts. |
| `capabilities.py` | Capability vocabulary (`monetary_authority`, `levy`, `set_fiscal_policy`, `legislate`, `amend_constitution`, `seize`, `grant_capability`, `spawn`) and the `INTENT_CAPABILITIES` registry. |
| `services.py` | Engine ops: `levy`, `seize`, `issue_money`, `retire_money`, `set_script`, `create_entity`, `spawn_entity`, estate transfer, … |
| `conditions.py` | Needs-accrual → conditions → incapacity pass; the **estate rule** (asset transfer on incapacity). |
| `needs.py` | The consumption/satisfaction pass. |
| `production.py` | Recipes/processes (production is the only way goods appear). |
| `markets.py` | Orders/matching. |
| `fiscal.py`, `tech.py`, `combat.py`, `threats.py` | Fiscal policy, tech tree, military/danger. |
| `places.py`, `parcels.py`, `travel.py` | Spatial layer (see `docs/spatial.md`). |
| `weights.py`, `councils.py`, `delegations.py`, `constitution.py` | Governance: vote-weight resolvers, council registers, liquid-democracy delegations, the constitutional floor. |
| `clock.py`, `rng.py`, `edges.py`, `goods.py`, `catalog.py`, `describe.py`, `witness.py`, `observer`… | Support: world clock, determinism RNG, graph edges, good helpers, world-readable catalog/audit. |
| `models/` | SQLAlchemy models: `entity`, `account`, `good`, `holding`, `market`, `order`, `process`, `parcel`, `place`, `need`, `intent`, `observer`, … |

## Core concepts (the vocabulary you need)

- **Tick** — one step of the world. Scripts run per entity per tick; a compute
  budget can skip scripts (so schedules must be *skip-safe*, using `ctx.tick`
  rather than a self-counted counter).
- **Entity** — `INDIVIDUAL | BUSINESS | BANK | GOVERNMENT` (enum on
  `Entity.type`). `owner_id` links to a platform user; `capabilities` is a
  JSON set; `is_admin` is god mode.
- **Intent** — a proposed action (transfer, order, set_fiscal_policy, vote,
  levy, …). The ONLY way anything moves. `POST /intents` (platform) and the
  tick loop both funnel into `scripting.resolve_intent`, so there is one set
  of rules. **Ownership invariant**: an intent moves assets only out of
  accounts its entity owns — except declared, capability-gated mechanisms
  (levy/seize/estate/spawn) that bypass it for the authority only.
- **Scripts** — Lua, four kinds:
  - `BEHAVIOUR` — what an entity does each tick.
  - `POLICY` — world-level rules; runs with a government/authority context;
    sees every entity's events.
  - `VALIDATOR` — pure, fail-closed veto over money ops (the "constitution").
  - `HOOK` — fires after ops; may queue intents; never recursive.
  Scripts read via `ctx.query.*`, act via `ctx.action.*`, persist in
  `ctx.state`. They **cannot** adjust holdings — the only way goods/
  attributes/conditions change is declared engine passes (production,
  consumption, decay, estate, levy/seize).
- **WorldSetting** — a votable key/value row; how policy *parameters*
  (tax rates, caps, estate rule, constitution floor) are stored and tuned.
- **Capabilities** — checked at the intent boundary AND in the service
  (fail-closed). `grant_capability`/`revoke_capability` are the meta-power
  (gated by `GRANT_CAPABILITY`, constitutional tier).
- **Governance** — `Proposal → vote → enact`. Weight models: citizen,
  share (cap table), council, weighted (representative), liquid. Enactment is
  atomic and reuses `resolve_intent`, so capability gates and validators fire
  on enacted law exactly as on live intents. `set_script` retires the old
  script and activates a new one (auditable lineage, `lineage_id`).
  Constitutional tier (supermajority, `constitution.py`) is the only path to
  VALIDATORs.
- **Financial instruments** — data + Lua, never engine models. Convention:
  a traded fungible claim is a `Good`+`Holding` ("bonds-as-goods"); a
  non-traded claim lives in a script's `state` book; cross-script signals are
  `WorldSetting`s (oracles). `ctx.tick` is the clock for maturities.
- **Embodiment** — hunger/thirst/fatigue are Needs that accrue Conditions
  (Goods with `modifies` + `incapacitates_at` properties); skills/attributes
  are bare Holdings; `age` is derived (`birth_tick`); death reuses the estate
  rule and the `entity_incapacitated` event (insurance fires on any death);
  `lifespan` (NULL = immortal) enables old-age death; `spawn_entity`
  (capability `SPAWN`) creates children with immutable `parents` provenance;
  server hard caps via `ECON_MAX_ACTIVE_ENTITIES` / `ECON_MAX_ENTITIES` /
  `ECON_MAX_ENTITIES_PER_OWNER` env vars.

## Engine architecture (docs/design.md)

- **Vision**: one reusable engine, three products: **econ.me** (this app —
  the proving ground), a **sandbox platform** ("democratic Roblox":
  navigable worlds, player-governed rules, forking), and **economic
  modelling software** (seed a genesis, run ticks, measure). The engine
  must stay an importable module with no HTTP/auth knowledge.
- **Engine invariants** (never votable): **conservation** (money only via
  monetary-authority ops; goods only via declared recipe I/O, grants,
  perish rules), **ownership**, **determinism** (explicit ordering:
  priority, price-time, symbol-asc), **bounded computation** (sandboxed
  Lua + timeout + per-entity compute budget), **unpaced** (a tick is a
  function call; durations in ticks, never wall-clock).
- **Extraction is live**: `engine/econengine` is its own distribution
  (SQLAlchemy + lupa only), session-in only (never creates engines/sessions;
  `models/base.py` defines just `Base`). `econ/` owns DB config (`econ/db.py`)
  + Alembic. One-way rule: **econengine never imports econ**.
  Dev: `pip install -e ./engine`.
- **Built primitives** (build order, all shipped unless noted):
  ledger → scripting → tick → markets → recipes/processes (requirements +
  *reservation*; "better machinery" = recipe *variants*, not modifiers;
  waste is just an output) → tech tree (Technology DAG; scope is a
  per-Technology column `entity`/`world`; unlocks never revoked) →
  perishability + auto-issue (labor market: LABOR auto-issued as a
  *top-up*, before scripts; decay after the auction) → skills (holdings;
  `LABOR-SMITH` skilled-labor symbol; learning = byproduct output;
  decay = atrophy; schools are an industry) → stochastic recipes (branch
  tables + events-hash commit-reveal RNG; catalyst inputs) → needs +
  consumption pass → conditions (deficit→condition→effect; conditions are
  Goods with `modifies` + `incapacitates_at`; incapacity pass + estate
  rule) → parcels/facilities/deposits (land is a first-class primitive;
  facility access/capacity/fuel; deposits deplete + regenerate) →
  per-entity compute budget (votable `entity_tick_compute_budget_ms`) →
  machine intent API (`POST /intents`, batch, same resolver as the tick).
- **Designed, not built**: *military/contests* (ownership transfer by
  force — contest declared at tick N resolves N+1, same commit-reveal;
  platform-era) and *contracts* (recurring obligations: employment, rent,
  loans in one mechanism).
- **Platform layer** (consumer of engine, not in it): worlds + governance
  cycle (proposal → vote → enactment day; trials = COW test forks),
  forking (petition with binding pledges; forks copy, never split),
  identity platform-scoped vs property world-scoped, content packs
  (§4.6 → game.md §15), world layer = a *separate* real-time simulator —
  the engine knows at most "entity E is at place P"; positions never
  enter the engine (2D first; Luanti/Godot are later geometry/client
  options).

## The game (docs/game.md)

The product: a **continuously-running, batched economy played by AI
agents** (REST/MCP). Each player owns a **dynasty** of entities and
rewrites their own BEHAVIOUR scripts between **rounds**; the world resolves
K ticks per round; a **governance window** opens every N rounds; each
**epoch** carries one declared victory condition. The point is to watch
what strategies emerge.

- **Round** = submit window (blind edits/votes) → resolve K ticks →
  publish. **Readiness gate**: a round closes when every eligible player
  (owns ≥1 ACTIVE entity) signals `POST /rounds/ready` (MCP `set_ready`);
  the final ready resolves in-request. Operator `POST /admin/rounds/advance`
  is always the override; `ECON_ROUND_DEADLINE_S` is the AFK backstop;
  `ECON_WORLD_TICK_SECONDS` arms a wall-clock heartbeat instead (rounds
  become accounting boundaries at multiples of K; the extinction brake
  stops a dead world). Env: `ECON_TICKS_PER_ROUND`, `ECON_ROUNDS_PER_WINDOW`.
- **Control tiers** (who may change a BEHAVIOUR script):
  player-owned → *autonomy* (`services.set_entity_behaviour` +
  `POST /entities/{id}/behaviour`; ownership-gated; BEHAVIOUR only; refuses
  `Entity.is_fixed`); polity-owned → *legislation* (`set_script` →
  proposal → vote → enact); server-owned-and-fixed → *immutable*
  (`Entity.is_fixed`, set by the operator; both governed paths refuse it).
  The money-scope invariant is unaffected: a script only ever spends its
  own entity's money.
- **Victory** = achievement, not vote. The spec lives in the
  `epoch.state` WorldSetting (set once at epoch start); a platform
  observer runs per-tick inside round resolution and stamps the
  append-only `victory.stamps` / `epoch.eliminations` registers — no
  intent, vote, or script can write them (*immutability by surface
  absence*). Menu: `accumulate`, `innovate`, `endure`, `grow`, `rule`.
- **Dynasty**: `owner_id` propagates through `spawn_entity`; children are
  born with an *empty* capability list (privileges never breed);
  elimination = no ACTIVE entity with that owner; rejoin only next epoch
  (the join path checks `epoch.eliminations`).
- **Content pack** (Phase 0) = the **stone-age world**:
  `experiments/world/stone_age.py` (data + Lua), proving campaign in
  `experiments/agent/` (LLM dynasty loop). Parity doctrine: an agent sees
  exactly what its own BEHAVIOUR script sees (own events + public facts).
- **Readable world** (Phase 3): `name`/`description` columns on all content
  models; `GET /catalog` + MCP `world_catalog` (catalog text is folded into
  agent system prompts); `engine/econengine/describe.py` = the *action
  registry* — renders every intent/event into a sentence, total by
  construction; audit trail `GET /entities/{id}/activity` (+ world
  `/activity`, dashboard world log); `pack.json` pack envelope with
  `pack_id` provenance on every content row.
- **Speech** (Phase 3, v1): `say` intent (free, bounded: 1/tick, 256
  chars); *witness delivery* — who perceived what is derived data frozen
  into `event_observers` at emission; broadcast of the observable
  vocabulary (`say`, `entity_incapacitated`); "three ears":
  `ctx.events`, the round digest, the dashboard.
- **MCP player interface**: `econ/api/mcp_tools.py` + `econ/api/routers/mcp.py`
  (round_state, market_prices, epoch_state, leaderboard, entity_activity,
  world_catalog, set_ready, set_behaviour, …).

## Spatial layer (docs/spatial.md)

Doctrine: **topology in the engine, geometry outside it.** The engine stores
named **places** (opaque keys, like `extent_ref`) and weighted, mode-tagged
**edges** between them — never coordinates. Whether a map was drawn by hand
or projected from a grid/voxels is a pack-authoring fact.

- **Models**: `models/place.py` (places: unique `key`, `name`, `kind` tag,
  `region_id`, nullable `extent_ref`), `spatial_edges` (from/to, `mode`,
  `cost_ticks`, `bidirectional`), `entities.location_place_id`,
  `parcels.place_id`, `travel_routes` (frozen hop list per journey).
  `parcels.move_entity` is the single writer of location.
- **Status — S1–S4 shipped, S5 is the entry point**:
  - **S1 places + location**: `ctx.place`/`ctx.places`, `world.places()`/
    `world.place(key)`; spawn templates may take a `place`.
  - **S2 presence gates**: `Recipe.requires_place_kind` / `requires_place_key`,
    `Market.place_id` (NULL = global market = today's default) — checked in
    the ordinary `start_process`/`place_order` requirement pass; refusals
    name where you stand.
  - **S3 travel**: `ctx.action.travel(to, modes?)` — the **engine routes**
    (Dijkstra over mode-allowed edges) and the journey runs as **one
    PROCESS PER HOP** against the pack's `TRAVEL_{MODE}` template, so
    vehicles/mounts are recipe requirements, not mechanism. Stranding is a
    real state (`travel_stranded`). Read-only: `world.route`/
    `world.distance_ticks`.
  - **S4 located danger**: `Threat.place_id` (NULL = ambient everywhere);
    combat requires co-location when both fighters are placed; stone_age
    authors the map (6 places, 9 walk roads, work gated to places, markets
    at the post).
  - **S5 (next)**: local markets made real — carrying capacity per travel
    mode + fulfillment presence (orders fill only from goods present at the
    market's place; sellers must deliver).
  - **S6 (platform-era)**: the world-layer boundary via `extent_ref`;
    real-time 2D first, with spectators from W1 (`docs/world-layer.md`).
- **Explicitly unbuilt**: coordinates in the engine (permanent), per-entity
  pathfinding policy, fog of war, convoys.

## Scripting (docs/scripting.md + scripting-api.md)

- **Three tiers, injected as read-only namespaces** (never concatenated)
  into every script: **`std`** (engine-wide pure-Lua helpers, pinned by
  fingerprint), **`world`** (per-world WorldSetting
  `scripting.world_lib` — engine idioms, no play opinions; admin
  `GET/PUT/DELETE /admin/world-lib`), **`pack`** (WorldSetting
  `scripting.pack_lib` — role behaviour, the play opinions). Boundary test:
  *does it encode an opinion about how to play?* A script's source is only
  ever its own logic; `get_behaviour` shows only player source; line numbers
  are honest. MCP `get_script_libraries` returns the sources.
- **Safety**: fresh sandboxed `LuaRuntime` per tick (`lua_engine.py`;
  module system/`io`/`os`/`debug` nil'd, 16MB cap, instruction watchdog,
  queries wrapped + inerted after the run); **pure-Lua rule** — std members
  never wrap Python; **crash-revert** — a BEHAVIOUR erroring 3 ticks in a
  row is deactivated and its lineage ancestor re-activated; scripts only
  *request* intents (the money-scope invariant still binds). Broken
  content is refused **before** it runs: install gate
  (`validate_library_source` / `validate_script_source`, strict-globals
  smoke-run) + manifest pinning (`experiments/world/pack.json` fingerprint
  + sha per lua file; `python -m experiments.world.manifest` regenerates;
  drift → `PackManifestMismatch`) + `/admin/scripting-tiers` report.
  **Submit-time lint** on `set_entity_behaviour` and join: fatal findings
  (syntax, undeclared globals, tier reassignment, the two-places shape
  trap) → `ScriptRejected`, current behaviour untouched; non-fatals come
  back as `warnings` / MCP `lint_warnings`. MCP `dry_run_behaviour` runs
  the same gate without installing (bit-identical verdict; per-state
  smoke matrix day/night × hearth/thicket × stocked/empty).
  `world.private_holdings` WorldSetting: entity scripts see only their own
  holdings (`holding`/`holders` scoped); op-context scripts keep global.
- **Where the truth lives**: ctx fields — `tick.py::_build_script_ctx`;
  actions — `lua_engine.py` (`action_tbl`); queries —
  `scripting.py::build_queries`; std source — `lua_engine.py::_STDLIB_LUA`.
- **Conventions that bite** (full reference: docs/scripting-api.md):
  - Exact decimals are **strings** (`"2.0000"`): `tonumber()` in,
    `std.amount_str()` out — string arithmetic is a per-tick error.
  - **Keys are strings, facts are tables**: `ctx.entity.place` is the key
    string (or nil) — `.key` on it is ALWAYS nil; `ctx.place` is the facts
    table; `ctx.places` is the array of all facts.
  - Nil-ability is a documented shape (no clock → `ctx.clock` nil, never an
    error; no map → `ctx.place` nil); books can be bare (use
    `std.best_ask(sym, fallback)`).
  - `ctx.state` persists across runs; `local`s do not. `ctx.op` exists only
    in VALIDATOR/HOOK scripts.
- **`ctx.action.*`** (queue intents — resolved after all scripts return;
  outcomes land in next tick's `ctx.events` as your debugging loop):
  survival set `start_process`, `cancel_process`, `travel`, `place_order`,
  `cancel_order`, `transfer`, `attack` (nil target = prowl), `say`,
  `transfer_parcel`; governance set `issue_money`, `retire_money`, `levy`,
  `seize`, `set_fiscal_policy`, `set_script`, `set_validator`,
  `set_constitution`, `grant_capability`, `revoke_capability`,
  `create_proposal`, `vote`, `enact`, `spawn_entity`. Extra args are refused
  at the call site with the arity in the message.
- **`ctx.query.*`** (read-only, exact-decimal strings or nil):
  `balance`, `total_supply`, `market_price`, `best_bid`, `best_ask`,
  `holding`, `unreserved`, `has_unlock`, `holders`, `age`, `lifespan`,
  `population`, `parents`/`children`, `route`, `distance_ticks`,
  `public_facilities`, `conditions`, `carriers`, `world_setting`,
  `fiscal_policy`, `constitution`, `active_script`, `script_history`,
  `proposal`/`proposals`, `tally`.
- **`ctx.events`** — last tick's events for YOUR entity (+ delivered
  speech). Rejected intents carry the `reason` — reading them is the debug
  loop. Key types: `start_process`, `process_completed/failed`,
  `need_satisfied`, `place_order`, `trade` (one per side), `travel*`,
  `combat`, `entity_incapacitated`, `decay`, `facility_*`, `script_error`.

## Running things

- Tests: `pytest tests/` (engine + platform + contracts); the web client
  checks with `cd web && npm run build && npm run test:browser`.
- Server: the app is `econ.api.main:app` (FastAPI, one SQLite world db);
  `run.sh` launches a multi-agent stone-age experiment instead. For a
  browser-visible run, add `--world-tick-seconds 15 --browser-user "Name"`
  to the `nim_run` command, then point the client proxy at the run's world
  port: `cd web && ECON_API_PROXY_TARGET=http://127.0.0.1:<port> npm run dev`.
  Paste the printed browser token at `http://localhost:5173`; Join uses
  `POST /join`, Possess uses the W2 stream. See `experiments/agent/README.md`.
- Experiments: each is a package with a `run` module, e.g.
  `python -m experiments.lifecycle.run`; see `experiments/<name>/` for its
  proving world + tests.

## Docs index (read for depth, in this order)

1. `docs/design.md` — vision + mechanism/data/policy + what's built.
2. `docs/game.md` — the product: continuously-running world, control model,
   rounds, victory, content packs (stone age).
3. `docs/actors.md` — multi-actor control roadmap; all six steps are DONE
   (capabilities → levy → policy actor → governance → financial substrate →
   embodied lifecycle). Long; the Status section is a build checklist.
4. `docs/spatial.md` — places/travel/location-based economy (steps S1–S6).
5. `docs/world-layer.md` — real-time 2D world, multiple clients/coordinate
   systems, generated chunks, AI/human actors, spectators, possession, and
   local walk/talk (W1–W4).
6. `docs/scripting.md` — the three script tiers and the safety model.
7. `docs/scripting-api.md` — full `ctx` reference + failure catalogue.
