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

## The engine primitive that was missing, now built

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
(outright expropriation of goods/parcels, not just money) is the
companion primitive — `services.seize` shares levy's gating model under
the `seize` capability, moving goods and/or parcels the authority does
not own into a declared recipient (itself by default), under a declared
`rule_ref`, vetoable by a VALIDATOR (fail-closed).

So the government can now compel *money* and *goods* and set its own
rates votably. The enforced-state-action primitive (tax and seizure) is
complete.

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
   that makes the world self-governing. *— done (4a-i, 4a-ii, 4b, 4c all
   landed; see "Step 4 design" below and Status).*
5. **The financial substrate** — the four affordances that make bonds,
   loans, credit money, derivatives, and insurance implementable as
   *data + Lua*, not as engine features: (a) `ctx.tick`, (b) an
   owned-claim/position primitive, (c) a signal/observation layer, (d) a
   reference contract library. Each is independently focusable; see
   "Step 5 design" below. *— 5a + 5b + 5c done (gov bond ships); the
   rest of 5d next.*

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
  (reuses the holding register `ctx.query.holders` exposes, the cap
  table); *shipped in 4c*;
- *council* / *weighted* — electorate = the members of a named register
  (a WorldSetting, `engine/econengine/councils.py`), weight 1 each under
  `council` or the declared per-member weight under `weighted`; *shipped
  after 4c*. `weighted` subsumes a *representative* chamber — set each
  MP's weight to their constituency size and the majority is of
  represented population, not of heads;
- *liquid* — liquid democracy: every active INDIVIDUAL, weight 1 each
  plus the weight delegated *to* them (resolved transitively against a
  delegation graph in ``delegations.py``, a WorldSetting). A delegator
  leaves the electorate (they voted by redirecting); an empty graph is
  plain direct democracy. *Shipped after 4c.*

Shipping only the *citizen* resolver in 4a-ii kept the slice minimal
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

**4a-5. Capability transfer — `grant_capability` / `revoke_capability`.**
*Done.* The meta-privilege above every other capability: changing *who
can exercise power*. Both the `grant_capability` and `revoke_capability`
intents gate on the single `GRANT_CAPABILITY` capability (conferring and
withdrawing power are the same meta-act), checked at the intent boundary
AND in the service, exactly like levy/seize. Three locked decisions:

- **Free-grant model.** A holder may confer any *declared* capability
  (`capabilities.ALL`) on any entity — a legislature constitutes agencies
  with powers it does not itself exercise. The safety floor is the gate +
  a VALIDATOR veto + the supermajority (below), not "you may only delegate
  what you hold" (capabilities are non-conserved permissions, not
  assets). A VALIDATOR may veto any grant/revoke (fail-closed), so the
  constitution can forbid conferring a dangerous capability regardless of
  who authorises it.
