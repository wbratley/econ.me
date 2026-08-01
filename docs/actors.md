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

So the government can now compel *money*. Compelling goods, and *deciding*
the rate schedule votably, are steps 3–4.

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
4. **Shareholder voting** (Fork 5C) and the full **vote→enact cycle**
   (Fork 4D), layered on the same machinery.

### A correctness note for step 2

A levy the *government* originates must bypass the ownership invariant
*for the government only, under a declared rule*. That is exactly what
`_apply_estate` does for death, so the precedent is sound — but the gating
(capability + a votable rate schedule) is where all the safety lives.
Review that boundary carefully and seizure/tax share one bulletproof
mechanism.

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
- Steps 3–4 — planned, not started.
