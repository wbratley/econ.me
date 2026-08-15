# The "world" content-pack experiment (Phase 0)

> A richer single-region economy, authored entirely as **data + Lua** on an
> untouched engine. This is the substrate every later phase of the to-market
> game (`docs/game.md`) is built on. No engine change.

## What this proves

That the engine is already most of a multiplayer economic-sandbox world. A
multi-stage industrial chain (ORE → IRON) gated by a tech tree runs across
markets beside a food chain (GRAIN), with needs, deposits, and facilities —
and a small population of specialists survives, trades, and produces **with
zero calibration effort**, because survival is robust by construction.

## The clerk (the polity, Phase 2b)

`make_clerk` builds the **Assembly**: a server-owned GOVERNMENT entity
holding LEGISLATE + AMEND_CONSTITUTION + SET_FISCAL_POLICY (operator fiat
at content time), running `lua/clerk.lua` as a POLICY script. It reads
`round.state` each tick, derives the window calendar (`r % N == 0`, with N
projected into the round counter by each advance), and on a window close
sweeps the docket — every OPEN proposal decided by the ordinary `enact`
intent. Out-of-window proposals are legal but dormant; the clerk is the
only moment they take effect (`docs/game.md` §14.4). In a raw-tick world
(no round scheduler) `round.state` is absent and the clerk is inert.

## The cast (three specialist INDIVIDUALs)

| entity | script | produces | buys | proves |
|---|---|---|---|---|
| **Farmer** | `starter.lua` | GRAIN (FARM_GRAIN: 1 LABOR → 4 GRAIN on a FARM) | — | the survival loop + the food market's supply side |
| **Miner** | `miner.lua` | ORE (MINE_ORE: 1 LABOR + a drawn seam → 2 ORE) | GRAIN | extraction / depleting deposits |
| **Smith** | `smith.lua` | IRON (SMELT_IRON: 2 ORE + 1 LABOR → 2 IRON) | ORE, GRAIN | the tech-gated, multi-stage node — production crosses a market |

The chain is **balanced by construction**: the Miner produces exactly the
2 ORE/tick the Smith consumes, and the Farmer's surplus covers both buyers'
food. So the proving run survives without the heroic calibration a
self-organising *uniform* population would demand (the lesson of
`experiments/inequality`, whose entire calibration effort centred on food
affordability). Here food is cheap to produce (the Farmer's LABOR is
auto-issued and free), so the food market clears at a low price and anyone
with a token endowment eats.

`starter.lua` is the **default behaviour a player inherits and edits** — a
defensive minimum (farm, sell surplus, buy food if the pantry runs low). A
lone entity endowed with a farm and this script survives indefinitely
(`test_starter_template_survives`). A player's edge comes from *rewriting*
it.

Scripts are written against the tiered library model (`docs/scripting.md`):
`std.*` (engine stdlib), `world.*` (this world's idioms — `lua/world_lib.lua`)
and `pack.*` (this content pack's play opinions — `lua/pack.lua`) are all
injected read-only at run time, so a script's own source carries only its
own logic. Every pack script passes the install-time gate (syntax / strict
smoke-run / lint) before it reaches a Script row, and `pack.json` pins the
engine-stdlib fingerprint plus a sha per lua/ file — bootstrap refuses drift
(regenerate deliberately with `python -m experiments.world.manifest`). A
player rewriting from scratch reads the tiers (`get_script_libraries`),
keeps, drops, or replaces the opinions their starter leaned on; the
vocabulary beneath never goes missing. Submissions are linted at submit
time against those same tiers — a script citing vocabulary that is not
injected is refused with the finding in hand, not zombied at the next
tick.

## The content pack (declared)

The full recipe graph lives in `scenario.py::create_content`. Three recipes
live in the survival run; the rest are exercised **in isolation** by the
focused feature tests so every `Recipe` feature is proven without entangling
the survival economy:

| recipe | feature proven |
|---|---|
| `FARM_GRAIN`, `MINE_ORE`, `SMELT_IRON` | the live chain (survival + extraction + tech-gated smelting) |
| `MILL_FLOUR` | a tech gate + a located facility (`MILL` + `MILLING`) |
| `BAKE_BREAD` | the food value-add chain |
| `MINE_COAL`, `QUARRY_STONE` | further extraction |
| `MAKE_STEEL` | a multi-tick, **flow-fed** recipe (`per_tick_inputs`, duration 2) |
| `MAKE_TOOLS` | a capital-good output |
| `BUILD_FORGE` | **construction** (`builds_facility`) + a hold-not-consume `good_requirement` |
| `RESEARCH_STEEL` | **research** — the output is an unlock, not goods |

The tech tree exercises the `ENTITY`/`WORLD` scope distinction: food skills
(FARMING, MILLING, BAKING) are per-person; smelting physics (SMELTING,
STEELMAKING) is world-shared once discovered; TOOLMAKING is a per-person
skill that presupposes the world knows how to smelt.

## How to run

```bash
.venv/bin/python -m experiments.world.run --ticks 40
.venv/bin/pytest experiments/world/test_world.py        # the proving tests
```

The CLI prints a per-tick digest (balances, grain, ore, iron, trades, alive
count) and a final summary (money conserved, script errors). Expected:
everyone stays `active`, `HUNGER` stays at zero, the Smith accumulates IRON,
4–6 trades clear per tick, and money is conserved at the 2000 USD genesis
endowment.

## Design notes

- **Survival is decoupled from market calibration.** FARM_GRAIN yields more
  than the Farmer eats; the surplus feeds the specialists. The food market
  *does* clear (it's the proving point) but nobody starves if it hiccups,
  because the chain is balanced and food is cheap.
- **The ORE→IRON chain is the proving trade.** Production crosses a market:
  the Smith's smelt intent fails first (no ORE in hand — the buy hasn't
  cleared), the auction clears the buy, then the engine **retries** the smelt
  and it succeeds. This is the engine's input-short-retry mechanism doing
  exactly what it was built for.
- **No monetary authority.** Money is a genesis endowment; nothing mints or
  burns during the run, so the money supply is conserved by trade (asserted).
  A bank/issue path is a Phase 1+ concern.
- **The IRON market has no buyer** in this cast — the Smith accumulates IRON
  and runs on its endowment. A Toolmaker/Steelmaker that closes the loop is a
  natural Phase 1 addition; for Phase 0 the point is that the chain *runs*,
  not that it reaches a sustainable steady state.

## What Phase 0 deliberately does NOT do

- **Player control** (editing your own entity's behaviour script) — that's the
  Phase 1 ownership-gated autonomy path (`docs/game.md` §6).
- **A labourer pool / firm hiring** — LABOR is auto-issued and self-consumed
  by each specialist; the LABOR *market* is proven by a focused two-entity
  test, not by the live economy. Hiring is a Phase 1 firm mechanic.
- **Victory observer, leaderboard, logistics** — Phases 2a/2c and 3. The
  governance-window *clerk* (above) landed with Phase 2b.
