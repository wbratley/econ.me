# Fielding inequality experiment — progress notes

Status: pricing rebuilt, scenario recalibrated, and runs finally made
reproducible — which killed both the matrix's headline finding and the
follow-up theory built on it. Current findings are in "Variance is the
actual result" below.
The earlier findings table has been deleted rather than annotated: it
predated bugs 6, 7 and 8, and every number in it was an artifact of a price
system that could not converge.
Branch: `claude/inequality-experiment`. Nothing in `experiments/` is committed
yet.

## Goal

Exercise econ.me as the "economic modelling software" consumer design.md
always described (§1.3) — build a synthetic economy directly on `econengine`
(no HTTP layer) and run real experiments: how does wealth concentration,
survival, and mobility differ under different redistribution and estate-rule
policies, at different scales. See `../../docs/design.md` §1–§2 for the
engine's mechanism/data/policy split this leans on throughout.

## What's built

- `scenario.py` — genesis setup for "Fielding": a Central Bank, a Treasury
  (GOVERNMENT), N Firms (BUSINESS, land-owning), and a population of
  Individuals with heterogeneous starting cash (skewed 70/20/10 poor/middle/
  rich), a minority of whom are smallholders (own land + starting skill).
  Full goods/needs/recipes/tech/parcels content: `LABOR`/`LABOR-FARM` (labor
  market + skill conversion), `SKILL-FARM` (learning-by-doing), `FOOD`/
  `CLOTHES` (needs, priority-ordered), `TOOLS` (capital good, fractional
  wear), `COND-WEAK` (poverty-condition, halves labor productivity and
  eventually incapacitates), stochastic harvest yields (branch tables),
  `AGRONOMY` tech gating a better production method, `FIELD`/`FARM`
  parcels+facilities with a regenerating `SOIL-FERTILITY` deposit.
- `lua/prelude.lua`, `lua/individual.lua`, `lua/firm.lua`,
  `lua/treasury.lua` — static Lua source shared verbatim across every
  rule-variant. The prelude (holdings/price lookup, order-fill feedback, the
  reservation-price helpers) is prepended to each behaviour script by
  `scenario.py`, since scripts live in the DB as flat source strings and the
  sandbox has no `require` — note this offsets Lua error line numbers.
  Beyond that, only each entity's
  `Script.state` differs (tax rate, treasury account, recipient list). This
  is the mechanism/data split done properly: **taxation is voluntary
  self-assessed remittance** (the ownership invariant means a Government
  script can never force a transfer out of someone else's account — see
  design.md § military for the same rule blocking forced asset seizure),
  and the Treasury's own POLICY script redistributes only from its own
  collected balance.
