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
  numpy/pandas in this repo's `.venv`), on two wealth measures: `net_worth`
  (cash + priced goods) and `total_wealth`, which also values land and shares.
- `run.py` — single-scenario CLI + `run_scenario()` for programmatic use.
- `matrix.py` — runs the 5-variant sweep (tax none/flat/progressive ×
  estate burn/treasury/heir) and writes one JSON per variant.
- `horizon.py` — how long a run has to be. Scores the whole arm comparison at
  a cheap tick against a long one, measured inside the same runs.

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

## How long must a run be? 250 keeps the ranking, not the magnitude

The re-run matrix is ~4h at 400 ticks and ~2.5h at 250, and the saving is only
worth taking if the *comparison* survives the shorter horizon. "Have the
metrics settled by 250" is the wrong test for that: a metric can still be
moving at 250 while every arm moves together (gap already decided), and a
metric can look flat while two arms are mid-crossing. What has to hold is that
the conclusion drawn at t250 is the conclusion drawn at t400.

`horizon.py` runs the arms to 400 with every outcome recorded at both ticks
**inside the same run**, so run-to-run luck is removed from the comparison
entirely — seed 3's t250 and seed 3's t400 are one economy at two moments.
It then runs the identical pairwise Welch analysis twice and asks whether the
verdicts match. 4 arms x 12 seeds x 400 ticks, `metrics_every=10`, 50m01s
(`results/horizon_n30_t400.json`). Arms chosen to span the effect range:
`tax_none` (baseline), `tax_progressive` (the only arm with a residual effect
at t400 on the old outcome), `estate_treasury` (a pure delay-pattern arm),
`margin_00` (the largest lever, and the only one that moves *production*).

| outcome | pairs agreeing | sign flips | arm-ordering rank r |
|---|---|---|---|
| gini | **6/6** | 0 | **+1.00** |
| mobility | **6/6** | 0 | +0.80 |
| COND-WEAK burden | 5/6 | 0 | **+1.00** |
| COND-WEAK carriers | 5/6 | 0 | +0.89 |
| mean hunger (point sample) | 4/6 | 0 | +0.80 |
| deaths | — | — | 0.000 in all four arms at every tick |

**Zero sign flips in 30 comparisons.** No pair of arms crosses over between
250 and 400, on any outcome. Arm means at the two horizons:

| arm | hunger | COND-WEAK | carriers | gini | mobility |
|---|---|---|---|---|---|
| tax_none | 0.801 → 0.803 | 230 → 289 | 17.5 → 23.3 | 0.284 → 0.456 | 0.643 → 0.291 |
| tax_progressive | 0.930 → 0.869 | 186 → 242 | 24.6 → 26.1 | 0.020 → 0.018 | −0.006 → −0.074 |
| estate_treasury | 0.843 → 0.860 | 161 → 230 | 24.6 → 25.9 | 0.031 → 0.030 | −0.011 → −0.025 |
| margin_00 | 0.809 → 0.753 | 306 → 339 | 20.3 → 23.3 | 0.372 → 0.554 | 0.482 → 0.246 |

**The magnitudes do not survive.** diff@250 / diff@400 per pair runs from 0.32
to 2.74, and the bias has a *direction per outcome* rather than being noise:
t250 overstates carrier gaps (x2.58, x2.74, x1.68) and mobility gaps (x1.78,
x2.07, x1.82, x1.52), and understates every gini gap (x0.59, x0.60, x0.65,
x0.66, x0.90, x0.91). A 250-tick matrix would rank the arms correctly and then
misreport how much any of it matters, in opposite directions depending on
which row you read. That is a milder version of the same trap the old deaths
table set — 6.70 at t200 was really 1.43 at t400.

Four pairs disagree. Two are p-values straddling the Bonferroni line
(a=0.00833) rather than genuine changes of mind — `tax_none` vs
`tax_progressive` on hunger (0.0027 → 0.0168) and `tax_progressive` vs
`margin_00` on carriers (0.0089 → 0.0000). Two are substantive:

| pair | outcome | diff@250 | p | diff@400 | p |
|---|---|---|---|---|---|
| estate_treasury vs margin_00 | hunger | +0.035 | 0.329 | +0.107 | 0.0012 |
| tax_none vs margin_00 | COND-WEAK | −76.0 | 0.0054 | −50.2 | 0.124 |

Both involve `margin_00`, the arm still moving fastest at 250 (gini 0.372 →
0.554, burden 306 → 339 over that stretch). **So: 250 is safe for "which arms
differ and in which direction", and specifically unsafe for `margin_00`.**

Two caveats on the test itself. n=12, so the significance calls are themselves
noisy, and "the horizons disagree" is partly conflated with "n=12 straddles
the threshold" — which is exactly what the two borderline pairs are.
And **neither horizon is a steady state**: `tax_none` gini goes 0.286 → 0.326
→ 0.397 → 0.453 → 0.507 across t200–t400 with no sign of levelling. The
ordering is stable; the economy is not.

### The new outcomes separate the arms far harder than deaths ever did

This is the bigger cost lever. At n=12, most gini and mobility pairs are
already at p<0.0001 — the arms are separated by 0.018 against 0.554 on gini,
and −0.074 against 0.291 on mobility. The old matrix needed 30 seeds because
deaths had sd 1.5–3.2 around means 12–16. If the re-run is judged on gini or
mobility, n=12–15 may be enough, which saves more than the tick reduction
does. Size it on the smallest gap worth detecting, not the largest one here.

