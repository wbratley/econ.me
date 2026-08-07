# Roadmap: multi-actor control and the safe action API

Goal: let different *kinds* of actor drive the economy through one safe
interface, with the engine — not a gentleman's agreement — enforcing what
each may do.

- An **individual** entity controlled directly by a human or an AI, via the
  HTTP API (and, eventually, an MCP server).
- A **government** entity controlled by a rule system — deterministic policy
  today, votable policy later — that can tax and spend.
- A **corporate** entity controlled by its shareholders.

The common theme: every actor acts through the same intent protocol, and
every privileged action is gated by the engine so that no actor can do
something the rules do not grant — seizure, tax policy, money creation are
*capabilities*, not requests.

This document is a roadmap. The governing architecture principles live in
`docs/design.md` (esp. §2 *mechanism/data/policy* and the §military
restatement of the ownership invariant). Read that first; this is the
build plan for the platform-era controls on top of it.

## What already exists (do not rebuild)

| Need | Already present |
|---|---|
| One safe action surface | `POST /intents` (`econ/api/routers/intents.py`) runs the *same* `scripting.resolve_intent` the tick loop uses. No duplicated rules. |
| Ownership enforcement | The **ownership invariant** in `resolve_intent`: an intent moves money/goods only out of accounts owned by its entity. |
| Who acts as an entity | `entity.owner_id == user.id`; JWT auth; `is_admin` for god mode. |
| Money creation privilege | `is_monetary_authority` flag + `issue_money`/`retire_money`. |
| Extensible policy gates | **VALIDATOR** scripts (pure, veto every money op, fail-closed) and **HOOK** scripts (post-op, can queue intents, non-recursive). Global or per-entity. |
| Votable parameters | `WorldSetting` rows (estate rule, compute budget already use this). |
| Entity types | `INDIVIDUAL`, `BUSINESS`, `BANK`, `GOVERNMENT` in the enum. |
| Government actor precedent | `treasury.lua` — a GOVERNMENT entity running a POLICY script. |
| **Enforced** non-consensual asset movement | The **estate rule** (`conditions.py`): on incapacity the engine moves *all* an entity's assets per a votable rule, without the owner's consent, by engine authority. |

The last row is the key. The engine already moves assets against an owner's
will — it just only does it for *death* today.

## The engine primitive that was missing, now half-built

**Enforced state action: tax and seizure.** In the inequality experiment
tax is *voluntary* — `firm.lua` chooses to remit. That is fine while one
experimenter controls every script; it collapses the moment a
shareholder-controlled firm simply does not pay. `design.md` (§military)
already anticipated this: *"assets move by the owner's intent, or by
engine mechanism under explicitly-declared votable rules — estate transfer
at incapacity, enacted policy (taxes, expropriation), and contest
outcomes."*

**Tax is now enforced** (step 2): `services.levy` generalises the estate
rule from death to policy — an entity holding the `levy` capability compels
a money transfer out of an account it does not own, into its own, under a
declared `rule_ref`, and a VALIDATOR may veto it (fail-closed). **Seizure**
(outright expropriation of goods/parcels, not just money) shares the same
mechanism under a future `seize` capability and is still unbuilt.

So the government can now compel *money* and set its own rates votably.
Compelling goods (seizure) is the remaining unbuilt half of this primitive.

## The forks (decisions, with the chosen option marked)

### Fork 1 — Where does "the state's will" live? (enforcement)

- **A. Status quo — voluntary script.** Dead for independent actors.
- **B. Validator-amend.** Promote VALIDATOR scripts from pure veto to
  *transform* an op (split a wage transfer, divert the tax fraction). Fires
  at the one choke point. Cost: validators become impure; the doc currently
  forbids that to keep recursion impossible.
- **C. New `levy` engine mechanism — generalise the estate rule.** *(chosen)*
  `services.levy(authority, from_account, to_account, amount, rule_ref)`
  callable only by an entity holding the `levy` capability, moving assets
  it does not own under a declared votable rule. Mirrors `_apply_estate`,
  triggered by policy instead of death. Design-doc-endorsed.

### Fork 2 — How are actors authorised? (capabilities)

- **A. Keep binary** (`owner_id` / `is_admin`). A government would need
  `is_admin` to change tax → wrong (god mode for policy).
- **B. Entity capabilities/roles.** *(chosen)* A capability model so a
  GOVERNMENT entity holds `levy` / `set_fiscal_policy` / `seize`, an
  INDIVIDUAL holds none. The API and `resolve_intent` check capability per
  action. Explicit, auditable, testable.
