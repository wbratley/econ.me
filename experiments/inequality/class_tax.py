"""Run the tax-rate matrix on a two-class economy and render a comparison
dashboard showing how the fiscal structure changes the gap between the
ownership class and the working class.

The economy is fixed at share_allocation="class" -- the richest 20% own all
the firms, the rest are workers who live on wages -- and ONLY the tax rates
move across the arms:

  no tax      -- payroll 0,   capital 0    (the laissez-faire baseline)
  flat        -- payroll .15, capital .15  (same rate on both incomes)
  progressive -- payroll .15, capital .40  (capital income taxed harder)
  regressive  -- payroll .30, capital .05  (capital taxed lightly -- the
                                            real-world case; owners pull ahead)

Taxes are firm-withheld (see firm.lua): payroll on the wage bill, capital on
each dividend. The Treasury recycles what it collects as a flat UBI every few
ticks (treasury.lua). The question is what that fiscal loop does to the two
classes' trajectories -- not whether the economy "scores well", but how the
ownership/working split responds to who is taxed and how much.

Usage:
    .venv/bin/python -m experiments.inequality.class_tax --ticks 150 --seed 0
"""

from __future__ import annotations

import argparse
import time
from decimal import Decimal
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .run import run_scenario
from .scenario import ScenarioConfig
from .dashboard import _to_b64, _style_axes, _legend, _BG, _GRID, _TXT, _MUTED

# (key, payroll_rate, capital_rate, color, label)
TAX_REGIMES = [
    ("notax", 0.0, 0.0, "#6e7781", "no tax"),
    ("flat", 0.15, 0.15, "#0969da", "flat (labour 15, capital 15)"),
    ("progressive", 0.15, 0.40, "#1a7f37", "progressive (labour 15, capital 40)"),
    ("regressive", 0.30, 0.05, "#cf222e", "regressive (labour 30, capital 5)"),
]


def _class_split(snaps):
    """Per-tick (owner_mean_tw, worker_mean_tw, ratio). Owners are the
    shareholders (shares_value > 0); workers hold no shares."""
    o, w, r = [], [], []
    for s in snaps:
        ents = s["entities"]
        owners = [e for e in ents if e["shares_value"] > 0]
        workers = [e for e in ents if e["shares_value"] == 0]
        om = sum(e["total_wealth"] for e in owners) / len(owners) if owners else 0.0
        wm = sum(e["total_wealth"] for e in workers) / len(workers) if workers else 0.0
        o.append(om); w.append(wm)
        r.append(om / wm if wm > 1e-9 else 0.0)
    return o, w, r


def _overlay(data, ticks, series, title, ylabel=None, ylim=None):
    """series: list of (values_list, color, label)."""
    fig, ax = plt.subplots(figsize=(7.6, 3.3))
    for vals, color, label in series:
        ax.plot(ticks, vals, label=label, color=color, linewidth=1.7)
    if ylim:
        ax.set_ylim(*ylim)
    ax.set_xlabel("tick", fontsize=9, color=_MUTED)
    _style_axes(ax, title, ylabel)
    _legend(ax)
    return _to_b64(fig)


def _regime_series(data, extractor):
    """Build the (values, color, label) list for one line per tax regime."""
    out = []
    for key, _, _, color, label in TAX_REGIMES:
        out.append((extractor(data[key]), color, label))
    return out


def chart_owner(data, ticks):
    return _overlay(data, ticks, _regime_series(data, lambda d: _class_split(d)[0]),
                    "Ownership class — mean total wealth", ylabel="per owner")


def chart_worker(data, ticks):
    return _overlay(data, ticks, _regime_series(data, lambda d: _class_split(d)[1]),
                    "Working class — mean total wealth", ylabel="per worker")


def chart_ratio(data, ticks):
    return _overlay(data, ticks, _regime_series(data, lambda d: _class_split(d)[2]),
                    "Wealth gap — owner mean ÷ worker mean", ylabel="ratio (×)")


def chart_gini(data, ticks):
    return _overlay(data, ticks, _regime_series(data, lambda d: [s["gini_total"] for s in d]),
                    "Inequality — Gini (total wealth)", ylim=(-0.02, 1.02))


def chart_treasury(data, ticks):
    return _overlay(data, ticks, _regime_series(data, lambda d: [s["treasury_balance"] for s in d]),
                    "Treasury balance (tax revenue held, post-UBI)", ylabel="currency")


