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

*Status: shipped — inputs/outputs/duration (Step 7); requirements with
reservation (Step 12, § parcels): good requirements reserve against the
entity's running processes and are unavailable to settlement, facility
requirements reserve per parcel.*

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

**Scope is a per-Technology column** (`entity` or `world`), not a single
world default: a smithing rank is per-person even in worlds where physics
knowledge is shared. An `entity`-scoped unlock belongs to the entity that
completed the research; a `world`-scoped one is held by everyone the
moment anyone first discovers it. The DAG is **acyclic by construction** —
prerequisites are fixed at creation and must already exist as rows, so a
technology can never reference itself or a later one. Prerequisites are
enforced at *grant* time (research and admin grants alike), recipe
requirements at *process start*; both checks happen at start for research
recipes, which is sufficient because **unlocks are never revoked** — what
is satisfiable at start stays satisfiable at completion. A completion
that would duplicate an existing unlock grants (and emits) nothing;
re-running research wastes the inputs, and whether that is stupid is the
script's problem. Completion emits one `unlocked` event per technology
actually granted, attributed to the discovering entity even at world
scope.

Deliberately votable/configurable: each technology's scope; diffusion
variants (e.g. become public N ticks after first discovery) as a future
mechanism; research costs; the shape of the DAG itself.

#### Perishable holdings and the labor market

Holdings gain two optional, data-driven properties per symbol
(a **Good** definition row): *perishable* (expires at end of tick, or
decays at a rate) and *auto-issued* (entities of a given type receive N
units per tick).

Auto-issue is a **top-up** — `holding = max(holding, N)` — not an
addition: it prevents issuer-side hoarding without age-tracked inventory
lots and never destroys stock an entity bought. Buyer-side banking is the
accepted v1 trade-off; worlds can vote decay onto `LABOR` if they want it
gone. In the tick, auto-issue runs *before* scripts (fresh labor sells the
same tick) and decay runs *after* the auction (unsold perishables rot);
both emit one summary event per good, never per-entity.

Labor then needs **no new market mechanism**: each person auto-issues N
units of perishable `LABOR` (or `LABOR-<SKILL>`) per tick, which trades on
the existing call auction, and recipes list labor among their inputs.
Wages, labor scarcity, and skill premiums all emerge from the mechanisms
already built. Skills get a full treatment in the next section.

Perishability doubles as food spoilage and prevents labor hoarding.

#### Skills and stochastic production

Skills feel like machinery ("you need X to do Y") but attach to the
wrong entity for the requirements mechanism: a requirement binds to the
*producing* entity, and in a market economy that is usually a business
buying labor from persons. If `FORGE_SWORD` required a SMITHING unlock
on the entity running it, a smithy could never hire smiths. So **skill
must travel inside the labor good** — the only thing that crosses the
market boundary between person and firm. Skilled work is a distinct
symbol (`LABOR-SMITH`), recipes simply consume it, the firm pays for
skill on a market rather than holding it, and the skill premium is
literally the price gap between the `LABOR` and `LABOR-SMITH` auctions.

What stops the unskilled selling `LABOR-SMITH` is conservation: gate the
recipe that *produces* it. Persons auto-issue only plain `LABOR` (one
labor budget each); a skilled person runs a duration-0 conversion recipe
`WORK_AS_SMITH: 1 LABOR → 1 LABOR-SMITH` — consuming plain hours keeps
the budget conserved, and which hours to convert is the person's script,
policy where it belongs. Convert-and-sell works in one tick with current
intent ordering.

