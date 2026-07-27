"""Powered comparison: every policy variant crossed with every seed.

`matrix.py` sweeps variants at one seed; `tipping.py` sweeps seeds at one
variant. Neither answers the question the variance result left open — whether
the interventions differ from each other at all — because that needs the full
grid and enough replicates to separate means that sit inside each other's
spread.

At n=12 they did not separate: flat tax 6.0 (sd 6.3) against estate→treasury
5.5 (sd 6.8). For a two-sample comparison at sd≈6.5 the replicates needed per
arm go roughly as 16σ²/δ²:

    δ = 6.5 (1 sd)   16 seeds     δ = 3    75 seeds
    δ = 4            42 seeds     δ = 2   169 seeds

So n≈100 buys separation down to ~3 deaths and n≈170 down to ~2. Below that,
adding further policy arms just produces more arms that cannot be told apart.

Reports mean/sd/median/range per arm plus Welch's t against the no-tax
baseline and against every other arm, since "does redistribution beat doing
nothing" and "do the two redistributions differ" are separate questions and
the second is the one that was open.

Usage:
    .venv/bin/python -m experiments.inequality.sweep --seeds 100 --ticks 400 \
        --out /path/to/sweep.json
"""

import argparse
import json
import math
import time
from decimal import Decimal
from pathlib import Path

from ..parallel import Job, default_workers, run_jobs
from ..progress import _duration
from .scenario import ScenarioConfig
from .tipping import summarise

# The three arms the variance table compared, plus the two the matrix carried,
# plus the capital-ownership pair. Keyed identically to matrix.VARIANTS where
# they overlap, so results stay cross-referenceable.
#
# share_wealth and share_equal are deliberately no-tax, burn-estate runs
# differing ONLY in who owns the firms. Every redistribution arm moves income
# after the fact; these two hold production identical and vary the ownership
# of it, which is the one question a redistribution matrix cannot ask.
# tax_none is their control: same policy, no shares issued at all.
ARMS = {
    "tax_none": dict(tax_rate=Decimal("0"), tax_threshold=Decimal("0"), estate_rule="burn"),
    "tax_flat": dict(tax_rate=Decimal("0.10"), tax_threshold=Decimal("0"), estate_rule="burn"),
    "tax_progressive": dict(tax_rate=Decimal("0.20"), tax_threshold=Decimal("200"), estate_rule="burn"),
    "estate_treasury": dict(tax_rate=Decimal("0.10"), tax_threshold=Decimal("0"), estate_rule="treasury"),
    "estate_heir": dict(tax_rate=Decimal("0.10"), tax_threshold=Decimal("0"), estate_rule="heir"),
    "share_wealth": dict(tax_rate=Decimal("0"), tax_threshold=Decimal("0"), estate_rule="burn",
                          share_allocation="wealth"),
    "share_equal": dict(tax_rate=Decimal("0"), tax_threshold=Decimal("0"), estate_rule="burn",
                         share_allocation="equal"),
    # Firm profit margin. Same policy as tax_none in every other respect, so
    # tax_none is their exact control and the three together read as a
    # dose-response rather than a single before/after. This is the one arm
    # family that changes the economy's *production* side: every other arm
    # here moves money after the goods exist.
    "margin_10": dict(tax_rate=Decimal("0"), tax_threshold=Decimal("0"), estate_rule="burn",
                       firm_margin=Decimal("0.10")),
    "margin_20": dict(tax_rate=Decimal("0"), tax_threshold=Decimal("0"), estate_rule="burn",
                       firm_margin=Decimal("0.20")),
}

