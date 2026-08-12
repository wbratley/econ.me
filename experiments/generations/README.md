# Generations — Step 6d proving experiment (death-by-old-age + inheritance)

A tiny world built to **prove** that death-by-old-age and inheritance work
end-to-end, with **no engine change and no Lua scripts**. Where
`experiments/population` proved birth (Step 6c) — which *needs* a POLICY to
fire `ctx.action.spawn_entity` — this experiment proves death. And death is
the opposite face of birth: an **invariant engine pass** that needs no
script at all. That absence is the central point of 6d.

> Build: engine mechanism, then a proving experiment (a world where founders
> die of old age and heirs inherit, closing the cycle 6c opened).
> — `docs/actors.md`, Step 6d

## Why no scripts

6c added `spawn_entity` — a *mechanism*: bringing an entity into being is an
**act** (someone must call it, holding a capability). So population needed a
midwife POLICY to drive births.

6d added nothing scriptable. Mortality is a **pass** the engine runs at the
end of every tick, the way the condition pass (starvation, disease) already
does. There is no `ctx.action.kill`; there is no `death` capability; there is
no new Lua action. Death happens because `age >= lifespan`, full stop. So
this world has **zero `Script` rows** — and the smoke test asserts exactly
that. The proof runs purely on genesis data + `run_tick`.

## What it exercises

| concept | how |
|---|---|
| **lifespan** | each founder carries a per-entity `lifespan` (immutable, stamped at creation). At `age >= lifespan` the age pass deactivates them. |
| **the death event** | the SAME `entity_incapacitated` event a starvation death fires, with `condition: "age"` — the only new signal. |
| **inheritance** | the votable `heir` estate rule + a per-entity `heir_id` transfer the estate (goods + money) to the next generation. |
| **the burn fallback** | an entity with no `heir_id` burns — the estate vanishes, the money supply shrinks. |
| **lineage** | the heir's `parents` (6c's provenance) make the handoff genealogically meaningful — closing the cycle: birth → aging → death → inheritance. |
| **observability** | `ctx.query.lifespan()` — the one new read — returns each entity's lifespan (or nil for the immortal). |

## The cast

All pre-seeded at genesis (the setup path, exactly as population's founders
pre-seeded `birth_tick` and marriage). Everyone is born at tick 0, so
`age == tick`:

| person | lifespan | wealth | heir |
|---|---|---|---|
| **Government** | immortal | none | — (the estate rule is votable data, not an account) |
| **Abraham** | 3 | GOLD 100 + USD 500 | Isaac |
| **Sarah** | 5 | SILVER 50 + USD 300 | Isaac |
| **Cain** | 4 | BRONZE 20 + USD 100 | *none* → burns |
| **Isaac** | immortal | nothing at birth | parents = [Abraham, Sarah] |

The estate rule is `"heir"` (votable WorldSetting data; in a full world a
governance vote would set it). Abraham and Sarah both designate Isaac;
Cain designates no one — so the same rule produces an inheritance for two
and a burn for one.

## The timeline

```
tick 3   Abraham (age 3/3) dies → Isaac inherits GOLD 100 + USD 500
tick 4   Cain    (age 4/4) dies → BURN BRONZE 20 + USD 100   (no heir)
tick 5   Sarah   (age 5/5) dies → Isaac inherits SILVER 50 + USD 300
```

At the end, Isaac — the immortal child of Abraham and Sarah — holds the
consolidated wealth of two founders. Cain's wealth is gone. Money supply:
**900 → 800** (100 burned with the heirless Cain). No issuance anywhere:
inheritance is a transfer, burning is a deletion.

## Run it

```
.venv/bin/python -m experiments.generations.run            # 6 ticks
.venv/bin/python -m experiments.generations.run --ticks 8
```

## The design points this proves

- **Death reuses the estate rule and the `entity_incapacitated` event.** One
  cause label (`"age"`) is the only new signal. Inheritance and insurance
  need nothing new.
- **`NULL` = immortal.** Isaac and the Government never die; the feature is
  opt-in.
- **Per-entity `heir_id` on top of a global estate rule.** The rule is one
  votable datum; which entity actually receives (vs burns) is per-entity
  provenance.
- **Lifespan is immutable, stamped once.** No engine setter exists — the same
  provenance pattern as `birth_tick` and `parents`. The world changes the
  *regime* by amending the spawn POLICY (what lifespan *future* births get);
  a *living* entity's death date is the non-votable floor 6d exists to provide.
- **Closing the cycle 6c opened.** Birth (6c: `spawn_entity`) → aging (6a:
  `birth_tick` + `age`) → death (6d: `lifespan`) → inheritance (the estate
  rule + `heir_id`). The `parents`/`children` queries trace the lineage that
  makes the handoff genealogically meaningful.

## What this experiment is *not*

- It does not re-prove `spawn_entity` (population did) or the survival loop
  (the inequality/lifecycle experiments did). It isolates the new 6d pieces.
- It does not wire an insurance contract. The design claims an old-age death
  fires an insurer's death benefit "for free" — that composition is already
  covered by `tests/test_contract_insurance.py` (against condition deaths)
  and `tests/test_lifespan.py` (proving the age event is shape-identical);
  together they entail the composition without re-staging it here.

## Files

| file | role |
|---|---|
| `scenario.py` | genesis: the cast, lifespans, wealth, `heir_id`, lineage, the estate rule |
| `run.py` | the run harness + the human-readable report |
| `test_generations.py` | the machine-checkable proof (21 assertions) |

No `lua/` directory. That absence is the point.