### The carriers/burden split is now separable

`tax_none` has *fewer* people carrying `COND-WEAK` (23.3) but a *higher* total
burden (289) than `tax_progressive` (26.1 carriers, 242 burden). Redistribution
spreads deprivation across more people while reducing its total. That is the
"separate how much deprivation from how concentrated" item below — and unlike
the twelve-replicate version, where the two were collinear at r=+0.90, they
now move in **opposite directions across arms**, so they are separable
without needing new conditions.

## Two things found while checking the horizon

### Point-sampled hunger is too noisy to be an outcome; the stocks are fine

`metrics_every=10` samples only *even* ticks, which given the period-2 hunger
oscillation looked like a worse version of the `metrics_every=5` aliasing that
hid the mortality artifact for this project's whole life. Measured at
`metrics_every=1`, seed 0, 400 ticks — **the parity worry was wrong, and a
different problem is real.**

| outcome | all ticks | even only | odd only | even − odd |
|---|---|---|---|---|
| hunger | 0.8456 | 0.8377 | 0.8534 | −0.0157 |
| COND-WEAK burden | 195.01 | 195.45 | 194.57 | +0.88 |
| carriers | 16.09 | 16.10 | 16.08 | +0.02 |
| gini | 0.3859 | 0.3855 | 0.3862 | −0.0007 |

No phase lock. But the horizon comparison reads a *single tick*, not an
average, and one tick of hunger is very noisy — against its two odd
neighbours it is +0.053 at t250 and +0.086 at t400, while the stocks move
under 2%:

| | at the tick | odd neighbours | gap |
|---|---|---|---|
| hunger @ t250 | 0.8345 | 0.7811 | **+0.053** |
| hunger @ t400 | 0.8392 | 0.7528 | **+0.086** |
| COND-WEAK @ t250 | 277.67 | 278.15 | −0.48 (0.2%) |
| gini @ t250 | 0.3264 | 0.3216 | +0.005 (1.5%) |
| carriers @ t250 | 16.00 | 16.00 | 0.00 |

The entire spread across all eight arms of the last matrix was 0.534 to 0.628
— 0.094. So one tick of measurement noise is about as large as the whole
effect. Hunger is a fast square wave (63.8 fed↔unfed flips per person over
400 ticks, down from the pre-decay 59-in-150) and an instant of it carries
almost no information. `summarise()` now also records `hunger_win_at_*`, a
mean over ±50 ticks. The stocks need no such treatment because they integrate
history rather than sampling it. **The 4/6 hunger agreement above is measured
on the point version and should be redone on the windowed one.**

### The circular flow reverses at ~tick 200, and redistribution accelerates it

Chasing an implausible number: `tax_progressive` reports gini 0.018–0.025,
near-perfect equality, and `gini()` returns 0.0 for an all-zero population, so
that reading is ambiguous between "equal" and "broke". It is genuinely equal —
seed 0 at t400, everyone in a band around 320, and the *median* person is 22%
better off than under no tax (323 against 265). But total household net worth
is **21,271 under no tax against 9,725 under progressive tax**, with zero
deaths, and NOTES says money is conserved to the cent absent a burned estate.

It is conserved — 28,154 at every tick in both arms. The money is in the firms:

| tick | no tax: household / firms / solvent | progressive: household / firms / solvent |
|---|---|---|
| 0 | 13,154 / 15,000 / 5 | 13,154 / 15,000 / 5 |
| 100 | 18,436 / 9,717 / 5 | 19,482 / 7,240 / 5 |
| 200 | 25,633 / 2,521 / 4 | 24,134 / 2,584 / **2** |
| 250 | 24,135 / 4,019 / 3 | 20,321 / 6,678 / 2 |
| 400 | 21,169 / 6,985 / 3 | **9,604 / 18,181** / 2 |

**The battery discharges until ~t200 and then recharges from the households.**
The late-run rise in firm cash was already recorded above as monopsony rents
after consolidation; what is new is that it is not a mild uptick but a
reversal of the whole circular flow, and that **redistribution roughly triples
it** (+15,597 against +4,464 from the t200 trough). The mechanism is velocity:
redistribution keeps poor households spending on food, and with a 0.20 margin
on every sale, a faster circular flow drains the household sector into a
two-firm oligopoly faster. Doing nothing leaves the money hoarded by a rich
household that still only eats 0.8/tick — 2,814 sitting idle at t400.

This also puts a health warning on gini as the matrix's headline. **Net worth
excludes land**: `_holdings_value()` prices only FOOD, CLOTHES, TOOLS, LABOR
and LABOR-FARM, and parcels are not holdings, so a smallholder's field counts
zero. "Progressive tax achieves gini 0.018" means *cash and goods* are
near-equal while 4 of 30 people still own all the productive land — measured
by an instrument that structurally cannot see the asset the "land beats cash"
item below says ends up deciding everything. **Fixed below.**

## Land and shares now count as wealth, and it moves two results

