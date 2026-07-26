# Design: the economy engine and what gets built on it

*Status: draft for discussion — 2026-07-25*

## 1. Vision

One reusable **economy engine**, consumed by (at least) three products:

1. **econ.me** — the current FastAPI app: entities, ledgers, monetary
   authorities, sandboxed Lua scripts, commodity markets. It doubles as the
   engine's proving ground.
2. **The sandbox platform** ("democratic Roblox") — a *navigable* social
   sandbox: players walk around a real world, farm, build, and trade, and
   each world's rules are decided by its players through periodic votes,
   with worlds able to fork into new ones. Different games emerge from
   governance rather than from a single developer.
3. **Economic modelling software** — a general simulator for running
   economic experiments: seed a genesis state, script agent behaviour and
   policy, run ticks, measure outcomes.

The engine must therefore stay an importable Python module with no knowledge
of HTTP, auth, or any particular product's rules.

## 2. Architecture principle: mechanism / data / policy

Every system in the engine is split three ways. This is the proven pattern
from heavily-moddable game engines, and it is also exactly the platform's
three rule layers:

| Layer | Lives in | Example | Platform mapping |
|---|---|---|---|
| **Mechanism** | Engine code | Double-entry transfer; the call auction; recipe execution | Un-votable engine invariants |
| **Data** | DB rows / genesis config | Which markets exist; recipe definitions; need weights; the tech DAG | Votable parameters |
| **Policy** | Sandboxed Lua scripts | Central-bank behaviour; NPC bidding; validators; vote-enacted rules | Sandboxed scripting |

The engine's invariants are the things no vote or script may break:

- **Conservation** — money is created/destroyed only by monetary-authority
  operations; goods are created/destroyed only by declared recipe inputs and
  outputs, admin grants, and declared perish/consumption rules.
- **Ownership** — an intent can only move money or goods out of accounts and
  holdings owned by the entity that queued it.
- **Determinism** — given the same state and scripts, a tick produces the
  same result. All ordering is explicit (priority, price-time, symbol-asc).
- **Bounded computation** — scripts run in a capability-limited Lua VM with
  in-VM timeout enforcement and a memory cap; per-entity compute budgets are
  the platform-level extension of the same idea.
- **Unpaced** — a tick is a function call; the engine never waits on
  wall-clock time and measures every duration in ticks, never seconds.
  A real-time world paces ticks externally (e.g. one per hour); the
  modelling product fast-forwards at as many ticks per second as hardware
  allows. Both must always be possible against the same engine. (This is
  true today — `run_tick(session)` — and must stay true; script timeouts
  are the one wall-clock exception, as a compute bound rather than
  simulation time.)

Everything content-like — what a WHEAT is, what technologies exist, what a
person needs to eat — is data, editable by admins today and by votes on the
platform.

## 3. Engine core

### 3.1 Built (Steps 1–6)

- **Ledger** — entities, accounts, double-entry transactions, currency and
  balance invariants, monetary-authority issue/retire.
- **Scripting** — sandboxed Lua (lupa) with `ctx.query.*` (read-only) and
  `ctx.action.*` (queues intents resolved centrally after all scripts run).
  Four script roles: BEHAVIOUR (per-entity, per-tick), POLICY (per-tick,
  sees all events), VALIDATOR (pre-operation veto, fail-closed), HOOK
  (post-operation, non-recursive).
- **Tick engine** — the simulation heartbeat: run policies, run behaviours,
  resolve intents by priority in savepoints, clear markets, record every
  outcome as events that feed the next tick's `ctx.events`.
- **Commodity markets** — holdings, GTC limit orders, per-tick uniform-price
  call auction with live settlement checks (no escrow) and deterministic
  clearing-price selection.

### 3.2 Planned primitives

These four mechanisms complete the production side of the economy. All
content they operate on is data.

#### Recipes and processes (production)

A **Recipe** is a data row describing a transformation. Its full
vocabulary is three lists:

- **inputs** — quantities of holdings consumed at start
- **requirements** — things that must be *present but are not consumed*:
  held goods (machinery, tools), a facility on a controlled parcel
  (§ parcels), unlocks (§ tech)
- **outputs** — quantities of holdings credited at completion (possibly
  none — see waste below)

