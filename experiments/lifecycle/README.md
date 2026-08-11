# lifecycle — age-driven policy, proven (Step 6b)

A self-contained experiment that proves `ctx.query.age()` — the keystone
shipped in [6a](../../docs/actors.md) — actually drives real policy,
end-to-end, with **no engine change**. This is the platform layer exercising
the primitive, the way `contracts/bond` proved 5a–5c.

## Run it

```bash
.venv/bin/python -m experiments.lifecycle.run            # 6 ticks
.venv/bin/python -m experiments.lifecycle.run --ticks 8
.venv/bin/pytest experiments/lifecycle/test_lifecycle.py # machine-checked
```

## What it demonstrates

A tiny economy — a government with a treasury and four citizens of
**staggered age** — and three age-driven instruments, each nothing but a read
of `ctx.query.age()`:

| instrument | type | what it does |
|---|---|---|
| **age-gate** | VALIDATOR | vetoes the poll-tax for minors and retirees — only working-age citizens are taxed |
| **pension** | POLICY | pays every senior a stipend each tick |
| **coming-of-age** | POLICY | a one-time grant the tick a citizen first reaches working age |

The cast is chosen so every demographic stage **and** both transitions land
inside six ticks:

| citizen | `birth_tick` | at tick 1 | transition during the run |
|---|---|---|---|
| Eve   | −13 | age 14 (child) | comes of age at tick 3 (age 16) |
| Adam  | −30 | age 30 (worker) | none — stays working |
| Noah  | −63 | age 64 (worker) | retires at tick 2 (age 65) |
| Sarah | −70 | age 70 (senior) | none — stays retired |

`birth_tick` is normally stamped by `create_entity` to the latest committed
tick (0 at genesis). The scenario **overrides** it to simulate a world
already in progress — a legitimate setup, and exactly how a long-running
world would look if you joined it late. `age = ctx.tick − birth_tick` then
behaves as in that world, without making the demo run for 70 ticks.

## The headline finding: the dual-source lead

The instruments are split across two script types, and they read **different
ticks** (the 5a dual-source design): a POLICY reads the *executing* tick; a
VALIDATOR reads the *last-committed* tick. So a policy-side transition and
the matching validator-side transition for the *same* age threshold fire
**one tick apart** — the policy leads:

- **Eve** is *granted* at tick 3 (the policy sees age 16) but *admitted to
  labor* only at tick 4 (the validator catches up and sees 16).
- **Noah** is *pensioned* at tick 2 (policy sees 65) but *tax-exempt* only at
  tick 3 — and is therefore both pensioned **and** taxed for one tick.

This is **not a bug**. A validator must see committed reality (not the
in-flight tick) for integrity; a policy acts on the live tick. The demo
shows both reading `age()` and gating on it *exactly* — which is the point —
while their read points differ by design.

## Why no goods, markets, or production

The lifecycle here is purely about **money flows** (transfers) gated by age.
Adding production/consumption would test the *economy*, not the *affordance*.
Keeping it minimal keeps the proof focused: `age()` is the single input to
every instrument, and every observable (balance changes, veto reasons,
transition timing) traces back to it.

## What this proves (and what it doesn't)

**Proves:** a single primitive — `ctx.query.age()` — is sufficient to express
a pension, a coming-of-age grant, and an age-gated tax, with both lifecycle
transitions (admission and retirement) firing correctly. Layer 1 of the Step
6 design (scripts read age and act) is validated end-to-end.

**Does not prove (deferred):** generational turnover (6c, `spawn_entity`),
invariant mortality (6d, age-based incapacitation). The cast here is fixed;
ages are set, not born. The population never changes — that is the next
engine mechanism, gated on a world wanting a population rather than a cast.
