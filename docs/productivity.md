# Productivity — conditions as capacity, not gates

The run-58 killer was a boolean: SQUALOR scaled the LABOR ration to 0.85,
and every recipe declaring a `1.0` LABOR input refused to start, so a
tired house could not gather, fish, or stoke its own fire. A tired person
does not stop working — they work less well. Conditions should therefore
act on **how well energy converts**, never on the holding as a gate.

Two axes, two mechanisms:

- **Energy** — a tired entity has *fewer hours*. Built: the condition
  modifies the LABOR good (`modifies_pattern="LABOR"`,
  `modifies_factor=0.85`), so `auto_issue` tops the ration to 0.85.
  §1 makes that ration a *fraction of work* instead of a gate.
- **Productivity** — tired/sick work *converts worse*: less yield, more
  misses, more defects. §2 (design, to be built).

| § | Option | Status |
|---|--------|--------|
| 1 | Effort — LABOR is energy, not a gate | **built** |
| 2 | Productivity `P` — conditions tax outcomes (yield / success / quality) | **design — to be built** |
| 3 | Stored quality + per-condition axis profiles | **deferred** |

---

## 1. Effort — the ration, not the gate (built)

**Rule.** A recipe's `LABOR` input is paid at the fraction *held*, never
overdrawn, and the fraction is applied to credited outputs at completion:

- `drawn = min(available, declared)`; `effort = quantize(drawn / declared, 4dp)`.
- The process starts whenever `available >= declared * EFFORT_FLOOR`;
  `EFFORT_FLOOR = Decimal("0.25")` (`engine/econengine/production.py`).
  Below the floor — or with zero hours — the start is refused:
  `... -- exhausted (a start needs at least {min} LABOR)`.
- `effort` is stamped on the process row (`processes.effort_factor`,
  `Numeric(18,4)`, **NULL = full effort, reads as 1** — the same
  convention as `processes.scale_factor` (P5)) and applied at completion:
  credited outputs × (`scale_factor` × `effort_factor`).
- `process_completed` events carry `"effort"` (4-dp string, always;
  `"1.0000"` when full).

**Deliberately unchanged.**

- `auto_issue` keeps scaling issuance by the condition factor (the energy
  axis is untouched — this pass only stops the *gate*).
- Every non-LABOR input stays **hard**: a recipe transforms exactly what it
  declares; short flour still refuses, short wood still refuses. Only
  LABOR is energy. ("LABOR" is hard-coded the way the `"COIN"` leg already
  is; design.md names LABOR as the labor-market primitive.)
- `per_tick_inputs` stays hard (all-or-nothing per tick, the existing
  principle). The stone age's only per-tick LABOR recipe is FEAST — a
  comfort luxury, not a survival action; a SQUALOR'd house that starts a
  feast without the full hour loses the feast, which is the right price.
- The woodpile pull-first, requirements, currency, and parcel machinery
  are untouched.

**What it does for run-58.** Harald/Ivar: the SQUALOR'd house now gathers
and stokes at 85% instead of 0%; sitting by the fire (WARM_BY_FIRE, 1
LABOR) now *starts*, so comfort is met and SQUALOR decays back to zero —
the throttle self-clears instead of deadlocking. Lagertha's starvation is
food-supply (#2) and account (#3) territory, not this pass.

**Conservation / determinism.** The draw is `min(available, declared)` —
never an overdraft, never a mint. The factor is stamped at start and
applied at completion, both ordinary row facts; no new RNG, no new seed,
`events_hash` unaffected except the (deterministic) credited quantities
and the new event field.

**Tests** (`tests/test_production.py`, section `LABOR effort`): full
ration = status quo (NULL factor, verbatim outputs); 0.85 ration starts
and credits 1.7 of 2 berries with LABOR exactly 0 (no overdraft); zero
and below-floor refuse with `exhausted`, at-floor starts at 0.25; short
FLOUR still refuses with the ration intact; effort persists across ticks
and rides the completion event; P5 herd × effort combine (3 hens × 0.85
= 2.55 eggs); banked COIN output lands at 1.7 of 2.

**Migration.** `alembic/versions/z9a4c2e7f1d8_process_effort_factor.py`
(tracking-only `add_column`).

---

## 2. Productivity `P` — conditions tax outcomes (design)

### 2.1 Principle

§1 made the *energy* axis soft. §2 makes the *conversion* axis real:
each condition held while working supplies a productivity factor
`P ∈ [0, 1]`, and each **recipe** declares which axis that factor shows
up on:

- `yield` — the work converts less: credited outputs × P.
- `success` — the work misses more: completion-branch weight shifts from
  the success branches toward the designated failure branch. ("Quality
  rolls" are this same mechanism with the failure branch carrying a
  defect/partial/waste output the pack declares — a tired smoker's jerky
  burns; the quality *kind* is a pack-authoring choice, not a mechanism.)
- `none` — the condition's productivity tax does not apply (comfort acts:
  a meal is a meal).