plus a duration in ticks. A `start_process` intent atomically consumes the
inputs and creates a **Process** (work-in-progress) row; the tick engine
completes it `duration` ticks later and credits the outputs, emitting
`process_started` / `process_completed` events. Cancellation policy
(refund or forfeit inputs) is a votable parameter.

The **manufacturing tree is not code** — it emerges from which recipes
exist, the same way the market list emerges from Market rows. Conservation
is the engine invariant: a recipe transforms exactly what it declares.

*Status: inputs/outputs/duration shipped (Step 7); requirements land with
parcels & facilities, which need the identical check.*

**Machinery, wear, and reservation.** A good-type requirement means "hold
≥ N of SYMBOL while this runs" — checked at start, never consumed.
Requirements **reserve**: at `start_process`, the sum of a symbol's
requirements across the entity's RUNNING processes must not exceed its
holding, so one hammer cannot back unlimited concurrent workshops; and
settlement's live holdings check must treat reserved quantities as
unavailable, so you cannot sell the oven mid-bake. This is a query against
running processes, not an escrow — nothing is moved or locked, exactly
like the markets' no-escrow stance. Machinery *wear* needs no mechanism at
all: it is a fractional input (`0.01 OVEN` per bake ⇒ an oven survives
~100 bakes), and since machines are themselves recipe outputs, a
capital-goods industry emerges from data alone.

