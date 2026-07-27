"""Run harness: build the Fielding economy, loop run_tick(), snapshot
metrics, and report results -- all directly against econengine, no HTTP.

CLI usage:
    .venv/bin/python -m experiments.inequality.run --ticks 200 --individuals 30 \
        --tax-rate 0 --estate-rule burn --out /path/to/result.json

Firm count is derived from population unless --firms says otherwise. A
progress bar with an ETA goes to stderr; --no-progress suppresses it.
"""

import argparse
import json
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import conditions
from econengine.models import Base
from econengine.tick import run_tick

from ..determinism import deterministic_ids
from ..progress import Progress
from . import metrics as metrics_mod
from .scenario import Scenario, ScenarioConfig, build_economy


def run_scenario(
    config: ScenarioConfig,
    ticks: int,
    db_path: str = ":memory:",
    metrics_every: int = 1,
    progress: bool = True,
    progress_label: str = "",
) -> dict:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)

    # Covers the whole run, genesis included: stochastic harvest outcomes are
    # rolled off process IDs, so seeding the ID source is what makes config.seed
    # actually control the run rather than just its starting conditions.
    with deterministic_ids(config.seed), Session(engine) as session:
        scenario = build_economy(session, config)

        if config.estate_rule == "treasury":
            conditions.set_estate_rule(
                session, "treasury", treasury_entity_id=scenario.treasury_id
            )
        elif config.estate_rule == "heir":
            conditions.set_estate_rule(session, "heir")
        else:
            conditions.set_estate_rule(session, "burn")
        session.commit()

        snapshots = [metrics_mod.snapshot(session, scenario, 0)]

        started = time.perf_counter()
        with Progress(ticks, progress_label, enabled=progress) as bar:
            for tick_number in range(1, ticks + 1):
                # run_tick() only flushes, never commits -- many ticks share
                # one transaction (design.md §"fast-forward"); reads in this
                # same session see flushed state fine, so we only commit at
                # the end.
                run_tick(session)
                if tick_number % metrics_every == 0 or tick_number == ticks:
                    snapshots.append(metrics_mod.snapshot(session, scenario, tick_number))
                bar.advance()
        elapsed = time.perf_counter() - started
        session.commit()

    config_out = asdict(config)
    for key, value in config_out.items():
        if isinstance(value, Decimal):
            config_out[key] = str(value)

    return {
        "config": config_out,
        "ticks": ticks,
        "wall_clock_seconds": elapsed,
        "ticks_per_second": ticks / elapsed if elapsed > 0 else None,
        "starting_balance": {k: float(v) for k, v in scenario.starting_balance.items()},
        "landed": scenario.landed,
        "snapshots": snapshots,
    }


def main() -> None:
    # Defaults are read straight off ScenarioConfig's own field defaults --
    # a second hardcoded copy of each number here previously drifted out of
    # sync with the dataclass (smallholder_fraction silently stayed at an
    # old value through several rounds of calibration because argparse's own
    # default always wins unless the flag is passed explicitly).
    defaults = ScenarioConfig()
    parser = argparse.ArgumentParser(description="Run the Fielding inequality experiment")
    parser.add_argument("--individuals", type=int, default=defaults.n_individuals)
    parser.add_argument("--firms", type=int, default=None,
                         help="default: derived from population (scenario.recommended_n_firms)")
    parser.add_argument("--ticks", type=int, default=200)
    parser.add_argument("--tax-rate", type=str, default=str(defaults.tax_rate))
    parser.add_argument("--tax-threshold", type=str, default=str(defaults.tax_threshold))
    parser.add_argument("--estate-rule", type=str, default=defaults.estate_rule,
                         choices=["burn", "treasury", "heir"])
    parser.add_argument("--redistribution-period", type=int, default=defaults.redistribution_period)
    parser.add_argument("--smallholder-fraction", type=float, default=defaults.smallholder_fraction)
    parser.add_argument("--seed", type=int, default=defaults.seed)
    parser.add_argument("--metrics-every", type=int, default=1)
    parser.add_argument("--db", type=str, default=":memory:")
    parser.add_argument("--out", type=str, required=True)
    parser.add_argument("--no-progress", action="store_true",
                         help="suppress the progress bar (it writes to stderr)")
    args = parser.parse_args()

    # None flows through to build_economy, which derives it from the
    # population -- deliberately not re-derived here.
    config = ScenarioConfig(
        n_individuals=args.individuals,
        n_firms=args.firms,
        tax_rate=Decimal(args.tax_rate),
        tax_threshold=Decimal(args.tax_threshold),
        estate_rule=args.estate_rule,
        redistribution_period=args.redistribution_period,
        smallholder_fraction=args.smallholder_fraction,
        seed=args.seed,
    )

    result = run_scenario(config, args.ticks, db_path=args.db,
                          metrics_every=args.metrics_every,
                          progress=not args.no_progress, progress_label="ticks")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))

    last = result["snapshots"][-1]
    print(f"ticks={args.ticks} wall_clock={result['wall_clock_seconds']:.2f}s "
          f"ticks/s={result['ticks_per_second']:.2f}")
    print(f"final gini={last['gini']:.4f} incapacitated={last['incapacitated_count']} "
          f"mean_hunger_satisfaction={last['mean_hunger_satisfaction']:.3f}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
