# Design: the economy engine and what gets built on it

*Status: draft for discussion — 2026-07-25*

## 1. Vision

One reusable **economy engine**, consumed by (at least) three products:

1. **econ.me** — the current FastAPI app: entities, ledgers, monetary
   authorities, sandboxed Lua scripts, commodity markets. It doubles as the
   engine's proving ground.
2. **The sandbox platform** ("democratic Roblox") — a social sandbox where
   each world's rules are decided by its players through periodic votes, and
   worlds can fork into new ones. Different games emerge from governance
   rather than from a single developer.
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

A **Recipe** is a data row describing a transformation:

- inputs: quantities of holdings (and optionally money) consumed at start
- outputs: quantities of holdings credited at completion
- duration: number of ticks
- requirements: unlocks the entity must hold (§ tech), labor inputs
  (§ labor), and later facility/land requirements

A `start_process` intent atomically consumes the inputs and creates a
**Process** (work-in-progress) row; the tick engine completes it
`duration` ticks later and credits the outputs, emitting
`process_started` / `process_completed` events. Cancellation policy
(refund or forfeit inputs) is a votable parameter.

The **manufacturing tree is not code** — it emerges from which recipes
exist, the same way the market list emerges from Market rows. Conservation
is the engine invariant: a recipe transforms exactly what it declares.

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

### 4.3 Identity and the meta-layer

- Identity, name, and reputation are **platform-scoped** and persist across
  worlds; property (accounts, holdings, land) is **world-scoped**.
- Worlds form a visible family tree — a phylogeny of games — browsable by
  ancestry, popularity, and rule drift.

### 4.4 What this demands of the engine

- **World scoping**: cleanest as one database/schema per world (isolation
  for free, and fork = snapshot copy), rather than a `world_id` column on
  every table. Decide before extraction.
- **Snapshot/fork support**: copy a world's state (optionally filtered to a
  region) into a new instance. With per-world DBs this is a file/schema
  copy plus a manifest.
- **Parameter surface**: every votable number/definition must be a data row
  the platform can edit through a single audited path — no engine constants
  that matter to gameplay.
- **Compute budgets**: per-entity/per-proposal script budgets on top of the
  existing per-script timeout and memory cap.

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
5. **Engine extraction** — package split per §5, econ.me becomes consumer
   #1.
6. **Platform vertical slice** — one world, proposals + voting + enactment
   day over the parameter surface; trials; then forking and the meta-layer.

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
- **Land/space** — the concept references land grants and regions; the
  engine has no spatial model yet. Decide whether land is just another
  holding symbol per region (cheap, fits today) or a real spatial primitive
  (needed for terrain copy on fork).
- **Population** — are all persons players, or are there NPC persons run by
  behaviour scripts? (The engine supports both today; the needs system
  makes NPCs meaningful.)
