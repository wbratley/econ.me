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
- **The IRON market has no buyer in the proving cast** — the Smith
  accumulates IRON and runs on its endowment (the cast's 30-IRON upkeep
  buffer keeps the 40-tick proving run inert to the sink). The agent
  world closes the loop: every house carries the **UPKEEP** need (0.5
  IRON/tick, unmet → DISREPAIR, HUNGER's twin), so there the IRON market
  has three standing customers and specialization is a paid choice.

## The stone age (content pack #2)

`stone_age.py` is the second content pack — same rule (data + Lua, zero
engine change), different problem. The frontier pack proves the engine is
an *economy*; the stone age proves it is a *survival game*: survival
costs most of the tick, tools buy back time, and neglect kills.

| axis | frontier | stone age |
|---|---|---|
| bind | markets / specialization | needs / the elements + the day/night clock |
| scarcity | ORE (deposit-gated) | LABOR (1 labor-hour per DAYLIGHT hour; none at night) |
| teeth | DISREPAIR (the UPKEEP sink) | HUNGER, EXPOSURE, DISEASE |
| starter script | comfortable | hand-to-mouth treadmill |

The shape: two needs — **FOOD** (0.5/hour from SATIETY, the stomach,
else HUNGER) and **WARMTH** (1/hour by day, 3/hour at night, from the
WARMTH flow-good, else EXPOSURE). **The clock (run 18)**: tick = hour,
round = 24 ticks = one day; daylight is hours 06..19 — LABOR issues
only then, and GATHER/HUNT are refused in the dark (a clear error that
names the window). `std.hour()` / `std.is_night()` / `std.day()` are
pure-info queries over `ctx.clock`. **Conscious eating (run 19)**:
nothing is eaten for you — the FOOD need drinks only SATIETY, and only
EAT recipes fill the stomach: EAT_BERRIES (2 berries, ~3h),
EAT_COOKED (~4h), EAT_JERKY (~5½h, densest), EAT_RAW (~1h, a
25%-per-meal DISEASE lottery). Meals are labor-free, instant and
night-legal; the stomach spills a tenth an hour, so a day costs ~14 —
the treadmill arithmetic is unchanged, but a full larder now feeds
nobody until someone runs the recipe. **Wolves (run 20)**: creatures,
not mechanics — spawned ENTITIES with stats (ATK/DEFENSE rows), health
(a **HITS** holding: 12 for a wolf, 20 for a house, never regrown),
the same needs as a house, and a hunting program: they hunt at night
because they are hungry, target the loudest speaker they *heard*
(the witness feed is their ears), eat raw meat, and keep warm by
PACE. Combat (engine `combat.py`) is entity-vs-entity under declared
`COMBAT_RULES`: daylight refuses the hunt, a lit hearth (WARMTH ≥ 1)
turns an attacker at the door (a loud miss), hit% = 50 + 5×(ATK−DEF)
clamped 5–95 on the commit-reveal RNG, damage = max(1, ATK−DEF);
zero HITS is the ordinary death machinery, and the victor seizes the
loot (PELT + MEAT — the post bids pelts). Weapons are carried, not
born: SPEAR +3 ATTACK, CLOTHES +1 DEFENSE; any entity may `attack()`
anyone it can name, and every fight is a loud fact every house hears.
Population renews at round boundaries (`spawns.py`): from round 5,
every 5 rounds, up to 3 more packs, never more than 4 alive.
Food comes from GATHER (a
loot-table recipe) and HUNT (a lottery: 55% nothing bare-handed);
warmth from TEND_FIRE (1 WOOD → 10 WARMTH at a FIRE facility — a log
carries the colder night) plus a graded ladder — REST under a SHELTER
(1.0, labor-free), HUDDLE in CLOTHES (+0.5, labor-free). Shelter +
clothes cover every mild day for free; nights still draw 3 an hour,
so dusk wants either banked warmth stock or the fire tended. MEAT rots
(0.30/hour) and eating it raw is a 25%-per-meal DISEASE lottery —
cooking is a FIRE facility away. Capital goods are the escape: SPEAR /
BAG / TRAP improve the hunt and the gather, CLOTHES / SHELTER / BED
the warmth budget. All conditions follow the run-5 equilibrium lesson:
grant/decay equilibrium sits **above** the incapacitation threshold
(HUNGER 20 ≫ 15, EXPOSURE 30 ≫ 18, DISEASE 20 ≫ 2.5), so neglect
reliably kills between ticks 18–40, and adaptation reliably saves.

The balance contract is three policy tests, not numbers in a comment:
`test_neglect_kills` (a seat that gathers nothing dies in 18–40),
`test_shelter_alone_is_misery_not_death` (chronic cold, alive), and
`test_starter_survives` (the bare `lua/stone_age_starter.lua` treadmill
survives 40 ticks — barely, by design: it never builds capital).

Expansion hooks left deliberately inert: BED (built, no effect yet),
TRAP-consuming HUNT_TRAPS (best table, consumable), and the whole
GOOD/RESEARCH axis from `docs/game.md` — the pack is data, so phases
land as new rows, not engine patches.

### The trading post (run 1–3 postmortem: the missing sell side)

Three agent runs, zero trades. Coin existed (30 at genesis), hunger
existed, surplus existed (run 3: Llama ended with 30 FLINT, 11 YARN and
no buyer in the world; OSS starved at the bid, never seeing a seller) —
what never existed was a counterparty or a price to quote against
("the price is unknown", OSS diary). The fix is pack data, not engine
change: `spawn_trading_post` (called by `create_content`, so every
stone-age world has one) creates THE TRADING POST — a `BUSINESS`
entity (no needs, no LABOR: it can neither starve nor freeze) with a
small COIN purse (30), a finite larder of safe food (60 BERRIES, 20
COOKED_MEAT — it rots like anyone's — plus 30 JERKY, salted meat
that never rots: the shelf that is never bare, so late-arriving coin
always has something to buy), and `lua/trading_post.lua`, a
market-maker behaviour. It sells the whole larder at an ask and bids
4 units for MEAT/WOOD/YARN/FLINT/BERRIES, never crossing its own
spread, and stopping at 20 held of any good. The purse is split
pro-rata across every good it wants — a lean budget shrinks all bids
together instead of letting the head of the list eat the coin and
starve the tail (run 4: MEAT/WOOD bids at the 5.00 cap consumed the
purse; 57 FLINT of surplus found no bid). Prices haggle from its own
fills (own events feed `ctx.events`): each food sale raises the ask
5% (demand), each filled bid lowers it 5% (supply), and 3 quiet ticks
move prices the other way (ask −5%, bid +3%), floored/capped at 1/8
and 0.50/5. Quiet is counted only while an order actually rests — a
bid gone dark (no budget for it) freezes its price instead of
walking it to the cap for nothing (run 4 again). The standing
orders are the world's first public price reference; the coin flows
are the first circular income (houses sell surplus → coin → buy
food).

Run 4 then supplied two lessons the engine itself had to learn. First:
an auction is between counterparties — one entity quoting both legs
off the same reference crossed itself 360 times, wash volume that
pinned the price at its own anchor, so `_settle` now bars same-entity
matches (the younger order steps aside; both stay resting). Second:
the diary is a diary — Nemotron's entries were leaked chain-of-thought
("We are given the current behavior..."), so the diary prompt now
forbids reasoning traces and demands first-person past tense.

Agent runs pick it with `--scenario stone_age`:

```bash
.venv/bin/python -m experiments.agent.nim_run --scenario stone_age ...
.venv/bin/pytest experiments/world/test_stone_age.py
```

### stone-run1 postmortem: three pack fixes

The first agent run on this pack (`stone-run1`, 3 models × 10 rounds)
ended with a starvation death at tick 101, **zero trades**, and an
untouched tech tree — nobody ever built a tool, and the 500-coin seat
endowment sat unspent because there was nothing to buy and nobody
conceived of selling. Three pack-level changes answer it:

1. **Coins are found, not endowed** (SEAT_COIN 500 → 10). GATHER_BAG
   carries a 5% shiny-stone branch that *mints* COIN — the money supply
   grows with digging, via the engine's production-mint seam
   (`_credit_output`: a branch output whose symbol some account banks in
   credits the account and rides the ledger; anything else stays a good).
   A start-from-zero fortune now has a visible origin story.
2. **A legible world** (the 3a fold): `world.manual` (WorldSetting,
   shipped by `create_content`) now carries only the authored NOTES —
   the ladder, the trading post, privacy — while every good, need,
   recipe, technology and market rides the system prompt as a
   GENERATED catalog (`catalog_text`, derived from the installed
   content: inputs, odds, durations, gates, death thresholds). The tech
   tree was always *visible*; now it is *readable*, and it can never
   drift from the physics it renders.
3. **Rival privacy** (`world.private_holdings`, enforced by the engine):
   `ctx.query.holding` of another entity returns nil, `holders()` is
   empty, and the agent loop's observation drops the leaderboard's money
   column. Nobody plans around a rival's pantry they cannot see; the
   referee (op-context scripts) stays sighted.

## What Phase 0 deliberately does NOT do

- **Player control** (editing your own entity's behaviour script) — that's the
  Phase 1 ownership-gated autonomy path (`docs/game.md` §6).
- **A labourer pool / firm hiring** — LABOR is auto-issued and self-consumed
  by each specialist; the LABOR *market* is proven by a focused two-entity
  test, not by the live economy. Hiring is a Phase 1 firm mechanic.
- **Victory observer, leaderboard, logistics** — Phases 2a/2c and 3. The
  governance-window *clerk* (above) landed with Phase 2b.