Neither asset has a market price — no script trades parcels or shares, so every
`SHARE-FIRM-n` market sits at `last_price` None and parcels have no market at
all. Leaving them out of wealth was never neutral, so they are now valued
(`metrics.field_value`, `metrics._share_unit_values`):

- **Land: capitalised Ricardian rent.** `rent = 4.95 x P_FOOD - 1 x P_LABOR`,
  the residual accruing to the field rather than to whoever works it, floored
  at zero and divided by a discount rate. It is the same quantity whoever
  holds the field: a firm collects it as its margin (exactly `m x yield x P`
  by construction), a smallholder collects it by not paying themselves a wage.
- **Shares: book value.** Firm net assets (cash + priced goods + its own land)
  over shares outstanding. Deliberately not earnings-based — firm earnings
  swing from nothing to monopsony rents inside one run, and any multiple on
  them would amplify that swing rather than measure it.

`LAND_DISCOUNT_PER_TICK = 0.01`, i.e. a field is worth 100 ticks of net rent
(~1,025 at P_FOOD 3.08 / P_LABOR 5.00). This is a modelling choice, not a
measurement: capitalising a perpetuity at any realistic annual rate is
meaningless in a world that ends at tick 400, and 100 ticks is a quarter of a
standard run. It is exposed as a constant so results can be checked against it.

**`net_worth` is unchanged** — still cash + priced goods — because every gini
and mobility number recorded above is measured on it, and redefining it in
place would silently invalidate all of them. The new measure is
`total_wealth`, with `gini_total`, `mobility_total`, `top10_share_total` and a
`wealth_components` breakdown alongside.

4 seeds, 400 ticks (t400 means):

| arm | gini | gini_total | mobility | mobility_total | land % of wealth |
|---|---|---|---|---|---|
| tax_none | 0.477 | 0.514 | 0.324 | 0.257 | 9.5% |
| tax_progressive | **0.018** | **0.146** | −0.001 | −0.049 | 15.1% |
| share_wealth | 0.483 | 0.492 | 0.324 | **0.567** | 6.9% |

**1. `tax_progressive`'s near-perfect equality was largely the blind spot.**
gini 0.018 → gini_total 0.146, eight times higher. It is still the most equal
arm by a wide margin, but "near-perfect equality" and "meaningfully unequal"
are different claims and only the second one survives seeing the land.

**2. Concentrated share ownership entrenches position, and the old instrument
could not see it.** `share_wealth` mobility goes 0.324 → **0.567**: including
shares makes the hierarchy *more* rigid, not less. Mechanically that has to
happen — shares were allocated by starting wealth and no script trades them,
so the register is a frozen record of everyone's genesis position. But it
bears on the standing null: "who owns the firms does not change who survives"
was tested on deaths and is probably still true there, while the *inequality
and mobility* half of the ownership question was never actually measured,
because the measure omitted the shares. Worth re-running the ownership pair on
`gini_total` / `mobility_total`.

### How much of this is the discount rate?

All of the magnitude and none of the ranking. Seed 0, t400, recomputed off one
snapshot since land value scales as 1/r:

| rate | ticks of rent | field $ (none / prog) | tax_none gini_total | tax_progressive gini_total |
|---|---|---|---|---|
| 0.050 | 20 | 102 / 144 | 0.514 | **0.072** |
| 0.020 | 50 | 254 / 360 | 0.523 | 0.134 |
| **0.010** | **100** | **508 / 719** | **0.538** | **0.217** |
| 0.005 | 200 | 1,016 / 1,438 | 0.565 | 0.338 |
| 0.002 | 500 | 2,540 / 3,595 | 0.623 | **0.527** |

`tax_progressive` is the more equal arm at every rate, so that conclusion is
not an artifact of the choice. The *size* of the advantage is: a 7x gap at 20
ticks of rent, 1.2x at 500. **State the ranking, not the multiple.**

Note also that land is a larger share of wealth under progressive tax (22.8%
against 8.7%) partly because `P_LABOR` has collapsed further there — 0.52
against 3.44. Lower wages mechanically raise Ricardian rent, so this land
valuation is coupled to the wage collapse documented in "land beats cash"
rather than independent of it. That is arguably correct — it is what land being
the residual claimant *means* — but it does mean the measure moves most in
exactly the arms where the labour market has broken down.

Two limits to keep in view. Under `share_allocation="none"` no shares exist,
so firm net assets belong to nobody and enter no one's wealth — that is the
model being honest (those firms genuinely have no owners), but it means total
measured household wealth is **not comparable between share and non-share
arms**. Compare within, not across. And `mobility_total` for a share arm is
partly measuring an untraded allocation; it will mean something different once
shares can move.

## The matrix re-run at 250 ticks — and three arms that are one arm

8 arms x 15 seeds x 250 ticks, `metrics_every=10`, 1h15m
(`results/matrix_m20_n30_t250_s15.json`). Dashboard:
https://claude.ai/code/artifact/c4c1ab25-2a0b-475c-8917-e21d7f8c9904

**Deaths: 0 in all 120 runs**, as expected since the COND-WEAK decay fix.

### The estate arms stopped being policies

An estate rule is a rule about what happens when someone dies. Nothing dies
here, so it never executes, and the three arms that differ only in their
estate rule are not three policies:

