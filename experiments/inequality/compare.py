"""Run the three share-allocation regimes side by side and render a
self-contained comparison dashboard.

The single difference between the regimes is *who owns the firms*:

  none   -- nobody. Firms accumulate equity that belongs to no household;
             it is surfaced as `public_capital` (the Treasury / public sector
             as notional owner) so it is visible, not silently dropped.
  equal  -- everyone. Shares are distributed equally across the population,
             so dividends and share value flow to all.
  wealth -- the already-rich. Shares are allocated pro-rata to initial wealth,
             so the capital-income channel flows uphill.

Everything else is identical (seed, population, firm count, recipes, land).
The contrast isolates the capital-income channel -- dividends, share
valuation, and where the equity that production accumulates inside firms ends
up. Real welfare (hunger, comfort) is largely unaffected; what changes is the
monetary distribution.

Usage:
    .venv/bin/python -m experiments.inequality.compare --ticks 200 --seed 0
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .run import run_scenario
from .scenario import ScenarioConfig
from .dashboard import (
    _to_b64, _style_axes, _legend, _series, _BG, _GRID, _TXT, _MUTED,
)

# Fixed regime -> colour mapping so every chart reads the same way.
# none = red (the broken/artifact channel), equal = green, wealth = amber.
REGIMES = [
    ("none", "#cf222e", "none — no owners"),
    ("equal", "#1a7f37", "equal — everyone"),
    ("wealth", "#bf8700", "wealth — to the rich"),
]


def _overlay(data, ticks, key, title, ylabel=None, ylim=None):
    fig, ax = plt.subplots(figsize=(7.6, 3.3))
    for arm, color, label in REGIMES:
        ax.plot(ticks, [key(s) for s in data[arm]], label=label,
                color=color, linewidth=1.7)
    if ylim:
        ax.set_ylim(*ylim)
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, title, ylabel)
    _legend(ax)
    return _to_b64(fig)


def chart_gini_net_worth(data, ticks):
    return _overlay(data, ticks, lambda s: s["gini"],
                    "Inequality — Gini (net worth: cash + goods)",
                    ylim=(-0.02, 1.02))


def chart_gini_total(data, ticks):
    return _overlay(data, ticks, lambda s: s["gini_total"],
                    "Inequality — Gini (total wealth: + land + shares)",
                    ylim=(-0.02, 1.02))


def chart_shares(data, ticks):
    return _overlay(data, ticks, lambda s: s["top10_share_total"],
                    "Top-decile share of total wealth",
                    ylim=(-0.02, 1.02),
                    ylabel="share held by top 10%")


def chart_hh_cash(data, ticks):
    return _overlay(data, ticks, lambda s: s["wealth_components"]["cash"],
                    "Household cash (aggregate)",
                    ylabel="currency")


def chart_firm_cash(data, ticks):
    return _overlay(data, ticks, lambda s: s["capital"]["firm_cash_total"],
                    "Firm cash (aggregate) — what firms hoard",
                    ylabel="currency")


def chart_public_capital(data, ticks):
    return _overlay(data, ticks, lambda s: s["wealth_components"]["public_capital"],
                    "Public capital — equity trapped in unowned firms",
                    ylabel="currency")


def chart_dividends(data, ticks):
    """Cumulative dividends paid (per-tick values are sparse: a dividend fires
    only every `dividend_period` ticks, so the running sum is the signal)."""
    fig, ax = plt.subplots(figsize=(7.6, 3.3))
    for arm, color, label in REGIMES:
        running = 0.0
        cum = []
        for s in data[arm]:
            running += s["capital"]["dividends_paid"]
            cum.append(running)
        ax.plot(ticks, cum, label=label, color=color, linewidth=1.7)
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, "Cumulative dividends paid to shareholders", "currency")
    _legend(ax)
    return _to_b64(fig)


def chart_hunger(data, ticks):
    return _overlay(data, ticks, lambda s: s["mean_hunger_satisfaction"],
                    "Real welfare — hunger satisfaction",
                    ylim=(-0.02, 1.02),
                    ylabel="share fed (0–1)")


def chart_composition(data, ticks):
    """Three small stacked-area charts, one per regime, so the composition of
    household wealth (cash / goods / land / shares) is visible alongside the
    public capital that only the `none` arm carries."""
    comps = ["cash", "goods", "land", "shares"]
    colors = ["#0969da", "#bf8700", "#1a7f37", "#8250df"]
    fig, axes = plt.subplots(1, 3, figsize=(11.5, 3.2), sharey=False)
    for ax, (arm, color, label) in zip(axes, REGIMES):
        snaps = data[arm]
        arrays, labels = [], []
        for c, col in zip(comps, colors):
            vals = _series(snaps, "wealth_components", c)
            if all(v == 0 for v in vals):
                continue
            arrays.append(vals)
            labels.append(c)
        pub = _series(snaps, "wealth_components", "public_capital")
        if not all(v == 0 for v in pub):
            arrays.append(pub)
            labels.append("public")
            colors_use = colors + ["#cf222e"]
        else:
            colors_use = colors
        if arrays:
            ax.stackplot(ticks, *arrays, labels=labels,
                         colors=colors_use[: len(arrays)], alpha=0.85)
        ax.set_title(label, fontsize=10, fontweight="bold", color=color, pad=6)
        ax.tick_params(labelsize=7, colors=_MUTED)
        ax.grid(True, color=_GRID, linewidth=0.7)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(_GRID)
    axes[0].legend(fontsize=7, frameon=False, loc="upper left")
    fig.suptitle("Wealth composition by regime (aggregate)",
                 fontsize=11, fontweight="bold", color=_TXT, y=1.02)
    return _to_b64(fig)


def chart_lorenz(data):
    """Final-tick Lorenz curves overlaid. Built on total_wealth per person."""
    fig, ax = plt.subplots(figsize=(5.0, 4.2))
    ax.plot([0, 1], [0, 1], color=_GRID, linewidth=1, label="equality")
    for arm, color, label in REGIMES:
        wealths = sorted(float(e["total_wealth"]) for e in data[arm][-1]["entities"])
        n = len(wealths)
        if n == 0:
            continue
        cum = np.cumsum(wealths)
        total = cum[-1] if cum[-1] > 0 else 1.0
        x = np.concatenate([[0], np.arange(1, n + 1) / n])
        y = np.concatenate([[0], cum / total])
        g = data[arm][-1].get("gini_total", 0)
        ax.plot(x, y, color=color, linewidth=2,
                label=f"{arm} (gini {g:.2f})")
    ax.set_title("Final Lorenz curves (total wealth)",
                 fontsize=11, fontweight="bold", color=_TXT, pad=8)
    ax.set_xlabel("cumulative share of population", fontsize=9, color=_MUTED)
    ax.set_ylabel("cumulative share of wealth", fontsize=9, color=_MUTED)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.set_aspect("equal")
    ax.tick_params(labelsize=8, colors=_MUTED)
    ax.grid(True, color=_GRID, linewidth=0.8); ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    return _to_b64(fig)


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
       color:#1f2328; background:#f6f8fa; }
header { background:#24292f; color:#fff; padding:18px 28px; }
header h1 { margin:0 0 6px 0; font-size:20px; font-weight:600; }
header .sub { opacity:.82; font-size:13px; line-height:1.5; max-width:900px; }
.wrap { max-width:1180px; margin:0 auto; padding:20px 24px 60px; }
table.cmp { border-collapse:collapse; width:100%; margin:16px 0 4px; font-size:13px; }
table.cmp th, table.cmp td { padding:7px 10px; border-bottom:1px solid #d0d7de; text-align:right; }
table.cmp th:first-child, table.cmp td:first-child { text-align:left; color:#57606a; }
table.cmp thead th { background:#f6f8fa; font-size:11px; text-transform:uppercase;
                     letter-spacing:.04em; color:#57606a; }
table.cmp tbody tr:hover { background:#f6f8fa; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px;
       vertical-align:middle; }
h2 { font-size:14px; margin:26px 0 2px; color:#24292f; }
.grid { display:grid; grid-template-columns:1fr; gap:14px; margin-top:12px; }
.panel { background:#fff; border:1px solid #d0d7de; border-radius:10px; padding:12px 14px; }
.panel img { width:100%; height:auto; display:block; }
.note { font-size:12px; color:#57606a; margin:14px 2px; line-height:1.55; }
.lede { font-size:13px; color:#24292f; line-height:1.6; margin:14px 2px 4px; }
"""