- **Constitutional-tier mutations.** As a proposal mutation, a capability
  transfer is constitutional (`CONSTITUTIONAL_MUTATIONS`) — power transfer
  is meta, so a simple majority must not be able to escalate power. An
  ordinary proposal may not carry one; a constitutional proposal may. This
  is the governance the roadmap demanded ("granting power must itself be
  governed: a vote / constitutional process").
- **Defense-in-depth + atomic rollback.** The capability is checked at the
  intent boundary (`INTENT_CAPABILITIES`) and re-checked in the service
  (`MissingCapabilityError`); a veto during enactment rolls back the whole
  grant. Reaching every actor surface: `POST /intents`, the tick loop
  (`ctx.action.grant_capability(to_id, cap)` / `revoke_capability` from an
  enacted directive), and the vote→enact path.

The admin path (`PATCH /admin/entities/{id}`) remains the bootstrap; the
engine primitive lets a self-governing world transfer power in-world
without an operator. No migration (capabilities are a JSON column on
`Entity`).

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
- **Step 4c — shareholder governance (Fork 5C).** *Done.* The *share*
  resolver (electorate = holders of a symbol, weighted by quantity; the
  spec `share:SYMBOL` carries the per-proposal scope). An enacted
  share-weighted proposal carries an ordinary `set_script` (a BEHAVIOUR
  script bound to the firm) — the directive the shareholders pass runs as
  the firm on the next tick. The firm needs `legislate` (a capability
  grant, data not code); no new mutation type.

Parameter voting (step 3) stays the common case throughout: most
proposals are still `set_fiscal_policy` edits, because most legislation
*is* rate changes.

### Decisions (locked) & deferrals

- **`set_script` semantics: retire-old + activate-new** (not in-place) —
  chosen to preserve a full legislative lineage.
- **Direct democracy MVP** (every citizen votes on every proposal);
  representative is now the `weighted` model (set each MP's weight to
  constituency size); council/weighted landed after 4c.
- **Cadence-agnostic enactment** — admin/platform calls `enact` in MVP.
- **Defaults** — simple majority + modest quorum, both constitutional
  params.
- **Deferrals** — bicameral/multi-body enactment (a richer enactment
  condition); federated polities. Trials (§4.2) are platform (CoW fork
  audit), out of engine scope. (Delegation/liquid democracy was deferred
  here once; it has since shipped — see 4a-2.)

### What stays explicitly unbuilt (engine)

The governance/enforcement primitive surface (steps 1–4) is complete:
capabilities, levy/seize, the policy actor, the governed-script lifecycle,
proposal/vote/enact, the constitutional tier, share/council/weighted/liquid
weight models, and governed capability transfer. **The next engine work is
the financial substrate (Step 5):** three small, individually-focusable
affordances — `ctx.tick`, an owned-claim/position primitive, and a
signal/observation layer — that together make the full richness of the real
economy (bonds, loans, bank credit money, futures, options, insurance)
implementable as Lua + data rather than as engine features. The reference
contract library (5d) that consumes them is platform-layer. Beyond Step 5,
the genuinely platform-only items remain: the enactment-day
cadence/scheduler, trials (copy-on-write fork audit, §4.2), and the
proposal/campaigning UI.

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
  power is itself privileged). `levy`, `set_fiscal_policy`, `seize`,
  `legislate`, `amend_constitution` are wired to their actions; and
  `grant_capability` / `revoke_capability` are now wired too (the
  meta-privilege of changing who can exercise power — see "Capability
  transfer" below). Every declared capability now gates at least one
  intent.
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
  transaction flavour. **`seize` landed** as levy's goods/parcels
  companion: `services.seize(authority, from_entity, *, symbol, quantity,
  parcel_ids, to_entity, rule_ref)` moves goods (goods-conserving, debit
  victim / credit recipient, raises if the victim is short — fail-closed)
  and/or parcels (reassigned via `parcels.grant_parcel`, which refuses a
  parcel with running processes) out of an entity the authority does not
  own, into a declared recipient (itself by default; a different recipient
  is redistribution). It records no `Transaction` (transactions are
  money-only); the movement rides the holding rows and parcel ownership,
  like `transfer_parcel`. The `seize` capability gates it at the intent
  boundary and in the service; `rule_ref` rides `ctx.op`; a VALIDATOR may
  veto (fail-closed). Reachable from every actor surface: `POST /intents`,
  the tick loop, and `ctx.action.seize(from_id, spec, rule_ref)` from
  scripts.
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
- Step 4 — **done (4a-i, 4a-ii, 4b, 4c all landed)**. See "Step 4 design:
  governance —
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
  `ProposalRead.proposal_type`. **4c landed** (shareholder governance): the
  `share` weight-model resolver — electorate = holders of a symbol
  (`share:SYMBOL` spec), weighted by quantity, read live from the cap
  table; an enacted share-weighted proposal carries an ordinary
  `set_script` (a BEHAVIOUR script bound to the firm), so the directive
  runs as the firm next tick. A corporation is now a row in
  `weights.WEIGHT_MODELS`, not new mechanism. The governance stack
  (4a-i/ii + 4b + 4c) is complete. `seize` (step 2's goods/parcels
  companion) has since landed — see Step 2. The `council` / `weighted`
  weight models have since landed too (a named membership register,
  `engine/econengine/councils.py`, a WorldSetting; `weighted` subsumes a
  representative chamber). The `liquid` weight model has since landed too
  (liquid democracy — direct democracy plus transitive delegation,
  `engine/econengine/delegations.py`, a WorldSetting delegation graph).
  The `grant_capability` / `revoke_capability` primitives have since landed
  too (the meta-privilege of changing who can exercise power — both gate
  on `GRANT_CAPABILITY`, both are constitutional-tier mutations, and a
  VALIDATOR may veto any transfer). **The governance/enforcement primitive
  surface is complete; the next engine work is the financial substrate
  (Step 5, see below).**
- Step 5 — **5a done**. `ctx.tick` is now exposed to every script: for
  POLICY/BEHAVIOUR scripts the tick currently executing (threaded from
  `run_tick`'s `number` into `_build_script_ctx`); for VALIDATOR/HOOK
  scripts the latest committed tick (`_op_ctx` reads the newest `Tick`
  row — honest, since the op applies before the current tick commits, and
  correct for direct API ops between ticks). Surfaced on the Lua `ctx` in
  `lua_engine.py` and documented in its module docstring. The motivating
  gap it closes: maturity dates and coupon/expiry schedules that read
  `ctx.tick` survive a compute-budget skip, where a self-counter in
  `state` would desynchronise and drift. No migration, no model. Tests:
  `tests/test_ctx_tick.py` (tick reflects the current tick across two
  ticks; the budget-skip acceptance test — a skipped tick leaves the
  counter unchanged and the next run reads the true wall-tick, not a
  run-count; BEHAVIOUR scripts see the same tick; a hook reads the latest
  committed tick, both before tick 1 and after). 5b–5d remain.
- Step 5 — **5a done; government bond (5d-1) done; 5b convention locked.**
  The reference contract library opened with the government bond
  (`contracts/bond/`): a fungible bond is a `Holding` whose symbol is the
  bond's (Fork A), the live register is `ctx.query.holders`, and the terms
  live in the issuer's servicing-script `state`. `gov_bond.lua` (a POLICY
  bound to the issuer) honours coupons and redeems face each tick from
  `ctx.tick` — proving the time primitive works inside a real contract and
  that a schedule survives a compute-budget skip. `bond.py` is the data
  half: `issue_bond` (a `transfer` of existing money + an `adjust_holding`
  of the claim — neither creates money) and `redeem_holdings` (retires the
  units; scripts move money, not goods, so the goods half of redemption is
  an admin op). `monetization_cap.lua` is the ships-with VALIDATOR: a
  constitutional cap on `issue_money` that can forbid monetising the debt.
  This locks the 5b bonds-as-goods convention (no non-fungible traded claim
  has appeared to demand Fork B) and validates 5a end-to-end. Tests:
  `tests/test_contract_gov_bond.py` (full lifecycle; total money supply
  invariant across sale/coupons/redemption; a traded bond pays its new
  holder; proportional multi-holder payout; skip-safe schedule; an
  insolvent issuer's coupon is rejected not crashed; goods retirement; the
  cap vetoes issuance). 5c is done: `ctx.query.world_setting(key)` ships
  (the generic read behind fiscal_policy/constitution and the Fork-A
  signal channel), and the bond's `monetization_cap` cap is now
  data-driven through it. The rest of 5d (bank, loan, futures, option,
  insurance) remains.

## Step 5 design: the financial substrate

### The gap steps 1–4 left

Steps 1–4 built the machinery of power: who may act (capabilities), how the
state enforces its will (levy/seize), how policy is set and voted on
(scripts + governance), and how power itself transfers. What they did *not*
build is the machinery of **obligation over time** — one entity promising
another a future payment, a stream of coupons, a delivery at a strike. That
is the substrate of the whole financial economy: bonds, loans, bank
deposits, futures, options, insurance.

The architectural insight that governs this step (and rejects the
instinct to add a `Contract` engine model): **a financial instrument is
data + a behaviour/policy script that interprets that data each tick.**
The tick loop is the clock; `transfer`/`levy`/`seize`/`issue_money` move
value; script `state` + `WorldSetting`s hold the terms; capabilities
enforce that only the issuer/bank/exchange can act. Almost every
instrument reduces to those primitives *today, with no new engine code*.

This is not a coincidence. It is the same mechanism/data/policy split
(design.md §2) that already governs tax: **mechanism** = the money/goods
movement primitives; **data** = the contract terms (script state or
WorldSettings); **policy** = the contract script. A bond is no more an
engine feature than a tax schedule is — and a dedicated `Contract` table
would be the same mistake a `Tax` table would be.

One alignment worth naming, because it shapes 5b and 5d: the engine is
already a **faithful two-tier monetary system**. The ledger that
`issue_money` writes is *base money* (central-bank reserves) — only the
monetary authority creates it. A commercial bank's deposit balances are,
by construction, a *shadow ledger in the bank's script state* — claims on
the bank, created by lending, not base money. That is exactly how real
banks create money, and it costs the engine nothing: 5d's bank reference
script does not create money, it keeps a book.

Step 5 closes the four gaps that stop this from being *convenient*. They
are small, ordered by dependency, and each is independently focusable.

### 5a. `ctx.tick` — the time primitive (engine, trivial)

*The need.* Maturity dates, coupon schedules, futures expiry, loan
due-dates are all "tick T". A settlement script must know the current
tick. Today a script counts its own runs in `state` (`s.n = (s.n or 0)+1`)
— workable, but **wrong the moment a script is ever skipped** (compute
budget exceeded → the counter desynchronises from wall-tick and every
maturity drifts).

*The change.* One line in `_build_script_ctx` (tick.py): add `"tick":
number` to the ctx dict, and surface it as `ctx.tick` in the Lua context.
The tick number is already computed at the top of `run_tick`; pass it
through. No migration, no model.

*The fork (minor).* Should the engine also expose a *calendar* (a
world-defined tick→day/month mapping, so a script says "pay on the 1st of
the month")? **Defer.** Raw `ctx.tick` is enough for v1; a calendar is a
convention a world can layer in WorldSettings if it wants it. Build only
`ctx.tick` now.

*Acceptance.* A script reads `ctx.tick` and writes it to `state`; two
consecutive ticks show `ctx.tick` increment by exactly 1 even if the
script was budget-skipped on one of them.

### 5b. Owned claims / positions — the ownership primitive (engine, the
meaty fork)

*The need.* "Entity X owns claim Y" should be an engine-mediated record so
that (a) transfers are enforced by an invariant, not by a script's
bookkeeping; (b) a query can enumerate everything an entity owns; (c) a
contract script can issue/redeem without reinventing a register each time.

*The fork.*

- **Fork A — bonds-as-goods (do nothing).** A claim *is* a `Holding` row
  with a symbol (e.g. `TBILL-2030`); transfer via `markets`; query via
  `ctx.query.holding` / `holders`; issue/redeem via `adjust_holding`.
  Covers every **fungible, identical-terms** instrument — government bonds,
  shares, tokenised deposits — for free. **Limit:** cannot represent a
  non-fungible or varying-terms claim (loan #42 with *its* collateral; an
  option with *its* strike).
- **Fork B — a generic `Position` model.** A first-class
  `Position(entity_id, instrument_id, quantity)` where `Instrument` is a
  record (issuer, kind, terms-JSON); a new `transfer_position` mutation;
  `ctx.query.positions(entity_id)`. Fully expressive (fungible *and*
  bespoke), but a new model + migration + mutation type.
- **Fork C — contract registry as a WorldSetting.** A contract is a row in
  a JSON blob; ownership is a field; a registry-script mediates transfers.
  Cheapest, but **no engine invariant** — the registry script *is* the
  invariant, and a bug there silently loses money.

*Recommendation.* **Ship Fork A as the default, defer Fork B until a
traded non-fungible instrument actually demands it.** The decisive
question is *does the claim trade?* A loan does not trade (it lives in the
lender's book); a bond does (it must move between strangers). Traded
fungible claims are already `Holding`s. Non-fungible traded claims
(securitised loans, bespoke OTC derivatives) are the *only* thing that
justifies Fork B — and those are a v2 concern. So: build the reference
library (5d) on Fork A; revisit Fork B empirically when something hurts.

*What this step therefore builds now.* Nothing new, *if* Fork A holds.
What 5b actually delivers is a **decision and a convention**: a documented
claim-issuance pattern ("a bond issuance = `adjust_holding` of a symbol +
a POLICY script bound to the issuer that honours coupons/redemption") and
the query affordances it needs (`ctx.query.holders` already exists; add
`ctx.query.issuer_state` only if a script can't reach the issuer's book
another way). If, during 5d, a non-fungible traded claim appears, 5b
re-opens as "build Fork B."

### 5c. Signals / observation — the trigger primitive (engine, small)

*The need.* A settlement script wants "the wheat price crossed 50" or
"loan #42 defaulted" delivered to it, not polled for. Today every
interested script re-reads the same `WorldSetting` each tick — fine at
small scale, wasteful and noisy at large scale.

*The fork.*

- **Fork A — signals-as-WorldSettings (do nothing new).** A price-feed
  POLICY script posts `signal:wheat` each tick; consumers read
  `ctx.query.world_setting("signal:wheat")`. Costs nothing. Works today.
  Limit: every consumer polls.
- **Fork B — an event/signal bus.** Scripts subscribe to conditions; the
  engine dispatches when met. Efficient at scale, but a real engine
  feature (subscription model, dispatch ordering, lifecycle).
- **Fork C — enrich `ctx.events`.** `ctx.events` already carries the
  previous tick's per-entity outcomes. Extend it to carry *world events*
  (a price crossed a level, a payment defaulted, a maturity fired) that
  any script can react to. Cheaper than Fork B, richer than Fork A.

*Recommendation.* **Fork A for v1; revisit Fork C if event volume grows.**
The poll cost is one WorldSetting read per interested script per tick —
negligible until a world has hundreds of reacting scripts, at which point
Fork C (a shared event channel) is the natural upgrade and reuses the
existing `ctx.events` plumbing. Do not build Fork B (a general pub/sub)
speculatively — it is the most engine for the least current need.

*What this step therefore builds now.* Possibly nothing, *if* Fork A
holds — except a documented signal convention and confirming that
`ctx.query` exposes a generic `world_setting(key)` read (if it does not,
that is the one small engine affordance here). Fork C is deferred with a
clear trigger condition.

### 5d. The reference contract library (platform, the payoff)

*The need.* The engine ships *primitives*, not instruments. Worlds should
not each reinvent the data shape of a bond. A reference library of
documented, tested Lua contract scripts — each a template to adopt or fork
— is what makes the richness *available*, and it is the empirical test of
5a–5c (every "this hurt" moment is a substrate decision re-opened with
evidence).

*The catalogue (each a separately-focusable deliverable, ordered by what
validates the most substrate first).*

- **Government bond** (fungible, Fork A) — issuance as `adjust_holding` of
  a symbol; a POLICY script bound to the issuer honours coupons (pays
  `ctx.query.holders`) and redeems at maturity (`ctx.tick` + 5a). *Validates
  5a and Fork A; shows a bond sale does not create money (only a
  MONETARY_AUTHORITY purchase does).* *— done:* `contracts/bond/`
  (`gov_bond.lua` + `bond.py` + `monetization_cap.lua`), tested in
  `tests/test_contract_gov_bond.py` (lifecycle, no-money-creation, traded
  bond pays new holder, skip-safe schedule, insolvency, goods retirement,
  and the constitutional cap).
- **Commercial bank + deposit shadow-ledger** (two-tier money) — a
  CORPORATION whose BEHAVIOUR script keeps deposit balances in `state`,
  creates deposits by lending, settles interbank in base money via
  `transfer`. *Validates the two-tier-money framing; shows credit money is
  a book, not a ledger feature.*
- **Loan + collateral seizure** — a lender's script holds the loan book,
  accrues interest, levies payment or `seize`s collateral on default.
  *Validates `levy`/`seize` as the enforcement spine of private debt.*
- **Futures + margin** — an exchange CORPORATION matches longs/shorts,
  holds margin (`seize` on margin breach), settles cash or physical at
  expiry against a signal price (5c). *Validates `seize` as margin call;
  validates the signal convention.*
- **Option** — same shape, settlement pays the long only if in the money.
- **Insurance** — premium `transfer` in, risk pool in `state`, payout on a
  trigger event read from `ctx.events`.

Each ships with a documented `state` shape and at least one VALIDATOR
(e.g. a usury cap on loan rates, a margin-sufficiency check on futures) —
the same constitutional-constraint pattern fiscal policy already uses.

*Where it lives.* A `contracts/` (or `examples/contracts/`) directory plus
tests against the current engine — platform-layer, no engine changes
beyond 5a–5c. The bond is the natural first deliverable: it is the
smallest, validates the most, and is the template for the rest.

### Build sequence & decisions (locked for now)

1. **5a (`ctx.tick`)** — first; trivial, unblocks every maturity-bearing
   script, and is a dependency of the bond reference impl. Engine.
   *— done (see Status).*
2. **5b (decision + convention)** — no engine build yet under Fork A; lock
   the bonds-as-goods convention and the claim-issuance pattern. Re-opens
   as "build Fork B" only if 5d produces a traded non-fungible claim.
   *— done:* the convention is locked and proven by the government bond
   (a claim is a `Holding`; the register is `ctx.query.holders`; issuance
   is `adjust_holding` + `transfer`). Fork B stays deferred — no
   non-fungible traded claim has appeared yet.
3. **5c (decision + one affordance)** — confirm a generic
   `world_setting(key)` query exists (add it if not); lock the
   signals-as-WorldSettings convention. Fork C deferred with a trigger.
   *— done:* `ctx.query.world_setting(key)` ships in `build_queries`
   (`scripting.py`) — the generic read behind `fiscal_policy()` and
   `constitution()`, and the Fork-A signal channel (an oracle posts a
   `WorldSetting`; contract scripts read it live each tick instead of each
   keeping a copy). The engine affordance is read-only (writing is
   platform/oracle-layer, like every other `ctx.query`). Fork A locked;
   Fork C (a shared `ctx.events` world-event channel) deferred until event
   volume grows. Proven inside a real contract: the bond's
   `monetization_cap.lua` cap is now data-driven through it (a
   `monetary:issue_cap` signal retunes the ceiling without re-enacting the
   validator). Tests: `tests/test_world_setting_query.py`.
4. **5d (reference library)** — platform; one instrument at a time,
   starting with the government bond, each validated end-to-end against
   the engine. *— government bond done; bank/loan/futures/option/insurance
   remain.*

*Decisions locked here:*
- **No `Contract` engine model.** A contract is data + a script. The
  orphan branch's `Contract`/`ContractEvent` tables (a dedicated lifecycle)
  are explicitly rejected as the wrong layer.
- **Fork A defaults.** Bonds-as-goods for fungible claims; loans in
  script-state books; signals as WorldSettings. Forks B and C are deferred
  *with explicit re-opening triggers*, not abandoned.
- **The engine/platform line holds.** 5a–5c are engine; 5d is platform. The
  engine never ships an instrument, only the affordances that make
  instruments expressible.

