"""Run the inequality scenario once and render a self-contained HTML
dashboard of the full time-series: needs, prices, production, land use,
inequality, builds, research, wealth, and firm solvency.

No external charting service: every figure is drawn with matplotlib and
inlined as base64 PNG, so the single .html file is portable and opens in any
browser. The run is the same path calibrate.py / horizon.py use, so the page
shows a real simulation, not a stub.

Usage:
    .venv/bin/python -m experiments.inequality.dashboard \
        --ticks 120 --seed 0 --out dashboard.html
"""

from __future__ import annotations

import argparse
import base64
import io
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
import numpy as np

from .run import run_scenario
from .scenario import ScenarioConfig


# ---------------------------------------------------------------------------
# data collection
# ---------------------------------------------------------------------------

def _series(snapshots: list[dict], *path, default=float("nan")) -> list[float]:
    """Walk a nested key path through each snapshot, returning a float list."""
    out = []
    for s in snapshots:
        v = s
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
        out.append(float(v) if v is not None else default)
    return out


def _price_series(snapshots, symbol):
    return [float(s["prices"].get(symbol)) if s["prices"].get(symbol) else float("nan")
            for s in snapshots]


def collect_events(db_path: str) -> dict:
    """Per-tick cumulative counts of build/research outcomes, straight from
    the event log (the snapshot carries stocks, not process lifecycles)."""
    from collections import defaultdict
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from econengine.models import Tick

    per_tick: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ticks_seen: set[int] = set()
    with Session(create_engine(f"sqlite:///{db_path}")) as s:
        for t in s.execute(select(Tick).order_by(Tick.number)).scalars():
            ticks_seen.add(t.number)
            bucket = per_tick[t.number]
            for e in (t.events or []):
                et = e.get("type")
                recipe = (e.get("params") or {}).get("recipe") or e.get("recipe")
                if et == "start_process" and e.get("status") == "applied":
                    bucket[f"{recipe}_started"] += 1
                elif et == "process_completed":
                    bucket[f"{recipe}_completed"] += 1
                elif et == "process_failed":
                    bucket[f"{recipe}_failed"] += 1
    return {"per_tick": per_tick, "max_tick": max(ticks_seen)}


def _cumulative(per_tick: dict, key: str, ticks: list[int]) -> list[float]:
    running = 0
    out = []
    for t in ticks:
        running += per_tick.get(t, {}).get(key, 0)
        out.append(running)
    return out


# ---------------------------------------------------------------------------
# charting helpers
# ---------------------------------------------------------------------------

_BG = "#ffffff"
_GRID = "#e6e8ec"
_TXT = "#1f2328"
_MUTED = "#57606a"


def _style_axes(ax, title, ylabel=None):
    ax.set_title(title, fontsize=11, fontweight="bold", color=_TXT, pad=8)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=9, color=_MUTED)
    ax.tick_params(labelsize=8, colors=_MUTED)
    ax.grid(True, color=_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(_GRID)


def _legend(ax, loc="best"):
    ax.legend(fontsize=8, frameon=False, loc=loc)


def _to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


PALETTE = ["#0969da", "#1a7f37", "#bf8700", "#cf222e", "#8250df", "#0550ae"]


# ---------------------------------------------------------------------------
# individual charts -> base64
# ---------------------------------------------------------------------------

def chart_welfare(snaps, ticks):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    needs = {c: _series(snaps, "needs", c) for c in ("HUNGER", "SHELTER", "POWER", "COMFORT")}
    for (c, vals), color in zip(needs.items(), PALETTE):
        ax.plot(ticks, vals, label=c.capitalize(), color=color, linewidth=1.6)
    ax.set_ylim(-0.02, 1.02)
    _style_axes(ax, "Need satisfaction (share of population, 0–1)")
    _legend(ax)
    return _to_b64(fig)


def chart_welfare_detail(snaps, ticks):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    hunger = _series(snaps, "mean_hunger_satisfaction")
    incap = _series(snaps, "incapacitated_count")
    cond = _series(snaps, "cond_weak_carriers")
    ax.plot(ticks, hunger, label="Hunger satisfaction", color=PALETTE[0], linewidth=1.8)
    ax.set_ylim(-0.02, 1.02)
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, "Hunger & destitution", "satisfaction")
    ax2 = ax.twinx()
    ax2.plot(ticks, incap, label="Incapacitated", color=PALETTE[3], linewidth=1.3, linestyle="--")
    ax2.plot(ticks, cond, label="COND-WEAK carriers", color=PALETTE[5], linewidth=1.3, linestyle=":")
    ax2.tick_params(labelsize=8, colors=_MUTED)
    ax2.set_ylabel("people", fontsize=9, color=_MUTED)
    for spine in ("top",):
        ax2.spines[spine].set_visible(False)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=8, frameon=False, loc="lower right")
    return _to_b64(fig)


