# population — spawn_entity, proven (Step 6c)

A self-contained experiment that proves `spawn_entity` — the mechanism
shipped in [6c](../../docs/actors.md) — actually works end-to-end, with **no
engine change**. This is the platform layer exercising the mechanism, the way
`experiments/lifecycle` proved 6a. The engine mechanism is done; this composes
world policy on top of it.

## Run it

```bash
.venv/bin/python -m experiments.population.run            # 6 ticks
.venv/bin/python -m experiments.population.run --ticks 9
.venv/bin/pytest experiments/population/test_population.py # machine-checked
```

## What it demonstrates

A founding world — a spawn-capable government (the "midwife") and three
founders — and **four design points of 6c**, each nothing but composed world
policy over `spawn_entity` and the 6c queries:

| instrument | type | what it does |
|---|---|---|
| **birth-law** | VALIDATOR | the world's eligibility rule: exactly two parents, distinct, one male + one female (sex is a **holding**), both of age (`age()`), married to each other (a **WorldSetting** datum). Composed entirely from `ctx.query` reads. |
| **population-cap** | VALIDATOR | a votable ceiling reading `ctx.query.population()` — the world caps its own growth below the operator's hard server cap. |
| **midwife** | POLICY | fires `ctx.action.spawn_entity` each tick — the government is the **caller** (holds `SPAWN`); Adam and Eve are the **parents**. Attempts a valid birth (Adam × Eve) and an illicit one (Adam × Lilith) each tick. |
| **endowment** | HOOK | moves starting wealth to each newborn by **transfer** — proving "endowment is a transfer, not mechanism." `spawn_entity` opens the account at zero; this hook fills it. |

### The cast

| member | `birth_tick` | sex (holding) | married |
|---|---|---|---|
| Government | — | — | — (the midwife/caller) |
| Adam   | −30 | MALE   | Eve |
| Eve    | −28 | FEMALE | Adam |
| Lilith | −25 | FEMALE | nobody (unwed) |

`birth_tick` is overridden after creation to simulate a world already in
progress (as in the lifecycle demo), so the founders are adults at tick 1.
Founders carry **no accounts and no behaviour** — they are pure genetic
lineage (holdings of sex) for the birth law to read. The only money is the
treasury (which funds endowments) and the children's accounts.

### The run, in six ticks

Each tick the midwife attempts a **valid** birth (Adam × Eve) and an
**illicit** one (Adam × Lilith). Both tier-C rules are observably active:

```
tick  pop  Adam × Eve        Adam × Lilith
   1    5  + Child-1         x unwed        ← birth-law: not married
   2    6  + Child-2         x unwed
   3    7  + Child-3         x unwed
   4    7  x capped          x unwed        ← population-cap reached (7)
   5    7  x capped          x unwed
   6    7  x capped          x unwed
```

The valid pair is admitted until the world is full (cap 7); then the
population-cap vetoes. The illicit pair is refused every tick by the
birth-law (not married) — regardless of capacity. The two validators give
**distinct reasons**, proving they compose independently.

## The four things this proves

**1. `spawn_entity` brings entities into being mid-tick.** Each child is a
real `Entity`, stamped with immutable `parents = [Adam, Eve]` and an
always-created empty account — by the government's POLICY, during the tick.

**2. `birth_tick` is the executing tick (the threading).** A child born
during tick N has `birth_tick = N`, **not** N−1. The current `Tick` row
commits only at the *end* of `run_tick`, so a naive read of "latest committed
tick" would be off by one. The 6c mechanism threads the executing tick
through, so `age()` never disagrees with `ctx.tick` — a child born at tick 1
is age 1 at tick 2.

**3. Three tier-C rules compose from pure `ctx.query` reads.** The engine
ships **none** of the birth semantics — sex, age-gating, marriage,
population limits are all world policy:
- **sex** is an entity-attached holding (`MALE`/`FEMALE`), read-only to
  scripts (the invariant that protects the body); the engine has no sex
  column (a robot world has no use for one).
- **marriage** is a `WorldSetting` registry — a validator cannot read
  another script's state, so relationship data is mirrored into the
  world-readable store.
- **age** reuses the 6a keystone; **population** reuses the 6c query.

**4. Endowment is a transfer, not a mechanism.** `spawn_entity` never
endows — the newborn's account opens at zero. A **HOOK** moves `ENDOWMENT`
from the treasury to the child the very tick it is born (queued actions
resolve inline, same tick). Money is conserved: the treasury's loss equals
the children's gain; no issuance.

## What this proves (and what it doesn't)

**Proves:** `spawn_entity` works end-to-end — a midwife POLICY births
children mid-tick; a birth-law VALIDATOR composes sex (holding) + age +
marriage (WorldSetting) into an eligibility rule; a population-cap VALIDATOR
caps growth via `population()`; an endowment HOOK transfers starting wealth.
The executing-tick threading is correct (`birth_tick` == tick of birth).
Lineage queries (`parents`/`children`) walk provenance. The three concentric
gates (capability → server cap → world rules) are distinct and composable.

**Does not prove (deferred):** generational replacement / mortality (6d —
age-based incapacitation reusing the estate rule), `transfer_ownership` (the
sibling mechanism with its own consent policy), a votable per-owner cap. The
cast here reproduces but does not die; the population caps but never shrinks.