def chart_cumtax(data, ticks):
    def cum(d):
        run = 0.0; out = []
        for s in d:
            run += s["capital"]["payroll_tax_collected"] + s["capital"]["capital_tax_collected"]
            out.append(run)
        return out
    return _overlay(data, ticks, _regime_series(data, cum),
                    "Cumulative tax collected (payroll + capital)", ylabel="currency")


def chart_cumubi(data, ticks):
    def cum(d):
        run = 0.0; out = []
        for s in d:
            run += s["capital"]["ubi_paid"]
            out.append(run)
        return out
    return _overlay(data, ticks, _regime_series(data, cum),
                    "Cumulative UBI paid out (flat, to everyone)", ylabel="currency")


def chart_hunger(data, ticks):
    return _overlay(data, ticks, _regime_series(data, lambda d: [s["mean_hunger_satisfaction"] for s in d]),
                    "Real welfare — hunger satisfaction", ylim=(-0.02, 1.02),
                    ylabel="share fed (0–1)")


def chart_lorenz(data):
    fig, ax = plt.subplots(figsize=(5.0, 4.2))
    ax.plot([0, 1], [0, 1], color=_GRID, linewidth=1, label="equality")
    for key, _, _, color, label in TAX_REGIMES:
        wealths = sorted(float(e["total_wealth"]) for e in data[key][-1]["entities"])
        n = len(wealths)
        if n == 0:
            continue
        cum = np.cumsum(wealths)
        total = cum[-1] if cum[-1] > 0 else 1.0
        x = np.concatenate([[0], np.arange(1, n + 1) / n])
        y = np.concatenate([[0], cum / total])
        g = data[key][-1].get("gini_total", 0)
        ax.plot(x, y, color=color, linewidth=2, label=f"{label.split('(')[0].strip()} (gini {g:.2f})")
    ax.set_title("Final Lorenz curves (total wealth)", fontsize=11,
                 fontweight="bold", color=_TXT, pad=8)
    ax.set_xlabel("cumulative share of population", fontsize=9, color=_MUTED)
    ax.set_ylabel("cumulative share of wealth", fontsize=9, color=_MUTED)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.set_aspect("equal")
    ax.tick_params(labelsize=8, colors=_MUTED)
    ax.grid(True, color=_GRID, linewidth=0.8); ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(fontsize=7.5, frameon=False, loc="upper left")
    return _to_b64(fig)