The condition supplies the **magnitude**, the recipe supplies the **axis**.
That is the boundary test: *does this work's output degrade, or does it
fail, or is it immune?* is a property of the work; *how badly* is a
property of the worker.

### 2.2 Model (three columns, one migration, tracking-only)

1. `Good.productivity_factor` — `Numeric(18,4)`, nullable, **NULL = no
   tax (reads 1)**. Set on *condition* goods only; `create_good` refuses
   the factor on a non-condition good (mirrors the `scales_with`
   "must be a good requirement" validation — a held berry must not tax
   work).
2. `Recipe.effect_kind` — `String(16)`, nullable, NULL = `"none"`.
   Values: `none`, `yield`, `success`. `create_recipe` validation:
   - value must be one of the three (else `ValueError` naming it);
   - `success` requires `branches` with **at least two** branches —
     the last branch (highest `position`) is the **failure branch** and
     receives the shifted weight; one branch leaves the shift nowhere to
     go. (Convention, documented; an explicit
     `RecipeBranch.is_failure` is a follow-up only if a pack needs
     multiple failure branches.)
3. `Process.productivity` — `Numeric(18,4)`, nullable, **NULL = no tax
   (reads 1)** — the P actually applied, stamped at completion next to
   `scale_factor` / `effort_factor`. Audit story on one row:
   "why 1.4 berries?" → herd × effort × P.

### 2.3 Definition and computation of P

```
P(entity, at completion) = ∏  Good.productivity_factor
                          over held condition goods (quantity > 0)
                          with factor NOT NULL, symbol-ascending
```

- Computed in `conditions.py` as `held_productivity(session,
  entity_id) -> Decimal` — the `held_modifiers` / `effective_factor`
  pattern, product instead of matched-symbol filter. No held conditions
  → 1.
