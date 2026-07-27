"""Run the redistribution and estate-rule sweeps and write one JSON per
variant. Usage:
    .venv/bin/python -m experiments.inequality.matrix --scale small --out-dir /path
"""

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

from ..progress import _duration
from .run import run_scenario
from .scenario import ScenarioConfig, recommended_n_firms

VARIANTS = {
    "tax_none": dict(tax_rate=Decimal("0"), tax_threshold=Decimal("0"), estate_rule="burn"),
    "tax_flat": dict(tax_rate=Decimal("0.10"), tax_threshold=Decimal("0"), estate_rule="burn"),
    "tax_progressive": dict(tax_rate=Decimal("0.20"), tax_threshold=Decimal("200"), estate_rule="burn"),
    "estate_treasury": dict(tax_rate=Decimal("0.10"), tax_threshold=Decimal("0"), estate_rule="treasury"),
    "estate_heir": dict(tax_rate=Decimal("0.10"), tax_threshold=Decimal("0"), estate_rule="heir"),
}


def run_matrix(
    n_individuals: int, n_firms: int | None, ticks: int, out_dir: Path,
    metrics_every: int = 1, progress: bool = True,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    for index, (name, overrides) in enumerate(VARIANTS.items(), start=1):
        config = ScenarioConfig(n_individuals=n_individuals, n_firms=n_firms, **overrides)
        # Variants cost about the same, so completed ones give an honest ETA
        # for the whole matrix -- the per-variant bar only ever knows about
        # the run it is in.
        if progress and index > 1:
            per_variant = (time.perf_counter() - started) / (index - 1)
            remaining = per_variant * (len(VARIANTS) - index + 1)
            print(f"=== {name} ({index}/{len(VARIANTS)}): {overrides} "
                  f"-- matrix eta {_duration(remaining)} ===", file=sys.stderr)
        else:
            print(f"=== {name} ({index}/{len(VARIANTS)}): {overrides} ===", file=sys.stderr)

        result = run_scenario(
            config, ticks, db_path=str(out_dir / f"{name}.db"),
            metrics_every=metrics_every, progress=progress, progress_label=name,
        )
        last = result["snapshots"][-1]
        print(f"  ticks/s={result['ticks_per_second']:.2f} gini={last['gini']:.4f} "
              f"incapacitated={last['incapacitated_count']} "
              f"mean_hunger_satisfaction={last['mean_hunger_satisfaction']:.3f}")
        (out_dir / f"{name}.json").write_text(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--individuals", type=int, default=30)
    parser.add_argument("--firms", type=int, default=None,
                         help="default: derived from population (scenario.recommended_n_firms)")
    parser.add_argument("--ticks", type=int, default=200)
    parser.add_argument("--metrics-every", type=int, default=1)
    parser.add_argument("--out-dir", type=str, required=True)
    parser.add_argument("--no-progress", action="store_true",
                         help="suppress progress bars (they write to stderr)")
    args = parser.parse_args()
    # None flows through to build_economy, which derives it -- deliberately
    # not re-derived here, so there is exactly one place that decides.
    print(f"n_individuals={args.individuals} "
          f"n_firms={args.firms if args.firms is not None else 'auto'} "
          f"(auto = {recommended_n_firms(args.individuals)})")
    run_matrix(args.individuals, args.firms, args.ticks, Path(args.out_dir),
               args.metrics_every, progress=not args.no_progress)


if __name__ == "__main__":
    main()