- **`tax_flat` and `estate_treasury` are bit-identical** — every seed, every
  recorded key, the same values. Verified cell-by-cell, not inferred from the
  means. `dashboard_data._identical_groups` detects this from the data rather
  than asserting it, so the claim cannot outlive the condition that produced it.
- **`estate_heir` is the same policy on a different genesis draw.**
  `_assign_heirs` calls `rng.shuffle` (scenario.py:203) *before*
  `_wire_scripts` draws from the same generator, so the heir arm plays
  identical rules against a differently-wired set of firms.

### Which makes it a free null replicate, and that is the useful part

Same policy, different luck, 15 seeds. Nothing separates the two, and the gap
they open is the floor below which no other arm difference on this matrix
means anything:

| measure | tax_flat | estate_heir | gap by luck | p |
|---|---|---|---|---|
| hunger (windowed) | 0.866 | 0.879 | **0.013** | 0.34 |
| hunger (1 tick) | 0.857 | 0.897 | 0.040 | 0.18 |
| COND-WEAK burden | 166.4 | 180.3 | **13.9** | 0.47 |
| carriers | 24.60 | 25.60 | 1.00 | 0.14 |
| gini (cash) | 0.030 | 0.031 | 0.001 | 0.79 |
| gini (all wealth) | 0.131 | 0.122 | **0.010** | 0.37 |
| mobility (all wealth) | −0.065 | −0.068 | 0.003 | 0.95 |

Note the point-sampled hunger opens **three times** the luck gap of the
windowed version (0.040 against 0.013) — an independent confirmation, from a
direction not designed to test it, that reading hunger at one tick is mostly
reading noise.

**Apply it as a second gate.** A difference has to survive the Bonferroni
correction *and* exceed this floor. It immediately caught two claims that
would otherwise have gone out: `estate_heir` "wins" gini_total at 0.1216
against `tax_progressive`'s 0.1227 — a 0.0011 margin against a 0.0096 floor,
and from a duplicate policy at that. The honest statement is that **flat and
progressive tax are indistinguishable on total-wealth gini**; what separates
is tax against no tax.

### Results at t250

| arm | hunger (win) | burden | carriers | gini | gini (all) | mobility (all) |
|---|---|---|---|---|---|---|
| tax_progressive | **0.914** | 185.2 | 24.8 | **0.018** | 0.123 | −0.061 |
| estate_heir | 0.879 | 180.3 | 25.6 | 0.031 | 0.122 | −0.068 |
| share_equal | 0.872 | 217.5 | 17.1 | 0.301 | 0.293 | 0.372 |
| tax_flat / estate_treasury | 0.866 | **166.4** | 24.6 | 0.030 | 0.131 | −0.065 |
| share_wealth | 0.866 | 224.8 | 17.6 | 0.299 | 0.395 | 0.556 |
| tax_none | 0.843 | 232.6 | 18.1 | 0.290 | 0.356 | 0.331 |
| margin_00 | **0.789** | **296.1** | 20.3 | 0.377 | 0.441 | 0.294 |

Standing reads, all clear of the luck floor: **`margin_00` is the worst arm on
every measure**, which reproduces "the margin is the largest single lever" on
a non-degenerate outcome. `tax_progressive` genuinely leads on hunger. The
carriers/burden inversion holds — the tax arms put *more* people on the
COND-WEAK register (24–26 of 30) while carrying a *lower* total burden than
`tax_none` (18 carriers, 233 burden), which is deprivation spread thin rather
than concentrated.

### Caveats specific to this run

- The 28 comparisons are not 28 independent questions: two arms are identical
  and a third is the same policy, and the duplicate pair contributes a
  guaranteed p = 1.0. That makes the correction conservative, not generous.
- **Any arm that changes what genesis creates may also shift the random
  stream**, so part of its gap against another arm is a different draw rather
  than the policy. `estate_heir` is the measured case; the share arms allocate
  holdings at genesis and have not been checked for the same effect. Worth
  ruling out before the share comparison is quoted again.

## Rent and bills: housing and energy sectors, both competing for land

Built, runs, **not yet calibrated** — read the calibration section before
using any number out of it.

### It needed no engine changes at all

Every piece was already there, which is worth recording because it was not
obvious going in:

- `Parcel` carries a zoning tag and `Facility` is a built improvement on one,
  so a dwelling and a power plant are the same primitive as a farm.
- **Facility capacity is already a reservation rule** — one facility backs one
  running process per tick — which is exactly the semantics housing wants:
  one dwelling houses a fixed number of households and the only way to house
  more is to build more.
- `Deposit` regenerates toward a capacity, so an energy plot has a `FUEL-SEAM`
  the way a field has `SOIL-FERTILITY`. Land has *quality*, not just a permit.
