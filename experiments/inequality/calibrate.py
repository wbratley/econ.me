"""Calibrate the three-bill economy: what makes food, rent and power all
affordable at once?

Rent and power tripled the real flow a household has to fund every tick, while
the nominal anchor -- `spend_rate = balance / planning_horizon`, split by fixed
budget shares -- was calibrated against a basket of food alone. Measured
result: all three needs stuck at 0.4-0.6 satisfaction indefinitely, where food
alone used to sit above 0.85.

This is the same job the field-count sweep did (see NOTES "Bug 8"), and the
criteria are deliberately the same, because "everyone is fed" is not on its own
a good economy:

  MET       every need's satisfaction is high. The point of the exercise.
  TIGHT     most of what is offered actually sells. A market clearing 30% of
            its supply is not cheap, it is broken -- and a market clearing
            everything at any price has no scarcity to set a level by, which
            is exactly the frozen-price failure that made food cost less when
            land got scarcer.
  MOVING    prices are neither exploding nor pinned. A flat price series in
            this model has usually meant the auction tie-break is choosing the
            level, not supply and demand.
  ALIVE     nobody incapacitated, and the firm sector still solvent at the end.

An arm passes only on all four. Ranking on satisfaction alone would pick the
slackest economy in the grid every time.

Usage:
    .venv/bin/python -m experiments.inequality.calibrate --seeds 3 --ticks 150 \
        --out results/calibration_bills.json
"""

import argparse
import json
import statistics
import time
from pathlib import Path

from ..parallel import Job, default_workers, run_jobs
from ..progress import _duration
from .scenario import ScenarioConfig

# Budget splits, each summing to <= 1 (the remainder is saved). Named so the
# report says what was tried rather than printing four numbers.
#
# `cost_weighted` is the principled candidate: shares proportional to what each
# good costs to make, per unit of the routine quantity a household turns over.
# Shelter is cheap to produce (0.5 labour -> 6 units) and energy dear (1 -> 6),
# so a split that gives them equal shares is quietly asking for a shortage of
# whichever is dearer.
SHARE_SETS = {
    "legacy":         dict(food=0.50, shelter=0.20, energy=0.10, clothes=0.15),
    "cost_weighted":  dict(food=0.45, shelter=0.12, energy=0.25, clothes=0.08),
    "shelter_heavy":  dict(food=0.40, shelter=0.25, energy=0.22, clothes=0.08),
    "food_light":     dict(food=0.33, shelter=0.20, energy=0.30, clothes=0.07),
}
HORIZONS = [20.0, 12.0, 8.0, 5.0]

NEEDS = ("HUNGER", "SHELTER", "POWER")
MET_FLOOR = 0.85          # every need must reach this
TIGHT_BAND = (0.55, 0.98)  # sell-side fill ratio: neither rationed nor a glut


def _config(share_name: str, horizon: float, seed: int, n: int) -> ScenarioConfig:
    s = SHARE_SETS[share_name]
    return ScenarioConfig(
        n_individuals=n, seed=seed, planning_horizon=horizon,
        food_budget_share=s["food"], shelter_budget_share=s["shelter"],
        energy_budget_share=s["energy"], clothes_budget_share=s["clothes"],
    )