- `metrics.py` — pure-Python Gini/percentile/share/mobility stats (no
  numpy/pandas in this repo's `.venv`).
- `run.py` — single-scenario CLI + `run_scenario()` for programmatic use.
- `matrix.py` — runs the 5-variant sweep (tax none/flat/progressive ×
  estate burn/treasury/heir) and writes one JSON per variant.

## Bugs found and fixed (chronological)

1. **GTC order stacking → runaway price ratchet.** Orders are good-til-
   cancelled; every script placed a fresh order every tick without
   cancelling the last one, so unfilled orders piled up and prices
   ratcheted indefinitely in one direction. Fix: every script now cancels
   its own previous order (found via `ctx.events`, already filtered to the
   entity's own events) before placing a new one — exactly one resting
   order per symbol at a time.
2. **`LABOR` decay timing made market-bought labor unusable.** `LABOR` fully
   decayed every tick; decay runs *after* the auction but *before* the next
   tick's scripts, so a firm's just-bought labor was destroyed before the
   firm's script ever got a chance to act on it. Fix: `LABOR`/`LABOR-FARM`
   decay reduced to 0.5/tick so a bought unit survives to be usable next
   tick.
3. **`SKILL-FARM` bootstrap deadlock.** Firms/smallholders started with
   exactly `SKILL-FARM = 1`, the exact threshold `good_requirements` checks
   for `WORK_AS_FARMER`. `SKILL-FARM` decays every tick regardless of use,
   and buying labor takes ≥1 tick to arrive, so it decayed below the
   threshold before a firm ever got its first chance to use it — permanent
   lockout. Fix: seed with headroom (2, not 1).
4. **Smallholders never sold surplus food.** They ate their own harvest but
   had no sell-side logic at all, so any surplus beyond their own need just
   sat and decayed instead of reaching landless buyers. Fix: added a
   surplus-sell step to `individual.lua`.
5. **CLI/dataclass default mismatch silently undid calibration.**
   `run.py`'s argparse had its own hardcoded default for every
   `ScenarioConfig` field; several rounds of editing `smallholder_fraction`
   in the dataclass had zero effect because the CLI default always won
   unless the flag was passed explicitly. Fix: CLI defaults now read off
   `ScenarioConfig()`'s own fields — single source of truth.
6. **Labor glut from a fixed employer count.** `n_firms` was a flat number
   independent of population size. Measured directly: with 5 firms and 20
   landless individuals, exactly 5 units/tick (25%) of offered labor went
   unsold every tick regardless of price — a hard quantity constraint, not
   a price one. In no real economy does employment capacity stay fixed
   while population grows. Fix: `recommended_n_firms()` scales employer
   count with the landless population.
7. **Pricing had no feedback and no fundamentals.** Every bid/ask was a
   fixed percentage of the last market price — a positive feedback loop with
   nothing in it referring to what anything is worth. Fix: agents quote
   reservation prices derived from fundamentals, firms bid a demand schedule,
   sellers get bounded fill feedback. Full write-up below.
8. **The price level was frozen, not converged.** With the ratchet gone,
   prices held steady — but doubling the money stock moved them *down*, and
   scarcer land made food *cheaper*. The old calibration had 20 fields
   feeding 30 people, so a wide band of prices all cleared the same volume
   and the auction's tie-break pinned the level to whatever the opening ticks
   set. Fix: household demand anchored on cash and fixed real consumption
   rates, and farmland calibrated so scarcity is real. Full write-up below.

## Bug 7, in full: pricing had no feedback and no anchor

Fixing 6 improved landless survival but surfaced a third symptom of the same
gap: after adding more firms (more labor *demand*), `LABOR` price should have
risen, but it crashed toward zero while `FOOD` climbed past 10. Every
script's bid/ask was `market_price(sym) * 1.1` to buy and `* 0.95` to sell --
a pure positive feedback loop, since the clearing price those orders produce
becomes the next tick's anchor. Nothing in it referred to what anything was
actually worth, so there was no level for it to converge ON.

The replacement is in `lua/prelude.lua` (prepended to every behaviour script
by `scenario.py`, since scripts are flat source strings in the DB and the
sandbox has no `require`). Three parts:

1. **Reservation prices from fundamentals, not from the last trade.** A firm
   values raw labor at its marginal revenue product -- it knows its own
   recipes, so it knows what an extra unit of labor produces and what that
   output sells for. A household prices food off its cash and its fixed real
   consumption rate. A worker's reservation wage is what a day's food costs
   *them*, which is why the poor are the cheapest workers and fill first.
   The auction is uniform-price, so quoting honestly never means overpaying
   -- you pay the clearing price, not your limit.
2. **Firms bid a schedule, not a number.** One `LABOR` order per use
   (farming, clothes, tools, banked research), each at that use's own value.
   This is what finally gives labor a downward-sloping demand curve; the old
   flat "buy 3 units" had no quantity response to price at all, so no amount
   of cheap labor ever induced anyone to hire more of it.
3. **Fill feedback, sell side only, bounded.** `settle_last_orders()` cancels
   the entity's own resting GTC orders and measures what fraction of each
   actually filled (matching `trade` events back to `place_order` events by
   `order_id`); `concede()` walks a seller's ask down from its cost anchor
   when stock will not move and back up when it all does, clamped to
   [0.4x, 2.5x]. Buyers deliberately do not adapt: at any real equilibrium a
   buyer's low-value orders are *supposed* to go unfilled, so reading that as
   "bid higher" walks straight back into the original bug.

Orders that die at the auction for want of funds or holdings are excluded
from the feedback -- that is a failure to be solvent, not evidence about the
price, and adapting on it would push a broke agent to bid ever higher for
what it already could not afford.

## Bug 8: the price level was frozen, not converged

With the ratchet gone, prices held steady -- and a money-neutrality check
showed the stability was fake. Doubling every starting balance changed
nothing real, so prices should have scaled; instead `FOOD` went *down*
(1.64 -> 0.59) and land scarcity moved food prices the wrong way too.

Price paths explain it. In the old calibration `FOOD` sat at exactly 1.636
for ninety consecutive ticks while only a quarter of the food offered ever
sold. When supply hugely exceeds demand, a wide band of prices all clear the
same volume, and `_clearing_price`'s tie-break (nearest the last price) pins
the level at whatever the opening ticks happened to set. That is path
dependence wearing convergence's clothes: the level was determined by a
historical accident, because with no scarcity there was nothing to determine
it by.

Two fixes:

- **Household demand is quoted from cash and fixed real consumption rates,
  never from a market price** (`normal_food_price` in `individual.lua`). A
  given stock of money chasing a given real flow of goods gives the price
  level somewhere to sit. Food is bought in two tiers -- tonight's meal,
  nearly price-insensitive and worth digging into savings for, and the
  pantry restock, worth doing only at a discount -- which is what gives a
  household a demand *curve* rather than a fixed quantity at any price.
- **`recommended_n_firms()` recalibrated around land, not headcount.** The
  bug-6 fix cured a labor glut by creating a food glut: 10 firms plus 10
  smallholders is 20 fields feeding 30 people who eat 0.8/tick. Farmland is
  now derived from the population's food requirement.

Calibration sweep, 30 individuals, 100 ticks:

| Fields | FOOD price | Food sold | Mean hunger | Incapacitated |
|---|---|---|---|---|
| 7 | 0.94 -> 1.19 | 1.00 | 0.69 | 0 -> 11 |
| 9 | 1.69 -> 2.62 | 0.69 | 0.86 | 0 -> 0 |
| 11 | 1.79 -> 2.85 | 0.86 | 1.00 | 0 -> 0 |
| 13 | 0.57 -> 0.61 | 0.70 | 1.00 | 0 -> 0 |

Seven fields is a death spiral rather than an economy (unmet hunger applies
`COND-WEAK`, which halves labor productivity, which cuts output further).
Thirteen is the frozen-price failure above. Nine keeps the food market
genuinely tight: most of what is offered sells, prices move, scarcity bites
at the margin without taking the whole population with it. In a tight
economy money neutrality roughly holds (2x cash -> ~1.8x food price), which
it never did in a slack one.

## Instrumentation added

- `metrics.py` records per-symbol ordered-vs-filled quantity per tick, read
  back off the tick's own event log. A price series alone hid the bug-6
  labor glut completely; a fill ratio makes "rationed by quantity" and
  "priced by scarcity" distinguishable at a glance.
- `experiments/progress.py` -- progress bar with an ETA for `run.py` and
  `matrix.py`, since these runs are minutes long and usually launched into a
  log file. Renders in place on a terminal, as periodic lines when
  redirected. `--no-progress` suppresses it.
- `ScenarioConfig.n_firms` now defaults to `None` and is resolved once in
  `build_economy`, so there is exactly one place that decides it -- the same
  duplicated-default trap as bug 5.

## Performance: runs were quadratic in run length

Sweeps were taking hours. Profiled rather than guessed, and the cause was
not where it looked.

**The bottleneck was one transaction held open for the whole run.**
`run_scenario` committed only at the end, on the reasoning that `run_tick`
flushes but never commits so ticks can share a transaction (design.md
§"fast-forward"). The effect is that per-tick cost grows linearly with tick
number, making total cost quadratic in run length — 30 individuals, measured:

| tick | one transaction | commit each tick |
|---|---|---|
| 10 | 0.58 s/tick | 0.59 |
| 50 | 1.15 | 0.72 |
| 100 | 2.11 (still climbing) | 0.74 |

It is not query volume and not table scans. Over ticks 6→65 statement
counts rise ~10%, but time inside `sqlite3.Cursor.execute` goes 0.66s →
4.05s for the same work, and the expensive statements are primary-key
`UPDATE`/`INSERT`s against *tiny* tables — 0.22ms to update the 143-row
`holdings` table. Every write records undo state into an ever-growing
uncommitted transaction, with ~320 open savepoints per tick on top of it.
The harness gains nothing from batching, so it now commits per tick.
Verified byte-identical output across all 61 snapshots of two variants.

**Sweeps now run one process per scenario.** `matrix.py` and `tipping.py`
looped serially on a 12-core box. `experiments/parallel.py` fans them out;
`--workers 1` keeps the serial path and its progress bar. Processes, never
threads — see `determinism.py`. Measured: matrix 1:52 → 0:28, a 4-seed
tipping sweep 1:57 → 0:35, both byte-identical to serial.

Ruled out by measurement, recorded so it is not re-investigated:

- **Lua is 9% of wall time.** A fresh `LuaRuntime` and a fresh OS thread per
  script execution looks like the obvious culprit and is not one; running
  scripts inline on the calling thread saved 3%.
- **Indexes changed nothing.** The schema declares none at all, but adding
  12 covering ones made no measurable difference — `holdings` already gets
  an implicit index from its unique constraint, and the growth was never
  scan-bound.
- **Savepoints cost 9.5%**, and removing them costs the per-intent error
  isolation that keeps one bad intent from poisoning the rest. Bad trade.
- **`Transaction(account=...)` does not load the account's history.** A
  plausible quadratic via the `back_populates` collection; measured zero
  transaction-history SELECTs.
- **Population scaling is linear** (15→120 individuals, 0.30→2.80 s/tick),
  so larger economies are fine.

What remains is a flat ~0.65 s/tick of which **56% is SQLAlchemy ORM
overhead and only ~10% is actual SQLite** — auctions 36%, intent resolution
31%. Going faster means batching the per-fill `session.get(Account)` /
`adjust_holding` / `reserved_quantity` round-trips in `markets._settle` into
set-based operations. Not attempted; it is a real refactor of the settlement
path and the two changes above bought enough headroom.

## First matrix on the rebuilt pricing

30 individuals, 9 fields, 200 ticks, seed 0, one run per variant:

| Variant | Gini (final) | Incapacitated | Mean hunger satisfaction |
|---|---|---|---|
| No tax (baseline) | 0.657 | 16/30 | 0.543 |
| Flat tax 10% | 0.377 | 11/30 | 0.677 |
| Progressive tax 20% (>$200) | 0.479 | 10/30 | 0.751 |
| Estate → treasury | 0.211 | 2/30 | 0.906 |
| Estate → heir | 0.234 | 6/30 | 0.729 |

The shape of this is the *opposite* of what the pre-fix matrix appeared to
show. That one had redistribution collapsing inequality among survivors
while changing nothing whatsoever about who survived — every variant killed
exactly the same 20 people. The earlier result was an artifact: with prices
unable to converge, nothing that depended on being able to *afford* food
could have been measured in the first place.

## Seed sweep (superseded — see "Reproducibility" below)

Three further runs per variant, same settings. These were labelled by seed
but were not actually controlled by it — kept here because the reasoning
they prompted is what uncovered the reproducibility problem. Incapacitated
out of 30:

| Variant | seed 0 | seed 1 | seed 2 | seed 3 |
|---|---|---|---|---|
| No tax | 16 | 15 | 16 | 17 |
| Flat tax 10% | 11 | 4 | 9 | 4 |
| Estate → treasury | **2** | **14** | **17** | **1** |

The baseline is a stable, reproducible outcome — a 30-person economy on this
much land kills about half of it, every time. Flat tax reliably improves on
that: its worst draw (11) still beats the baseline's best (15).

This looked bimodal — either near-total rescue or no better than doing
nothing, nothing in between — and that reading drove the next round of work.
Twelve reproducible replicates later it is not bimodal at all, and the flat
tax turns out to have the same tail. See below.

**Still-live caveats.** One population size, one field count. The baseline is
still degrading at tick 200 (hunger 0.543 and falling), so these may be
points on a slope rather than steady states. Progressive tax ending more
unequal than flat tax while feeding people better is unexplained. And every
number in this section predates reproducibility, so none of it reproduces.

## Reproducibility: the seed never controlled the run

`ScenarioConfig.seed` only ever controlled *genesis* — who starts rich, who
owns land, which firm bids how hard. Nothing stochastic during a run was
seeded, and an identical config gave a different answer every time (measured:
same seed, two runs, 7 vs 2 people incapacitated; food-produced series
diverging from tick 3).

The chain is `rng.outcome_roll() = sha256(prev tick's event hash + ":" +
process_id)`, and `Process.id` is a `uuid.uuid4()` — so every harvest outcome
came from fresh OS entropy.

**This is not an engine defect.** `rng.py` guarantees that an auditor holding
the persisted rows can recompute every roll, and that a roller cannot
cherry-pick outcomes — which *requires* the process ID be unpredictable
before the last cancellation opportunity. That is auditability. Experiment
reproducibility is a different property, and the harness needed it and never
had it. Making IDs deterministic in the engine would trade the commit-reveal
away for every real deployment in order to serve a test harness.

`experiments/determinism.py` seeds `uuid.uuid4` for the duration of a run
instead. Engine untouched; same seed now reproduces bit-for-bit, different
seeds still differ. Everything below this line is reproducible; everything
above it, including the matrix table, is not — those runs stand as
independent replicates, not as reproducible individual results.

## Testing the tipping-point theory: both hypotheses refuted

The theory was that `estate → treasury`'s bimodality came from a tipping
point in `COND-WEAK` (H1), with a rival explanation found while instrumenting
— that `_apply_estate` moves parcels to a Treasury which has no production
script, so inherited fields stop growing food permanently (H2).

**The premise did not survive replication.** Twelve reproducible replicates
of `estate → treasury` give 0, 1, 1, 1, 1, 2, 3, 4, 6, 8, 16, 23 — a
continuous right-skewed spread, not two modes. The "either ~1 or ~15, nothing
in between" pattern was four uncontrolled draws landing at the ends.

Both hypotheses fail on their own predictions:

- **H1** predicted the split tracks *when* hunger first bites. It bites at
  tick 5 in all twelve runs, zero variance — the instrument is a constant.
- **H2** predicted idle farmland drives collapse. Idle fields are 0 in eleven
  of twelve runs, and the twelfth reaches 2 only *after* 23 deaths. The arrow
  points the other way: deaths idle the land, not the reverse.

What deaths do track, across the twelve:

| Instrument | r with deaths |
|---|---|
| Accumulated `COND-WEAK` at tick 100 | **+0.90** |
| Number of people carrying any `COND-WEAK` | **−0.59** |
| Food produced at tick 100 | −0.69 |
| When hunger first bit | 0.00 (no variance) |
| Idle farmland | 0.00 (no variance) |

The sign flip is the interesting part. More people going hungry predicts
*fewer* deaths; more accumulated hunger predicts more. Incapacitation is a
per-person threshold (30 units of `COND-WEAK`, which never decays), so the
same total shortfall spread across twenty people kills nobody and
concentrated onto five kills five. What matters is the distribution of
deprivation, not its level — which is a distributional finding, not a
tipping point.

Caveat: with carrier counts spanning only 14–20, total and per-carrier
`COND-WEAK` are near-collinear (r = +0.90 vs +0.90), so this separates the
*sign* of the two effects convincingly but not their independent magnitudes.

## Variance is the actual result, and it is not policy-specific

Twelve reproducible replicates per condition, incapacitated out of 30:

| Condition | mean | sd | median | range |
|---|---|---|---|---|
| No tax | 13.8 | 2.1 | 14.0 | 8–17 |
| Flat tax 10% | 6.0 | 6.3 | 4.5 | 0–24 |
| Estate → treasury | 5.5 | 6.8 | 2.5 | 0–23 |

Doing nothing is the *reliable* option: a dependable ~14 deaths, sd 2.1.
Either intervention usually does far better — medians of 2.5 and 4.5 — but
both carry a tail that is worse than doing nothing at all. The two
interventions are not distinguishable from each other at this n; an earlier
reading that the estate rule was uniquely risky was drawn from seven
replicates of the flat tax before its own bad tail showed up.

**Seed 7 is worth its own investigation.** It is unremarkable under no tax
(14 deaths, dead-on the baseline) and catastrophic under *both* interventions
(24 and 23). Redistribution reliably makes that particular economy much
worse, and it now reproduces exactly, so the mechanism is directly
inspectable.

## Firms have no profit margin, and decapitalise

Found while building shareholding: there was nothing for a firm to pay a
dividend *from*. Firm cash across a no-tax run, 30 individuals:

| tick | firm cash (total) | solvent firms | individual cash |
|---|---|---|---|
| 1 | 14,709 | 5 | 13,444 |
| 50 | 10,828 | 5 | 17,326 |
| 100 | 4,341 | 5 | 23,813 |
| 150 | 771 | 1 | 17,989 |

Firms convert their 3,000 genesis endowment into wages and go bankrupt
around tick 125–150. This is not a valuation bug — the recipe chain is
1 LABOR → 1 LABOR-FARM → 4.95 FOOD, so firm.lua's `farm_yield * food_price`
is correct. It is structural: a firm bids labour at `farm_yield ×
food_price` and prices its output at `wage / farm_yield`. Those are exact
inverses, so the margin is **zero by construction**, and every friction
(0.3/tick FOOD decay on unsold stock, 5% crop failure, `concede()` cutting
asks below cost) comes straight out of capital.

Two consequences that reach beyond shares:

- **The economy is subsidised by firm capital for its first ~150 ticks.**
  The baseline's failure to reach a steady state is at least partly the
  endowment running out, not a property of the steady state. Any run longer
  than ~150 ticks spends most of its length in a post-bankruptcy economy of
  one or two survivors, which is a confound in reading the arm comparison.
- **Zero economic profit is textbook perfect competition** and defensible on
  its own. Combined with a fixed endowment it makes the firm sector a
  draining battery rather than a going concern. Giving firms a markup would
  fix it and would recalibrate the entire economy, so it is left as an open
  modelling decision, not silently patched.

## Capital ownership: SHARE-FIRM-n

The model had no capital-income channel at all — wages were an individual's
only income, so the main driver of real wealth concentration was simply
absent. Now: `share_allocation` = `none` | `wealth` | `equal`.

`wealth` and `equal` are the experiment. Both are no-tax, burn-estate runs
with identical firms and identical production; only *who owns them* differs.
That isolates what concentrated ownership does, which no redistribution arm
can ask, because redistribution only ever moves income after the fact.

- Shares are bare symbols (`SHARE-FIRM-1`…), needing no `Good` row: the
  defaults a Good would supply — no decay, no auto-issue — are exactly what
  a share wants. Markets are created for them, so they are tradable in
  principle; no script trades them yet.
- Allocation uses largest-remainder so each firm's register sums to exactly
  `shares_per_firm`. Rounding each slice independently would mint or leak
  fractions of a company, and the dividend divides by the register's live
  total.
- Dividends pay only from cash **above the genesis endowment** — real profit,
  never working capital. Given the decapitalisation above, a lower reserve
  would simply bankrupt firms faster.
- The register is read live via the new `ctx.query.holders(symbol)`, not
  cached in `Script.state`, so a dividend follows the shares the moment any
  of them change hands.

**The channel is real but late.** Nothing pays out until the sector
consolidates and the survivor earns genuine monopsony rents (n=1, wealth
allocation): 8 cumulative by tick 200, 1,008 by tick 300, 4,350 by tick 400.
So capital income redistributes wealth among *survivors* without changing
who survives — the deaths are over by tick 200, long before the first
dividend.

A single seed of `wealth` vs `equal` ends at gini 0.557 vs 0.523, both with
15 incapacitated. **That is n=1 and proves nothing** — this project's own
replicates span 0–23 deaths within one condition. The direction matches the
mechanism, which is reassurance about the wiring and nothing more. The real
comparison needs `sweep.py --arms tax_none,share_wealth,share_equal` at
n≈100.

### Engine addition: `ctx.query.holders(symbol)`

Returns the live register — entity_id, quantity, and the account to pay
through — batched into two queries rather than one per holder, since a
dividend reads it every payout period. Two notes:

- Query callables previously returned only scalars. A Python list arrives in
  Lua as an opaque object where `#` and `ipairs` do not work and indexing is
  0-based, so `lua_engine` now converts list/dict results into real Lua
  tables. Any query can return a row set from here on.
- It is a **global** read: any script can enumerate holders of any symbol.
  Right for a share register (real ones are public), considerably more than
  that for `FOOD`. If per-symbol visibility should be votable data,
  `build_queries` is where it would be gated. Deliberately not gated yet.

## Not yet done

- Establish whether the matrix numbers are steady states or points on a
  slope — the baseline is still degrading at tick 200. Needs a 400+ tick run.
- Work out why seed 7 collapses under redistribution and not without it.
  Reproducible, so the tick-by-tick mechanism is directly inspectable.
- Separate "how much deprivation" from "how concentrated" properly — they are
  near-collinear at this carrier count, so it needs conditions that move the
  number of people affected independently of the total.
- Population and field-count sensitivity (seeds are now swept; these are
  not). The calibration sweep in particular was a single seed.
- Understand why progressive tax ends more unequal than flat tax while
  feeding people better.
- Decide whether firms should carry a profit margin (see "Firms have no
  profit margin" above). It shapes two separate findings — the baseline's
  endless degradation, and capital income arriving too late to affect
  mortality — so it is the highest-leverage open modelling question.
- Run the capital-ownership comparison properly:
  `sweep.py --arms tax_none,share_wealth,share_equal --seeds 100`. The n=1
  result recorded above is not evidence.
- Empirically time and pick a large-scale target (§ Scale in the original
  plan); throughput is now flat across a run and profiled in detail (see
  "Performance" above), which also answers part of the design.md §7
  fast-forward question.
- Visualization artifact (Gini-over-time, wealth-share, mobility scatter,
  skill-premium price series, incapacitation counts).
- Findings write-up.

## Scope added mid-session, not yet incorporated

User requested modeling: a much larger needs list (shelter, electricity/
heating, transport to work, insurance, council tax, road tax, VAT, drink,
entertainment/subscriptions, education, childcare) and consequences for
non-payment of tax/debts (jail, asset seizure, loss of services).

- The expanded needs list is mechanically straightforward (more `Need` rows
  + recipes/goods), just content-heavy.
- "Loss of services" for delinquency is buildable now with zero engine
  changes (a `COND-DELINQUENT`-style good + a VALIDATOR script vetoing
  specific operations while it's held).
- Actual jail/forced asset seizure hits the same ownership invariant that
  blocks forced taxation — it needs a genuinely new engine mechanism,
  structurally similar to the military CONTEST (design.md § military) or
  the estate-transfer-at-incapacity mechanism. Not a genesis-data addition;
  a real, separate build.

## How to reproduce the current state

```bash
# single run
.venv/bin/python -m experiments.inequality.run --individuals 30 --ticks 200 \
  --db /path/to/scratch.db --out /path/to/result.json

# 5-variant matrix (firm count derived from population unless --firms given)
# variants run in parallel, one process each; --workers 1 forces serial
.venv/bin/python -m experiments.inequality.matrix --individuals 30 --ticks 200 \
  --metrics-every 5 --out-dir /path/to/outdir

# seed sweep, one process per seed
.venv/bin/python -m experiments.inequality.tipping --seeds 10 --ticks 200 \
  --out /path/to/tipping.json
```

`run.py` and serial sweeps write a progress bar with an ETA to stderr;
`--no-progress` suppresses it. Under parallelism the bar is suppressed
automatically — N workers sharing one stderr produce noise, not a bar — and
each run reports a line as it finishes.

Roughly 1.4 ticks/sec at 30 individuals, now flat across a run rather than
degrading (see "Performance" above), so a 200-tick run is a couple of
minutes and a sweep costs about one run given enough cores. Cost still
scales linearly with population, which the whole-run ETA accounts for.