- **C. Everything-is-a-validator.** Encode "only government may change tax"
  as validator logic on `entity_type`. Most flexible, hardest to audit.

### Fork 3 — How does an external actor (AI/user/MCP) drive an entity?

- **A. Fully external.** *(chosen for AI/user)* No BEHAVIOUR script; an
  agent polls state and posts to `/intents` between ticks. Tick paces
  externally (real-time world) or on demand.
- **B. Hybrid with overrides.** Entity keeps an autonomous script; the
  agent sets strategy params in `script.state` rather than micro-acting.
  Best for agents that cannot be real-time every tick.
- **C. Script-is-the-AI.** Lua calls out to an LLM — inflexible; rejected.

An **MCP server is orthogonal** — a thin adapter over `/intents`,
identical under A or B. Add once the intent surface is finalised.

### Fork 4 — Government policy: who sets it, how?

- **A. Admin sets policy** (status quo for estate rule / compute budget).
- **B. A GOVERNMENT entity sets policy via privileged intents** gated by
  capability. *(minimum viable for this goal)*
- **C. Deterministic policy script + votable parameters.** The "AI
  government" is a POLICY script reading `WorldSetting`s; citizens vote on
  the *numbers* (tax rates), not the code. Pragmatic middle.
- **D. Full proposal→vote→enact cycle** (`design.md §4.1`). Most
  ambitious; stage after B/C work.

### Fork 5 — Corporations controlled by shareholders

- **A. Autonomous firm, passive shareholders** (status quo).
- **B. Shareholders vote on firm parameters** (dividend %, production mix)
  stored as votable rows; firm script reads them.
- **C. Board/cap-table model.** *(chosen if realism is wanted)*
  Shareholders vote *by share quantity*; enacted directives bind the
  firm's BEHAVIOUR script. Reuses the Fork 4 voting machinery.
- **D. Firm "owned" by largest shareholder** who directs it like any
  entity. Simplest; breaks when shares trade and ignores minorities.

## Build order (smallest coherent slice first)

Each step is independently useful and unblocks the next.

1. **Capabilities** (Fork 2B) — a capability model + a check in
   `resolve_intent`. Makes `/intents` safe for arbitrary actors with zero
   new action surface. *Re-expresses `is_monetary_authority` as the
   `monetary_authority` capability (backward compatible).*
2. **Levy mechanism** (Fork 1C) — generalise `_apply_estate` into
   `services.levy`, callable only by an entity holding `levy`. Tax becomes
   enforceable and stops being a polite request. *— done (see Status).*
3. **Government as policy actor** (Fork 4B) — privileged intents to set
   fiscal `WorldSetting`s and fire levies. Replaces admin-god-mode for
   fiscal policy. A POLICY script on the government entity that fires
   `levy` each tick per the votable rate schedule makes collection
   automatic *and* enforced *and* votable (mechanism/data/policy split).
   *— done (see Status).*
4. **Governance: voting on code** (Fork 4D + 5C) — `set_script`/`legislate`
   so citizens enact new POLICY scripts (new tax types, not just rate
   tweaks), a proposal→vote→enact cycle with citizen *and* share-weight
   models, and a constitutional tier protecting validators. Parameter
   voting (step 3) stays the common case; code voting is the rare path
   that makes the world self-governing. *— 4a-1 done (`set_script`); design
   written (see "Step 4 design" below).*

### A correctness note for step 2

A levy the *government* originates must bypass the ownership invariant
*for the government only, under a declared rule*. That is exactly what
`_apply_estate` does for death, so the precedent is sound — but the gating
(capability + a votable rate schedule) is where all the safety lives.
Review that boundary carefully and seizure/tax share one bulletproof
mechanism.

## Step 4 design: governance — voting on code, safely

### The gap step 3 left

Step 3 made tax *enforced* (`levy`) and *votable* (the `fiscal_policy`
dict): citizens change the **numbers** a fixed POLICY script reads. What
they still cannot do is change the **law itself** — introduce a new tax
base the script does not handle, replace the collection logic, or enact
any policy the active script's vocabulary does not already cover.
Parameter voting is the cheap, frequent 80% of legislation; but a polity
that can only tune dials and never write new statutes is not
self-governing. Step 4 closes that gap: **voting on code**, under the
same enforced floor.

This realises Fork 4D (full proposal→vote→enact) and Fork 5C
(shareholder cap-table governance), consistent with `design.md` §4.1.

### Engine vs platform — where the line is

`design.md` §4.1 places the governance *cycle* in the platform layer (a
consumer of the engine). The split this design holds to:

