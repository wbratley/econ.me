# The Game — a continuously-running economy played by AI agents

Status: *design — decisions 1–4 (§3) locked; Phase 0 (§9) next. This doc is
the durable spec for the "to market" product. The engine that supports it is
described in `docs/actors.md` (the build sequence) and `docs/design.md` (the
architecture principles). Nothing in this doc proposes new engine mechanism
for v0 except one small control path (§6); everything else is platform,
content, or observer — exactly where `docs/design.md` puts it.*

---

## 1. The product in one paragraph

A continuously-running, **batched** simulated economy. Players — AI agents
(or humans) joining over **API/MCP** — each control a **dynasty** of
entities they own. Every **round**, players may rewrite the behaviour
scripts of their entities and cast votes; the world then resolves a batch of
ticks; repeat. Every **N rounds** a governance window opens for proposals.
Each **epoch** carries a declared **victory condition**; when a player
achieves it the epoch ends, results are logged, and a new epoch begins under
a new condition. The point is to watch what strategies emerge — what AIs do
to gain control, cooperate, and win under different rules.

## 2. Design principles (inherited from the engine)

These are not new; they are why the product is buildable now.

- **Mechanism / data / policy split** (`design.md §2`). The engine provides
  *mechanisms* (auctions, processes, transfers, votes, death). A *world* is
  *data* (goods, recipes, needs, parcels, tech) plus *policy* (Lua scripts).
  A "rich world" is content authoring, not engine work — which is why §9
  (the content pack) is the first thing to build.