- `builds_facility` erects on the bound parcel at completion, `ctx.parcels`
  reports each parcel's facilities and deposits, and `start_process(recipe,
  parcel_id)` is exposed to Lua. That is the whole build-and-convert mechanic.

**Rent dodges the ownership invariant the same way taxation does.** A landlord
cannot reach into a tenant's account any more than a treasury can. So rent is
not taken, it is *bought*: `SHELTER` and `ENERGY` decay **completely** every
tick, consumption runs after the auction and before decay, and a household
buys exactly this tick's occupancy and this tick's power or does without.
Next tick the bill falls due again regardless. Eviction is the absence of a
purchase — no forced transfer, no seizure, no new primitive. That same total
decay is what makes them bills rather than shopping: there is no pantry, so
nobody can stockpile a year of rent in a good week.

### Land is now genuinely rival

One fixed pool. Every parcel carries *both* deposits whatever gets built on
it, because endowing only the farming parcels with soil would decide the
allocation at genesis under another name — and the allocation is meant to be
the run's output. Three `BUILD_` recipes are mutually exclusive uses of the
same acre: a dwelling is a field that is not growing food. A firm reads which
of its parcels are bare, prices the three uses off their own output net of the
labour to work them, and puts up whichever pays. **One use per parcel is
enforced in `firm.lua`, not by the engine** — nothing stops a farm and a
dwelling sharing an acre. That is the right split (zoning is policy, not
mechanism) but the invariant is only as good as the script.

Yields are set so one parcel serves ~6 people whichever way it is used (a
field feeds 4.95/0.8 = 6.2), so "which use pays best" is decided by prices
rather than by an accident of units.

Genesis: 5 firms x (1 farm + 1 dwelling + 1 plant + 2 bare) + 4 smallholder
farms = 29 parcels, of which 9 farms — the calibrated food number, preserved.

### Two bootstrap deadlocks, both measured

Same family as the `SKILL-FARM` deadlock (bug 3) and worth the same warning:

1. **Nothing was ever built.** `BUILD_DWELLING` wants 4 LABOR held at once and
   the firm only ever bid for one or two, so the intent failed silently every
   tick for a whole run. Every parcel stayed bare, the population failed
   shelter and power for 120 ticks, and because `COND-EXPOSED` and `COND-COLD`
   both cut labour productivity on top of `COND-WEAK`, the *food* economy went
   with them: food price 3 → 111, hunger satisfaction 0.03. Fixed by bidding
   for build labour as its own tranche and only starting the build once the
   hours are in hand.
2. **Housing stock has to exist at genesis.** Even with the labour, no firm
   can assemble a build before the whole population has failed both new needs
   for the entire bootstrap. Real economies start with a housing stock; this
   one has to as well. The build mechanic then operates at the *margin*, which
   is where a build-or-not decision is interesting anyway.

And one ordering bug that is pure engine-semantics: **intent priority decides
who gets the firm's labour**, and generation was last. `GENERATE_POWER` failed
on all five plants nearly every tick for sixty ticks — energy output flat zero
against a standing demand of 30 — while clothes were made on schedule. An hour
generating is worth six units of energy; an hour on clothes is worth one and a
half units of clothes. The bid schedule already said so; the intent priorities
did not. Letting and generation now sit at 21/22, ahead of tools and clothes.

### The two conditions bite on different margins

Not one effect at two strengths. What makes that possible is *where* the
engine reads a condition's modifier: exactly two sites — the auto-issue top-up
target (`goods.py`) and a recipe's inputs and `good_requirements`
(`production.py`). So a condition can throttle what you are **issued** and what
you are **able to do**, and cannot reach consumption, orders or cash. Both new
consequences live inside that.

| | pattern | factor | incapacitates | what it means |
|---|---|---|---|---|
| `COND-COLD` (no heating) | `LABOR` | 0.80 | never | fewer hours to sell; recoverable the moment the bill is paid |
| `COND-EXPOSED` (rough sleeping) | `*` | 0.70 | at 40 | cuts labour **and** every recipe input and requirement |

The `*` on rough sleeping is the design, not laziness. It scales every symbol
at both read sites, so it hits `SKILL-FARM` — which `WORK_AS_FARMER` gates on
at >= 1 — as well as labour. **A smallholder sleeping rough stops being able to
work their own land.** Losing your home is not a slice off your wage, it locks
you out of skilled and self-provisioning work, and that is the difference
between a bad month and a trap: less labour, less income, still cannot pay
rent.

Rough sleeping is also the one new route to incapacity, and the arithmetic is
the point. Grant 1 x (1 - satisfaction) per tick against decay 0.02 settles at
`50f` for an unhoused fraction `f`, so a threshold of 40 fires only if
`f > 0.8` — ~80 ticks of near-continuous destitution. Housed half the time
settles at 25 and never dies. That is precisely the property COND-WEAK lacked,
and at current shelter satisfaction (~0.5) it fires for nobody: a tail, on
purpose, not a default.

**Measured cost of adding them**: the economy gets markedly harsher, because
the three conditions compound multiplicatively — 0.7 x 0.8 x 0.7 = 0.39 of
normal labour for someone failing all three needs. Food price spiked to 157
and the wage to 406 before settling, and no firm built anything in 120 ticks
(bare land stayed at 10). That is a real consequence and probably the right
*direction*, but it makes calibration more urgent, not less: the sweep below
now has to cover these factors as well as the budget shares.

### Calibration: NOT done, and the numbers show it

120 ticks, seed 0, after all three fixes:

| tick | farms | dwellings | plants | bare | P_FOOD | P_SHELTER | P_ENERGY | hunger | shelter | power |
|---|---|---|---|---|---|---|---|---|---|---|
| 15 | 9 | 5 | 5 | 10 | 34.7 | 15.9 | 13.0 | 0.29 | 0.50 | 0.00 |
| 45 | 10 | 5 | 5 | 9 | 14.5 | 3.0 | 7.5 | 0.53 | 0.80 | 0.60 |
| 90 | 10 | 5 | 5 | 9 | 9.4 | 3.6 | 8.9 | 0.53 | 0.60 | 0.40 |
| 120 | 10 | 5 | 5 | 9 | 8.9 | 11.1 | 7.0 | 0.50 | 0.50 | 0.60 |

The mechanism works — both sectors produce and sell, bills get paid, and a
firm converted bare land into a farm on its own (9 → 10 farms). **The economy
cannot afford its own basket.** All three needs sit at 0.4–0.6 satisfaction
indefinitely, where food alone used to sit at 0.85+.

The cause is the nominal anchor, and it is structural rather than a bad
number. `spend_rate = balance / PLANNING_HORIZON` caps what a household will
commit per tick at a twentieth of its savings — about 15 for a median holder
— while the basket at current prices costs 0.8xFOOD + 1xSHELTER + 1xENERGY
≈ 25. That anchor was calibrated for an economy whose only recurring purchase
was food, and it is the thing that pins the price level (bug 8), so it cannot
be casually widened. The budget shares are the other half: they were
food 0.5 / clothes 0.15 and are now food 0.5 / shelter 0.20 / energy 0.10 /
clothes 0.15, which is a guess, not a measurement — energy costs 1 labour per
6 units against shelter's 0.5 per 6, so a cost-proportional split would look
nothing like that.

### The calibration sweep: the demand side is not the problem

`calibrate.py`, 4 budget splits x 4 planning horizons x 3 seeds x 150 ticks,
17m46s (`results/calibration_bills.json`). Pass needs all four of MET (every
need >= 0.85), TIGHT (sell-side fill in 0.55-0.98), ALIVE, and prices moving —
ranking on satisfaction alone would pick the slackest economy in the grid.

**Nothing passed, and the grid barely moved:**

| | hunger | shelter | power |
|---|---|---|---|
| best config in grid | 0.47 | 0.52 | 0.35 |
| worst config in grid | 0.35 | 0.39 | 0.22 |

Across a 4x range of planning horizon (20 → 5, quadrupling what a household
will commit per tick) and four radically different budget splits, hunger moves
0.12 and power never gets above 0.35. **That is the demand side ruled out**,
and it is worth having on record rather than treating as a failed run.

### It is labour, and the arithmetic was there to be done

Counted directly: **~11.8 LABOR clears per tick for the whole economy against
~17.5 needed just to staff the facilities.** Most of them sit idle:

| tick | farms run | lets run | generation runs | failed starts |
|---|---|---|---|---|
| 30 | 3 of 10 | 3 of 5 | 1 of 5 | LET x2, GEN x4 |
| 60 | 2 of 10 | 2 of 5 | 2 of 5 | LET x3, GEN x3, BUILD x2 |
| 90 | 2 of 10 | 2 of 5 | 2 of 5 | LET x3, GEN x3, BUILD x2 |

Serving 30 people across three sectors needs ~15 facilities, since a parcel
serves ~6 whatever is on it. Staffing them costs 10x1 + 5x0.5 + 5x1 = 17.5
labour a tick. Supply is 30 people x 1 unit, cut ~30% by the stacked
conditions, so ~21. **The facilities alone want 83% of all the labour in the
economy** before clothes, tools or any building — and the market only clears
11.8 of it. No budget share can reach that, which is exactly why power sat
flat at 0.22-0.35 across all sixteen configs while its market cleared
80-90% of the little it offered.

**The lever is labour intensity, not household budgets.** Housing and power
are utilities — capital-intensive, low ongoing labour — and they were given
farm-like labour costs (0.5 and 1.0 per parcel-tick) on no evidence. At ~0.2
and ~0.3 the sector total drops to 12.5 and leaves the rest of the economy
room to function. That is both the realistic shape and the knob the
measurement points at. Done, and measured in the next section.

One thing that did work as designed: `COND-EXPOSED` incapacitation fired in
the worst runs (5.7 dead at `legacy|h12`, 1.0-1.3 in three others) and nowhere
else. That is the f > 0.8 tail doing precisely what the arithmetic said it
would — sustained near-total destitution, never an intermittent miss.

Until this is settled, nothing here is comparable to any result above, and
`bare_land_per_firm = 0` reverts the economy to farming only.

### Cutting utility labour: it fixed the two sectors it touched and not the third

`LET_DWELLING` 0.5 → 0.2 and `GENERATE_POWER` 1.0 → 0.3, same grid, same four
criteria, 16m21s (`results/calibration_labour.json`). The prediction was
specific and half of it was right.

| across the whole grid | before | after |
|---|---|---|
| hunger | 0.35–0.47 | 0.30–0.46 |
| shelter | 0.39–0.52 | **0.52–0.69** |
| power | 0.22–0.35 | **0.31–0.59** |

Utilisation moved with it — seed 0, per tick, over 60 ticks: lets 2 of 5 → 4.0
of 5, generation 2 of 5 → 3.1 of 5. **Housing and power were labour-starved
and are not any more.** Still nothing passes: the closest is `legacy|h12` at
worst-need 0.45, and hunger did not move at all.

### Hunger is not a labour-allocation problem, it is a supply cap

Labour *offered* to the market is pinned at **11.76 every single tick**, and
that number is not an accident of pricing:

    30 individuals x 0.7 (COND-WEAK) x 0.80 (COND-COLD) x 0.70 (ROUGH-SLEEPING)
      = 11.76

All 30 carry all three conditions permanently (`cond_weak_carriers` is 30 from
tick 20 onward). **The economy runs at 39% of its nominal labour and cannot
climb out**, because the conditions are caused by the shortfall the conditions
cause. Cutting recipe costs changes how far that 11.76 goes; it cannot change
the 11.76.

Which is why food never improved. Against demand of ~150/tick and a bare
subsistence requirement of 30 x 0.8 = **24 FOOD/tick, production runs ~12** —
half of subsistence, and flat across the whole grid. Farms run 2.4 of 13 per
tick (145 `FARM_FOOD_HAND` starts in 60 ticks x 4.95 expected yield = 11.9,
matching measured output almost exactly).

Two mechanisms worth having on record:

- **The freed labour was spent on capacity, not output.** Land use went from
  9–10 farms / 10 bare to **13 farms / 6 bare**. Firms responded to slack
  labour by building more farms and then leaving them idle. More capacity, the
  same food.
- **Most bought labour perishes.** `LABOR` decays 0.5/tick; ~11 units clear
  each tick against ~4 actually consumed by processes. The rest evaporates
  before it is used.
- **`food_light` is now lethal**: 15.3 and 18.3 dead of 30 at h20/h12, against
  0.3 and 1.0 before. When food output is already half of subsistence, cutting
  the food budget share is not a preference, it is a famine.

So the ordering was wrong. Labour intensity was a real bug and worth fixing —
it is what freed shelter and power — but it was never going to reach hunger.
**The binding constraint is the condition stack multiplying down labour
issuance**, and nothing on the demand side or the recipe side can reach it.

### The tick order was taxing every hire 50%, and that was the real bug

Processes resolved at step 6, the auction cleared at step 7, decay ran at step
9. So an input bought this tick could not be used until next tick and took a
full round of decay first. With `LABOR` decaying 0.5/tick that is a flat **50%
tax on hired labour** — while a smallholder's own auto-issued labour, issued at
step 2 and self-used at step 6, arrived intact. The engine was quietly paying
people to self-supply.

Fixed in `engine/econengine/tick.py` by **retrying**, not reordering: every
intent is still tried before the auction in priority order, and a
`start_process` rejected *solely* for want of inputs is retried after clearing.
`InsufficientHoldingsError` is tagged onto the event as `short_of_holdings` so
the tick discriminates by exception type rather than pattern-matching a
human-readable reason string. The held-back rejection is not recorded, so each
intent still yields exactly one event.

The first, simpler attempt — move all `start_process` after the auction — was
wrong in two ways that the scenario's own scripts happened to hide, and both
are now regression tests in `tests/test_tick_process_retry.py`:

- **Production would have lost first claim on its own entity's goods.** Orders
  do not escrow (`markets.py`: holdings are checked live at settlement), so
  whichever pass runs first wins. Deferring production lets a sell order take
  inputs out from under the same entity's process — and worse, makes intent
  *type* silently outrank *priority*, so an author who writes "priority 10: use
  it, priority 40: sell the rest" no longer gets what they asked for.
- **Duration-0 recipes would no longer reach the same tick's auction.**
  `start_process` completes them inline, so their output was sellable
  immediately; deferring pushes it past the auction and into the decay pass —
  the same bug, mirrored onto producers.

Both tests fail against the deferred version and pass against the retry.
290 tests pass, and the 285 that predate this were not modified.

Seed 0, 60 ticks:

| | before | after |
|---|---|---|
| `start_process` rejections | 231 | **74** |
| `LET_DWELLING` | 4.0 of 5 | **4.7 of 5** |
| `GENERATE_POWER` | 3.1 of 5 | **4.3 of 5** |
| `FARM_FOOD_HAND` | 2.4 (idle 145) | 2.2, **zero rejections** |
| land | 13 farms / 6 bare | 19 farms / **0 bare** |

Housing and power are now essentially fully utilised. **Food still is not**,
and the bottleneck has moved: it is no longer labour clearing (11.76 clears in
full) but `WORK_AS_FARMER` conversions, which run ~2.5/tick against 19 standing
farms. Each conversion needs a whole unit of `LABOR`, and an individual issued
0.392 can never make one — so the conversion is firm-only, and firms appear to
run about one each per tick. That is the next thing to count.

### Labour supply had no extensive margin at all

Every individual dumped their whole endowment on the market every tick,
however rich. The reservation wage already scaled with savings, so wealth
expressed itself as a *price* — but participation was unconditional, which
says people work for the love of it. Work is a disutility; you sell your
labour when you cannot otherwise pay for the week.

Added to `individual.lua`: a household stops offering wage labour once its
balance covers `work_free_cover` ticks of the basket at market prices.
Smallholders still work their own land at any wealth — living off what you own
is the point, and a field is an asset. Config-driven so it can be swept.

Two things the measurement forced:

- **The cover measure has to be smoothed.** On one tick's prices the basket
  swings 14 → 200 here, so on a crash tick the whole population reads as rich,
  withdraws together, loses its only income and starves: **15 of 30 dead by
  t150, against 0 with participation forced on.** Nobody had retired — a
  one-tick price dip was mistaken for a fortune. An EMA (0.85) fixes it.
- **Single-seed threshold comparisons are worthless here.** A sweep of the
  threshold came back non-monotone, which is impossible from the logic. Every
  `LABOR` sell_ordered movement decomposed exactly into mortality
  (9.41 = 11.76 × 24/30, i.e. 6 dead), not withdrawal; the runs had simply
  diverged chaotically, since `events_hash` seeds the harvest rolls.

**The mechanism works and currently binds on nobody.** Individual-only,
smoothed, cover peaks at 36.8 for the richest person at t30 and decays to 11.9
by t150, median 10.3 → 0.39. At a threshold of 5 two workers do withdraw at
t15 with all 30 alive, so it is live, not dead code. Nobody in this economy can
live off what they own, which is a fact about the economy and consistent with
everything else here. Default left at 40 — inert — rather than tuned down to
bind on destitution, which would be fitting to the brokenness the calibration
is trying to remove.

Not yet done: the *investment* half of the same preference. Individuals have no
way to buy shares or land at all, so surplus can only sit as cash. Until there
is a portfolio choice, "prefers passive income" is only half expressible.

## Not yet done

- Redo the hunger row of the horizon comparison on `hunger_win_at_*` rather
  than the point sample, which is too noisy to carry an outcome (above). The
  other four outcomes need no re-run.
- ~~Put land into net worth.~~ **Done**, with shares — see "Land and shares now
  count as wealth" above. It cut `tax_progressive`'s apparent equality by 8x
  and flipped the sign of the ownership arms' mobility story.
- **Re-run the ownership pair (`share_wealth` vs `share_equal`) on
  `gini_total` / `mobility_total`.** The existing null was measured on deaths
  and on a wealth measure that omitted the shares themselves, so the
  distributional half of the question is untested rather than answered.
- **Make land and shares tradable.** The valuation above is the half that was
  missing: a trading script needs a reservation price and `field_value()` is
  one. The open design question is what triggers a listing or a bid.
  Sketch: a field is worth capitalised rent *to someone who can work it*
  (needs `SKILL-FARM` above the `WORK_AS_FARMER` threshold), so an owner who
  has lost that ability values it below what a working farmer would pay —
  a real asymmetry rather than an arbitrary one. For shares, bid when the
  dividend yield beats holding cash, and sell under distress: a household
  below the pantry-restock threshold liquidating an asset to eat is the
  wealth-concentration channel this model currently cannot express at all,
  and it is the one most likely to overturn the ownership null, since it
  would make the register move instead of sitting at its genesis allocation.
  `ctx.query.holders()` already reads the register live, so the engine side
  is ready.
- Work out what stops the firm sector re-absorbing the whole household sector
  after t200 — the reversal above is now the dominant late-run dynamic and it
  makes every t400 number a measurement of oligopoly extraction as much as of
  policy. Related to the existing circular-flow item below.
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
- ~~Re-run the arm matrix on a non-degenerate outcome.~~ **Done** at 250 ticks
  x 15 seeds — see "The matrix re-run at 250 ticks" above. Everything above
  that section reporting deaths is superseded.
- **Calibrate the three-bill economy.** Half done: the budget-share and
  planning-horizon grid is swept and comes back empty (above), which rules the
  demand side out. The open half is labour intensity — cut `LET_DWELLING` to
  ~0.2 LABOR and `GENERATE_POWER` to ~0.3, then re-run `calibrate.py`
  unchanged. No arm comparison should run on this economy until all three
  needs can be met.
- With bills in, revisit **loss-of-services for delinquency** — the
  `COND-DELINQUENT` idea below is now mostly built, since a missed bill
  already credits a condition that cuts what you can earn. That is the poverty
  trap, and it wants measuring: does a household that misses rent once ever
  climb back?
- **Replace the estate arms with policies that can fire.** Three of the eight
  are inert while mortality is zero. Either give the matrix arms that differ
  in something that happens to the living, or reintroduce a cause of death
  that is not the COND-WEAK artifact. Until then the matrix is six arms.
- **Check whether the share arms shift the random stream** the way
  `_assign_heirs` does. If allocating holdings at genesis consumes draws that
  `_wire_scripts` would otherwise have taken, part of every share-arm result
  is a different economy rather than a different policy. `estate_heir` shows
  the effect is real and roughly the size of the luck floor.
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

# is a cheaper horizon good enough? runs to --late, scores the arm comparison
# at --early against it. --reanalyse re-scores a saved file without re-running.
.venv/bin/python -m experiments.inequality.horizon --seeds 12 --ticks 400 \
  --arms tax_none,tax_progressive,estate_treasury,margin_00 \
  --metrics-every 10 --out results/horizon_n30_t400.json
```

`run.py` and serial sweeps write a progress bar with an ETA to stderr;
`--no-progress` suppresses it. Under parallelism the bar is suppressed
automatically — N workers sharing one stderr produce noise, not a bar — and
each run reports a line as it finishes.

Roughly 1.4 ticks/sec at 30 individuals, now flat across a run rather than
degrading (see "Performance" above), so a 200-tick run is a couple of
minutes and a sweep costs about one run given enough cores. Cost still
scales linearly with population, which the whole-run ETA accounts for.
