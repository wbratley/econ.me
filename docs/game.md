# The Game — a continuously-running economy played by AI agents

Status: *decisions 1–4 (§3) locked; Phases 0–2 complete (§11); Phase 3
(the readable world — catalog, audit trail, pack standard) specced
(§15). This doc is
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
| batched ticks | `run_tick()` is an independent step; `POST /admin/rounds/advance` resolves K per round | ✅ mechanism + scheduler |
| propose / vote / enact on a slower cycle | full governance stack (`actors.md` 4) | ✅ done; cadence landed in Phase 2b (§14.4) |
| governance cadence (windows) | proposals/votes/enact already exist; `round.state` readable by scripts | ✅ done (platform + content clerk, Phase 2b — §14.4) |
| tech / skills / production | `Technology` (ENTITY/WORLD scope, DAG), `Recipe` (inputs/outputs/per_tick/deposit/facility/branches/unlocks) | ✅ rich; needs **content** |
| land, resources, improvements | `Parcel` (`region_id`, opaque `extent_ref`), `Facility`, `Deposit` (depleting+regen) | ✅ model; needs content |
| markets, money, borrowing, insurance | call auction; two-tier money; bank/bond/loan/futures/option/insurance | ✅ done |
| birth, ageing, death, inheritance | 6a–6d: `birth_tick`, `age`, `lifespan`, estate rule, lineage | ✅ done |
| safety vs. player scripts | money-scope invariant + capabilities + validators | ✅ done |
| scripting libraries (std / world / pack) | all three tiers injected read-only per run; install gate + submit-time lint refuse broken scripts; `pack.json` pins versions | ✅ Phases 1–3 — `docs/scripting.md` |
| **player sets OWN behaviour script** | `set_entity_behaviour` — ownership-gated autonomy path (docs/game.md §6) | ✅ done |
| join / onboarding | `POST /join` — founder entity + endowment + starter, config in `join.config` WorldSetting | ✅ done |
| victory observer + epoch records | `WorldSetting` (readable via `ctx.query.world_setting`); ownership, balances, unlocks, ticks all queryable | ✅ done (platform observer, Phase 2a — §14) |
| leaderboard / epoch records | all derived reads (owners, accounts, holdings, unlocks, stamps) | ✅ done (Phase 2a epochs, Phase 2c leaderboard — §14) |
| readable catalog of the world | `name` columns on every content model (unfilled); `world.manual` precedent (stone_age); pack manifests | §15 — Phase 3, planned |
| audit trail (per-entity readable action log) | events: per-tick, entity-attributed, status'd, hash-chained — the mechanism **already exists** | §15 — Phase 3, planned (render + surface) |
| governance cadence (windows) | proposals/votes/enact already exist; `round.state` readable by scripts | ✅ done (platform + content clerk, Phase 2b — §14.4) |

## 6. The control model and the one engine gap

**Existing.** `set_script` (`services.py`) is the *governed write surface*:
capability-gated by `LEGISLATE`, intended for proposal→vote→enact, and it is
about *authority* (an entity holding a capability), with no notion of
ownership. That is correct for legislation and must stay that way.

**The gap (now closed).** A player replacing *their own entity's* BEHAVIOUR
script is **autonomy, not legislation** — it needs no vote, only proof of
ownership. Phase 1 added an ownership-gated autonomy path — **implemented**
as `services.set_entity_behaviour` (engine) plus the player-facing
`POST /entities/{id}/behaviour` (API) — with these properties:

- **Scope:** BEHAVIOUR scripts only (never POLICY/VALIDATOR/HOOK — those are
  legislation/constitution). 
- **Authorisation:** the authenticated User must own the target entity
  (`entity.owner_id == user.id`). 
- **Refusal:** server-owned-and-fixed entities are refused (immutable tier,
  §4). "Fixed" is an attribute on the entity — `Entity.is_fixed` (resolved
  in Phase 1); both `set_entity_behaviour` and the legislation path
  (`set_script`) refuse it. The operator sets it at content time (admin
  API / scenario); no governed path may flip it.
