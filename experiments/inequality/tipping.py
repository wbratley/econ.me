"""Why is `estate -> treasury` bimodal?

Four runs of that variant gave 2, 14, 17 and 1 incapacitated out of 30 --
either near-total rescue or no better than doing nothing, with nothing in
between, while the no-tax baseline sat at 15-17 every time. Two questions
sit on top of each other there: whether the spread is real, and what decides
which side of it a run lands on.

(Those four runs were labelled by seed but were not actually controlled by
it -- see experiments/determinism.py. They stand as four independent
replicates; they are not reproducible individually. Runs from here are.)

Two candidate explanations, both checkable:

  H1 TIPPING POINT. `COND-WEAK` has no decay_per_tick, so it never recovers:
     it accumulates for anyone who ever goes hungry and permanently costs
     them 30% of their labor. Past some density of carriers the economy
     cannot produce its way back out, and whether redistribution arrives
     before or after that point decides the run. Predicts the split tracks
     WHEN people first go hungry, not how much money the Treasury moves.

  H2 STERILISED LAND. `_apply_estate` moves a dead entity's parcels to the
     recipient, and in this variant the recipient is the Treasury -- which
     has no production script. Every field it inherits stops growing food,
     permanently. Predicts the split tracks how much FARMLAND falls idle,
     and that collapse runs are the ones where early deaths took land out of
     production rather than the ones where more people happened to die.

The two make different predictions about the same runs, so running the
variant across many seeds and correlating outcome against each separates
them. Usage:

    .venv/bin/python -m experiments.inequality.tipping --seeds 10 --out /path.json
"""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from ..parallel import Job, default_workers, run_jobs
from .run import run_scenario
from .scenario import ScenarioConfig

# A run is a rescue or a collapse; nothing has ever landed between these.
RESCUE_MAX = 6
COLLAPSE_MIN = 10


def _first(snapshots: list[dict], predicate) -> int | None:
    for snap in snapshots:
        if predicate(snap):
            return snap["tick"]
    return None


def summarise(result: dict) -> dict:
    snapshots = result["snapshots"]
    last = snapshots[-1]

    deaths = sorted(
        (e["incapacitated_tick"], e["landed"])
        for e in last["entities"]
        if e["incapacitated_tick"] is not None
    )
    early_deaths = [d for d in deaths if d[0] is not None and d[0] <= 100]

    return {
        "seed": result["config"]["seed"],
        "incapacitated": last["incapacitated_count"],
        "gini": last["gini"],
        "hunger": last["mean_hunger_satisfaction"],
        # H1 instruments: when hunger first bit, and how fast the permanent
        # damage accumulated.
        "first_hunger_tick": _first(snapshots, lambda s: s["cond_weak_carriers"] > 0),
        "carriers_at_50": next(
            (s["cond_weak_carriers"] for s in snapshots if s["tick"] >= 50), 0),
        "carriers_at_100": next(
            (s["cond_weak_carriers"] for s in snapshots if s["tick"] >= 100), 0),
        "cond_weak_at_100": next(
            (s["cond_weak_total"] for s in snapshots if s["tick"] >= 100), 0.0),
        "first_death_tick": deaths[0][0] if deaths else None,
        # Death count at fixed ticks, so a run's mortality TRAJECTORY survives
        # summarising. Without these, comparing a 200-tick result against a
        # 400-tick one cannot separate "the intervention stopped working" from
        # "we measured at a different time" — which is exactly the ambiguity
        # the n=100 sweep hit, because reducing in the worker (parallel.Job)
        # discards the snapshots these are read from.
        **{
            f"deaths_at_{t}": sum(1 for d, _ in deaths if d is not None and d <= t)
            for t in (100, 150, 200, 250, 300)
        },
        # H2 instruments: how much land stopped producing, and whether the
        # early deaths were the people who owned it.
        "idle_fields_final": last["farmland"]["idle"],
        "treasury_fields_final": last["farmland"]["treasury_held"],
        "idle_fields_at_100": next(
            (s["farmland"]["idle"] for s in snapshots if s["tick"] >= 100), 0),
        "early_deaths": len(early_deaths),
        "early_landed_deaths": sum(1 for _, landed in early_deaths if landed),
        # The confound to rule out: money moved, which H1 and H2 both say is
        # not what decides it.
        "treasury_balance_final": last["treasury_balance"],
        "food_at_100": next(
            (s["produced"].get("FOOD", 0.0) for s in snapshots if s["tick"] >= 100), 0.0),
        "food_final": last["produced"].get("FOOD", 0.0),
        # Capital-ownership instruments. .get() throughout because snapshots
        # taken before metrics grew a "capital" block have no such key, and
        # old result JSONs should still summarise rather than crash.
        "firm_cash_final": last.get("capital", {}).get("firm_cash_total", 0.0),
        "firms_solvent_final": last.get("capital", {}).get("firms_solvent", 0),
        # Firm capital at fixed ticks, for the same reason deaths_at_* exist:
        # the sector's decapitalisation is a trajectory, and comparing a
        # margin arm against the baseline on final cash alone cannot separate
        # "it never drained" from "it drained later". Sampled at the first
        # snapshot at or after each tick, so it is robust to metrics_every.
        **{
            f"firm_cash_at_{t}": next(
                (s.get("capital", {}).get("firm_cash_total", 0.0)
                 for s in snapshots if s["tick"] >= t), 0.0)
            for t in (50, 100, 150, 200, 300)
        },
        **{
            f"firms_solvent_at_{t}": next(
                (s.get("capital", {}).get("firms_solvent", 0)
                 for s in snapshots if s["tick"] >= t), 0)
            for t in (50, 100, 150, 200, 300)
        },
        # Dividends are a per-tick flow and snapshots are every metrics_every
        # ticks, so this is a SAMPLE of the flow, not the cumulative total --
        # comparable across arms at equal sampling, not a payout figure.
        "dividends_sampled": sum(
            s.get("capital", {}).get("dividends_paid", 0.0) for s in snapshots
        ),
    }


