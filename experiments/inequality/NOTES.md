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

> **Reading older numbers in this file.** `firm_margin` defaulted to 0 for
> everything recorded before "Firms with a margin", and now defaults to
> **0.20**. Sections above that one are all margin-0 measurements and are
> still correct *as such* — pass `--firm-margin 0` (or the `margin_00` sweep
> arm) to reproduce them. They are marked inline where the distinction
> changes how a result reads.

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

30 individuals, 9 fields, 200 ticks, seed 0, one run per variant, all at
`firm_margin` 0 (the default at the time — see the note at the top):

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

**Superseded — read "Redistribution delays deaths" below first.** Everything
in this section is measured at tick 200 and replicates at higher n, but tick
200 is mid-transition: the effect it reports is a delay, not a rescue, and
most of the variance it reports is variance in *when* a run dies rather than
*whether*. Kept because the numbers are sound and the reasoning is what
prompted the trajectory measurement that corrected it.

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

## Redistribution delays deaths; it does not prevent them

*(All at `firm_margin` 0, the default at the time. Re-tested at the current
0.20 default in "The arm matrix at margin 0.20" below.)*

The headline result above ("either intervention usually does far better —
medians of 2.5 and 4.5") is **correct at tick 200 and gone by tick 400**.
Deaths out of 30, mean, from 30 seeds per arm measured at fixed ticks within
the same runs:

| arm | t100 | t150 | t200 | t250 | t300 | t400 |
|---|---|---|---|---|---|---|
| tax_none | 0.13 | 4.60 | **13.73** | 15.20 | 15.30 | 15.53 |
| tax_flat | 1.07 | 1.57 | **4.63** | 12.30 | 14.17 | 15.77 |
| estate_treasury | 1.07 | 1.63 | **4.70** | 12.13 | 14.53 | 15.80 |

Significance of tax_none vs tax_flat decays with the effect: p=0.0000 at
t200, 0.0049 at t250, 0.156 at t300, **0.75 at t400**. At n=100 and 400
ticks all three arms sit at 14.9–15.8 with no pair surviving correction for
three comparisons.

**The old numbers were not wrong.** At tick 200 they replicate almost
exactly — old n=12 gave none 13.8 (sd 2.1) / flat 6.0 (6.3) / estate 5.5
(6.8); this run at n=30 gives none 13.7 (2.0) / flat 4.6 (5.6) / treasury
4.7 (6.0). What was wrong was reading a measurement taken mid-transition as
a steady state. Every arm converges on ~15.5 deaths; redistribution buys
about 50–100 ticks of delay, and buys them at the cost of a slightly earlier
*first* death (95.2 vs 141.7, p<0.0001) because taxing subsistence-margin
households pushes them under sooner.

### "Variance is the actual result" was mostly transition timing

sd of the death count by tick:

| arm | t100 | t150 | t200 | t250 | t300 | t400 |
|---|---|---|---|---|---|---|
| tax_none | 0.57 | **5.70** | 2.03 | 1.35 | 1.64 | 1.78 |
| tax_flat | 1.74 | 4.26 | **5.56** | 5.09 | 3.96 | 3.54 |

Each arm's sd peaks exactly when that arm is passing through its die-off and
collapses afterwards. The intervention arms' famous sd of 6.3–6.8 was tick
200 landing in the middle of *their* transition while the baseline had
already finished its own — so a run running slightly fast or slow showed a
wildly different death count. It was variance in *when*, misread as variance
in *whether*. At t400, sd is 1.8 (none) against 3.5 (flat): the
interventions remain genuinely more variable, but by a factor of two, not
three, and around an identical mean.

### It lines up with the firm sector going bankrupt

Firm cash hits 771 with one solvent firm at tick 150 (see below), and
tax_none's deaths go 4.6 → 13.7 across exactly that window. Redistribution
shifts the same die-off to t200 → t250. The mass-mortality event tracks the
exhaustion of firm capital, and redistribution postpones it by financing
consumption for a while longer. That makes the firm-margin question the
central open issue in this experiment rather than a modelling nicety: as
built, the economy is a battery discharging, and every policy result so far
is a measurement of how fast different policies drain it.

**Followed up and confirmed causally** — giving firms a margin moves the
capital-exhaustion point ~100 ticks later and moves the die-off with it, then
converges to the same place anyway. See "Firms with a margin" below. The
battery framing survives; the guess that firm pricing was the battery does
not.

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

**Resolved — the markup was built and swept. It does not fix it.** See
"Firms with a margin" below; the prediction in the second bullet was wrong,
and interestingly wrong.

## Firms with a margin: the same result again, one level down

`firm_margin` is now a scenario parameter (default 0, so every result above
reproduces unchanged — verified: seed 0, 400 ticks gives 20 incapacitated /
gini 0.823 against the 20 / 0.8225 recorded in `sweep_n100_t400.json`).

**The wedge is symmetric, and it has to be.** The firm withholds the margin
from its labour bid (`× (1 - m)`) and adds it back to its ask (`÷ (1 - m)`).
A one-sided markup does not just create a margin — it makes the price level
drift forever, because nothing in this economy pins the price level. Mark
only the bid down and the wage settles at `y·P·(1-m)`, so the ask becomes
`w/y = P·(1-m)` and next tick's price is `(1-m)` times this one's: geometric
deflation, compounding. Mark only the ask up and it inflates the same way.
With both, the fixed point is `ask = w/y/(1-m) = P` — unchanged — while
labour cost per unit of output is `P·(1-m)`, a gross margin of exactly `m` on
revenue. The markup is applied in the bid loop rather than per tranche, so a
tranche added later cannot silently opt out of it.

### It works, mechanically

30 seeds per arm, 400 ticks, 30 individuals, no tax, burn estate — arms
differ *only* in the margin (`results/sweep_margin_n30_t400.json`). Mean
firm cash (genesis endowment: 5 × 3,000 = 15,000):

| arm | t50 | t100 | t150 | t200 | t300 | final |
|---|---|---|---|---|---|---|
| tax_none (m=0) | 10,946 | 6,162 | 2,832 | 3,429 | 4,210 | 6,324 |
| margin_10 | 11,543 | 8,028 | 3,623 | 2,282 | 2,503 | 4,371 |
| margin_20 | 12,152 | 9,870 | **7,345** | **5,034** | 2,329 | 2,876 |

Mean firms solvent, of 5:

| arm | t50 | t100 | t150 | t200 | t300 | final |
|---|---|---|---|---|---|---|
| tax_none | 5.00 | 5.00 | 3.17 | 2.23 | 1.93 | 1.87 |
| margin_10 | 5.00 | 5.00 | 4.57 | 2.53 | 1.97 | 1.77 |
| margin_20 | 5.00 | 5.00 | **5.00** | **4.67** | 3.20 | 1.90 |

At m=0.20 the whole sector is still solvent at tick 150, where the baseline
has already lost two firms and 82% of its capital. So the margin does what
it was supposed to do.

### And it reproduces the redistribution result exactly

Mean deaths out of 30:

| arm | t100 | t150 | t200 | t250 | t300 | t400 |
|---|---|---|---|---|---|---|
| tax_none | 0.13 | 4.60 | **13.73** | 15.20 | 15.30 | 15.53 |
| margin_10 | 0.80 | 4.20 | 12.23 | 14.20 | 14.47 | 14.57 |
| margin_20 | 1.33 | 5.10 | **7.03** | 9.37 | 11.60 | **14.10** |

tax_none vs margin_20, Welch (\* survives Bonferroni at α=0.05/3):

| measure | none | m=0.20 | diff | p |
|---|---|---|---|---|
| deaths at t200 | 13.73 | 7.03 | +6.70 | 0.00000 \* |
| deaths at t300 | 15.30 | 11.60 | +3.70 | 0.00001 \* |
| deaths at t400 | 15.53 | 14.10 | **+1.43** | 0.00727 \* |
| first death tick | 141.1 | **115.6** | +25.5 | 0.00003 \* |

This is the same shape as "Redistribution delays deaths" above, down to the
detail that the intervention **kills someone earlier** — first death at 115.6
against 141.1 — because withholding the margin from wages pushes
subsistence-margin households under sooner, exactly as taxing them did. A
6.7-death advantage at t200 decays to 1.43 by t400. The residual is real and
survives correction, but it is a fifth of the mid-run effect, and the
mid-run effect is what a naive read of the t200 table would have reported.

**This is stronger evidence for the old mechanism than the old evidence
was.** "The die-off tracks the exhaustion of firm capital" was an
observational alignment between two curves. Here that alignment was
*intervened on*: moving the capital-exhaustion point ~100 ticks later moved
the die-off ~100 ticks later with it, dose-responsively. The correlation
turned out to be causal in the direction assumed.

### The economy is still a battery, and no margin in this range changes that

Firm cash still ends below the genesis endowment in **30 of 30 runs in every
arm**, including m=0.20. Runs ending with ≥3 of 5 firms solvent: 0/30, 0/30,
2/30. A margin re-times the discharge; it does not convert the firm sector
into a going concern.

Why not — total money, 3 seeds, individuals + firms + treasury:

| margin | t1 | t50 | t100 | t150 | t200 | t400 |
|---|---|---|---|---|---|---|
| 0 (seed 0) | 28,154 | 28,154 | 28,154 | 18,760 | 18,760 | 18,253 |
| 0.20 (seed 0) | 28,154 | 28,154 | 27,846 | 25,502 | 25,502 | 22,708 |

**Money is conserved to the cent until the first death**, then falls in steps
that land exactly on deaths — burn-estate destroying the deceased's cash. So
the drain is not a leak in normal operation, and this was worth ruling out
before theorising further. What actually happens is a one-way circular flow:
firms pay out more in wages than they recover in sales (money moves firm →
household and stays there — at m=0, seed 0, firms go 14,356 → 4,341 while
households go 13,798 → 23,813 with the total flat), because food decays,
crops fail, and `concede()` sells below cost. The margin slows that flow
without reversing it. Note also the late-run *rise* in baseline firm cash
(771 at t150 → 6,453 at t400): once the sector consolidates the survivor is
a monopsonist and the flow finally turns around — long after the deaths.

So the honest statement of what the margin settled: the firm sector's zero
margin was a real defect and it was worth fixing, it was **not** what made
the baseline degrade, and every policy result above stands. The battery is
the circular flow, not the pricing rule.

## The arm matrix at margin 0.20

Every policy arm re-run at the new default, since everything above it was
measured in a world where the firm sector was structurally doomed. 8 arms ×
30 seeds × 400 ticks, 3h47m (`results/sweep_matrix_m20_n30_t400.json`).
`margin_00` is the old regime, carried as the anchor.

**Read the caveat below first** — "deaths" here means the `COND-WEAK` counter,
not starvation.

| arm | t100 | t150 | t200 | t300 | **t400** | sd | gini | hunger |
|---|---|---|---|---|---|---|---|---|
| tax_none | 1.33 | 5.10 | 7.03 | 11.60 | 14.10 | 2.19 | 0.629 | 0.587 |
| tax_flat | 0.80 | 1.07 | 2.33 | 10.47 | 13.43 | 3.22 | 0.713 | 0.602 |
| **tax_progressive** | 0.83 | 1.73 | 2.90 | 10.60 | **12.03** | 1.47 | **0.585** | **0.628** |
| estate_treasury | 0.80 | 1.17 | 2.00 | 11.00 | 13.93 | 2.36 | 0.688 | 0.574 |
| estate_heir | 0.80 | 1.40 | 3.63 | 9.77 | 13.70 | 2.35 | 0.692 | 0.574 |
| share_wealth | 1.57 | 5.17 | 7.03 | 11.10 | 13.17 | 1.70 | 0.614 | 0.610 |
| share_equal | 1.53 | 4.80 | 7.00 | 11.63 | 13.50 | 1.48 | 0.624 | 0.594 |
| **margin_00** | 0.13 | 4.63 | 13.90 | 15.37 | **15.63** | 1.83 | 0.641 | 0.534 |

Seven of 28 pairwise comparisons survive Bonferroni (α=0.00179):

| comparison | diff | p |
|---|---|---|
| tax_progressive vs margin_00 | −3.60 | <1e-6 |
| share_wealth vs margin_00 | −2.47 | 1e-6 |
| share_equal vs margin_00 | −2.13 | 7e-6 |
| tax_none vs tax_progressive | +2.07 | 0.000079 |
| tax_progressive vs share_equal | −1.47 | 0.000300 |
| tax_progressive vs estate_treasury | −1.90 | 0.000489 |
| estate_heir vs margin_00 | −1.93 | 0.000788 |

### Three things changed, one did not

**1. The margin is the largest single lever in the matrix.** `margin_00` is
the worst arm at 15.63, worse than every arm run at 0.20, and four of those
comparisons survive correction. Nothing on the redistribution side moves the
number as far as simply letting firms hold their capital. That is the
strongest available justification for the default change.

**2. Progressive tax stops being a wash and becomes the best arm.** At margin
0 the tax arms were indistinguishable at t400 (15.53 / 15.30 / 14.94 at
n=100). Here `tax_progressive` reaches 12.03 and beats `tax_none`,
`estate_treasury`, `share_equal` and `margin_00` under correction — the only
arm with a residual effect at t400 rather than a delay that decays. It also
has the **lowest** sd (1.47), the **lowest** gini (0.585) and the **highest**
hunger satisfaction (0.628) of any arm.

**3. That dissolves an old open question.** "Understand why progressive tax
ends more unequal than flat tax while feeding people better" — it no longer
does. With a margin it ends *most equal* (0.585 against flat tax's 0.713)
*and* feeds people best. The puzzle was an artifact of the zero-margin world.

**4. Capital ownership is still null, and now that means something.** The
prediction on making the change was that the share arms were "genuinely
re-opened" because dividends would now flow during the window that decides
who lives. **The prediction was wrong.** The channel did open — dividends
sampled are 398 (`share_wealth`) and 311 (`share_equal`) against 0 for every
non-share arm, where at margin 0 nothing paid out before t300 — but
`share_wealth` vs `share_equal` is 13.17 vs 13.50, p=0.42. Who owns the firms
still does not change who survives, and this is now a real negative result
with the mechanism live rather than an untested one with the mechanism
switched off.

### The delay pattern survives the margin

`tax_flat` at t150 is 1.07 against `tax_none`'s 5.10, and 13.43 against 14.10
by t400. `estate_treasury`: 1.17 → 13.93. The mid-run rescue that decays to
nothing is exactly the shape recorded in "Redistribution delays deaths", and
a margin does not change it. Only `tax_progressive` breaks the pattern.

Note also that `margin_00` has the *fewest* early deaths (0.13 at t100 against
0.8–1.6 for the margin arms) and the most by t200 — the same "buys delay at
the cost of an earlier first death" trade documented above, seen from the
other side.

## What "incapacitated" actually measures — read this before any mortality result

Prompted by a plain question about the dashboard: people show 100% hunger
satisfaction and a rising cash balance in the sample before they die. How?

Measured at **tick resolution** (`metrics_every=1`, seed 0, margin 0, 150
ticks) rather than the usual 5, because 5-tick sampling aliases the answer
away — it reported 9 fed↔unfed flips per person where the truth is 59.

**Every single hungry spell in the run is exactly one tick long.** 532 spells
across the population, none of length 2, mean length 1.00. One person's
trace, ticks 60–119 (`.` fed, `H` hungry, `x` dead):

```
...H.H......H.H.H.H.H....H...H.H...H...H...H......H...H...xx
```

So nobody in this economy starves. What kills them is `COND-WEAK`:
`+1` per hungry tick, **no `decay_per_tick` at all**, `incapacitates_at=30`.
Thirty *non-consecutive* missed meals, spread over 150 ticks, with full
recovery in between and no recovery in the counter. 13.4% of person-ticks are
hungry; the median person banks exactly 30 and dies.

Three things rule out the economic explanations:

- **There was no shortage.** Food output ran 39–48/tick against a subsistence
  need of 24/tick — a 60–100% surplus — straight through the die-off.
- **They were not broke.** Cash *rises* monotonically to the death tick
  (376→460, 450→538). The zero that follows is the burn-estate rule.
- **They were not outbid.** A hungry household bids
  `normal_food_price × (1 + 20 × urgency)`, and `normal_food_price` scales
  with its own balance — ~105 against a market price of ~3. It wins whenever
  it bids, which is exactly why the tick after every dip is back at 100%.

The actual mechanism is the pantry, and it has a hard threshold. Tier-1
buying is `0.8 × urgency`, so a fed household buys **nothing**; restocking
bids `0.6 × normal_food_price`; FOOD decays 0.3/tick. That lowball clears
only above ~470 balance:

| balance | restock bid | vs market 3.08 |
|---|---|---|
| 250 | 1.63 | fails |
| ~470 | ~3.08 | the line |
| 1000 | 6.52 | clears |

Above it you hold a buffer and never dip; below it you never build one and
ride a period-2 oscillation until the counter fills. The people dying held
376–538 — straddling exactly that line.

**What this does and does not invalidate.** Every arm shares this mechanism,
so the *comparisons* still compare something real: an arm that reduces deaths
is genuinely reducing how often households fall below the pantry threshold.
What is not supported is the natural reading of the word — "incapacitated"
here means *missed roughly one meal in seven, thirty times, while solvent and
surrounded by surplus food*, not "starved". Any claim of the form
"redistribution saved lives" should be stated as "redistribution kept
households above the restock threshold for longer".

**The fix is a condition that recovers.** `COND-WEAK` with a small
`decay_per_tick` would make it a hunger *stock* that heals — the thing it was
described as — rather than an unforgiving lifetime counter.

### Applied: `decay_per_tick = 0.02`, and the mortality result disappears entirely

Rate picked off the contract in `conditions.py` (proportional decay against a
constant grant converges to grant/decay). Grant is +1 per hungry tick, so an
entity hungry a fraction `f` of the time settles at `f/d` and dies iff
`f > 30d`. At d=0.02: a true famine (f=1) settles at 50 and kills in ~46
ticks; you die if hungry more than 60% of the time; the measured f=0.134
settles at 6.7.

Measured, 6 seeds, 400 ticks, current defaults:

| | before (no decay) | after (d=0.02) |
|---|---|---|
| deaths at t400 | 14.10 mean | **0, every seed** |
| peak COND-WEAK | 30 (the threshold) | 9.7–19.7, then falls |
| mean hunger satisfaction | 0.53–0.63 | **0.70–1.00** |
| final gini | 0.585–0.713 | **0.428–0.507** |

Mean COND-WEAK settles at 6.71 against the 6.7 the arithmetic predicted —
the model behaves exactly as the contract says it should once the contract is
honoured.

**So the honest summary of this whole experiment to date: the mortality it
measured was an artifact.** An economy carrying a 60–100% food surplus does
not kill anybody once missing a meal is survivable, which is the right
answer; the old runs killed half the population because the damage counter
had no way down. Every "deaths" number above this section is a measurement of
that artifact, including the margin result and the arm matrix.

**The outcome variable has to change.** Deaths are now degenerate (always 0),
so the arm comparisons need a measure with variance in it. The obvious
candidates, all already collected: mean hunger satisfaction, the COND-WEAK
burden itself (a continuous measure of chronic deprivation — which is also
what "separate how much deprivation from how concentrated" in Not-yet-done
was asking for), gini, and mobility.

### Engine fix that this depended on

Proportional decay stalls: `lost = quantize(quantity × rate)` rounds to zero
once `quantity × rate < 0.00005`, so a holding sticks at ~0.0024 forever.
Harmless dust for a commodity — but `held_modifiers` applies a condition's
modifier at **any** positive quantity, so a recovered entity would have
carried the 0.7 labour throttle permanently and "recovery" would not have
recovered. `apply_decay` now sweeps the remainder when decay has stalled.

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

**"Late" was an artifact of `firm_margin` 0**, where a firm could only clear
its reserve by outliving its competitors. At the current 0.20 default firms
earn a margin from the start, so dividends flow during the period that
decides who lives. Re-measured in "The arm matrix at margin 0.20" below.

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

- ~~Establish whether the matrix numbers are steady states or points on a
  slope.~~ **Done**: they were points on a slope, and the slope was the
  whole result. See "Redistribution delays deaths" above.
- Work out why seed 7 collapses under redistribution and not without it.
  Reproducible, so the tick-by-tick mechanism is directly inspectable.
- Separate "how much deprivation" from "how concentrated" properly — they are
  near-collinear at this carrier count, so it needs conditions that move the
  number of people affected independently of the total.
- Population and field-count sensitivity (seeds are now swept; these are
  not). The calibration sweep in particular was a single seed.
- Understand why progressive tax ends more unequal than flat tax while
  feeding people better.
- ~~Decide whether firms should carry a profit margin.~~ **Done**: built,
  swept at n=30, and it delays the die-off without preventing it — the same
  result redistribution gave. See "Firms with a margin" above. It did not
  shape either finding it was expected to: the baseline still degrades, and
  the residual mortality effect at t400 is 1.4 deaths.
- Pick a margin for the default scenario, now that one *can* be picked on
  evidence. Left at 0 deliberately: every result in this file was measured
  there, and re-baselining the whole experiment is a separate decision from
  establishing what the margin does. m=0.20 is the natural candidate — it is
  the only arm that holds the sector solvent through tick 150.
- ~~Give `COND-WEAK` a `decay_per_tick`.~~ **Done** (0.02), and it removed
  the mortality result outright — 0 deaths in 6/6 seeds at 400 ticks. See
  "Applied" above.
- **Re-run the arm matrix on a non-degenerate outcome.** Deaths are now always
  zero, so the eight arms need comparing on hunger satisfaction, COND-WEAK
  burden, gini and mobility instead. Everything above this line that reports
  deaths is superseded; do the performance work first, since the matrix is
  ~4h at current throughput.
- Land beats cash once the firm sector stops growing — **mechanism traced,
  needs a seed sweep**. Mean net worth of the landed against the landless
  goes 0.76 → 0.96 → **1.82** → 5.41 across ticks 50→350, and the crossover
  tracks the bankruptcies (it moves to ~300 in the margin arm). The mechanism
  is *not* rent extraction: food price **falls** over the divergence (4.33 at
  t200 → 1.10 at t400), and the gap is almost entirely cash, not goods or
  asset value. It is the wage market dying. `P_LABOR` peaks at 15.39 (t100)
  and collapses 84% to 2.37 as solvent firms go 5 → 3 → 2 → 1, because firms
  are the only buyers of labour in this economy. From t175 the landless lose
  cash **every single period** (−147, −228, −173, −126, −92, −67, −46, −40)
  while the landed gain in every period without exception: a smallholder's
  income comes from a field and does not require any firm to exist, so when
  wages go, owning the productive asset is the only way to have an income at
  all. Cash is a stock that depletes; land is a flow that continues. (The
  late uptick in landless *mean* cash is survivorship — the median keeps
  falling, 242 → 169, as the poorest are removed by dying.) Only 3–4 landed
  people in one seed, so the direction is worth chasing and the magnitude is
  not yet a number.
- Find what actually stops the circular flow running one way, since the
  margin only slows it. Candidates: firms that hold inventory instead of
  conceding below cost, FOOD decay (0.3/tick is punishing for a good the
  economy is short of), or households that spend down rather than accumulate
  cash they cannot convert into food.
- ~~Run the capital-ownership comparison properly.~~ **Done at n=30** in the
  margin-0.20 matrix above: `share_wealth` 13.17 vs `share_equal` 13.50,
  p=0.42, with dividends actually flowing. Null, and now a meaningful null.
  n=100 would tighten it but the direction is not there to find.
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

# ...the same run in the pre-margin regime every section above
# "Firms with a margin" was measured in
.venv/bin/python -m experiments.inequality.run --individuals 30 --ticks 200 \
  --firm-margin 0 --out /path/to/result.json

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