**Better machinery = recipe variants, not modifiers.** Equipment that
shortens duration or cuts input use is expressed as separate recipe rows —
`BAKE_BREAD_HAND` (5 ticks, more labor) vs `BAKE_BREAD_OVEN` (2 ticks,
requires OVEN, consumes 0.01 OVEN) — with the better variant gated behind
an unlock (Victoria 3's "production methods" pattern). The engine never
evaluates efficiency formulas: a formula-in-mechanism would break
auditability (a player could no longer read a recipe and know what it
does) and put arithmetic policy where votes can't safely amend it. Which
variant to run is a script's choice — policy, in Lua. If a world wants so
many quality tiers that hand-written rows get silly, the answer is a
*generator* emitting recipe rows at proposal time (formula lives in
tooling/votes; the engine still sees only plain declared recipes).

**Waste is just an output.** `SMELT_IRON: 2 ORE + 1 COAL → 1 IRON +
1 SLAG` — the engine has no product/waste distinction. Waste-ness is
economic (nobody bids for SLAG) and policy (a dumping tax, a validator
blocking TOXIN transfers). Because conservation forbids goods vanishing,
waste accumulates until dealt with — so pollution, disposal industries,
and environmental politics *emerge* rather than being designed. The one
allowance: zero-output disposal recipes (`BURY_SLAG: 5 SLAG + 1 LABOR →
nothing`) — destruction stays conservation-legal because it is declared.

#### Unlocks and the tech tree (research)

A **Technology** is a data row with a set of prerequisite technologies
(a DAG). Research is *not* a separate mechanism: a research project is a
recipe whose inputs are money/labor/time and whose output is an **unlock**
rather than goods. Recipes may require unlocks; the engine mechanism is
only "does this entity's (or world's) unlock set contain the recipe's
requirements?".

Deliberately votable/configurable: whether unlocks are per-entity, shared
world-wide, or diffuse over time (e.g. become public N ticks after first
discovery); research costs; the shape of the DAG itself.

#### Perishable holdings and the labor market

Holdings gain two optional, data-driven properties per symbol
(a **Good** definition row): *perishable* (expires at end of tick, or
decays at a rate) and *auto-issued* (entities of a given type receive N
units per tick).

Labor then needs **no new market mechanism**: each person auto-issues N
units of perishable `LABOR` (or `LABOR-<SKILL>`) per tick, which trades on
the existing call auction, and recipes list labor among their inputs.
Wages, labor scarcity, and skill premiums all emerge from the mechanisms
already built. Skills slot in later as unlocks on the person.

Perishability doubles as food spoilage and prevents labor hoarding.

#### Needs and consumption (demand)

A **Need** definition (per entity type) is data: the need, which goods
satisfy it, per-tick quantity, and a weight/priority (the hierarchy).
The engine runs a **consumption pass** each tick: decrement satisfying
holdings, update a per-need satisfaction score on the entity, emit
`need_satisfied` / `need_unmet` events.

Needs are what make the economy circulate — without a demand sink,
production just accumulates inventory and prices collapse. Satisfaction
scores are also the natural inputs for NPC behaviour scripts ("if food
satisfaction < 0.5, bid up for BREAD"), for win conditions, and for the
platform's quality-of-life metrics. *Consequences* of unmet needs
(productivity loss, migration pressure, death?) are policy — scripts and
votable parameters — not engine code.

#### Parcels and facilities (land)

Land is a first-class engine primitive, not a holding symbol, because the
platform builds a real navigable world on top (§4.4) and fork petitions can
copy regions as terrain.

A **Parcel** is a non-fungible asset: an owner (entity), a region id, a
coarse spatial extent (the world layer defines the geometry; the engine
stores the reference and grants), and a zoning/type tag (FIELD, LOT, …).
A **Facility** is a built improvement on a parcel (FARM, SMITHY, MARKET
HALL), itself produced by a recipe whose output is a facility rather than
goods. Recipes may require a facility of a given type, which is how
production becomes *located*: you farm on your field, smelt at a forge.
This step is where recipe **requirements** (§ recipes) get built — the
present-but-not-consumed check with reservation is one mechanism serving
facilities ("a SMITHY you control") and machinery ("hold 1 OVEN") alike.

A **Deposit** is a natural resource dotted onto the map: a parcel/region
carries deposit rows (IRON, TIMBER, fertile soil as a FIELD quality tier)
with a remaining quantity and optionally a regeneration rate. Extraction
recipes (MINE_IRON, FELL_TIMBER) require a parcel with the matching deposit
and draw it down — geography *is* the resource distribution, so where
things are found drives where industry, transport, and settlement emerge.
Deposit placement is genesis data (hand-placed, procedurally generated, or
derived from real-world geodata if a world uses a real map as terrain).

Engine invariants: parcel ownership moves only by the owner's intent (sale,
grant) or by explicitly-declared policy (land taxes, expropriation votes —
which are data/policy, not mechanism); deposits deplete only through
extraction recipes. Land *grants* — who may claim what — are votable policy
from day one, per the concept.

The engine deliberately knows nothing about meters or meshes: it stores
who controls which parcel and what stands on it. Continuous space belongs
to the world layer.

#### Later: contracts (recurring obligations)

A generalized "A pays B x per tick in exchange for y per tick, until
terminated/breached" primitive. Gives employment proper (vs. spot labor),
rent, and loans in one mechanism. Not needed for the first playable
economy; spot markets carry it until then.

## 4. Platform layer (built *on* the engine, not in it)

Everything in this section is a consumer of the engine and lives outside
the engine module.

### 4.1 Worlds and the governance cycle

- A **world** is one engine instance: its own DB/genesis state, script set,
  parameter set, and tick cadence.
- Worlds run on a periodic cycle (weekly to start): players submit
  **proposals**, campaign, vote; the top X changes are enacted at once on
  **enactment day** — the platform's heartbeat, distinct from the engine
  tick (which may run much faster, e.g. hourly).
- A proposal is one of: parameter change (edit data rows: recipes, needs,
  taxes, genesis-style grants), script change (add/modify/remove a Lua
  script), or constitutional change (amend the governance parameters
  themselves, likely at a higher threshold).
- **Trials**: a pending proposal can be applied to a copy-on-write test
  fork of the world so players can audit scripts for exploits before
  voting. This is the same machinery as forking (§4.2) with a TTL.
- Rule layers: engine invariants (§2) are un-votable; the voting system's
  own integrity is un-votable except via constitutional process; everything
  else is fair game.

### 4.2 Forking

- A fork is a public **petition** around a concrete manifesto: proposed
  ruleset plus optionally a *copy* of a region as starting terrain (the
  parent world keeps everything — forks copy, never split).
- Requires a critical mass of **binding pledges** within a deadline;
  signing means you migrate when it launches.
- Forks are separate engine instances — no shared state, territory, or
  physics between worlds.
- Leaving costs your full material stake (possessions, land, holdings —
  all world-scoped). You can return later, but as a newcomer.

### 4.3 The spatial world (the world layer)

The platform is a *real navigable world*: players walk about, farm, build.
This is a second simulator with different physics from the economy engine,
and the two must stay separate systems with a narrow boundary:

| | World layer | Economy engine |
|---|---|---|
| Time | Continuous, real-time (10–60Hz) | Discrete ticks |
| Space | Meters, terrain, collision | Parcels and region ids |
| Authority over | Where things *are*; movement, physics, interaction range | Who *owns* what; value, production, governance |
| State | Positions, terrain, animations | Ledger, holdings, orders, processes |

The world server is a **client of the economy engine** — the third
consumer, alongside the HTTP API and the Lua scripts, speaking the same
intent protocol. Spatial verbs map to economic intents at the boundary:

- Walk to your field and plant → world server checks you're standing on
  your FIELD parcel, fires a `start_process` intent (GROW_WHEAT recipe
  bound to that parcel). The crop growing over ticks is an engine Process;
  its visible growth stages are world-layer state *derived from* the
  process. Harvest → process completion credits WHEAT holdings.
- Hand an item to someone / put goods on a cart → transfer intents; the
  world layer may require physical proximity or transport, the engine
  records the ownership change.
- Build a smithy → construction recipe whose output is a Facility on the
  parcel; the world layer renders it and gates interaction by distance.

Consequences worth designing for from the start:

- **Located resources.** Deposits (§ parcels) are dotted across the map,
  so extraction happens where the resources are: mines at ore, mills at
  forests, farms on fertile soil. Combined with local markets this makes
  geography the economic terrain — settlement patterns and trade routes
  emerge from where things are found.
- **Local markets and logistics.** Markets gain an optional location
  (a MARKET HALL facility); goods must be physically present to be sold
  there, so transport, trade routes, and regional price differences become
  gameplay. Global-vs-local markets is a votable parameter per world.
- **Physics-level votes** (gravity, movement, damage) live in the world
  layer's parameter surface, mirroring the engine's: same mechanism/data/
  policy split, same enactment-day pipeline, one proposal system spanning
  both.
- **Terrain and fork copies.** "Copy a region as starting terrain" means
  copying world-layer terrain chunks plus the engine's parcel records for
  that region. A chunked (voxel or tile) terrain representation makes this
  nearly free; a continuous mesh makes it hard. This argues strongly for
  chunked terrain.
- **Position never enters the engine.** Player coordinates at 20Hz stay in
  the world server; the engine sees at most "entity E is at parcel P" when
  an intent requires it. This keeps the engine deterministic, auditable,
  and reusable for the modelling product, which has no world layer at all.

Build-vs-reuse: a from-scratch multiplayer 3D server is the biggest lift in
the whole project. **Luanti** (formerly Minetest — open-source, C++,
server-authoritative voxel engine with a Lua modding API) is a serious
candidate for the world layer: chunked voxel terrain (cheap region copies),
farming/building affordances out of the box, and its Lua-first modding
model matches the sandboxed-Lua rule layer almost exactly. The alternative
is a custom server with a Godot client. Decide by prototyping the farming
loop on Luanti against the engine's API first — it's the cheapest possible
test of the whole boundary design.

### 4.4 Identity and the meta-layer

- Identity, name, and reputation are **platform-scoped** and persist across
  worlds; property (accounts, holdings, land) is **world-scoped**.
- Worlds form a visible family tree — a phylogeny of games — browsable by
  ancestry, popularity, and rule drift.

### 4.5 What this demands of the engine

- **World scoping**: cleanest as one database/schema per world (isolation
  for free, and fork = snapshot copy), rather than a `world_id` column on
  every table. Decide before extraction.
- **Snapshot/fork support**: copy a world's state (optionally filtered to a
  region via parcel records) into a new instance, paired with the world
  layer's terrain-chunk copy. With per-world DBs this is a file/schema copy
  plus a manifest.