- Read **at completion** — the same commit point as `outcome_roll` and
  `_scale_factor` ("facts of the completion stamped on the row the same
  tick"). A condition that accrues *during* a long process affects the
  completion, not the start.
- Applied only when `recipe.effect_kind in ("yield", "success")`.
- Stamped: `process.productivity = P if P != 1 else None`.

### 2.4 Application points

- **yield**: in `_complete`, the credit factor becomes
  `herd × effort × P` (all three quantized, all NULL-reads-1).
  `credited_outputs` multiplies all three, so events and travel
  arrivals report the same fact.
- **success**: before `rng.weighted_index`, transform the branch weights:
  - success branches (all but last): `w_i ← w_i × P`
  - failure branch (last): `w_fail ← w_fail + (1 − P) · Σ_success w_i`
  - Σ preserved; P = 1 is the identity; P = 0 sends all success mass to
    failure.
  - The roll itself is untouched: `rng.outcome_roll(seed, process.id)`
    with the *same* seed — commit-reveal intact; the event already
    records `roll`, `branch`, `branch_label`, so a shifted outcome is
    auditable as a fact.
- **Event**: `process_completed` gains `"productivity"` (4-dp string,
  always; `"1.0000"` when none) — next to §1's `"effort"`.

### 2.5 Determinism and conservation

- P is a product of held rows read at completion — deterministic given
  the tick's state; no new RNG, no new seed; the branch roll and its seed
  are unchanged (only the weights move).
- Conservation: `P ≤ 1` only shrinks credited outputs; uncredited output
  is **unproduced**, never minted. The weight shift preserves Σw and only
  redistributes between *declared* branches — the failure branch's
  outputs (waste, ASH, a cracked tool, nothing) are declared by the pack,
  so a miss cannot mint or consume undeclared goods.

### 2.6 Stone-age tuning (pack data, no mechanism)

Proposed initial values — tuning knobs, not scripture:

| condition | energy (existing: modifies LABOR) | productivity P (new) |
|-----------|-----------------------------------|----------------------|
| SQUALOR   | ×0.85                             | — (NULL)             |
| FATIGUE   | —                                 | 0.9                  |
| DISEASE   | —                                 | 0.8                  |
| EXPOSURE  | —                                 | 0.75                 |
| HUNGER / THIRST | — (kill lines only)      | —                    |

SQUALOR deliberately stays energy-only: "slaking off" is fewer hours,
and §1 already makes that soft. A condition may set *both* axes
(a designer's choice) — it is documented as allowed, not hidden.

Recipe axes (stone age; as built):

- **yield**: `GATHER`, `GATHER_BAG` — the foraging loot table converts
  less. The other §2.6 candidates were walked back after the first
  regression, for one reason: a `yield` recipe whose output is a house's
  *fuel* or its *food* is a death spiral, not "work converts less".
  `CHOP_WOOD` is the salter's winter *income* (sold at the counter) and
  its *fuel* (stoked for the fire): P-scaling it is cold → chop less
  wood → less fire → colder → chop less wood, and the salter's coin
  (and so its jerky) runs out with it. `COLLECT_EGGS` is a house's
  *food*: P-scaling it is a hungry winter, not a sick gatherer. `FORAGE_*`
  (satiety meals), `STOKE_FIRE` / `MAKE_FIRE` (facility fuel, no goods),
  `WARM_BY_FIRE` (warmth/rest/comfort) are the same story — a cold
  worker getting *less warmth from the fire* is backwards.
- **success**: `FISH`, `HUNT`, `HUNT_SPEAR`, `HUNT_TRAPS`, `HUNT_BOW`,
  `HUNT_IBEX`, `HUNT_IBEX_BOW` — the hunt's `nothing` branch is the
  failure branch, moved to the last (highest-position) slot so the
  shifted weight flows to it. These are the only recipes that already
  *have* a failure branch. `SMOKE_MEAT`, `COOK_MEAT`, `STUDY_*`,
  `MAKE_SPEAR` / `KNAPPING` (the rest of the proposal) have a single
  declared output and would need a new failure-branch output (burnt/ASH,
  partial, nothing, cracked tool) to take the axis — content authoring
  beyond §2.6, deferred with §3.
- **none**: everything else — `EAT_*`, `DRINK_*`, `SLEEP`, `FEAST`,
  `TRAVEL_*`, the food collection (`COLLECT_EGGS`), the fuel
  (`CHOP_WOOD`, `STOKE_FIRE`, `MAKE_FIRE`), the comfort acts
  (`WARM_BY_FIRE`), and the single-output crafts — immune.

### 2.7 What it does (and does not do) for run-58

- Harald/Ivar: §1 already breaks the deadlock; §2 adds nothing to their
  deaths specifically (SQUALOR has no P), but it makes the world
  *gradual*: a DISEASE'd fisher starts catching less and missing more
  instead of either full or nothing.
- Lagertha: unchanged — her death is food supply (#2) and the account
  bug (#3). §2 is honest about that.

### 2.8 Test plan (at build time)

- `held_productivity`: two held conditions multiply (0.9 × 0.8); no
  conditions → 1; a non-condition good's factor is refused at
  `create_good`.
- yield: credited outputs × P; P stamped on the row and on the event;
  `credited_outputs` agrees with the event's `outputs`.
- success: with a known roll (the existing stochastic-test pattern),
  P = 1 reproduces today's branch exactly; P → 0 sends the roll to the
  failure branch; Σ of transformed weights equals Σ of declared weights.
- `create_recipe` validation: bad `effect_kind` refused; `success` with
  zero or one branch refused, naming the reason.
- Conservation: credited outputs never exceed declared × (herd × effort
  × P); a failure branch credits only what it declares.
- Full regression: engine + stone-age + camp-storage suites.

**Migration**: one tracking-only revision adding
`goods.productivity_factor`, `recipes.effect_kind`,
`processes.productivity`.

**Pack/manifest**: `stone_age.py` is pack data (not pinned Lua) —
condition factors and recipe axes are a genesis change; the starter
script is untouched (P is applied by the engine; scripts observe
outcomes), so no manifest regeneration unless the starter changes.

---

## 3. Option 3 — stored quality and per-condition profiles (deferred)

- A stored `quality` attribute on goods/holdings (a buyer can see and
  pay for a tired smoker's burnt jerky) — needs a model, a migration,
  consumption, and UI; §2's failure-branch outputs (defect/waste goods)
  already express "the work came out worse" without it.
- Per-condition axis profiles (DISEASE taxes success, FATIGUE taxes
  yield, independently of the recipe's axis) — a second-order knob; §2's
  split (condition = magnitude, recipe = axis) already covers
  "some goods a quality roll, others a yield".

Build only if a run shows agents needing to *see* stored quality, or the
single-recipe-axis proves too coarse.

---

## 4. Build order (when §2 is built)

1. `models/good.py`: `productivity_factor` column.
2. `models/recipe.py`: `effect_kind` column.
3. `models/process.py`: `productivity` column.
4. `alembic/versions/…_productivity.py` (tracking-only, three
   `add_column`s, chained on `z9a4c2e7f1d8`).
5. `conditions.py`: `held_productivity(session, entity_id)`.
6. `create_good` validation (factor ⇒ condition good).
7. `create_recipe` validation (axis vocabulary; `success` ⇒ ≥ 2
   branches).
8. `production.py::_complete` + `credited_outputs`: P in the credit
   factor; weight shift before `rng.weighted_index` for `success`.
9. `production.py::_completed_event`: `"productivity"` field.
10. `experiments/world/stone_age.py`: the §2.6 table (condition factors
    + recipe axes).
11. Tests per §2.8; then engine + stone-age + camp-storage regression;
    full suite before any fresh run.