| Engine owns (step 4 builds) | Platform drives (out of scope here) |
|---|---|
| Governed script lifecycle (`set_script`) | Proposal authoring UI, campaigning |
| Proposal + vote + enact as governed ops | Enactment-day cadence / scheduler |
| Vote-weight from existing engine data | Trials (copy-on-write fork audit, §4.2) |
| Constitutional tier (validator amendment) | Fork petitions (§4.2) |

The engine exposes primitives the platform consumes via the intent
protocol; it does not own *when* enactment happens or *how* players
campaign. Enactment is a governed engine op the platform triggers; the
tally and the atomic application live in the engine so they are auditable
and cannot be bypassed.

### The safety thesis (why voting on code breaks nothing)

Four nested layers, each constraining the ones below, none depending on
authorship:

> **engine invariants (un-votable)** ⊃ **voting-system integrity
> (constitutional-only)** ⊃ **validators = the constitution
> (supermajority)** ⊃ **ordinary law: POLICY scripts + parameters
> (simple majority)**

1. **Code-author-independence.** An enacted POLICY script runs with the
government entity's capabilities and is vetoed by VALIDATORs —
   regardless of who wrote it or how it got there. A script enacted by a
   51% vote has exactly the powers and limits of one an operator pasted
   in. Democratic authorship adds no new engine powers; it only changes
   the authorship path.
2. **Validators are the constitution.** A citizen-enacted script that
   tries to levy 100% into one citizen's account is still vetoed by a
   validator capping rates (step 3 demonstrates this). Without this
   backstop, code-voting is a tyranny-of-the-majority / flash-loan trap.
3. **Constitutional tiering.** If citizens can amend validators, they can
   repeal the constitution. So validator (and voting-system) changes sit
   at a higher threshold (supermajority), and the voting system's own
   integrity is un-votable except through that process.

### Forms of government are data, not mechanism

The mechanism (`proposal → vote → enact`) is **form-agnostic**. A "form
of government" is three pieces of data, never new code:

1. **the electorate** — who may vote,
2. **the weight function** — how much each vote counts,
3. **the threshold** — how much weight "yes" needs to enact.

So direct democracy, a corporation, a monarchy, a council, and a
representative chamber are all the same machinery with different rows:

| Form | electorate | weight | threshold |
|---|---|---|---|
| Direct democracy (4a) | all active individuals | 1 each | simple majority |
| Corporation (4c) | holders of the firm's share | shares held (cap table) | majority of shares |
| Autocrat / king | {the sovereign} (or none — direct `set_script`) | 1 | 1 (sole voter) |
| Council / oligarchy | a council register | 1 each | majority of council |
| Weighted council | council register | per-member weight (register) | majority of weight |
| Representative (MPs) | the MPs | each MP = constituency size | majority |
| Liquid democracy | all individuals | own vote + all delegated to you | majority |

This is the mechanism/data/policy split applied to governance itself.
Every row is "add a weight-model resolver + some data," not new mechanism.

### The engine primitives