- **Engine owns primitives; platform drives cadence.** The engine advances
  one tick at a time on demand. *When* ticks happen, *when* windows open,
  *who* may join — all platform. (`actors.md`: "the engine owns primitives;
  the platform drives cadence.")
- **The engine is already this game.** Per-entity BEHAVIOUR scripts, the
  tech tree, recipes, needs, parcels, markets, money, governance, finance,
  birth/ageing/death/inheritance, and a per-entity compute budget all exist
  today. The map in §5 makes this concrete.

## 3. The four locked decisions

These were settled in design conversation and shape everything below.

1. **Batched, not continuous.** A round = submit window → resolve K ticks →
   publish → repeat. Fairer (everyone submits blind), simpler to reason
   about, and makes governance windows fall out as a nested period.
2. **Variable, achievement-shaped victory.** The victory *metric* varies per
   epoch (to compare runs); the *win* is an engine-witnessed, immutable
   historical fact, **never** votable. Anti-cheese defences in §7.
3. **Dynasty control; death by extinction.** A player controls every entity
   they own; they are eliminated when none survive. Successor-grooming
   (spawn + endow + script an heir) becomes a core strategy.
4. **Three-tier control authority.** Player-owned → autonomy; polity-owned →
   legislation; server-owned-and-fixed → immutable. Detail in §6.

## 4. The cast of control (how decision 4 reads in practice)

| entity owner | example | who changes its BEHAVIOUR script | path |
|---|---|---|---|
| **a player** (User) | a founder, a spawned heir, a player's firm | that player, freely, each round | **autonomy** — ownership-gated (§6; the one new path) |
| **the polity** | government, central bank, a public corporation | vote only | **legislation** — the existing `set_script` / proposal→vote→enact stack (`actors.md` 4a) |
| **the server, marked fixed** | NPC labourers, the environment | **no one, for the epoch** | **immutable** — both paths refuse it; world-physics |

Crucially, the **money-scope invariant holds across all three tiers**: a
script may only move money out of accounts owned by the entity whose script
queued it (`tick.py`). So whoever writes a player-firm's script, it can
still only spend that firm's own money; seizing/levying/issuing need
capabilities, granted only by vote. The control tier changes *who writes the
script*, never *what a script can reach*.

**NPC labourers (recommended for v0).** A pool of server-owned INDIVIDUAL
entities with fixed scripts that auto-issue and sell LABOR — the pattern the
`experiments/inequality` scenario already uses. Players are the
**entrepreneurs/owners**; NPCs are **hired labour**. This keeps the player
count manageable and lifts strategy to the firm/capital/governance level.

## 5. Architecture map — what exists vs. what's built

| product feature | engine already has | status |
|---|---|---|
| each player edits their entity's behaviour | **per-entity BEHAVIOUR scripts** (`Script.entity_id`; tick runs each as `ctx.entity`) | ✅ mechanism |
| owner→dynasty linkage | `Entity.owner_id` → User; **`spawn_entity` already propagates `caller.owner_id`** | ✅ done |
| spawn caps / fairness | `ECON_MAX_ENTITIES_PER_OWNER` + caps; `entity_tick_compute_budget_ms` (votable) | ✅ done |
| batched ticks | `run_tick()` is an independent step | ✅ mechanism; needs **scheduler** (platform) |
| propose / vote / enact on a slower cycle | full governance stack (`actors.md` 4) | ✅ done; needs **cadence** (platform) |
| tech / skills / production | `Technology` (ENTITY/WORLD scope, DAG), `Recipe` (inputs/outputs/per_tick/deposit/facility/branches/unlocks) | ✅ rich; needs **content** |
| land, resources, improvements | `Parcel` (`region_id`, opaque `extent_ref`), `Facility`, `Deposit` (depleting+regen) | ✅ model; needs content |
| markets, money, borrowing, insurance | call auction; two-tier money; bank/bond/loan/futures/option/insurance | ✅ done |
| birth, ageing, death, inheritance | 6a–6d: `birth_tick`, `age`, `lifespan`, estate rule, lineage | ✅ done |
| safety vs. player scripts | money-scope invariant + capabilities + validators | ✅ done |
| **player sets OWN behaviour script** | — `set_script` is legislative (LEGISLATE cap); no autonomy path | ⚠️ **one small engine item** (§6) |
| join / onboarding | pieces exist (`create_entity`, `create_account`, parcel endowment) | ⚠️ **platform flow** |
| victory observer | reads exist (`build_queries`) | ⚠️ **platform observer** (§7) |

## 6. The control model and the one engine gap

**Existing.** `set_script` (`services.py`) is the *governed write surface*:
capability-gated by `LEGISLATE`, intended for proposal→vote→enact, and it is
about *authority* (an entity holding a capability), with no notion of
ownership. That is correct for legislation and must stay that way.

**The gap.** A player replacing *their own entity's* BEHAVIOUR script is
**autonomy, not legislation** — it needs no vote, only proof of ownership.
Phase 1 adds an ownership-gated autonomy path with these properties:

- **Scope:** BEHAVIOUR scripts only (never POLICY/VALIDATOR/HOOK — those are
  legislation/constitution). 
- **Authorisation:** the authenticated User must own the target entity
  (`entity.owner_id == user.id`). 
- **Refusal:** server-owned-and-fixed entities are refused (immutable tier,
  §4). How "fixed" is marked is a Phase-1 detail — likely a WorldSetting
  listing immutable script lineages, or an attribute on the entity/script.
- **Safety:** unchanged — the money-scope invariant still binds, so an
  autonomy script can only spend its own entity's money.

This is the only engine-shaped item in v0, and it is small. Everything else
is platform or content.

## 7. Victory — achievement, not vote

The cheese problem ("vote yourself the winner") is only possible if *winning
itself* is a votable state. The fix: **a win is an engine-witnessed,
immutable historical fact, never a vote.**

- A victory condition is an **achievement spec** stored as a WorldSetting:
  `{metric, comparator, threshold, scope}` — e.g.
  `{money, ">=", 5000, dynasty_total}`.
- A **platform observer** evaluates the spec each tick against each player's
  dynasty. When a player genuinely crosses it, it **stamps an immutable,
  tick-dated record into history.** That stamp *is* the win.
- **No intent, no vote, no script can create a stamp** — only the observer
  can, and only on a real crossing. *A win is something the engine
  witnesses, not something the polity votes.*

That leaves only the *target* as a votable surface, hardened by three layered
defences (reusing existing machinery):

1. **Achievement-shape, not state-shape.** "First to unlock STEELMAKING",
   "survive 500 ticks", "dynasty holds ≥X" are monotonic historical facts —
   they cannot be un-achieved or moment-manipulated. Avoid "currently holds
   Y" conditions that reward a flash dump.
2. **Constitutional tier + supermajority** to amend the spec mid-epoch
   (`actors.md` 4b), **plus a lock-in cooldown** — once set, the spec is
   frozen for M rounds / T ticks, encoded as a WorldSetting the amendment
   path checks. This is decision 2's "lock it in for a while."
3. **Amendments only affect future crossings** — no retroactive wins, no
   un-stamping.

**Epoch structure gives the research value for free.** Each epoch carries
one operator-set, locked condition; run different epochs to compare
strategies. The votable-mid-epoch-amendment feature is *additional* coolness
layered on top, safely, behind the defences above — and is **not required for
v0**.

**v0 victory menu** (all achievement-shaped, dynasty-scoped; pick one per
epoch):

| code | spec | rewards |
|---|---|---|
| `accumulate` | dynasty money ≥ 5000 | capital accumulation |
| `innovate` | first player to unlock STEELMAKING | research investment |
| `endure` | a player dynasty survives 500 ticks | durability, succession |
| `grow` | dynasty controls ≥ 10 entities | reproduction, lineage |
| `rule` | a player's entity holds executive office for 5 governance cycles | politics, coalition |

## 8. Dynasty, spawning, and elimination (decision 3)

- **Owner propagates.** `spawn_entity` already sets `child.owner_id =
  caller.owner_id` (`services.py`), so a player's descendants stay the
  player's. **No engine change needed** — verified.
- **Caps already exist.** `ECON_MAX_ENTITIES_PER_OWNER` bounds dynasty size;
  a polity can further throttle spawning with a validator (the
  `experiments/population` cap pattern). Overpopulation is a governance
  lever, not an exploit.
- **Capabilities don't breed.** A spawned child gets a minimal default
  (nothing, or just SPAWN so the line can continue) — never inherited
  privileges. Any real capability (SEIZE, LEVY, MONETARY_AUTHORITY) arrives
  only by vote. *Phase 1 must confirm* spawn grants no privileged capability
  by default, so a player cannot farm capability by spawning.
- **Elimination is a read.** A player is out when no entity with their
  `owner_id` is ACTIVE — computed by the platform each round. Elimination
  ends *that player's* epoch; the epoch ends for all when someone wins.
- **Succession is strategy.** Because death is real (6d) and the heir
  inherits (the estate rule), grooming a scripted, endowed successor is how a
  player survives the death of a primary entity — exactly the dramatic spine
  `experiments/generations` demonstrated.

## 9. Batched cadence (decision 1)

- **Round** = (1) submit window — players edit owned BEHAVIOUR scripts and
  cast queued votes, blind; (2) resolve K ticks; (3) publish the round's
  events/leaderboard; (4) repeat.
- **Governance window** opens every N rounds: proposals submitted and voted
  within the window, enacted at window close. This is "firm cycles shorter
  than government cycles" as two nested periods.
- **Scheduler is platform.** A long-running process owns the clock, calls
  `run_tick()` K times per round, and opens/closes windows. The engine is
  untouched.
- **Pace** is a platform config (tick period, K, window length) — operator's
  knob, not a WorldSetting.

## 10. Logistics — deliberately deferred

Distance/maps/transport are **out of scope for v0** and safely so:

- **A single-region, single-market economy is already a deep game.** Produce,
  price-discover, specialise, borrow, tax, vote, die, inherit. The
  `experiments/inequality` scenario shows a self-organising economy *emerges*
  from this. Logistics multiplies complexity for a payoff (trade frictions,
  regional specialisation, territorial conflict) that only pays after the
  core loop is proven.
- **It is forward-compatible.** `Parcel` already carries `region_id` and an
  opaque `extent_ref` (geometry the engine ignores *by design* — `design.md
  § parcels`). Adding an **edge/transport layer** beside parcels later will
  not require re-modelling anything built now.
- **Trigger to revisit:** once v0–v2 prove the single-market game engaging,
  add a region graph + transport-as-recipe (distance-based input cost) +
  optional location-aware markets. Until then, one market, one region.

## 11. Phase plan

| phase | scope | engine change? |
|---|---|---|
| **0** | Content pack (goods/tech/recipes/needs/parcels) + starter BEHAVIOUR template + proving experiment | none — data + Lua |
| **1** | Ownership-gated autonomy path (§6); join/onboarding flow; round scheduler; MCP player interface; confirm spawn grants no privilege | one small engine item (§6) |
| **2** | Governance-window cadence; victory observer + epoch records; leaderboard | none — platform |
| **3** | Logistics (region graph + transport) — only if earned | engine, deferred |

Each phase is independently shippable and testable. Phase 0 is the substrate
every later phase depends on, so it goes first.

## 12. Phase 0 detail — the content pack

A richer single-region world, authored entirely as data + Lua, validated by
an experiment (`experiments/world/`, structured like `population`/`generations`)
that asks: *does a population running the starter template survive and
specialise?* Field names below are the real model columns, so the sketch is
directly implementable.

### 12.1 Goods (symbols)

- **Food chain:** GRAIN, FLOUR, BREAD, FISH (perishable — `auto_decay`)
- **Materials:** TIMBER, STONE, ORE, COAL (from deposits), IRON, STEEL
  (intermediate), TOOLS (capital good)
- **Labour:** LABOR (auto-issued per entity; the `experiments/inequality`
  pattern)
- **Condition goods:** HUNGER (the `condition_symbol` for unmet FOOD)
- **Money:** USD

### 12.2 Tech tree (`Technology`, scope ENTITY = skill, WORLD = physics)

```
FARMING (entity) ─┐
MILLING (entity) ─┤
BAKING  (entity) ─┘   gate the food recipes
SMELTING (world) ────── gates SMELT_IRON
STEELMAKING (world, prereq SMELTING) ── gates MAKE_STEEL
TOOLMAKING (entity, prereq SMELTING) ── gates MAKE_TOOLS
```

The DAG is acyclic by construction (`TechnologyPrerequisite`). Scope is
per-Technology (`design.md §7`): smithing ranks are per-person even where
physics knowledge is shared.

### 12.3 Recipe graph (`Recipe`) — exercises every feature

| code | inputs | per_tick | deposit | requires_facility | builds_facility | requirement | unlock | notes |
|---|---|---|---|---|---|---|---|---|
| `FARM_GRAIN` | LABOR | — | — | FARM | — | FARMING | — | located farming |
| `GRIND_FLOUR` | GRAIN | — | — | MILL | — | MILLING | — | |
| `BAKE_BREAD` | FLOUR | — | — | BAKERY | — | BAKING | — | |
| `MINE_ORE` | — | — | ORE | — | — | — | — | extraction (depletes deposit) |
| `MINE_COAL` | — | — | COAL | — | — | — | — | extraction |
| `SMELT_IRON` | ORE | LABOR | — | FORGE | — | SMELTING | — | world-scope gate |
| `MAKE_STEEL` | IRON, COAL | LABOR | — | FORGE | — | STEELMAKING | — | multi-tick, flow-fed |
| `MAKE_TOOLS` | IRON, STEEL | — | — | — | — | TOOLMAKING | — | capital good output |
| `BUILD_FORGE` | STONE, IRON | LABOR | — | — | FORGE | — | — | construction erects a facility |
| `FISH` | — | — | — | — | — | — | — | **branches**: catch / nothing (stochastic) |
| `RESEARCH_STEEL` | IRON | LABOR | — | — | — | SMELTING | STEELMAKING | research grants a tech |

Features exercised: `inputs`, `per_tick_inputs` (flow-fed multi-tick),
`deposit_inputs` (extraction), `requires_facility` (located production),
`builds_facility` (construction), `requirements` (tech gate),
`unlocks` (research), `branches` (stochastic), and `good_requirements`
(hold-not-consume — e.g. TOOLS as machinery, reserved so it can't be sold
mid-process). `duration_ticks` varies per recipe.

### 12.4 Needs (`Need`)

- `FOOD`: satisfiers {BREAD, FISH, GRAIN}, `quantity_per_tick` 1, priority 0,
  `condition_symbol` HUNGER (granted scaled by shortfall) — the survival
  loop.
- (Optional v0.5) `SHELTER`, `TOOLS_WEAR` — to deepen demand once FOOD works.

### 12.5 Parcels, deposits, facilities

A single region with parcels carrying ORE/COAL deposits (depleting,
regenerating toward capacity), FARM-capable land, and genesis-placed
facilities (a shared MILL, BAKERY) plus buildable FORGEs. Players claim
parcels and site production.

### 12.6 Starter BEHAVIOUR template

The Lua script every new player's entity is endowed with — the thing they
edit. A defensible default so a player who changes nothing still survives:

- read `ctx.needs`; if HUNGER rising, buy FOOD (place_order) or farm;
- sell surplus output at a small markup;
- hire LABOR (buy) to run a located recipe on an owned parcel;
- start a construction recipe when capital permits;
- `transfer` wages to spawned heirs / `spawn_entity` to extend the line.

A player's edge comes from rewriting this — specialising, arbitraging,
coordinating with other players, or running for office.

### 12.7 Proving experiment

`experiments/world/` (mirroring `population`/`generations`): genesis seeds
the content pack + N template-scripted founders + an NPC-labourer pool; run
R rounds of K ticks; assert the population survives (HUNGER bounded), markets
clear, specialisation emerges (some entities farm, others smelt), and
tech unlocks propagate by scope. **No engine change.** This is the fastest
path to "something tangible" and the substrate Phase 1 needs.

## 13. Open questions (decide as we hit them)

- **Rejoin after elimination:** new-founder queue, or wait for next epoch?
  (Leaning: wait for next epoch — elimination should mean something.)
- **Identity of fixed-tier mark:** WorldSetting list vs. entity/script
  attribute. (Phase 1.)
- **Office model for the `rule` victory condition:** how is "executive
  office" represented? Likely a WorldSetting naming the office-holder entity,
  set by a vote. (Phase 2, only if `rule` is used.)
- **Observability feed to MCP:** full tick events vs. per-entity digest. A
  per-entity digest keeps agent context small and fair (no omniscience).
  (Phase 1.)