- **Parcels as the spatial join key**: region ids and parcel references are
  the only spatial vocabulary the engine speaks; the world layer owns
  geometry. Getting this boundary right is what keeps the engine usable by
  the modelling product (which has no world layer).
- **Parameter surface**: every votable number/definition must be a data row
  the platform can edit through a single audited path — no engine constants
  that matter to gameplay. The world layer needs the same discipline for
  its physics parameters.
- **Compute budgets**: per-entity/per-proposal script budgets on top of the
  existing per-script timeout and memory cap.
- **An intent API for machine clients**: the world server needs a faster,
  authenticated intent/query channel than the human HTTP API — same
  resolver underneath.

## 5. Engine extraction (modularity plan)

Current state is already close: `econ/api/` imports the core; the core
(`models`, `services`, `markets`, `scripting`, `lua_engine`, `tick`) never
imports the API, and the whole engine runs against a plain SQLAlchemy
session (proven by the non-HTTP test suite).

To extract when the time comes:

1. Parameterize the module-level `engine` in `econ/models/base.py` — the
   engine takes a session factory; products own DB config and Alembic.
2. Split packages: `econengine` (core) and `econ.me` (FastAPI app that
   imports it). The platform later imports the same core.
3. Keep the intent/resolver seam as the integration point: products define
   extra intent types and validators; the core owns invariants.