**4a-1. Governed script lifecycle — `set_script`.** *Done.* The privileged
write surface for POLICY/BEHAVIOUR/HOOK scripts. Retire-old +
activate-new (never in-place edit) so every enacted law leaves a lineage
of retired predecessors — auditable, revertible, sandbox-triable.
`lineage_id` is the stable identity; `name` is auto-versioned
(`{lineage_id}#{n}`). Gated by the `legislate` capability at the intent
boundary and in the service. VALIDATOR scripts are excluded (they are
the constitution, 4b's job) — and no validator gates `set_script` itself,
so the legislature cannot be locked out by what it governs. A HOOK fires
for audit. Read side: `ctx.query.active_script(lineage_id)` +
`script_history(lineage_id)`; script entry: `ctx.action.set_script(...)`.

**4a-2. Vote weight — a pluggable resolver, not a hardcoded model.** *Done*
(`engine/econengine/weights.py`). The weight function is a small registry
of `model-name → (electorate-finder, weight-finder)`, resolved per-proposal.
The electorate and weights come from existing engine data, so there is no
new voting token:

- *citizen* — electorate = all active INDIVIDUALs, weight = 1 each;
- *share* — electorate = holders of a symbol, weight = quantity held
  (reuses `ctx.query.holders`, the cap table);
- *council* / *weighted* / *representative* / *liquid* — later resolver
  entries backed by a register/WorldSetting, never new mechanism.

Shipping only the *citizen* resolver in 4a-ii keeps the slice minimal
while making every other form "register an entry + add data," not
"reopen the mechanism."

**4a-3. Proposal → vote → enact.** *Done.* Three engine models and ops:

- `Proposal`: a batch of proposed mutations (`set_fiscal_policy` and/or
  `set_script`), a weight model, a threshold, a quorum, a status.
- `vote` intent: an entity casts for/against; the engine records the
  entity's weight (computed by the resolver, not self-declared).
  Idempotent per entity per proposal.
- `enact`: triggered by the platform's cadence (or admin in MVP) — if
  tally ≥ threshold *and* quorum met, apply the proposal's mutations
  **atomically** (all-or-nothing, one savepoint); else mark failed.

Enactment reuses `resolve_intent`: each mutation is resolved through the
same dispatcher, so capability gates and validators fire exactly as for a
live intent. An enacted `set_script` therefore still needs `legislate`,
and a levy inside an enacted POLICY script still hits the constitutional
cap.

**4a-4. Constitutional tier — `amend_constitution`.** *Done.* A distinct
proposal type (`ProposalType.CONSTITUTIONAL`), requiring a
**supermajority** (the floor held in the `constitution` world setting,
`engine/econengine/constitution.py`; default two-thirds), that may
add/amend/retire a VALIDATOR script (`set_validator`) or amend the
voting-system floor (`set_constitution`). Both are gated by the
`amend_constitution` capability; both are reachable only through a passed
constitutional proposal — `set_script` still cannot touch validators, so
this is the *only* path that writes one. Below it, ordinary
`set_script`/`set_fiscal_policy` proposals cannot touch validators at all
(the tier check at propose time rejects an ordinary proposal carrying a
constitutional mutation; a constitutional amendment may also carry
ordinary law, since a harder bar may say more). The one intent whose
required capability is not a pure function of its name: `enact` left
`INTENT_CAPABILITIES`, and the enact branch checks `legislate` (ordinary)
or `amend_constitution` (constitutional) from the proposal's tier.
(Judicial review / a constitutional court that strikes down an enacted law
is *already* the validator layer — it costs nothing extra.)

### Build sequence

- **Step 4a-i — `set_script`.** *Done* (this PR). The minimum that
  answers "can citizens enact new laws?": yes, via the governed lifecycle.
- **Step 4a-ii — proposal/vote/enact + citizen resolver.** `Proposal`/
  `Vote` models, `create_proposal`/`vote`/`enact`, citizen weight model,
  simple-majority + quorum. A citizen vote (not an operator) drives
  `set_script`.
- **Step 4b — the constitutional tier.** *Done.* `amend_constitution` at
  supermajority (default two-thirds, held in the `constitution` world
  setting); `set_validator` / `set_constitution` are the only paths to the
  VALIDATOR scripts and the voting-system floor, and ordinary proposals
  cannot reach them.
- **Step 4c — shareholder governance (Fork 5C).** The *share* resolver;
  enacted directives bind a firm's BEHAVIOUR script.

Parameter voting (step 3) stays the common case throughout: most
proposals are still `set_fiscal_policy` edits, because most legislation
*is* rate changes.

### Decisions (locked) & deferrals

- **`set_script` semantics: retire-old + activate-new** (not in-place) —
  chosen to preserve a full legislative lineage.
- **Direct democracy MVP** (every citizen votes on every proposal);
  representative is a later weight model.
- **Cadence-agnostic enactment** — admin/platform calls `enact` in MVP.
- **Defaults** — simple majority + modest quorum, both constitutional
  params.
- **Deferrals** — delegation/liquid democracy (a weight-redirect rule +
  delegation register; a weight-function refinement, not new mechanism);
  bicameral/multi-body enactment (a richer enactment condition); federated
  polities. Trials (§4.2) are platform (CoW fork audit), out of engine
  scope.

### What stays explicitly unbuilt (engine)

`seize` (expropriation of goods/parcels, not money) remains its own
capability sharing the levy mechanism — orthogonal to governance,
landable anytime.

## Status

- Step 1 — **done**. Capability model landed: `Entity.capabilities`
  (JSON set) + `Entity.has_capability()` as the single check site;
  `engine/econengine/capabilities.py` declares the vocabulary and the
  `INTENT_CAPABILITIES` registry; `resolve_intent` gates at the same
  boundary as ownership; `services.issue_money`/`retire_money` and the
  API's monetary-authority check now route through `has_capability`.
  `is_monetary_authority` is kept as a backward-compatible alias implying
  the `monetary_authority` capability, so existing worlds keep working.
  Admin grants capabilities via `PATCH /admin/entities/{id}` (granting
  power is itself privileged). `levy`, `set_fiscal_policy`, `seize` are
  declared but not yet wired to actions.
- Step 2 — **done**. The levy mechanism landed: `services.levy(authority,
  from_account, to_account, amount, rule_ref, ...)` generalises
  `conditions._apply_estate` from death to policy — money moves out of an
  account the authority does NOT own, into its own treasury, by engine
  authority. Safety is all in the gating: the `levy` capability is checked
  at the intent boundary (`INTENT_CAPABILITIES["levy"]`) AND in the
  service (`MissingCapabilityError`); the recipient account must be the
  authority's own; a VALIDATOR may veto the op (fail-closed — a broken
  policy gate never silently seizes); `rule_ref` rides `ctx.op` as the
  audit key. Reachable from every actor surface: `POST /intents`, the tick
  loop, and `ctx.action.levy(...)` from scripts (the stub step 3's policy
  actor drives). Movement is money-conserving (a DEBIT/CREDIT pair, like
  `transfer`); the levy-ness lives in op-type + `rule_ref`, not a new
  transaction flavour. `seize` (goods/parcels, not money) remains unbuilt
  and will share this mechanism under its own capability.
