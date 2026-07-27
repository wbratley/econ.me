# Fielding inequality experiment — progress notes

Status: pricing rebuilt, scenario recalibrated, 5-variant matrix re-run
against it, and seed-swept — which killed one of the matrix's headline
findings (see "Seed sweep" below).
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

## Seed sweep: one of those columns does not survive it

Three further seeds per variant, same settings. Incapacitated out of 30:

| Variant | seed 0 | seed 1 | seed 2 | seed 3 |
|---|---|---|---|---|
| No tax | 16 | 15 | 16 | 17 |
| Flat tax 10% | 11 | 4 | 9 | 4 |
| Estate → treasury | **2** | **14** | **17** | **1** |

The baseline is a stable, reproducible outcome — a 30-person economy on this
much land kills about half of it, every time. Flat tax reliably improves on
that: its worst draw (11) still beats the baseline's best (15).

Estate → treasury does not have a mean worth quoting. It is **bimodal**:
either near-total rescue (1–2 deaths) or no better than doing nothing
(14–17), with nothing in between across four seeds. Gini tracks it exactly
(0.158 to 0.507). Seed 0 was one side of that split, and the "estate rules
outperform income tax" reading it suggested is not supported.

The bimodality is the interesting result here, not a nuisance to average
away. The plausible mechanism — untested — is a tipping point in
`COND-WEAK`: the condition halves labor productivity, so once enough people
carry it the economy cannot grow its way back, and whether an estate
windfall lands before or after that threshold decides the whole run. That
predicts the split should track *when* the first deaths occur rather than
how much money moves, which is a checkable claim.

**Still-live caveats.** One population size, one field count. The baseline is
still degrading at tick 200 (hunger 0.543 and falling), so these may be
points on a slope rather than steady states. Progressive tax ending more
unequal than flat tax while feeding people better is unexplained.

## Not yet done

- Establish whether the matrix numbers are steady states or points on a
  slope — the baseline is still degrading at tick 200. Needs a 400+ tick run.
- Test the tipping-point explanation for the estate → treasury bimodality:
  does the split track *when* the first deaths occur rather than how much
  money moves? Needs the per-tick snapshots the runs already save.
- Population and field-count sensitivity (seeds are now swept; these are
  not). The calibration sweep in particular was a single seed.
- Understand why progressive tax ends more unequal than flat tax while
  feeding people better.
- Empirically time and pick a large-scale target (§ Scale in the original
  plan); current throughput is ~1.6–6 ticks/sec depending on population/
  firm count and is itself informative for the design.md §7 fast-forward
  question, but hasn't been formally profiled.
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
.venv/bin/python -m experiments.inequality.matrix --individuals 30 --ticks 200 \
  --metrics-every 5 --out-dir /path/to/outdir
```

Both write a progress bar with an ETA to stderr; `--no-progress` suppresses
it. Runs are minutes long — roughly 1.4 ticks/sec at 30 individuals, and it
degrades as the population and order books grow, so the ETA is deliberately
computed over the whole run rather than a recent window.
