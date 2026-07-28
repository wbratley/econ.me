"""Is 250 ticks long enough to compare arms, or does it have to be 400?

The matrix costs ~4h at 400 ticks and ~2.5h at 250. That saving is only worth
taking if the *comparison* is the same at both horizons -- and "have the
metrics settled by 250" is the wrong question to ask about it. A metric can
still be moving at 250 while every arm moves together, in which case the arm
gap is already decided; and a metric can look flat while two arms are still
crossing. What has to hold is that the conclusion drawn at t250 is the
conclusion drawn at t400.

So: run a few arms x seeds out to 400, measure every outcome at both ticks
*within the same runs*, and check three things per outcome.

  1. AGREEMENT. Run the full pairwise Welch analysis twice, once on the t250
     values and once on the t400 values. Every pair is one conclusion --
     "these arms differ" or "they do not" -- and the two horizons either
     reach the same one or they do not. This is the test that decides it.
  2. SIGN. A pair that flips sign between the horizons is worse than one that
     merely loses significance: it means the arms crossed, and t250 would
     report the winner backwards.
  3. MAGNITUDE. diff@250 / diff@400 per pair. The deaths result was a
     *decaying* effect -- 6.70 at t200 down to 1.43 at t400 -- so a horizon
     that agrees on sign and significance can still overstate the size of
     everything by a factor of five.

Deaths are degenerate now (0 everywhere, since COND-WEAK decays), so the
outcomes here are hunger satisfaction, COND-WEAK burden, gini and mobility.
Deaths are still reported, as a check that they really are all zero.

Usage:
    .venv/bin/python -m experiments.inequality.horizon --seeds 12 --ticks 400 \
        --arms tax_none,tax_progressive,estate_treasury,margin_00 \
        --metrics-every 10 --out results/horizon_n30_t400.json
"""

import argparse
import json
import time
from pathlib import Path

from ..parallel import Job, default_workers, run_jobs
from ..progress import _duration
from .scenario import ScenarioConfig
from .sweep import ARMS, _mean, _p_two_sided, _sd, welch
from .tipping import HORIZON_TICKS, summarise

# Which direction counts as improvement, recorded alongside each result so a
# reader is never left to infer it. Mobility is the arguable one: "low" here
# means a low correlation between where you started and where you ended, i.e.
# more movement between positions -- that is a convention, not a finding.
OUTCOMES = {
    "hunger": ("mean hunger satisfaction (point sample)", "high"),
    "hunger_win": ("mean hunger satisfaction (trailing window)", "high"),
    "cond_weak": ("COND-WEAK burden (total)", "low"),
    "carriers": ("COND-WEAK carriers", "low"),
    "gini": ("gini (cash + goods)", "low"),
    "gini_total": ("gini (incl. land + shares)", "low"),
    "mobility": ("mobility r, cash + goods", "low"),
    "mobility_total": ("mobility r, incl. land + shares", "low"),
    "land_share_of_wealth": ("land as a share of household wealth", "n/a"),
    "deaths": ("deaths", "low"),
}

EARLY, LATE = 250, 400


def _values(rows: list[dict], outcome: str, tick: int) -> list[float]:
    """Per-seed values, dropping runs that never reached `tick` (summarise
    returns None for those rather than a zero that would read as real)."""
    return [float(r[f"{outcome}_at_{tick}"])
            for r in rows if r.get(f"{outcome}_at_{tick}") is not None]