- Step 3 — **done**. The government is a policy actor. The
  mechanism/data/policy split (`docs/design.md` §2) is now complete for
  tax: **mechanism** = `services.levy` (step 2); **data** = the
  `fiscal_policy` `WorldSetting`, a votable JSON dict (`fiscal.get_/set_`);
  **policy** = a government POLICY script that reads it via
  `ctx.query.fiscal_policy()` and fires `ctx.action.levy(...)` each tick.
  The `set_fiscal_policy` intent (capability `set_fiscal_policy`, wired
  into `INTENT_CAPABILITIES`) replaces admin god-mode for fiscal policy:
  whoever owns the government enacts the *numbers* (rates, schedules)
  without touching code — a set_fiscal_policy intent one tick changes
  collection the next, the script untouched. All the safety is in the
  gating: the capability, plus a VALIDATOR veto that makes a validator a
  *constitutional constraint* on the rate (fail-closed — an over-cap
  rate is refused and the incumbent policy survives). The policy rides as
  a JSON string in intent params (params are stringly typed) and is
  replaced wholesale (atomic, auditable). Reachable from every actor
  surface: `POST /intents`, the tick loop, and
  `ctx.action.set_fiscal_policy({…})` from scripts.
- Step 4 — **in progress (4a-1 done)**. See "Step 4 design: governance —
  voting on code, safely" above for the engine/platform boundary, the
  safety thesis, the forms-of-government-as-data framing (weight-model
  resolver), and the build sequence. **4a-1 (`set_script`/`legislate`)
  landed**: governed script lifecycle — retire-old + activate-new with
  full lineage history (`lineage_id` identity, `name` auto-versioned),
  capability-gated, validators excluded (they're the constitution). No
  validator gates `set_script` itself. Read side `ctx.query.active_script`
  / `script_history`; script entry `ctx.action.set_script`. **4a-ii
  landed** (`Proposal`/`Vote` + `create_proposal`/`vote`/`enact`):
  participation is the electorate (the pluggable weight-model resolver,
  `engine/econengine/weights.py`; `citizen` = 1 per active INDIVIDUAL);
  the tally is threshold-of-cast-weight AND quorum-of-electorate;
  enactment applies the proposal's mutations atomically as the target
  government through `resolve_intent`, so a citizen-passed over-cap rate
  is still vetoed by a VALIDATOR (the constitutional backstop, tested).
  Read side `ctx.query.proposal` / `proposals` / `tally`; admin `GET
  /admin/proposals`. **4b landed** (the constitutional tier):
  `ProposalType.CONSTITUTIONAL` + `set_validator` / `set_constitution`,
  both gated by `amend_constitution` and bound by the supermajority floor
  in the `constitution` world setting
  (`engine/econengine/constitution.py`; default two-thirds). `set_script`
  still cannot touch validators — `set_validator` is the only path, and an
  ordinary proposal cannot carry one (the tier check at propose time).
  `enact` is now data-driven: ordinary → `legislate`, constitutional →
  `amend_constitution`, checked in the enact branch (the one intent whose
  required capability is not a pure function of its name). An installed
  validator binds the very next op, including a later mutation in the same
  enactment (atomic); a validator may veto a `set_constitution` so the
  charter can guard its own amendment. Read side `ctx.query.constitution`;
  `ProposalRead.proposal_type`. Next: **4c** (shareholder governance — the
  `share` weight-model resolver + directives binding a firm's BEHAVIOUR
  script). `seize` (goods/parcels) also remains unbuilt and will share the
  levy mechanism under its own capability.