def chart_prices(snaps, ticks):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    symbols = ["FOOD", "LABOR", "CLOTHES", "SHELTER", "ENERGY", "TOOLS"]
    plotted = []
    for i, sym in enumerate(symbols):
        vals = _price_series(snaps, sym)
        if all(math.isnan(v) for v in vals):
            continue
        ax.plot(ticks, vals, label=sym, color=PALETTE[i % len(PALETTE)], linewidth=1.4)
        plotted.append(sym)
    ax.set_yscale("log")
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, "Market prices (log scale)")
    if plotted:
        _legend(ax)
    return _to_b64(fig)


def chart_real_wage(snaps, ticks):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    wage = _price_series(snaps, "LABOR")
    food = _price_series(snaps, "FOOD")
    real = [w / f if (f and f > 0 and not math.isnan(w)) else float("nan") for w, f in zip(wage, food)]
    ax.plot(ticks, real, color=PALETTE[1], linewidth=1.8, label="food units per wage-hour")
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, "Real wage  (wage ÷ food price)", "food / hour")
    _legend(ax)
    return _to_b64(fig)


def chart_production(snaps, ticks):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    for i, sym in enumerate(["FOOD", "CLOTHES", "TOOLS"]):
        vals = _series(snaps, "produced", sym)
        if all(v == 0 or math.isnan(v) for v in vals):
            continue
        ax.plot(ticks, vals, label=sym, color=PALETTE[i], linewidth=1.5)
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, "Production per tick")
    _legend(ax)
    return _to_b64(fig)


def chart_land_use(snaps, ticks):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    cats = ["FARM", "DWELLING", "POWER-PLANT", "BARE"]
    arrays = []
    labels = []
    colors = [PALETTE[1], PALETTE[3], PALETTE[4], _GRID]
    for i, c in enumerate(cats):
        vals = _series(snaps, "land_use", c)
        if all(v == 0 for v in vals):
            continue
        arrays.append(vals)
        labels.append(c)
    if arrays:
        ax.stackplot(ticks, *arrays, labels=labels,
                     colors=colors[: len(arrays)], alpha=0.85)
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    _style_axes(ax, "Land use (parcels)")
    _legend(ax, loc="center right")
    return _to_b64(fig)


def chart_inequality(snaps, ticks):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    ax.plot(ticks, _series(snaps, "gini"), label="Gini (net worth)", color=PALETTE[0], linewidth=1.8)
    ax.plot(ticks, _series(snaps, "gini_total"), label="Gini (total wealth)", color=PALETTE[3], linewidth=1.5)
    ax.plot(ticks, _series(snaps, "top10_share"), label="Top 10% share", color=PALETTE[2], linewidth=1.3)
    ax.plot(ticks, _series(snaps, "bottom50_share"), label="Bottom 50% share", color=PALETTE[5], linewidth=1.3)
    ax.set_ylim(-0.02, 1.02)
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, "Wealth inequality")
    _legend(ax)
    return _to_b64(fig)


def chart_builds(snaps, ticks, ev):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    pt = ev["per_tick"]
    bf_start = _cumulative(pt, "BUILD_FARM_started", ticks)
    bf_done = _cumulative(pt, "BUILD_FARM_completed", ticks)
    bf_fail = _cumulative(pt, "BUILD_FARM_failed", ticks)
    ax.plot(ticks, bf_start, label="BUILD_FARM started", color=PALETTE[4], linewidth=1.6)
    ax.plot(ticks, bf_done, label="BUILD_FARM completed", color=PALETTE[1], linewidth=1.8)
    ax.plot(ticks, bf_fail, label="BUILD_FARM failed", color=PALETTE[3], linewidth=1.4, linestyle="--")
    agron = _cumulative(pt, "RESEARCH_AGRONOMY_completed", ticks)
    ax.plot(ticks, agron, label="AGRONOMY researched", color=PALETTE[0], linewidth=1.8, linestyle=":")
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    _style_axes(ax, "Capital formation & research (cumulative)")
    _legend(ax)
    return _to_b64(fig)