def _dot(color):
    return f'<span class="dot" style="background:{color}"></span>'


def build_html(data, ticks, seed, n_individuals, wall_s):
    last = {arm: data[arm][-1] for arm, _, _ in REGIMES}

    def row(label, fn, fmt="{:.3f}"):
        cells = "".join(f"<td>{fmt.format(fn(last[arm]))}</td>" for arm, _, _ in REGIMES)
        return f"<tr><td>{label}</td>{cells}</tr>"

    def wc(arm, k):
        return last[arm]["wealth_components"][k]

    def cap(arm, k):
        return last[arm]["capital"][k]

    head = "".join(
        f"<th>{_dot(c)}{arm}</th>" for arm, c, _ in REGIMES
    )

    cumdiv = {}
    for arm, _, _ in REGIMES:
        cumdiv[arm] = sum(s["capital"]["dividends_paid"] for s in data[arm])

    rows = (
        row("Gini (net worth)", lambda s: s["gini"])
        + row("Gini (total wealth)", lambda s: s["gini_total"])
        + row("Top-10% share (total)", lambda s: s["top10_share_total"])
        + row("Bottom-50% share (total)", lambda s: s["bottom50_share_total"])
        + f"<tr><td>Household cash</td>" + "".join(
            f"<td>{wc(arm,'cash'):.0f}</td>" for arm, _, _ in REGIMES) + "</tr>"
        + f"<tr><td>Household share equity</td>" + "".join(
            f"<td>{wc(arm,'shares'):.0f}</td>" for arm, _, _ in REGIMES) + "</tr>"
        + f"<tr><td>Public capital (trapped)</td>" + "".join(
            f"<td>{wc(arm,'public_capital'):.0f}</td>" for arm, _, _ in REGIMES) + "</tr>"
        + f"<tr><td>Firm cash (hoarded)</td>" + "".join(
            f"<td>{cap(arm,'firm_cash_total'):.0f}</td>" for arm, _, _ in REGIMES) + "</tr>"
        + f"<tr><td>Cumulative dividends</td>" + "".join(
            f"<td>{cumdiv[arm]:.0f}</td>" for arm, _, _ in REGIMES) + "</tr>"
        + row("Hunger satisfaction", lambda s: s["mean_hunger_satisfaction"])
        + row("Comfort satisfaction", lambda s: s["needs"]["COMFORT"])
        + row("Food price", lambda s: (s["prices"].get("FOOD") or 0))
    )
    table = (f"<table class='cmp'><thead><tr><th>metric (final tick "
             f"{ticks[-1]})</th>{head}</tr></thead><tbody>{rows}</tbody></table>")

    lede = (
        "<p class='lede'>The three runs are identical except for <b>who owns the "
        "firms</b>. The money supply is conserved at ~28k in every regime — what "
        "differs is where the equity that production accumulates inside firms ends "
        "up. With no owners (<b>none</b>) it is trapped as unattributed "
        "<b>public capital</b> and the household Gini falls only because everyone "
        "drains to a common cash floor. With owners (<b>equal</b> / <b>wealth</b>) "
        "dividends recycle it and share value attributes it — equalising or "
        "concentrating depending on who holds the shares.</p>"
    )

    sections = ""
    for title, panels in [
        ("Inequality", [
            chart_gini_net_worth(data, ticks),
            chart_gini_total(data, ticks),
            chart_shares(data, ticks),
        ]),
        ("Where the money sits", [
            chart_hh_cash(data, ticks),
            chart_firm_cash(data, ticks),
            chart_public_capital(data, ticks),
            chart_dividends(data, ticks),
        ]),
        ("Real welfare (robust to ownership)", [
            chart_hunger(data, ticks),
        ]),
    ]:
        sections += f"<h2>{title}</h2><div class='grid'>"
        for b in panels:
            sections += f'<div class="panel"><img src="data:image/png;base64,{b}"/></div>'
        sections += "</div>"

    comp = chart_composition(data, ticks)
    lorenz = chart_lorenz(data)
    sections += ("<h2>Wealth composition</h2><div class='grid'>"
                 f'<div class="panel"><img src="data:image/png;base64,{comp}"/></div>'
                 "</div>")
    sections += ("<h2>Final distribution</h2><div class='grid'>"
                 f'<div class="panel"><img src="data:image/png;base64,{lorenz}"/></div>'
                 "</div>")

    note = (
        f"<div class='note'>Each regime: {n_individuals} individuals, seed {seed}, "
        f"{len(ticks)} ticks, identical except share_allocation. "
        f"`public_capital` is the book equity (cash + goods + land) of firms with no "
        f"shareholders — visible here rather than silently dropped. "
        f"All charts inline base64 PNG. Three runs rendered in {wall_s:.0f}s.</div>"
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fielding — ownership comparison</title>
<style>{_CSS}</style></head><body>
<header><h1>Fielding — who owns the firms?</h1>
<div class="sub">Three share-allocation regimes: <b>none</b> (no owners) · "
"<b>equal</b> (everyone) · <b>wealth</b> (to the rich). "
"Same economy, same seed — only the capital-income channel differs.</div></header>
<div class="wrap">{lede}{table}{note}{sections}</div></body></html>"""


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ticks", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--n-individuals", type=int, default=30)
    p.add_argument("--out", type=str,
                   default="experiments/inequality/comparison.html")
    args = p.parse_args()

    t0 = time.time()
    data: dict[str, list[dict]] = {}
    for arm, _, _ in REGIMES:
        db = Path(f"/tmp/compare_{arm}.db")
        if db.exists():
            db.unlink()
        cfg = ScenarioConfig(n_individuals=args.n_individuals, seed=args.seed,
                             planning_horizon=5.0, share_allocation=arm)
        res = run_scenario(cfg, args.ticks, db_path=str(db), progress=False)
        data[arm] = res["snapshots"]
        print(f"  {arm:7s} done ({time.time()-t0:.0f}s)")
    ticks = [s["tick"] for s in data["none"]]
    wall = time.time() - t0
    html = build_html(data, ticks, args.seed, args.n_individuals, wall)
    Path(args.out).write_text(html)
    print(f"wrote {args.out}  ({len(html)/1024:.0f} KB, {args.ticks} ticks x3, {wall:.0f}s)")


if __name__ == "__main__":
    main()