def _pairwise(rows_by_arm: dict[str, list[dict]], outcome: str, tick: int) -> dict:
    arms = list(rows_by_arm)
    out = {}
    for i, a in enumerate(arms):
        for b in arms[i + 1:]:
            xa, xb = _values(rows_by_arm[a], outcome, tick), _values(rows_by_arm[b], outcome, tick)
            if len(xa) < 2 or len(xb) < 2:
                continue
            t, df = welch(xa, xb)
            out[f"{a} vs {b}"] = {
                "diff": _mean(xa) - _mean(xb), "t": t, "df": df,
                "p": _p_two_sided(t, df),
            }
    return out


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation, so "do the arms come out in the same order" does not
    depend on the outcome's units or on one arm being an outlier."""
    def ranks(vs: list[float]) -> list[float]:
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        r = [0.0] * len(vs)
        i = 0
        while i < len(order):                     # average ranks within ties
            j = i
            while j + 1 < len(order) and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    ra, rb = ranks(xs), ranks(ys)
    n = len(ra)
    if n < 2:
        return float("nan")
    ma, mb = _mean(ra), _mean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    return cov / (va * vb) ** 0.5 if va and vb else float("nan")


def analyse(rows_by_arm: dict[str, list[dict]],
            early_tick: int = EARLY, late_tick: int = LATE) -> dict:
    arms = list(rows_by_arm)
    n_pairs = len(arms) * (len(arms) - 1) // 2
    alpha = 0.05 / n_pairs if n_pairs else 0.05
    analysis: dict[str, dict] = {"arms": arms, "bonferroni_alpha": alpha, "outcomes": {}}

    for outcome, (label, direction) in OUTCOMES.items():
        aim = "no better/worse direction" if direction == "n/a" else f"{direction} is better"
        print(f"\n{'=' * 78}\n{label}  ({outcome}, {aim})\n{'=' * 78}")

        header = "".join(f"{t:>9}" for t in HORIZON_TICKS)
        print(f"{'arm':<18}{header}")
        trajectories = {}
        for arm in arms:
            cells, traj = "", {}
            for t in HORIZON_TICKS:
                xs = _values(rows_by_arm[arm], outcome, t)
                traj[t] = _mean(xs) if xs else None
                cells += f"{traj[t]:>9.3f}" if xs else f"{'-':>9}"
            trajectories[arm] = traj
            print(f"{arm:<18}{cells}")

        early = {a: _values(rows_by_arm[a], outcome, early_tick) for a in arms}
        late = {a: _values(rows_by_arm[a], outcome, late_tick) for a in arms}
        if any(len(v) < 2 for v in early.values()) or any(len(v) < 2 for v in late.values()):
            print(f"  (no t{early_tick}/t{late_tick} data -- skipping the horizon comparison)")
            continue

        # Is a run's standing already decided by t250? Paired within run, so
        # this is not the same question as whether the arm means agree.
        print(f"\n  per-seed t{early_tick} -> t{late_tick} (paired within run):")
        for arm in arms:
            xs, ys = early[arm], late[arm]
            r = _spearman(xs, ys) if len(set(xs)) > 1 and len(set(ys)) > 1 else float("nan")
            print(f"    {arm:<18} mean {_mean(xs):>8.3f} -> {_mean(ys):>8.3f}   "
                  f"sd {_sd(xs):>6.3f} -> {_sd(ys):>6.3f}   rank r={r:+.2f}")

        order_r = _spearman([_mean(early[a]) for a in arms], [_mean(late[a]) for a in arms])
        print(f"\n  arm ordering, t{early_tick} vs t{late_tick}: rank r={order_r:+.2f}")

        pe, pl = _pairwise(rows_by_arm, outcome, early_tick), _pairwise(rows_by_arm, outcome, late_tick)
        print(f"\n  {'pair':<40}{'diff@' + str(early_tick):>11}{'p':>9}"
              f"{'diff@' + str(late_tick):>11}{'p':>9}  verdict")
        agree = flips = 0
        pairs = {}
        for key in pl:
            e, l = pe.get(key), pl[key]
            if e is None:
                continue
            se, sl = e["p"] < alpha, l["p"] < alpha
            same_sign = (e["diff"] >= 0) == (l["diff"] >= 0)
            if not same_sign and (se or sl):
                verdict, flips = "SIGN FLIP", flips + 1
            elif se == sl:
                verdict, agree = "agrees" if se else "agrees (neither)", agree + 1
            else:
                verdict = f"t{early_tick} only" if se else f"t{late_tick} only"
            ratio = e["diff"] / l["diff"] if l["diff"] else float("nan")
            pairs[key] = {"early": e, "late": l, "verdict": verdict, "ratio": ratio}
            print(f"  {key:<40}{e['diff']:>+11.3f}{e['p']:>9.4f}"
                  f"{l['diff']:>+11.3f}{l['p']:>9.4f}  {verdict}"
                  + (f"  (x{ratio:.2f})" if se or sl else ""))

        print(f"\n  {agree}/{len(pairs)} pairs reach the same conclusion at both "
              f"horizons; {flips} sign flips. (Bonferroni alpha={alpha:.5f})")

        analysis["outcomes"][outcome] = {
            "label": label, "direction": direction,
            "trajectories": trajectories, "pairs": pairs,
            "order_rank_r": order_r,
            "agree": agree, "total_pairs": len(pairs), "sign_flips": flips,
        }

    return analysis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seeds", type=int, default=12)
    parser.add_argument("--ticks", type=int, default=400)
    parser.add_argument("--individuals", type=int, default=30)
    parser.add_argument("--arms", type=str,
                         default="tax_none,tax_progressive,estate_treasury,margin_00",
                         help="comma-separated subset of: " + ",".join(ARMS))
    parser.add_argument("--metrics-every", type=int, default=10)
    parser.add_argument("--early", type=int, default=EARLY,
                         help=f"the cheap horizon under test (default {EARLY}); "
                              "must be one of " + ",".join(map(str, HORIZON_TICKS)))
    parser.add_argument("--late", type=int, default=LATE,
                         help=f"the horizon it is checked against (default {LATE})")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--out", type=str, required=True)
    parser.add_argument("--reanalyse", action="store_true",
                         help="re-run the analysis over an existing --out file "
                              "instead of running any scenarios")
    args = parser.parse_args()
    for name, tick in (("--early", args.early), ("--late", args.late)):
        if tick not in HORIZON_TICKS:
            parser.error(f"{name}={tick} is not sampled; summarise() records "
                         f"outcomes at {HORIZON_TICKS} (tipping.HORIZON_TICKS)")

    if args.reanalyse:
        existing = json.loads(Path(args.out).read_text())
        analyse(existing["rows"], args.early, args.late)
        return

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {unknown}; choose from {list(ARMS)}")

    jobs = [
        Job(
            label=f"{arm} seed {seed}",
            config=ScenarioConfig(n_individuals=args.individuals, seed=seed, **ARMS[arm]),
            ticks=args.ticks,
            kwargs=dict(metrics_every=args.metrics_every),
            reduce=summarise,   # only the summary row crosses the process boundary
        )
        for arm in arms
        for seed in range(args.seeds)
    ]
    workers = args.workers if args.workers is not None else default_workers(len(jobs))
    print(f"{len(arms)} arms x {args.seeds} seeds = {len(jobs)} runs of {args.ticks} "
          f"ticks, {args.individuals} individuals, metrics_every={args.metrics_every}, "
          f"{workers} workers")

    started = time.perf_counter()
    rows = run_jobs(jobs, workers=workers)
    elapsed = time.perf_counter() - started

    rows_by_arm: dict[str, list[dict]] = {arm: [] for arm in arms}
    for job, row in zip(jobs, rows):
        rows_by_arm[job.label.rsplit(" seed ", 1)[0]].append(row)

    print(f"\n{len(jobs)} runs in {_duration(elapsed)} "
          f"({elapsed / len(jobs):.1f}s per run, wall)")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "config": {"seeds": args.seeds, "ticks": args.ticks,
                   "individuals": args.individuals, "arms": arms,
                   "metrics_every": args.metrics_every,
                   "early_tick": args.early, "late_tick": args.late},
        "wall_clock_seconds": elapsed,
        "rows": rows_by_arm,
    }, indent=2))

    analysis = analyse(rows_by_arm, args.early, args.late)
    payload = json.loads(out.read_text())
    payload["analysis"] = analysis
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