Not urgent — the one-way dependency rule is the thing to protect until
then. Trigger for actually extracting: the second consumer starts.

## 6. Build order

Each step is playable/testable on its own and follows the established
step-commit → PR → squash-merge workflow.

1. **Recipes + processes** — Good/Recipe/Process models, `start_process`
   intent, tick completion pass. Leans directly on holdings + markets.
2. **Perishability + auto-issue** — Good properties + tick pass; labor
   trades on existing markets. First fully circular economy: people sell
   labor, producers make goods, people buy goods.
3. **Needs + consumption pass** — demand sink, satisfaction scores,
   `need_unmet` events; NPC behaviour scripts that respond to them.
4. **Unlocks + tech tree** — Technology DAG, research recipes, recipe
   gating.
5. **Parcels + facilities** — land ownership, construction recipes,
   parcel-bound production, and recipe *requirements* with reservation
   (machinery and facilities share the mechanism). Engine-side only; no
   world layer needed yet.
6. **Engine extraction** — package split per §5, econ.me becomes consumer
   #1.
7. **World-layer prototype** — the farming loop on Luanti (or a custom
   server) against the engine's intent API: walk, claim a parcel, plant,
   wait ticks, harvest, sell at a market hall. This is the go/no-go test
   of the world/engine boundary and of Luanti itself.
8. **Platform vertical slice** — one world, proposals + voting + enactment
   day over both parameter surfaces (engine + world physics); trials; then
   forking and the meta-layer.

## 7. Open questions

- **Fork threshold** — absolute pledge count vs. proportion of active
  players; petition deadline length.
- **Migration mechanics** — cooldowns, return-as-newcomer details, whether
  reputation carries penalties across a hostile fork.
- **Proposal spam** — rate-limit vs. cost (deposit refunded if the proposal
  clears some support floor is the classic answer; note the deposit is
  denominated in world currency, which the economy engine already models).
- **Tech scope default** — per-entity vs. world-shared unlocks as the
  genesis default (per-entity is more interesting economically; shared is
  simpler socially).
- **Time structure** — ratio of engine ticks to enactment cycles; whether
  worlds can vote their own tick rate within engine-imposed bounds.
- **World layer engine** — Luanti vs. custom server + Godot client; decide
  by prototyping the farming loop (§6 step 7). Related: voxel resolution,
  parcel size/granularity, and whether parcels are grid-aligned claims
  (Minecraft-style chunks) or free-form regions.
- **Real-world maps** — "resources dotted on top of a real map" can mean
  procedurally generated terrain or actual Earth geodata (elevation, soil,
  mineral surveys) as genesis input. Real geodata is a compelling default
  world and cheap to support if deposits are just genesis data — but
  licensing and scale (how much of Earth per world?) need a look.
- **Fast-forward performance target** — the modelling product wants max
  ticks/second; per-tick cost is dominated by script runs (fresh Lua VM
  per script per tick) and auction clearing. Decide the target
  (thousands of entities × hundreds of ticks in minutes?) and profile
  before the engine extraction locks in interfaces.
- **Population** — are all persons players, or are there NPC persons run by
  behaviour scripts? (The engine supports both today; the needs system
  makes NPCs meaningful.)