_CSS = """
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
       color:#1f2328; background:#f6f8fa; }
header { background:#24292f; color:#fff; padding:18px 28px; }
header h1 { margin:0 0 6px 0; font-size:20px; font-weight:600; }
header .sub { opacity:.82; font-size:13px; line-height:1.5; max-width:980px; }
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
    last = {key: data[key][-1] for key, *_ in TAX_REGIMES}

    # final class split per regime
    def class_row(label, idx):
        cells = ""
        for key, *_ in TAX_REGIMES:
            o, w, r = _class_split([data[key][-1]])
            v = (o[0], w[0], r[0])[idx]
            cells += f"<td>{v:.0f}</td>" if idx < 2 else f"<td>{v:.1f}×</td>"
        return f"<tr><td>{label}</td>{cells}</tr>"

    head = "".join(f"<th>{_dot(c)}{lab}</th>" for _, _, _, c, lab in TAX_REGIMES)

    def cum(key, k):
        return sum(s["capital"][k] for s in data[key])

    rows = (
        class_row("Owner mean total wealth", 0)
        + class_row("Worker mean total wealth", 1)
        + class_row("Owner/worker wealth ratio", 2)
        + f"<tr><td>Gini (total wealth)</td>" + "".join(
            f"<td>{last[k]['gini_total']:.3f}</td>" for k, *_ in TAX_REGIMES) + "</tr>"
        + f"<tr><td>Treasury balance</td>" + "".join(
            f"<td>{last[k]['treasury_balance']:.0f}</td>" for k, *_ in TAX_REGIMES) + "</tr>"
        + f"<tr><td>Cumulative tax collected</td>" + "".join(
            f"<td>{cum(k,'payroll_tax_collected')+cum(k,'capital_tax_collected'):.0f}</td>"
            for k, *_ in TAX_REGIMES) + "</tr>"
        + f"<tr><td>  — payroll</td>" + "".join(
            f"<td>{cum(k,'payroll_tax_collected'):.0f}</td>" for k, *_ in TAX_REGIMES) + "</tr>"
        + f"<tr><td>  — capital</td>" + "".join(
            f"<td>{cum(k,'capital_tax_collected'):.0f}</td>" for k, *_ in TAX_REGIMES) + "</tr>"
        + f"<tr><td>Cumulative UBI paid</td>" + "".join(
            f"<td>{cum(k,'ubi_paid'):.0f}</td>" for k, *_ in TAX_REGIMES) + "</tr>"
    )
    # welfare rows
    rows += (
        f"<tr><td>Hunger satisfaction</td>" + "".join(
            f"<td>{last[k]['mean_hunger_satisfaction']:.3f}</td>" for k, *_ in TAX_REGIMES) + "</tr>"
        + f"<tr><td>Comfort satisfaction</td>" + "".join(
            f"<td>{last[k]['needs']['COMFORT']:.3f}</td>" for k, *_ in TAX_REGIMES) + "</tr>"
        + f"<tr><td>Food price</td>" + "".join(
            f"<td>{(last[k]['prices'].get('FOOD') or 0):.2f}</td>" for k, *_ in TAX_REGIMES) + "</tr>"
    )
    table = (f"<table class='cmp'><thead><tr><th>metric (final tick "
             f"{ticks[-1]})</th>{head}</tr></thead><tbody>{rows}</tbody></table>")

    lede = (
        "<p class='lede'>Fixed two-class economy — the richest 20% own all firms, "
        "the rest are workers. Only the <b>tax rates</b> move. Payroll tax is "
        "firm-withheld on the wage bill (borne by workers via lower wages); capital "
        "tax is withheld from each dividend (borne by owners). The Treasury recycles "
        "it all as a flat UBI. The question: how does <b>who gets taxed</b> change the "
        "gap between the classes?</p>"
    )

    def row(title, chart):
        return f'<div class="panel"><img src="data:image/png;base64,{chart}"/></div>'

    sections = ""
    for title, panels in [
        ("The class gap", [
            chart_owner(data, ticks),
            chart_worker(data, ticks),
            chart_ratio(data, ticks),
            chart_gini(data, ticks),
        ]),
        ("The fiscal loop", [
            chart_treasury(data, ticks),
            chart_cumtax(data, ticks),
            chart_cumubi(data, ticks),
        ]),
        ("Real welfare", [chart_hunger(data, ticks)]),
    ]:
        sections += f"<h2>{title}</h2><div class='grid'>"
        for b in panels:
            sections += row(title, b)
        sections += "</div>"
    sections += ("<h2>Final distribution</h2><div class='grid'>"
                 + row("lorenz", chart_lorenz(data)) + "</div>")

    note = (
        f"<div class='note'>Each arm: {n_individuals} individuals, seed {seed}, "
        f"share_allocation=\"class\" (top 20% own all firms), {len(ticks)} ticks. "
        f"Only payroll_tax_rate / capital_tax_rate differ. Taxes are firm-withheld "
        f"(ownership invariant respected); UBI is flat. Four runs in {wall_s:.0f}s.</div>"
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fielding — tax-rate matrix</title>
<style>{_CSS}</style></head><body>
<header><h1>Fielding — who pays? (the tax-rate matrix)</h1>
<div class="sub">A two-class economy under four fiscal regimes: <b>no tax</b>, "
"<b>flat</b>, <b>progressive</b> (capital taxed harder), "
"<b>regressive</b> (capital taxed lightly — the real-world case). "
"Same firms, same workers, same seed — only the rates move.</div></header>
<div class="wrap">{lede}{table}{note}{sections}</div></body></html>"""


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--ticks", type=int, default=150)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--n-individuals", type=int, default=30)
    p.add_argument("--out", type=str,
                   default="experiments/inequality/class_tax.html")
    args = p.parse_args()

    t0 = time.time()
    data: dict[str, list[dict]] = {}
    for key, payroll, capital, _, label in TAX_REGIMES:
        db = Path(f"/tmp/ct_{key}.db")
        if db.exists():
            db.unlink()
        cfg = ScenarioConfig(
            n_individuals=args.n_individuals, seed=args.seed, planning_horizon=5.0,
            share_allocation="class",
            payroll_tax_rate=Decimal(str(payroll)),
            capital_tax_rate=Decimal(str(capital)),
        )
        res = run_scenario(cfg, args.ticks, db_path=str(db), progress=False)
        data[key] = res["snapshots"]
        print(f"  {key:12s} ({label}) done ({time.time()-t0:.0f}s)")
    ticks = [s["tick"] for s in data["notax"]]
    wall = time.time() - t0
    html = build_html(data, ticks, args.seed, args.n_individuals, wall)
    Path(args.out).write_text(html)
    print(f"wrote {args.out}  ({len(html)/1024:.0f} KB, {args.ticks} ticks x4, {wall:.0f}s)")


if __name__ == "__main__":
    main()