def chart_wealth_components(snaps, ticks):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    comps = [("cash", PALETTE[0]), ("goods", PALETTE[2]), ("land", PALETTE[1]),
             ("shares", PALETTE[5]), ("public_capital", "#cf222e")]
    arrays, labels, colors = [], [], []
    for c, col in comps:
        vals = _series(snaps, "wealth_components", c)
        if all(v == 0 for v in vals):
            continue
        arrays.append(vals); labels.append(c); colors.append(col)
    if arrays:
        ax.stackplot(ticks, *arrays, labels=labels, colors=colors, alpha=0.85)
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, "Household wealth components + public capital (aggregate)")
    _legend(ax, loc="upper left")
    return _to_b64(fig)


def chart_firms(snaps, ticks):
    fig, ax = plt.subplots(figsize=(7, 3.2))
    ax.plot(ticks, _series(snaps, "capital", "firms_solvent"),
            label="firms solvent", color=PALETTE[1], linewidth=1.8)
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, "Firm solvency & cash", "firms")
    ax.set_ylim(bottom=0)
    ax2 = ax.twinx()
    ax2.plot(ticks, _series(snaps, "capital", "firm_cash_total"),
             label="firm cash (total)", color=PALETTE[0], linewidth=1.3)
    ax2.plot(ticks, _series(snaps, "treasury_balance"),
             label="treasury balance", color=PALETTE[3], linewidth=1.3, linestyle="--")
    ax2.tick_params(labelsize=8, colors=_MUTED)
    ax2.set_ylabel("currency", fontsize=9, color=_MUTED)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=8, frameon=False, loc="center right")
    return _to_b64(fig)