BASELINE = "tax_none"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _sd(xs: list[float]) -> float:
    """Sample sd (n-1): these are replicates drawn from a distribution, not
    the whole population of possible runs."""
    if len(xs) < 2:
        return float("nan")
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _median(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def welch(a: list[float], b: list[float]) -> tuple[float, float]:
    """Welch's t and its degrees of freedom — unequal variances, which is the
    whole point here: the baseline's sd is a third of the interventions'."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return float("nan"), float("nan")
    va, vb = _sd(a) ** 2 / na, _sd(b) ** 2 / nb
    if va + vb == 0:
        return float("nan"), float("nan")
    t = (_mean(a) - _mean(b)) / math.sqrt(va + vb)
    df = (va + vb) ** 2 / (va ** 2 / (na - 1) + vb ** 2 / (nb - 1))
    return t, df


def _p_two_sided(t: float, df: float) -> float:
    """Two-sided p from Welch's t, via the regularised incomplete beta.
    Hand-rolled because this repo's .venv has no scipy (same reason
    metrics.py implements its own Gini)."""
    if math.isnan(t) or math.isnan(df) or df <= 0:
        return float("nan")
    x = df / (df + t * t)
    return _betainc(df / 2.0, 0.5, x)


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a,b) by continued fraction (Lentz)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _betainc(b, a, 1 - x)
    f, c, d = 1.0, 1.0, 0.0
    for i in range(0, 300):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        f *= c * d
        if abs(1.0 - c * d) < 1e-10:
            break
    return front * (f - 1.0)


def report(rows_by_arm: dict[str, list[dict]], key: str = "incapacitated") -> dict:
    stats = {}
    print(f"\n=== {key} out of population, per arm ===")
    print(f"{'arm':<18} {'n':>4} {'mean':>7} {'sd':>7} {'median':>7} {'min':>5} {'max':>5}")
    for arm, rows in rows_by_arm.items():
        xs = [float(r[key]) for r in rows]
        stats[arm] = {
            "n": len(xs), "mean": _mean(xs), "sd": _sd(xs),
            "median": _median(xs), "min": min(xs), "max": max(xs),
            "values": xs,
        }
        s = stats[arm]
        print(f"{arm:<18} {s['n']:>4} {s['mean']:>7.2f} {s['sd']:>7.2f} "
              f"{s['median']:>7.1f} {s['min']:>5.0f} {s['max']:>5.0f}")

    print("\n=== pairwise Welch (does any arm differ from any other?) ===")
    arms = list(rows_by_arm)
    comparisons = {}
    for i, a in enumerate(arms):
        for b in arms[i + 1:]:
            xa = [float(r[key]) for r in rows_by_arm[a]]
            xb = [float(r[key]) for r in rows_by_arm[b]]
            t, df = welch(xa, xb)
            p = _p_two_sided(t, df)
            comparisons[f"{a} vs {b}"] = {
                "diff": _mean(xa) - _mean(xb), "t": t, "df": df, "p": p,
            }
            flag = "" if math.isnan(p) or p >= 0.05 else "  *"
            print(f"  {a:<18} vs {b:<18} diff={_mean(xa)-_mean(xb):+6.2f}  "
                  f"t={t:+6.2f}  p={p:.4f}{flag}")
    print("\n  * p<0.05, uncorrected. With "
          f"{len(comparisons)} comparisons, treat single stars sceptically.")
    return {"per_arm": stats, "pairwise": comparisons}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seeds", type=int, default=100)
    parser.add_argument("--ticks", type=int, default=400)
    parser.add_argument("--individuals", type=int, default=30)
    parser.add_argument("--arms", type=str, default=",".join(ARMS),
                         help="comma-separated subset of: " + ",".join(ARMS))
    parser.add_argument("--metrics-every", type=int, default=5)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--out", type=str, required=True)
    args = parser.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {unknown}; choose from {list(ARMS)}")

    jobs = [
        Job(
            label=f"{arm} seed {seed}",
            config=ScenarioConfig(
                n_individuals=args.individuals, seed=seed, **ARMS[arm]
            ),
            ticks=args.ticks,
            kwargs=dict(metrics_every=args.metrics_every),
            # Reduce in the worker: only the summary row comes back, not the
            # ~0.8 MB of snapshots behind it. Analysis needs nothing else, and
            # a few hundred runs of full results would be a few hundred MB
            # pickled into a parent that has other things to do.
            reduce=summarise,
        )
        for arm in arms
        for seed in range(args.seeds)
    ]
    workers = args.workers if args.workers is not None else default_workers(len(jobs))
    print(f"{len(arms)} arms x {args.seeds} seeds = {len(jobs)} runs of "
          f"{args.ticks} ticks, {args.individuals} individuals, {workers} workers")

    started = time.perf_counter()
    rows = run_jobs(jobs, workers=workers)   # already summarised, see Job.reduce
    elapsed = time.perf_counter() - started

    rows_by_arm: dict[str, list[dict]] = {arm: [] for arm in arms}
    for job, row in zip(jobs, rows):
        rows_by_arm[job.label.rsplit(" seed ", 1)[0]].append(row)

    print(f"\n{len(jobs)} runs in {_duration(elapsed)} "
          f"({elapsed/len(jobs):.1f}s per run, wall)")
    analysis = report(rows_by_arm)

    Path(args.out).write_text(json.dumps({
        "config": {"seeds": args.seeds, "ticks": args.ticks,
                   "individuals": args.individuals, "arms": arms},
        "wall_clock_seconds": elapsed,
        "rows": rows_by_arm,
        "analysis": analysis,
    }, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