**Skill is continuous, and it is a holding**: your `SKILL-SMITH`
quantity is your skill level. The engine never computes on that number —
it only ever asks the question requirements already ask ("hold ≥ N"), so
*milestones* are thresholds in recipe data: `WORK_AS_SMITH` requires ≥ 1
`SKILL-SMITH`, `WORK_AS_MASTER_SMITH` requires ≥ 10 — the
production-methods pattern again (fine-grained curves are the
generator's job). Acquisition is declared, not mechanised:
learning-by-doing is a byproduct output (`… → 1 SWORD + 0.02
SKILL-SMITH`), atrophy is `decay_per_tick` on the skill symbol, and
because decay is proportional while practice gain is constant, every
working cadence has an equilibrium skill level (gain ÷ decay) — skill
plateaus unless you work more or study. Formal education is an industry,
not a mechanism: schools turn teachers' skilled labor into
`EDUCATION-SMITH` goods, tuition is a market purchase (money never
enters recipes; it buys inputs), and a training recipe consumes
education plus time — apprentices' forgone wages emerge as real
opportunity cost. Permanent *rank* layers on via the tech tree: a
certification recipe requiring ≥ N current skill outputs a JOURNEYMAN
unlock, which persists while skill decays underneath it — a lapsed
master is credentialed but rusty, and recipe variants may demand both.
Skills-as-goods are tradeable only if a world opens a market for them;
whether you can buy mastery is policy, not mechanism. Skills are also
the template for a family of entity-attached goods: an *attribute*
(STRENGTH as a holding, if a world wants one) and a *condition*
(§ conditions) are the same pattern — non-market holdings distinguished
only by their data.

**Stochastic recipes.** Failure, catastrophe, and variable yield need
one genuinely new mechanism: a recipe may declare **outcome branches** —
alternative output sets with fixed weights, sampled once at completion:

```
FORGE_SWORD: 2 IRON + 4 LABOR-SMITH + 1 FORGE →
  70%: 1 SWORD + 1 FORGE + 0.02 SKILL-SMITH
  25%: 1 SCRAP + 1 FORGE + 0.03 SKILL-SMITH   (ruined the blank)
   5%: 1 SCRAP                                 (wrecked the forge)
```

Branch tables are loot tables, not formulas: odds are constant *within*
a variant and a player reads the row and knows them exactly; skill
selects which variant you may run and never enters a probability
function. Equipment the dice can eat is a **catalyst input** — consumed
at start, re-emitted by the branches that spare it — so catastrophic
loss is a branch that keeps the forge (requirements + reservation remain
for equipment not at risk; steady wear remains a fractional input).
Because risk is declared and readable, insurers can price it and
policies can tax it. The same mechanism covers harvest yields,
prospecting, and research breakthroughs.

**Auditable randomness.** Ticks must stay replayable and verifiable
(determinism is an engine invariant), and a roller must not be able to
cancel bad rolls: inputs are consumed at start, but cancellation exists,
so a predictable outcome would be cherry-picked. The tick structure
supplies a commit-reveal: a process completing at tick N can last be
cancelled during tick N−1's intent pass, and the hash of tick N−1's full
event list is not determined until after that pass. So

```
outcome_roll = H(hash(tick N−1 events), process_id)
```

is unknowable at the final cancellation opportunity, reproducible by any
auditor afterwards, and needs no oracle — the entropy is the economy
itself.

*Status: built (build-order step 5). Branch tables live on recipes
(mutually exclusive with plain outputs; a branch may output nothing —
total loss), each tick persists `events_hash` — the sha256 commitment
over its canonical event list — and completion rolls are
`H(events_hash(N−1), process_id)` exactly as above, stored on the
process (`outcome_roll`, `outcome_branch`) for audit. The cancellation
window is enforced one tick early for every recipe: once the tick
before completion has run, its hash — the roll's seed — is committed,
so cancellation refuses. One composition note: branches carry goods
only; a stochastic research breakthrough is a lucky branch yielding a
marker good (`EUREKA-X`) that a duration-0 research recipe converts
into the unlock — no branch-level unlock mechanism needed. Skill
ladders, catalyst equipment, and yield risk are now pure data.*

#### Needs and consumption (demand)

A **Need** definition (per entity type) is data: the need, which goods
satisfy it, per-tick quantity, and a weight/priority (the hierarchy).
The engine runs a **consumption pass** each tick: decrement satisfying
holdings, update a per-need satisfaction score on the entity, emit
`need_satisfied` / `need_unmet` events.

The pass runs **after the auction and before decay**: after the auction so
goods bought this tick are eaten this tick (the wage→bread→dinner loop
closes in one tick) and declared sell orders settle against the holdings
that backed them before anything is eaten — the same reason decay is
post-auction; before decay so entities eat fresh stock and only unsold,
uneaten perishables rot. Within the pass all ordering is explicit
(determinism): needs by priority (lower = more essential, so when two
needs share a satisfying good the essential one draws first), satisfiers
symbol-ascending, entities id-ascending. Satisfaction is consumed ÷
required, rounded down so 1.0 means fully met. Unlike the goods passes,
events are **per entity** — they are the signal behaviour scripts react
to, and behaviour scripts see only their own entity's events.

Needs are what make the economy circulate — without a demand sink,
production just accumulates inventory and prices collapse. Satisfaction
scores are also the natural inputs for NPC behaviour scripts ("if food
satisfaction < 0.5, bid up for BREAD"), for win conditions, and for the
platform's quality-of-life metrics. *Consequences* of unmet needs
(productivity loss, migration pressure, death?) get their own treatment
(§ conditions): their decision rules — thresholds, rates, curves — are
votable data, but their effect mechanisms cannot be pure script policy,
because the ownership invariant stops any script docking another
entity's labor and no script can end an entity.

*Status: shipped (Step 9) — `ctx.needs` exposes each entity's applicable
needs with current satisfaction to its scripts.*

#### Conditions (consequences of unmet needs)

The original stance — "consequences of unmet needs are policy, never
engine code" — survives only for its *decision rules*. Three walls stop
consequences being pure script policy: the **ownership invariant** (no
script can dock a starving entity's labor or seize an estate — intents
move only the queueing entity's assets), the absence of any **entity
lifecycle** (nothing today can end an entity), and satisfaction having
**no memory** (`NeedState` is rewritten every tick — one missed meal
reads the same as a hundred ticks of starvation). There is an economic
wall too: while `need_unmet` is only a log line, a rational script
ignores needs entirely and the demand sink is voluntary. Consequences
are what make demand *inelastic* — and the need priority hierarchy only
means anything when consequences differ by need.

So the split: the engine owns **effect mechanisms**, data owns
**decision rules** — every threshold, rate, and curve stays votable,
preserving the spirit of the original stance.

Consequences flow **deficit → condition → effect**, and a **condition
is a Good** — the third member of the entity-attached family after
skills and attributes. The indirection decouples causes from effects:
starvation and plague both grant `COND-WEAK`, and the labor throttle
only has to read `COND-WEAK`, instead of every cause wiring to every
effect pairwise.

Most of the system already exists:

- **Accumulation**: on an unmet tick the consumption pass credits the
  need's declared condition symbol (quantity scaled by the shortfall) —
  a small extension to the pass, and the only new *cause* mechanism.
  `NeedState` stays instantaneous; the memory lives in the holding.
- **Stochastic injury**: an outcome branch that outputs `COND-INJURED`
  — branches already credit goods.
- **Natural recovery**: `decay_per_tick`. Note the skill-plateau math
  runs in reverse: proportional decay against a constant grant means a
  starving entity's condition converges to grant ÷ decay, so any
  threshold must sit *below* that equilibrium or it never fires.
- **Healing as an industry**: a recipe consuming `COND-SICK + MEDICINE`
  — inputs are consumed atomically at start, so medicine has a market
  price and doctors emerge as entities holding `SKILL-MEDICINE`.
- **Non-tradability**: market absence gates it, same as skills — and a
  condition cannot be dumped on you, because goods move only through
  auctions and a sale requires the buyer's own bid. (When the world
  layer's goods-transfer intent lands, non-transferability becomes an
  explicit Good flag.)

Two effect mechanisms are genuinely new:

- **Effective-quantity modifiers** — what "conditions modify skills or
  attributes" actually requires. A temporary fever halving your
  smithing must not draw down the `SKILL-SMITH` holding (that is
  atrophy's job, and it would be permanent); it needs a computed
  overlay: a condition Good declares `modifies: {pattern, factor}`, and
  *effective* quantity = held quantity × applicable factors.
  Multiplication commutes, so determinism is free. Scope it to exactly
  two read sites — recipe requirement checks and auto-issue quantity
  (which is how productivity loss reaches the labor market: auto-issue
  runs at the top of the tick, so tick N's labor is throttled by tick
  N−1's conditions with no ordering changes) — and it must not touch
  markets or consumption.
- **Incapacity and the estate** — a Good property (`incapacitates_at:
  50`) plus an entity lifecycle state. Hold ≥ the threshold and the
  engine deactivates the entity and applies the world's **estate rule**
  (burn / heir / world treasury — the choice is votable data, the
  transfer is engine, because no script may move a dead entity's
  assets). The engine state is *incapacitated*; permanent death is
  world policy layered on top — at one tick per hour, a week's holiday
  is 168 ticks of starvation, and worlds decide how harsh to be with
  player entities.

**Migration stays pure policy**: the pressure signal (satisfaction and
conditions) already reaches behaviour scripts, and the entity moves
*itself* once the world layer gives it somewhere to go.

One tuning caution, not a mechanism gap: this system expresses death
spirals (starving → `COND-WEAK` → less labor → less income → more
starving). That is correct — it is the poverty trap, and it is what
makes needs economically real — but whether spirals are *escapable*
lives entirely in curve data (decay rates, factors, thresholds).
Genesis configs must ship with recoverable defaults.

*Status: designed, not built; not yet scheduled in the build order.*

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

*Status: shipped (Step 12) — parcels/facilities/deposits with per-tick
regeneration toward capacity; construction (`builds_facility`) and
extraction (`deposit_inputs`) recipes; requirements with reservation for
machinery and facilities; a parcel with running processes bound to it
cannot change hands. Scripts see `ctx.parcels` and act via parcel-bound
`start_process` and `transfer_parcel` intents. Land claims stay
admin/genesis (`grant_parcel`) until grant policy becomes votable.*

#### Fast-forward (tick throughput)

Fast-forward is already solved semantically — and cannot be solved any
other way. The unpaced invariant (§2) makes it `run_tick` in a loop,
and *skipping* ticks is impossible by design: the events-hash
commit-reveal chain and script state make tick N depend on tick N−1,
so there is no analytical "jump ahead 1000 ticks". Fast-forward is a
throughput and product-surface question, never a semantics one.

What matters at modelling scale (say 10k ticks × 1k entities):

- **N+1 queries are the hot path.** The consumption pass queries one
  Holding per (entity, satisfier), auto-issue one per (good, entity),
  the script-ctx build ~5 per script. Correct-first was right;
  bulk-load per pass when it matters.
- **Event storage dominates long runs.** `events_hash` is kept forever
  (RNG auditability), but only the *previous* tick's event bodies are
  ever read in the hot path (`ctx.events`) — bodies can carry a
  retention policy (keep the last K ticks plus sampled snapshots).
- **Cheap wins already in place**: `LuaEngine` is injectable (one VM
  across a run); `run_tick` flushes but never commits, so many ticks
  can share a transaction; pure simulations run on in-memory SQLite.
- **The modelling product's real feature is snapshot/branch**, not raw
  speed: run 500 ticks, fork the state, compare two policies — with
  SQLite a fork is a file copy. Surface: `run_ticks(n)` /
  run-until-predicate, metric sampling instead of materializing
  everything, forkable snapshots.
- **Real-time worlds get catch-up free**: a server down ten hours runs
  ten ticks on restart; durations are tick-denominated, so wall-clock
  drift is harmless.

Everything tick-denominated — consequence curves included — behaves
identically at one tick per hour and ten thousand ticks per second.
That is the unpaced invariant doing its job; no simulation parameter
may ever be wall-clock.

#### Military and contests (ownership transfer by force)

In real economies ownership is not natural law — it is a social
contract enforced by whoever holds local force, usually a government.
The engine's **ownership invariant** ("assets move only by the owner's
intent") is therefore not neutral physics: it is the engine playing the
role of a perfectly effective, incorruptible state. That is the right
default, and for many worlds (and the whole modelling product) the
final word. But a platform about governance should be *able* to model
the thing governance actually rests on — so the engine supports
**ownership transfer by force**, even though its existence in any world
is purely rule-based: enabled, shaped, and priced entirely by votable
data, default off.

Force cannot be script policy, for the same three walls as § conditions:
the ownership invariant (looting is by definition moving someone else's
assets — only the engine may ever do that), fixed-odds branches (recipe
branch weights are constant loot tables, but combat odds must be a
function of *both sides'* committed strength), and the entity lifecycle
(casualties need incapacity). And there is the same economic wall in
mirror image: without a taking mechanism, a rational actor buys zero
weapons and any arms industry is a state-subsidized ornament. Conflict
is to weapons what consequences are to needs — the thing that makes the
demand real. It also makes *defense* real: a garrison's strength is
readable data, so deterrence becomes an economic calculation rather
than flavor text.

Most of a military is already pure data on shipped mechanisms:

- **Procurement**: weapons are goods (IRON → SWORD recipes, SMITHY
  facilities, market prices); an army is a government entity running
  payroll — buying LABOR-SOLDIER and weapons on the open market. The
  fiscal sink is real and circulates with zero new engine code.
- **Military technology**: a branch of the Technology DAG gating weapon
  recipes; SKILL-SOLDIER rides the stochastic training ladders.
- **Standing readiness**: a garrison is a running process bound to a
  FORT facility, consuming labor per tick and producing nothing — pure
  cost, which is exactly what a standing army is. Stockpiles rust via
  `decay_per_tick`.
- **Casualties**: condition credits (§ conditions) — COND-INJURED from
  an outcome branch, death via `incapacitates_at` and the estate rule.
  Death in battle is the same engine state as death by starvation.

One mechanism is genuinely new: the **contest**. An attacker declares
by intent at tick N, committing declared assets (weapons, labor —
soldiers are *holdings*, not entities; armies are payroll, not
population); the contest resolves at tick N+1. The one-tick gap is the
commit-reveal window doing double duty: no withdrawal once the seed is
knowable (same rule as process cancellation), and the defender's
scripts see the declaration — one tick to reinforce, negotiate, or
move liquid assets out. Fixed assets cannot flee, which is what makes
land warfare about land. Strength is a votable schedule (symbol →
weight, with defender bonuses for facilities like a FORT); odds are a
votable curve over relative strength, sampled through the events-hash
RNG like any outcome roll. Effects are bounded by votable rules: loot
capped at a fraction of holdings and symbol-filtered — non-transferables
(conditions, skills) can never be taken, you can take a man's sword but
not his swordsmanship — parcel conquest as ownership transfer with
facilities standing, casualties on both sides, attrition of committed
weapons catalyst-style (branches decide what survives).

The invariant, restated for the age of war: assets move by the owner's
intent, or by engine mechanism under explicitly-declared votable rules —
estate transfer at incapacity, enacted policy (taxes, expropriation),
and contest outcomes. Never by another entity's script directly. Rules
of engagement — whether contests exist at all, who may declare on whom,
what declaration costs, cooldowns — live in world settings beside the
estate rule; peace treaties and tribute are future § contracts.

*Status: designed, not built; unscheduled — platform-era (needs
multiple polities to matter). Deliberately designed before contracts
and transfer intents so nothing built earlier closes the door.*

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

*Done (§6 step 7).* The split is live in this repo:

- `engine/econengine/` — the core (`models`, `services`, `markets`,
  `scripting`, `lua_engine`, `tick`, and the domain passes), its own
  distribution (`engine/pyproject.toml`, deps: SQLAlchemy + lupa only).
  The core is session-in only: it never creates engines or sessions, and
  `econengine.models.base` defines just `Base`.
- `econ/` — the econ.me FastAPI app, consumer #1. It owns DB config
  (`econ/db.py`: `DATABASE_URL`, engine, sqlite `check_same_thread`
  handling) and Alembic. Future consumers (platform, modelling tool) own
  theirs the same way.
- The intent/resolver seam stays the integration point: products define
  extra intent types and validators; the core owns invariants.

Dev setup: `pip install -e ./engine` (the app package imports resolve from
the repo root as before). The one-way rule still holds — `econengine`
never imports `econ`.

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
5. **Stochastic recipes** — outcome branch tables + the event-hash RNG;
   skill ladders, at-risk equipment, and yield risk become pure data
   (§ skills and stochastic production).
6. **Parcels + facilities** — land ownership, construction recipes,
   parcel-bound production, and recipe *requirements* with reservation
   (machinery and facilities share the mechanism). Engine-side only; no
   world layer needed yet.
7. **Engine extraction** — package split per §5, econ.me becomes consumer
   #1.
8. **World-layer prototype** — the farming loop on Luanti (or a custom
   server) against the engine's intent API: walk, claim a parcel, plant,
   wait ticks, harvest, sell at a market hall. This is the go/no-go test
   of the world/engine boundary and of Luanti itself.
9. **Platform vertical slice** — one world, proposals + voting + enactment
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
- ~~**Tech scope default**~~ — *resolved (§ tech tree): scope is a
  per-Technology column (`entity` / `world`), defaulting to `entity` —
  skills settled the argument, since a smithing rank is per-person even
  in worlds where physics is shared. Diffusion ("public N ticks after
  first discovery") remains future work.*
- **Time structure** — ratio of engine ticks to enactment cycles; whether
  worlds can vote their own tick rate within engine-imposed bounds.
- **Enforcement vs. arms** (§ military) — once armies exist, "the vote
  enacts by engine magic" is a choice, not a given: does a military
  world let enactment be resisted by force (civil war as mechanism), or
  does enactment stay magical with contests confined to inter-entity
  raids? The former is the deepest possible governance sandbox and the
  biggest griefing vector.
- **Conquest vs. forking** (§ military) — forking is the peaceful exit;
  a world where conquest is cheaper than forking will get conquest.
  Whether declaration costs and fork thresholds need engine-imposed
  relative bounds, or data suffices, is unresolved.
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