def chart_lorenz(snaps):
    final = snaps[-1]
    wealths = sorted(float(e["total_wealth"]) for e in final["entities"])
    n = len(wealths)
    if n == 0:
        return None
    cum = np.cumsum(wealths)
    total = cum[-1] if cum[-1] > 0 else 1.0
    x = np.concatenate([[0], np.arange(1, n + 1) / n])
    y = np.concatenate([[0], cum / total])
    fig, ax = plt.subplots(figsize=(4.2, 3.4))
    ax.plot([0, 1], [0, 1], color=_GRID, linewidth=1)
    ax.plot(x, y, color=PALETTE[0], linewidth=2)
    ax.fill_between(x, x, y, color=PALETTE[0], alpha=0.12)
    gini = final.get("gini_total", 0)
    ax.set_title(f"Final Lorenz curve (total wealth)\nGini = {gini:.3f}",
                 fontsize=11, fontweight="bold", color=_TXT, pad=8)
    ax.set_xlabel("cumulative share of population", fontsize=9, color=_MUTED)
    ax.set_ylabel("cumulative share of wealth", fontsize=9, color=_MUTED)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    ax.tick_params(labelsize=8, colors=_MUTED)
    ax.grid(True, color=_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    return _to_b64(fig)


# ---------------------------------------------------------------------------
# html assembly
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
       color:#1f2328; background:#f6f8fa; }
header { background:#24292f; color:#fff; padding:18px 28px; }
header h1 { margin:0 0 6px 0; font-size:20px; font-weight:600; }
header .sub { opacity:.8; font-size:13px; }
.wrap { max-width:1180px; margin:0 auto; padding:20px 24px 60px; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:18px 0 8px; }
.stat { background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:10px 12px; }
.stat .k { font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:#57606a; }
.stat .v { font-size:18px; font-weight:600; margin-top:3px; }
.stat .v.good { color:#1a7f37; } .stat .v.bad { color:#cf222e; }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:16px; }
.panel { background:#fff; border:1px solid #d0d7de; border-radius:10px; padding:12px 14px; }
.panel img { width:100%; height:auto; display:block; }
.panel.full { grid-column:1 / -1; }
.note { font-size:12px; color:#57606a; margin:14px 2px; line-height:1.5; }
h2 { font-size:14px; margin:24px 0 2px; color:#24292f; }
@media (max-width:820px){ .grid{grid-template-columns:1fr} }
"""


def _stat(label, value, cls=""):
    return f'<div class="stat"><div class="k">{label}</div><div class="v {cls}">{value}</div></div>'


def _panel(b64, full=False):
    return f'<div class="panel{" full" if full else ""}"><img src="data:image/png;base64,{b64}"/></div>'


def build_html(result, ev, config, ticks, wall_s):
    snaps = result["snapshots"]
    last = snaps[-1]
    pf = last["prices"].get("FOOD") or 0
    wage = last["prices"].get("LABOR") or 0
    real = (wage / pf) if pf else 0
    hunger = last["mean_hunger_satisfaction"]
    gini = last["gini"]
    farm = last["land_use"].get("FARM", 0)
    bare = last["land_use"].get("BARE", 0)
    food_prod = last["produced"].get("FOOD", 0)
    pt = ev["per_tick"]
    builds_done = _cumulative(pt, "BUILD_FARM_completed", ticks)[-1] if ticks else 0
    agron = _cumulative(pt, "RESEARCH_AGRONOMY_completed", ticks)[-1] if ticks else 0
    incap = last["incapacitated_count"]

    hunger_cls = "good" if hunger >= 0.95 else ("bad" if hunger < 0.7 else "")
    stats = (
        _stat("Final hunger", f"{hunger:.3f}", hunger_cls)
        + _stat("Gini (net worth)", f"{gini:.3f}")
        + _stat("Gini (total wealth)", f"{last['gini_total']:.3f}")
        + _stat("Real wage", f"{real:.2f}")
        + _stat("Food price", f"{pf:.2f}")
        + _stat("Wage", f"{wage:.2f}")
        + _stat("Food / tick", f"{food_prod:.1f}")
        + _stat("Farm parcels", f"{farm}  (bare {bare})")
        + _stat("Builds done", f"{int(builds_done)}")
        + _stat("AGRONOMY", f"{int(agron)} firm(s)")
        + _stat("Incapacitated", f"{incap}", "bad" if incap else "good")
    )

    charts = [
        ("Welfare", [
            _panel(chart_welfare(snaps, ticks)),
            _panel(chart_welfare_detail(snaps, ticks)),
        ]),
        ("Markets & production", [
            _panel(chart_prices(snaps, ticks)),
            _panel(chart_real_wage(snaps, ticks)),
            _panel(chart_production(snaps, ticks)),
            _panel(chart_land_use(snaps, ticks)),
        ]),
        ("Capital formation", [
            _panel(chart_builds(snaps, ticks, ev)),
            _panel(chart_firms(snaps, ticks)),
        ]),
        ("Distribution", [
            _panel(chart_inequality(snaps, ticks)),
            _panel(chart_wealth_components(snaps, ticks)),
        ]),
    ]

    lorenz = chart_lorenz(snaps)
    sections = ""
    for title, panels in charts:
        sections += f"<h2>{title}</h2><div class='grid'>{''.join(panels)}</div>"

    note = (
        f"<div class='note'>Scenario: {config.n_individuals} individuals, seed {config.seed}, "
        f"{config.n_firms} firms, {len(ticks)} ticks. "
        f"Build mechanic: per-tick BUILD_FARM (idle one field while building + a "
        f"per-tick labour feeding tranche). Rendered in {wall_s:.1f}s. "
        f"All charts are inline base64 PNG — this file is fully self-contained.</div>"
    )

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fielding — run dashboard (seed {config.seed})</title>
<style>{_CSS}</style></head><body>
<header><h1>Fielding — synthetic economy run</h1>
<div class="sub">{config.n_individuals} individuals · {config.n_firms} firms · "
"seed {config.seed} · {len(ticks)} ticks</div></header>
<div class="wrap">{stats}{note}{sections}"""
    if lorenz:
        html += f"<h2>Final distribution</h2><div class='grid'>{_panel(lorenz)}</div>"
    html += "</div></body></html>"
    return html


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ticks", type=int, default=120)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--n-individuals", type=int, default=30)
    p.add_argument("--db", type=str, default="/tmp/fielding_dashboard.db")
    p.add_argument("--out", type=str, default="experiments/inequality/dashboard.html")
    args = p.parse_args()

    import time
    db = Path(args.db)
    if db.exists():
        db.unlink()
    t0 = time.time()
    config = ScenarioConfig(n_individuals=args.n_individuals, seed=args.seed,
                            planning_horizon=5.0)
    result = run_scenario(config, args.ticks, db_path=args.db, progress=False)
    wall = time.time() - t0
    ev = collect_events(args.db)
    snaps = result["snapshots"]
    ticks = [s["tick"] for s in snaps]
    html = build_html(result, ev, config, ticks, wall)
    Path(args.out).write_text(html)
    print(f"wrote {args.out}  ({len(html)/1024:.0f} KB, {args.ticks} ticks, {wall:.1f}s)")


if __name__ == "__main__":
    main()
