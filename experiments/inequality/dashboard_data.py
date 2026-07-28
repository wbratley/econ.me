"""Reduce a horizon.py result file to the compact payload a dashboard embeds.

The result JSON carries 120 summary rows of ~70 fields plus a full pairwise
analysis; a dashboard needs the per-seed values for the outcomes it plots, the
arm trajectories, and the p-values -- roughly a tenth of it. Artifacts are
self-contained (no fetch), so the payload is inlined into the page and its size
is the page's size.

Usage:
    .venv/bin/python -m experiments.inequality.dashboard_data \
        --in results/matrix_m20_n30_t250_s15.json --out /path/payload.json
"""

import argparse
import json
from pathlib import Path

from .horizon import OUTCOMES, _mean, _sd, _values
from .tipping import HORIZON_TICKS

# What the dashboard plots. Deaths are excluded from the charts and reported as
# a single assertion instead -- a chart of eight zeros is not a chart.
PLOTTED = [k for k in OUTCOMES if k != "deaths"]

# Two arms running the SAME policy on a different genesis draw. Not inferred:
# it rests on the judgement that an estate rule cannot do anything in a run
# where nobody dies, so `estate_heir` is `tax_flat` with `_assign_heirs`
# having consumed some rng draws before the firm scripts were wired. That
# makes it a free 15-seed null replicate, and the gap between the two is the
# floor below which no other arm difference on this page means anything.
NULL_REPLICATE = ("tax_flat", "estate_heir")


def _identical_groups(rows_by_arm: dict[str, list[dict]], tick: int) -> list[list[str]]:
    """Arms whose every seed agrees to the last bit. Detected rather than
    asserted, so the page cannot claim a collapse that is not in the data."""
    def sig(rows):
        return tuple(sorted(
            (r["seed"], k, r[k]) for r in rows for k in r if k.endswith(f"_at_{tick}")
        ))
    buckets: dict[tuple, list[str]] = {}
    for arm, rows in rows_by_arm.items():
        buckets.setdefault(sig(rows), []).append(arm)
    return [g for g in buckets.values() if len(g) > 1]


def build(result: dict) -> dict:
    rows_by_arm: dict[str, list[dict]] = result["rows"]
    config = result["config"]
    late = config["late_tick"]
    ticks = [t for t in HORIZON_TICKS if t <= config["ticks"]]
    analysis = result.get("analysis", {})

    outcomes = {}
    for key in PLOTTED:
        label, direction = OUTCOMES[key]
        per_arm = {}
        for arm, rows in rows_by_arm.items():
            xs = _values(rows, key, late)
            per_arm[arm] = {
                "values": [round(x, 6) for x in xs],
                "mean": _mean(xs), "sd": _sd(xs),
                "trajectory": [
                    (None if not (v := _values(rows, key, t)) else round(_mean(v), 6))
                    for t in ticks
                ],
            }
        pairs = analysis.get("outcomes", {}).get(key, {}).get("pairs", {})
        outcomes[key] = {
            "label": label,
            "direction": direction,
            "arms": per_arm,
            # Only the fields the page reads: diff/p at the late tick, and
            # whether the earlier tick agreed. `verdict` is horizon.py's.
            "pairs": {
                name: {
                    "diff": p["late"]["diff"], "p": p["late"]["p"],
                    "p_early": p["early"]["p"], "verdict": p["verdict"],
                }
                for name, p in pairs.items()
            },
            "order_rank_r": analysis.get("outcomes", {}).get(key, {}).get("order_rank_r"),
            "agree": analysis.get("outcomes", {}).get(key, {}).get("agree"),
            "total_pairs": analysis.get("outcomes", {}).get(key, {}).get("total_pairs"),
            "sign_flips": analysis.get("outcomes", {}).get(key, {}).get("sign_flips"),
        }

    deaths_all_zero = all(
        (r.get(f"deaths_at_{late}") or 0) == 0
        for rows in rows_by_arm.values() for r in rows
    )

    a, b = NULL_REPLICATE
    null_replicate = None
    if a in rows_by_arm and b in rows_by_arm:
        null_replicate = {
            "arms": [a, b],
            "by_outcome": {
                key: {
                    "a": _mean(_values(rows_by_arm[a], key, late)),
                    "b": _mean(_values(rows_by_arm[b], key, late)),
                    "gap": abs(_mean(_values(rows_by_arm[a], key, late))
                              - _mean(_values(rows_by_arm[b], key, late))),
                    "p": (outcomes[key]["pairs"].get(f"{a} vs {b}")
                          or outcomes[key]["pairs"].get(f"{b} vs {a}") or {}).get("p"),
                }
                for key in PLOTTED
            },
        }

    return {
        "config": config,
        "ticks": ticks,
        "wall_clock_seconds": result.get("wall_clock_seconds"),
        "bonferroni_alpha": analysis.get("bonferroni_alpha"),
        "n_runs": sum(len(r) for r in rows_by_arm.values()),
        "deaths_all_zero": deaths_all_zero,
        "identical_groups": _identical_groups(rows_by_arm, late),
        "null_replicate": null_replicate,
        "outcomes": outcomes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--in", dest="src", type=str, required=True)
    parser.add_argument("--out", dest="dst", type=str, required=True)
    args = parser.parse_args()

    payload = build(json.loads(Path(args.src).read_text()))
    text = json.dumps(payload, separators=(",", ":"))
    Path(args.dst).write_text(text)
    print(f"wrote {args.dst} ({len(text) / 1024:.0f} KB), "
          f"{payload['n_runs']} runs, {len(payload['outcomes'])} outcomes")


if __name__ == "__main__":
    main()