- **Safety:** unchanged — the money-scope invariant still binds, so an
  autonomy script can only spend its own entity's money.
- **Lint (Phase 3):** the source is checked at submit time against the
  injected tiers with the install gate's strict standard. Citing
  vocabulary that is not injected (the nil-call trap that zombied the
  first live demo's founder) is a 400 with the finding in hand — the
  entity keeps its current behaviour. Synthetic-ctx errors a healthy,
  state-dependent script can still produce come back as `warnings` on
  the accepted script (`lint_warnings` on MCP).

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
  only by vote. ~~*Phase 1 must confirm* spawn grants no privileged
  capability by default~~ — **confirmed (Phase 1):** a child is born with an
  empty capability list even when the caller holds SPAWN + SEIZE + LEVY +
  MONETARY_AUTHORITY (`test_spawn_child_inherits_no_capability`); the
  platform `POST /join` path is the same (`test_join_does_not_grant_capability`).
  A player cannot farm capability by spawning or joining.
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
  untouched. For small worlds the clock can instead be driven by player
  consent — the readiness gate (§9.1) — with no scheduler process at all;
  the operator's advance remains the override either way.
- **Pace** is a platform config (tick period, K, window length) — operator's
  knob, not a WorldSetting.

### 9.1 The readiness gate — rounds close by consent

*Design note (pre-implementation). Status: shipped — register +
`/rounds/ready` + gate + `set_ready` + agent `--ready`.*

The v0 answer to "who ticks the world": **each round stays open until
every eligible player signals readiness** (or the operator overrides).
No scheduler infrastructure; a world of N agents and humans paces
itself. The operator's role shifts from clockmaker to referee — the
admin advance is the stall-breaker, which is the proper operator role:
exceptions, not metronome.

**Why it fits the batched model.** A round here is not "the bell rang"
but "everyone had their turn": all player agency is between-rounds
behaviour rewrites, so readiness — done-ness — is the semantically exact
close signal. Pacing then matches cognition, not wall-clock: fast agent
worlds run fast; a human thinking holds the world only while they are
actually thinking.

**Why last-mover advantage is a non-issue here.** The usual objection to
ready-checks is the trigger-holder acting last on fresh information. But
the submit window is *information-static*: no ticks run mid-window, no
orders execute (orders are tick-time intents), prices and books are
frozen at the last resolved tick. The last player to ready learns nothing
except *who else has committed* — weak signal in a static window — and
withholding is symmetric self-harm (it delays their own production as
much as anyone's). The equilibrium is "ready when done," which is the
desired behavior. A real-time game could not say this; a batched one
can.

**Mechanism (platform only; engine and `tick.py` untouched).**

- **Storage:** one `WorldSetting` register, `round.readiness` =
  `{"round": N, "ready": [user ids]}` — same pattern as `round.state`
  and the council registers. Reset to the next round on every advance.
  No new tables, no migrations.
- **`POST /rounds/ready`** (authenticated player): record the caller as
  ready **for the round open at POST time** — the server derives the
  round number; clients never supply it, which makes the advance race
  harmless (a straggler POST just lands in the new round). Idempotent.
  If this ready completes the set, **the round resolves in the same
  request** and the response carries the round summary. `DELETE
  /rounds/ready` un-readies — allowed until the advance fires (see
  rules).
- **Gate:** when the mode is `readiness`, the set of ready users ⊇ the
  eligible set closes the round. The operator path `POST
  /admin/rounds/advance` is unchanged and always works — the override,
  not the clock.
- **MCP:** one tool, `set_ready(ready=true|false)`; `round_state`
  (already public) carries the gate block. This completes the agent
  story: `AgentLoop.cycle()` ends with `set_ready`, and a multi-agent
  world self-paces with no admin in the loop (the last agent's ready
  fires the round).
- **Status:** `current_round_state` gains `"readiness": {"mode",
"ready": k, "eligible": n, "ready_users": [...]}` — public facts, like
  prices and standings; `status` stays `"submit"`.

**Rules.**

- **Eligibility:** a user is eligible iff they own ≥ 1 ACTIVE entity.
  This excludes eliminated dynasties (no ACTIVE entities — they cannot
  block a world they no longer play) and pure spectators (no agency in
  the round) by the same test. The operator is eligible only if they
  own a playing entity; otherwise they are the referee, not a player.
- **Empty eligibility set:** the gate does not block (genesis world,
  operator-only bootstrap) — admin advance is the only clock.
- **Un-ready allowed, cheaply**, until the advance actually fires: a
  premature ready means a stale behaviour for a whole round, which is
  real in-game pain; do not lock people into typos. Once the round
  resolves, readiness is historical.
- **Mid-window joins:** the new player becomes eligible the moment they
  own an ACTIVE entity; they ready like anyone else. Joins never
  un-ready anyone.
- **Concurrency:** the final-ready advance rides the register row —
  serialize on it (trivial under SQLite's single writer; the
  `round.state` row lock under Postgres). Two simultaneous final-readies
  must produce exactly one advance.

**Policy vs. mechanism.** The readiness machinery (register, endpoints,
gate check) is mechanism and ships once. *Who must consent* is world
policy: the gate mode lives as a WorldSetting (`"readiness"` |
`"operator"`), set by the operator at creation, default `"operator"`
(existing worlds and demo flows unchanged). Amendment of the gate by
legislation — a world voting its own clock policy — is exactly the kind
of question the mid-epoch amendment analysis (scripting.md §8) was
scoped for; nothing built here forecloses it. A wall-clock backstop
(`ECON_SUBMIT_WINDOW_SECONDS`, auto-ready on expiry) is deliberately
deferred until a public world shows an AFK-human problem worth solving.

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
| **0** | Content pack (goods/tech/recipes/needs/parcels) ✅ (demo-world; `stone_age` — #91/#92/#96); starter BEHAVIOUR template ✅; proving experiment ✅ — **complete** (§12.7's ask answered and exceeded by the LLM dynasty campaign it grew into: `experiments/agent`, #77–#105, nine postmortemed stone-age runs; run-9 = zero deaths, seller/crafter/buyer specialisation from identical models) | none for the pack itself — data + Lua; the campaign later demanded three engine mechanisms (#99/#101/#104, recorded in actors.md Status) |
| **1** | Ownership-gated autonomy path (§6) ✅; join/onboarding flow ✅; confirm spawn grants no privilege ✅; round scheduler ✅; MCP player interface ✅ | **complete** — autonomy + onboarding + spawn check + scheduler + MCP (platform only) |
| **2** | Epochs + victory observer + elimination records (§14.1–14.3) ✅; governance-window cadence (§14.4) ✅; leaderboard + publish (§14.5) ✅ — **complete** | none — platform (+ one content-pack clerk script); design in §14 |
| **3** | The readable world — names/descriptions/effect docs for everything, readable text for every action, the per-entity audit trail on the dashboard, and the content-pack standard envelope (§15) | small additive engine surface (`describe` module + columns); rest platform + content |
| **4** | Logistics (region graph + transport) — only if earned | engine, deferred |

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

*Result:* answered emphatically. `experiments/agent` ran the proving
experiment as the LLM dynasty campaign (#77–#105): dynasties driven by
NIM-hosted models (Llama-3.3-70B, Nemotron, gpt-oss) rewrite their own
BEHAVIOUR between readiness-gated rounds. Survival bounded ✅ (run-9:
all three houses alive at tick 600), markets clear ✅ (post-intermediated
plus the first house↔house trades — BERRIES at a 2× duress premium),
specialisation emerges ✅ (seller/crafter/buyer forks from *identical*
models). The pack shipped with a hand-written manual (`world.manual`,
#92), a trading post (#96), and survival economics robust enough to
postmortem nine runs on.

## 13. Open questions (decide as we hit them)

- **Rejoin after elimination:** ~~new-founder queue, or wait for next epoch?
  (Leaning: wait for next epoch — elimination should mean something.)~~
  **Resolved (Phase 2 design, §14.3):** wait for the next epoch. Elimination
  is stamped (immutable, epoch-scoped) by the round scan; the platform
  join path refuses a user eliminated in the running epoch. The epoch
  boundary is the fresh start.
- **Identity of fixed-tier mark:** ~~WorldSetting list vs. entity/script
  attribute.~~ **Resolved (Phase 1):** an attribute on the entity —
  `Entity.is_fixed`. Both governed paths refuse it; the operator sets it.
- **Office model for the `rule` victory condition:** how is "executive
  office" represented? Likely a WorldSetting naming the office-holder entity,
  set by a vote. (Phase 2, only if `rule` is used.)
- **Observability feed to MCP:** ~~full tick events vs. per-entity digest.~~
  **Resolved (Phase 1):** per-entity digest, with a sharper principle — the
  agent sees **exactly what its own behaviour script sees**. The digest
  filters events to `entity_id == own`, the same filter the engine applies
  when feeding BEHAVIOUR scripts each tick (tick.py). No omniscience, and
  parity: the agent reasons over the same world its script will observe.
  World-visible facts (round clock, market prices) are public to all
  authenticated players, as they are in-world.

**A real agent client exists** (`experiments/agent/`): an LLM-driven loop
that plays exactly this surface — observe (the parity set above), think
(system prompt carries the tier vocabulary from `get_script_libraries`),
submit (`set_behaviour`), with Phase 3's lint refusals and warnings fed
back into the next prompt. The first live run of it proved the arc end
to end: a hallucinated helper refused with the finding in hand, fixed in
one round-trip, zero script errors while the rewrite played.

---

## 14. Phase 2 detail — epochs, victory, windows, leaderboard

The durable spec for Phase 2, mirroring §12 (Phase 0). Everything here is
**platform or content**: no engine model, no migration, no new intent. The
two structuring insights, both inherited from what Phase 1 built:

- **Immutability by surface absence.** No Lua action writes arbitrary
  WorldSettings — the only script-writable keys ride dedicated intents
  (`set_fiscal_policy`, `set_constitution`, ...). An append-only register
  stored under a key no intent can reach (`victory.stamps`,
  `epoch.eliminations`) is immutable by the same doctrine as "capabilities
  don't breed": there is no path, not a promise.
- **Cadence bites at enactment.** The engine does not time-gate
  `create_proposal` or `vote` — and it should not. A proposal created
  outside a governance window simply sits unenacted until the next window
  close; *when enactment runs* is the platform's lever (§14.4). Proposals
  are cheap; enactment is the law.

### 14.1 The epoch model — condition as data, set once

- **`epoch.state` WorldSetting**: `{number, condition, started_tick,
  ended_tick, winner_user_ids}`. Absent ⇒ no epoch is running (the world
  plays without a victory condition; the observer is inert).
- **The condition is the §7 achievement spec**, `{code, params}` — e.g.
  `{"code": "accumulate", "params": {"threshold": 5000}}`. Codes are the
  §7 menu: `accumulate`, `innovate`, `endure`, `grow` (`rule` stays §13).
- **Set only at epoch start.** The operator (admin API) starts an epoch
  with a condition; the condition is frozen for the epoch's life —
  `ended_tick` is set (win or operator close), then a new epoch may begin
  under a new condition. §7's defence 2 (constitutional mid-epoch amendment
  + cooldown) is explicitly **post-v0**: not-amendable-at-all is the
  stronger, simpler version of lock-in, and amendments-only-affect-future-
  crossings (defence 3) holds trivially when there are no amendments.
- **Epoch records are the stamps themselves** (§14.2) plus the ended
  `epoch.state` rows — no separate history table.

### 14.2 The victory observer — stamp, don't judge

- **Runs inside round resolution**, after each tick of the K in
  `advance_round` — per *tick*, not per round: an `accumulate` crossing
  that dips back below before the batch ends still counts, which is the
  anti-flash-dump defence (§7.1) operationalized. Monotonic conditions
  (`innovate`, `endure`, `grow`) are crossing-safe by construction.
- **Evaluation is pure reads** over engine tables, per dynasty
  (`owner_id`):

  | code | crossing when | reads |
  |---|---|---|
  | `accumulate` | Σ balances of accounts of owned ACTIVE entities ≥ threshold | Account |
  | `innovate` | any owned entity holds an unlock of tech `technology` | Unlock, Technology |
  | `endure` | tick ≥ started_tick + `ticks` and ≥ 1 owned ACTIVE entity | Tick, Entity |
  | `grow` | count of owned ACTIVE entities ≥ threshold | Entity |

  Dynasty scope is ownership, which already propagates through both birth
  paths (§8). Evaluations run only for players with ≥ 1 owned ACTIVE
  entity (the eliminated cannot win a *future* crossing — but a stamp
  already made stands forever).
- **The stamp.** On a genuine crossing the observer appends to
  `victory.stamps`: `{epoch, user_id, tick, code, value}`. **First
  crossing ends the epoch**: `ended_tick` = that tick, `winner_user_ids`
  set. Players crossing at the *same* tick co-stamp as co-winners (a tie
  is a result, not a dispute). The stamp *is* the win — no intent, vote,
  or script can write the key (surface absence, above).
- **Elimination records.** The same round-level scan appends to
  `epoch.eliminations` when a player who owned entities in the epoch has
  none ACTIVE: `{epoch, user_id, tick}`. Also append-only, epoch-scoped.

### 14.3 Rejoin after elimination — resolved

An eliminated player may not `join` again until the next epoch: the join
path checks `epoch.eliminations` for the *running* epoch and refuses
(409). When the epoch ends, the register becomes historical and the player
may found again. Elimination means something; the epoch boundary is the
fresh start. (This is the platform join path — a *player policy decision
encoded as data + one check*, not an engine birth gate; both birth
mechanisms are untouched.)

### 14.4 Governance windows — derived state, a clerk, and enactment

- **N is pace, not world-kind** → `ECON_ROUNDS_PER_WINDOW` env (default 5),
  exactly like `ECON_TICKS_PER_ROUND`. The window is **derived, never
  stored**: round `r` is a window round iff `r % N == 0`.
- **Visibility:** `GET /governance/current` — is the current round a
  window, round number, N, open proposals with live tallies. Public to
  authenticated players (it is an in-world fact), and MCP-exposed.
- **Enactment is the clerk's job.** The content pack ships a **clerk**: a
  server-owned polity entity holding `LEGISLATE` (and
  `AMEND_CONSTITUTION` for constitutional proposals — operator-granted at
  content time, capabilities arrive only by grant, §8) whose **POLICY
  script** reads `round.state` — already a WorldSetting scripts read via
  `ctx.query.world_setting` — and, on window rounds, enacts every passed
  proposal via the ordinary `enact` intent. Same mechanism any polity
  uses; **no new engine surface**. The mechanism/data/policy split stays
  intact: *when laws pass* is policy; *how laws pass* is mechanism.
- **Admin convenience:** `POST /admin/governance/enact` force-runs the
  same enactment through the same intent path (a by-election button, not
  a second law-making surface).
- **Out-of-window proposals are legal but dormant** — created by any
  script at any tick, they wait for a window close. Cadence is a property
  of *effect*, not of speech.
- **How the clerk learns N (built note).** Scripts cannot read env, so
  each advance re-projects N into `round.state` as
  `rounds_per_window` — the same channel the clerk already reads. Env
  stays the single source; the window stays derived, never stored.
- **Sweep timing (built note).** An advance writes the round counter
  *after* its batch, so the clerk first sees "round r resolved" on round
  r+1's ticks: a window closing at round r lands its decision as round
  r+1 opens. `ctx.state.last_window_swept` makes the sweep fire once per
  close, not once per tick. The sweep *decides the whole docket* — a
  failed tally closes a proposal FAILED (election-day semantics), leaving
  no zombie OPEN rows.
- **Clerk capabilities (built note).** The enact gate needs LEGISLATE /
  AMEND_CONSTITUTION; the laws it applies exercise *operating*
  capabilities too (a fiscal proposal runs `set_fiscal_policy` as the
  polity), so the content-pack clerk is granted SET_FISCAL_POLICY at
  content time. Capabilities arrive only by grant — the operator is the
  genesis grantor.

### 14.5 Leaderboard — publish the round

- **`GET /leaderboard`**: per dynasty, one row — money total, entity count
  (ACTIVE/total), oldest lineage age, tech unlocks, epoch wins (count of
  stamps), status (active / eliminated-this-epoch). A pure platform read
  over engine tables + stamps. Public to authenticated players.
- **Round publish** (§9.3): `RoundSummary` already aggregates the round's
  events; the leaderboard stays a separate endpoint — standings are a
  standing query, not per-round payload.
- **MCP:** two new public-fact tools, alongside `round_state` and
  `market_prices`: `epoch_state` (epoch number, condition, winner, your
  elimination status) and `leaderboard`. Public facts only — no
  per-dynasty detail beyond the standings row (no omniscience, §13).

*Status: shipped (Phase 2c).* `econ/api/leaderboard.py` is the pure read;
`GET /leaderboard` and the MCP `leaderboard` tool both serve it. The money
column reuses the observer's own dynasty-money definition
(`epochs.dynasty_money`, promoted public for exactly this) so the standings
can never disagree with a stamped `accumulate` win; `oldest_age` skips
members predating age tracking (NULL `birth_tick`, Step 6a) rather than
reading them as newborns; `unlocks` counts entity-scoped unlocks only
(world-scope belongs to the world, the same join `innovate` uses);
`epoch_wins` counts the player's stamps across all epochs. Status is
`active` / `eliminated` (stamped in the *running* epoch's register) /
`extinct` (a dead dynasty outside the running epoch — an earlier epoch's
elimination or death before any epoch), with `active` winning over a stale
stamp. Ranking is deterministic: epoch wins desc, then money desc, then
user id asc — wins first because the epoch's victory condition is the
point of play (§7), money only breaks ties within it. Rows come from
`Entity.owner_id` (dynasties, not accounts): server-owned entities are
invisible and a never-joined player has no row; entity rows are never
deleted, so every player who ever played keeps theirs. Tests:
`tests/test_api_leaderboard.py` (16), including MCP-payload-equals-REST.

### 14.6 Build order

1. **2a — epochs + observer + eliminations:** `epoch.state` /
   `victory.stamps` / `epoch.eliminations` registers, per-tick observer in
   round resolution, admin epoch endpoints, join rejoin-check, MCP
   `epoch_state`. *(platform only)* — *done (PR #67)*
2. **2b — governance windows:** derived window state + endpoints + the
   clerk script in the content pack + admin enact. *(platform + content)*
   — *done (PR #68)*
3. **2c — leaderboard:** standings endpoint + MCP `leaderboard`.
   *(platform read)* — *done; Phase 2 complete*
4. **2d — readiness gate (§9.1):** `round.readiness` register +
   `POST`/`DELETE /rounds/ready` + gate on final ready + MCP `set_ready`.
   *(platform only, specced)* — *done*

Each independently shippable; 2a first (windows and standings both
reference epochs).

---

## 15. Phase 3 detail — the readable world: catalog, audit trail, and the pack standard

The durable spec for Phase 3, mirroring §12 (Phase 0) and §14 (Phase 2).
The asks that motivated it — a readable per-entity log of actions
("bought 2 ORE for 10 COIN", "gathered berries", "made a bag"),
readable names and short descriptions for everything in the world,
conditions named with their effects documented, all riding a standard
content-pack format so packs compose and third parties can author them —
are one thing under one principle:

> **Status: design only (§15.5 build order: 3a catalog → 3b registry +
> audit trail → 3c dashboard → 3d pack envelope; nothing landed yet).
> The hand-written stone_age manual and pins-only `pack.json` are the
> pre-Phase-3 state this section replaces.**

**Legibility is a product surface.** The players are LLM agents and,
soon, modders-by-pack. A world whose vocabulary is machine-shaped —
GRAIN, MINE_ORE, `{"type": "trade", "market": "ORE", ...}` — is
unplayable from a prompt and unauthorable by a stranger. `stone_age`
proved the demand by hand (a `world.manual` WorldSetting folded into
every agent prompt); this phase makes it generated, total, and
standard.

Where each piece sits in the mechanism/data/policy split (`design.md
§2`):

| piece | layer | status |
|---|---|---|
| event recording | mechanism | **already built** — every intent resolution and pass outcome is an entity-attributed event with status, hash-chained per tick |
| names, descriptions, action templates | data — content packs | `name` columns exist on Good/Recipe/Technology/Need/Market (unfilled); `description` is additive |
| rendering (event → sentence) | presentation — a pure read | new `econengine.describe` module: session-in, no writes |
| pack envelope, install/merge rules | platform + data | generalises the pack.json pinning that already ships (scripting.md §5) |

The property that makes all of it cheap: **rendered text is a pure
function of (event payload, catalog rows), computed at read time.** It
never enters `events_hash`, so determinism, replay, and the RNG
commit-reveal chain are untouched — and a replayed world renders
identically.

### 15.1 The catalog — names, descriptions, self-documenting conditions

- Fill `name` (short label: "Iron Ore") and add `description` (one or
two sentences) on Good, Recipe, Technology, Need, Market. Entities
already carry names. One additive migration; `""` defaults keep every
existing world valid.
- **Derived where derivable, authored where meaningful.** A
condition's effect line is *generated from its row* — HUNGER:
"condition — granted 1 per fully-unmet FOOD tick; decays 5%/tick
(equilibrium ≈ grant ÷ decay); incapacitates at 15" — and likewise
`modifies` ("while held, effective LABOR-* × 0.5"), auto-issue, decay,
branch tables with odds and labels, recipe requirement gates. Prose
cannot drift from physics because the prose is a function of the
physics; the authored `description` carries only what the row cannot
say (flavor, advice). This promotes the stone_age module-docstring
knowledge from comment to generated output.
- `GET /catalog` + MCP `world_catalog`: the whole readable world,
grouped by pack — goods with condition effect lines, recipes
(inputs → outputs, duration, gates, branches), the tech tree, needs,
and actions (§15.2). The agent loop folds it into the system prompt
where the hand-written manual sits today — the §13 parity doctrine
extended from script vocabulary to world vocabulary: the prompt and
the script read the same catalog.

### 15.2 The action registry — readable text for every action

- Every intent type gets a sentence template. The engine ships the
base set for its own vocabulary (~30 types: transfer, place_order,
start_process, spawn_entity, levy, seize, vote, enact, ...); packs and
products extend the registry for intent types they add — the same
tier doctrine as script libraries (scripting.md §2). Pack display
sections are validated at install (a template may reference only
params the intent actually carries) and pinned in the manifest like
any lua/ file: display text feeds agent prompts, so it is a replay
input, and drift must refuse rather than silently re-render.
- Outcome events render too, not just intents: fills ("sold 2 ORE for
10 COIN at clearing 5" — trades already emit per-side events), process
lifecycle ("mined ore" ← MINE_ORE's name; "made a bag"; branch
labels: "hunted — and ruined the spear"), need outcomes, unlocks,
spawns, incapacity, estate application.
- **The registry is total.** A test walks every event type the engine
can emit and asserts a renderer exists — an unrenderable action is a
build failure, not a silent gap in the log.
- One registry, two reads: each action's entry also declares its
params, required capability, and ownership rule, so the same rows that
render the log are the "API docs" manifest of actions (§15.1).

### 15.3 The audit trail — a read, not a write path

- `GET /entities/{id}/activity`: scan ticks descending, filter events
by entity attribution, render through the registry with a catalog
join. Rejections are included ("bid 12 for BREAD — refused: insufficient
funds"): an attempt is an action. The world-level `GET /activity`
carries the unattributed public facts (auction summaries, decay,
unlocks) — the same public/private cut as §13: your log is your own
events; the world's log is public facts.
- MCP `entity_activity`, own entity only — parity again.
- **Dashboard**: a "world log" section (with a per-dynasty filter) on
the live dashboard, fed from a bounded rendered tail in the per-round
snapshot so the self-contained-HTML doctrine holds — the artifact
still carries the whole story offline.
- Counterparty enrichment (a trade event naming both sides) is an
additive payload change and rides the existing pinning discipline:
changing event shape is an engine-version change, exactly what pack
manifests pin.
- A retention tension is flagged, not solved here: the modelling
product's event-body retention policy (design.md § fast-forward)
bounds the audit trail's depth. Per-world retention is the knob; the
hash chain stays forever either way.

### 15.4 The content-pack standard — the mod envelope

- `pack.json` grows from pins-only into the standard envelope:
  **identity** (`id`, `version`, display name/description),
  **compatibility** (`engine_std` pin — shipped; `requires`: other
  packs by id + version), **content** (declarative
goods/tech/recipes/needs/markets/genesis cast; lua/ scripts — pinned,
  shipped), **display** (names, descriptions, templates).
- The shape of a pack will keep evolving as things scale; v1
standardizes the **envelope, identity, pinning, and conflict rules** —
the parts every consumer must agree on — not every content field
forever. Pack-format versions get migration treatment like any other
schema.
- **Installer rules.** Packs compose at world creation (the
join-composition seam scripting.md names): a world is created by
choosing which packs to load. Overlapping claims (two packs defining
GRAIN) are an install-time error unless an explicit override is
declared. Content rows gain `pack_id` provenance (additive column) so
the catalog attributes every row and a world knows what it is running;
live amendments (proposal → enact) edit rows as ordinary votable
data — provenance records origin, not ownership.
- **Voted-in packs** are the platform-era extension: a proposal type at
the constitutional tier whose enactment is an install + pin update
through the ordinary path. Third-party packs ride the same gate the
demo packs already pass (install-time validation, manifest pinning) —
authoring a mod is authoring data + Lua against published tiers. The
resolved pack set + versions enters replay inputs alongside the std
pin.

### 15.5 Build order

1. **3a — catalog:** name/description columns + fills for both packs +
   `GET /catalog` + MCP `world_catalog`; the stone_age manual becomes
   a generated artifact. *(one additive migration; data + platform
   read)*
2. **3b — registry + audit trail:** `econengine.describe` (pure) + the
   total-coverage test + `/activity` reads + MCP `entity_activity`.
3. **3c — dashboard:** the world-log section on the live dashboard.
4. **3d — pack envelope v1:** identity/compat/provenance/conflict
   rules; migrate demo-world and stone_age; world-creation pack
   selection.
5. *(platform-era)* voted-in packs; third-party distribution.

Each independently shippable; 3a first — the registry renders through
the catalog, and the agent prompts improve the moment it lands.