def _correlation(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx ** 0.5 * vy ** 0.5)


def report(rows: list[dict]) -> None:
    rescues = [r for r in rows if r["incapacitated"] <= RESCUE_MAX]
    collapses = [r for r in rows if r["incapacitated"] >= COLLAPSE_MIN]
    between = [r for r in rows if RESCUE_MAX < r["incapacitated"] < COLLAPSE_MIN]

    print(f"\n{'seed':>4} {'inc':>4} {'1st hungry':>11} {'carriers@100':>13} "
          f"{'idle@100':>9} {'idlefinal':>10} {'earlyLandDeaths':>16} "
          f"{'food@100':>9} {'treasury$':>11}")
    for r in sorted(rows, key=lambda r: r["incapacitated"]):
        print(f"{r['seed']:>4} {r['incapacitated']:>4} "
              f"{str(r['first_hunger_tick']):>11} {r['carriers_at_100']:>13} "
              f"{r['idle_fields_at_100']:>9} {r['idle_fields_final']:>10} "
              f"{r['early_landed_deaths']:>16} "
              f"{r['food_at_100']:>9.1f} {r['treasury_balance_final']:>11.0f}")

    print(f"\nrescues={len(rescues)} collapses={len(collapses)} between={len(between)}")
    if between:
        print("  NOTE: runs landed between the two modes -- the split is not "
              "as clean as four seeds suggested, and the framing needs revising.")

    outcome = [float(r["incapacitated"]) for r in rows]
    print("\ncorrelation with final incapacitated count:")
    for key in ("first_hunger_tick", "carriers_at_50", "carriers_at_100",
                "cond_weak_at_100", "idle_fields_at_100", "early_landed_deaths",
                "food_at_100", "treasury_balance_final"):
        values = [float(r[key] if r[key] is not None else 0) for r in rows]
        print(f"  {key:<24} r={_correlation(values, outcome):+.3f}")

    if rescues and collapses:
        print("\ngroup means (rescue vs collapse):")
        for key in ("first_hunger_tick", "carriers_at_50", "carriers_at_100",
                    "idle_fields_at_100", "idle_fields_final",
                    "early_landed_deaths", "food_at_100", "treasury_balance_final"):
            def mean(group):
                vals = [float(r[key]) for r in group if r[key] is not None]
                return sum(vals) / len(vals) if vals else float("nan")
            print(f"  {key:<24} rescue={mean(rescues):>9.2f}  "
                  f"collapse={mean(collapses):>9.2f}")


def _report_seed(seed: int, result: dict) -> dict:
    row = summarise(result)
    print(f"seed {seed}: incapacitated={row['incapacitated']} "
          f"idle_fields={row['idle_fields_final']} "
          f"carriers@100={row['carriers_at_100']}", flush=True)
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seeds", type=int, default=10)
    parser.add_argument("--ticks", type=int, default=200)
    parser.add_argument("--individuals", type=int, default=30)
    parser.add_argument("--estate-rule", type=str, default="treasury")
    parser.add_argument("--tax-rate", type=str, default="0.10")
    parser.add_argument("--metrics-every", type=int, default=5)
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--workers", type=int, default=None,
                         help="parallel processes (default: one per seed, capped at "
                              "core count). 1 runs serially with a progress bar.")
    args = parser.parse_args()

    jobs = [
        Job(
            label=f"seed {seed}",
            config=ScenarioConfig(
                n_individuals=args.individuals, seed=seed,
                tax_rate=Decimal(args.tax_rate), estate_rule=args.estate_rule,
            ),
            ticks=args.ticks,
            kwargs=dict(metrics_every=args.metrics_every),
        )
        for seed in range(args.seeds)
    ]
    workers = args.workers if args.workers is not None else default_workers(len(jobs))

    rows = []
    if workers == 1:
        # Serial path keeps the per-seed progress bar, which is only readable
        # when one run owns stderr.
        for seed, job in enumerate(jobs):
            result = run_scenario(
                job.config, args.ticks, metrics_every=args.metrics_every,
                progress=not args.no_progress, progress_label=job.label,
            )
            rows.append(_report_seed(seed, result))
    else:
        # Seeds are independent, and run_jobs returns them in submission
        # order, so the report does not depend on which worker finished first.
        for seed, result in enumerate(run_jobs(jobs, workers=workers)):
            rows.append(_report_seed(seed, result))

    report(rows)
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