def summarise_calibration(result: dict) -> dict:
    """Reduced in the worker -- only this row crosses the process boundary."""
    snaps = [s for s in result["snapshots"] if s["tick"] > 0]
    tail = snaps[len(snaps) // 2:]          # judge the second half, not the bootstrap
    last = snaps[-1]

    def need_mean(code: str) -> float:
        key = {"HUNGER": "mean_hunger_satisfaction"}.get(code)
        if key:
            return statistics.mean(s[key] for s in tail)
        vals = [s["needs"][code] for s in tail if s.get("needs", {}).get(code) is not None]
        return statistics.mean(vals) if vals else float("nan")

    def fill(symbol: str) -> float:
        ratios = [
            s["markets"][symbol]["sell_fill_ratio"]
            for s in tail
            if s.get("markets", {}).get(symbol, {}).get("sell_fill_ratio") is not None
        ]
        return statistics.mean(ratios) if ratios else float("nan")

    def price_move(symbol: str) -> float:
        """Coefficient of variation over the tail. Zero means pinned."""
        ps = [s["prices"][symbol] for s in tail if s["prices"].get(symbol)]
        if len(ps) < 3:
            return float("nan")
        m = statistics.mean(ps)
        return statistics.pstdev(ps) / m if m else float("nan")

    return {
        "seed": result["config"]["seed"],
        "needs": {c: need_mean(c) for c in NEEDS},
        "fill": {s: fill(s) for s in ("FOOD", "SHELTER", "ENERGY")},
        "price_cv": {s: price_move(s) for s in ("FOOD", "SHELTER", "ENERGY")},
        "prices": {s: last["prices"].get(s) for s in ("FOOD", "SHELTER", "ENERGY", "LABOR")},
        "incapacitated": last["incapacitated_count"],
        "firms_solvent": last.get("capital", {}).get("firms_solvent", 0),
        "firm_cash": last.get("capital", {}).get("firm_cash_total", 0.0),
        "gini_total": last.get("gini_total", last["gini"]),
        "land": last.get("land_use", {}),
    }


def _verdict(row: dict) -> tuple[bool, str]:
    fails = []
    for code, v in row["needs"].items():
        if not (v >= MET_FLOOR):
            fails.append(f"{code.lower()} {v:.2f}")
    for sym, v in row["fill"].items():
        if v == v and not (TIGHT_BAND[0] <= v <= TIGHT_BAND[1]):
            fails.append(f"{sym.lower()} fill {v:.2f}")
    if row["incapacitated"]:
        fails.append(f"{row['incapacitated']} dead")
    if row["firms_solvent"] < 1:
        fails.append("sector bankrupt")
    return (not fails), ", ".join(fails)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--ticks", type=int, default=150)
    parser.add_argument("--individuals", type=int, default=30)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--out", type=str, required=True)
    args = parser.parse_args()

    grid = [(s, h) for s in SHARE_SETS for h in HORIZONS]
    jobs = [
        Job(
            label=f"{share}|h{horizon:g}|s{seed}",
            config=_config(share, horizon, seed, args.individuals),
            ticks=args.ticks,
            kwargs=dict(metrics_every=5),
            reduce=summarise_calibration,
        )
        for share, horizon in grid for seed in range(args.seeds)
    ]
    workers = args.workers if args.workers is not None else default_workers(len(jobs))
    print(f"{len(grid)} configs x {args.seeds} seeds = {len(jobs)} runs of "
          f"{args.ticks} ticks, {workers} workers")

    started = time.perf_counter()
    rows = run_jobs(jobs, workers=workers)
    elapsed = time.perf_counter() - started

    by_cfg: dict[str, list[dict]] = {}
    for job, row in zip(jobs, rows):
        by_cfg.setdefault(job.label.rsplit("|s", 1)[0], []).append(row)

    print(f"\n{len(jobs)} runs in {_duration(elapsed)}\n")
    print(f"{'config':<24}{'hunger':>8}{'shelter':>9}{'power':>8}"
          f"{'fillF':>7}{'fillS':>7}{'fillE':>7}{'P_FOOD':>9}{'dead':>6}  verdict")

    scored = []
    for cfg, rs in by_cfg.items():
        mean = {
            "needs": {c: statistics.mean(r["needs"][c] for r in rs) for c in NEEDS},
            "fill": {s: statistics.mean(r["fill"][s] for r in rs if r["fill"][s] == r["fill"][s])
                     if any(r["fill"][s] == r["fill"][s] for r in rs) else float("nan")
                     for s in ("FOOD", "SHELTER", "ENERGY")},
            "incapacitated": statistics.mean(r["incapacitated"] for r in rs),
            "firms_solvent": statistics.mean(r["firms_solvent"] for r in rs),
        }
        ok, why = _verdict(mean)
        worst = min(mean["needs"].values())
        scored.append((ok, worst, cfg, mean, why))
        p_food = statistics.mean(r["prices"]["FOOD"] or 0 for r in rs)
        print(f"{cfg:<24}{mean['needs']['HUNGER']:>8.2f}{mean['needs']['SHELTER']:>9.2f}"
              f"{mean['needs']['POWER']:>8.2f}{mean['fill']['FOOD']:>7.2f}"
              f"{mean['fill']['SHELTER']:>7.2f}{mean['fill']['ENERGY']:>7.2f}"
              f"{p_food:>9.2f}{mean['incapacitated']:>6.1f}  "
              f"{'PASS' if ok else why}")

    passing = [s for s in scored if s[0]]
    print()
    if passing:
        best = max(passing, key=lambda s: s[1])
        print(f"PASSES: {len(passing)} of {len(scored)}. Best on worst-need: {best[2]} "
              f"(worst need {best[1]:.2f})")
    else:
        best = max(scored, key=lambda s: s[1])
        print(f"NOTHING PASSES all four criteria. Closest on worst-need: {best[2]} "
              f"(worst need {best[1]:.2f}) -- failing: {best[4]}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({
        "config": {"seeds": args.seeds, "ticks": args.ticks,
                   "individuals": args.individuals,
                   "share_sets": SHARE_SETS, "horizons": HORIZONS},
        "wall_clock_seconds": elapsed,
        "rows": by_cfg,
    }, indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
