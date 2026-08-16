"""The run dashboard: one self-contained HTML file from the snapshots.

No server, no CDN, no JS framework — inline SVG charts and honest tables,
so the artifact outlives the run: drop it on any disk or browser and the
whole story is there. Data view doctrine follows the platform's: every
number is what the dynasties themselves could see on their own MCP
surface (§13 parity), which is exactly what `multi.run_rounds` snapshots.

Sections: final standings; wealth / money / prices / needs charts over
rounds; a round-by-round activity table (attempts, refusals, the round's
event mix); and per-dynasty strategy panels — the latest behaviour
source with the sha trail of every rewrite, so "what is House Llama
doing?" is a scroll, not a query.
"""

from __future__ import annotations

import html
from decimal import Decimal
from pathlib import Path

from .multi import dynasty_assets, dynasty_money, price_table

PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2"]


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt(d: Decimal) -> str:
    q = d.quantize(Decimal("0.01"))
    return f"{q:,}"


# ---------------------------------------------------------------------------
# Charts (inline SVG; y-scaled, gridded, legended)
# ---------------------------------------------------------------------------

def line_chart(title: str, labels: list[str],
               series: dict[str, list[Decimal]], height: int = 260) -> str:
    """One polyline per series over the round labels. Empty series list
    renders a quiet placeholder — a world with no trades still deserves
    its section."""
    width, left, right, top, bottom = 860, 64, 16, 28, 34
    plot_w, plot_h = width - left - right, height - top - bottom

    def xs(i: int) -> float:
        return left + (plot_w * i / max(1, len(labels) - 1))

    def ys(v: float, lo: float, hi: float) -> float:
        span = (hi - lo) or Decimal("1")
        return top + plot_h - plot_h * (v - lo) / float(span)

    parts = [f'<div class="chart"><h3>{_esc(title)}</h3>']
    if not any(series.values()):
        parts.append('<p class="quiet">(no data)</p></div>')
        return "".join(parts)

    values = [v for vals in series.values() for v in vals]
    lo, hi = min(values), max(values)
    if lo == hi:                          # flat lines still need a band
        lo, hi = lo - Decimal("1"), hi + Decimal("1")

    parts.append(f'<svg viewBox="0 0 {width} {height}" role="img">')
    for g in range(5):                    # horizontal grid + y labels
        v = lo + (hi - lo) * g / 4
        y = ys(float(v), float(lo), float(hi))
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" '
                     f'y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" '
                     f'class="tick" text-anchor="end">{_fmt(Decimal(v))}</text>')
    for i, lab in enumerate(labels):      # x labels
        parts.append(f'<text x="{xs(i):.1f}" y="{height - 10}" '
                     f'class="tick" text-anchor="middle">{_esc(lab)}</text>')
    for color_i, (name, vals) in enumerate(series.items()):
        color = PALETTE[color_i % len(PALETTE)]
        pts = " ".join(f"{xs(i):.1f},{ys(float(v), float(lo), float(hi)):.1f}"
                       for i, v in enumerate(vals))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" '
                     f'stroke-width="2.2"/>')
        last_x, last_y = xs(len(vals) - 1), ys(float(vals[-1]), float(lo), float(hi))
        parts.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.4" '
                     f'fill="{color}"/>')
    parts.append("</svg><div class=\"legend\">")
    for color_i, name in enumerate(series):
        color = PALETTE[color_i % len(PALETTE)]
        parts.append(f'<span><i style="background:{color}"></i>{_esc(name)}</span>')
    parts.append("</div></div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Tables and panels
# ---------------------------------------------------------------------------

def _standings(snapshots: list[dict]) -> str:
    last = snapshots[-1]
    rows = []
    for name, view in last["dynasties"].items():
        prices = price_table(last["market"])
        money = dynasty_money(view)
        assets = dynasty_assets(view, prices)
        lb = view.get("leaderboard") or {}
        shas = [snap["dynasties"][name]["behaviour"]["sha"]
                for snap in snapshots
                if snap["dynasties"][name]["behaviour"]["sha"]]
        rewrites = len(set(shas)) - 1 if shas else 0
        refusals = sum(
            1 for snap in snapshots
            if (snap["dynasties"][name].get("entry") or {}).get("kept_old"))
        rows.append((money + assets, name, view, money, assets, lb,
                     rewrites, refusals))
    rows.sort(key=lambda r: (-r[0], r[1]))

    parts = ['<h2>Final standings</h2><table class="grid">',
             "<tr><th>#</th><th>Dynasty</th><th>Model</th><th>Money</th>"
             "<th>Assets (last px)</th><th>Wealth</th><th>Unlocks</th>"
             "<th>Rewrites</th><th>Kept-old rounds</th><th>Status</th></tr>"]
    for rank, (wealth, name, view, money, assets, lb, rewrites, refusals) in enumerate(rows, 1):
        parts.append(
            f"<tr><td>{rank}</td>"
            f"<td class=\"name\">{_esc(name)}</td>"
            f"<td class=\"quiet\">{_esc(view.get('model', ''))}</td>"
            f"<td class=\"num\">{_fmt(money)}</td>"
            f"<td class=\"num\">{_fmt(assets)}</td>"
            f"<td class=\"num strong\">{_fmt(wealth)}</td>"
            f"<td class=\"num\">{lb.get('unlocks', 0)}</td>"
            f"<td class=\"num\">{rewrites}</td>"
            f"<td class=\"num\">{refusals}</td>"
            f"<td>{_esc(lb.get('status', ''))}</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def _activity(snapshots: list[dict]) -> str:
    parts = ['<h2>Round by round</h2><table class="grid">',
             "<tr><th>Round</th><th>Ticks</th><th>Events</th>"
             "<th colspan=99>Dynasty cycles (attempts / outcome)</th></tr>"]
    for snap in snapshots:
        kinds = ", ".join(f"{k}×{v}" for k, v in
                          sorted(snap.get("events_by_type", {}).items()))
        parts.append(f"<tr><td>{snap['round']}</td>"
                     f"<td>{snap['ticks'][0] if snap['ticks'] else '?'}–"
                     f"{snap['ticks'][-1] if snap['ticks'] else '?'}</td>"
                     f"<td class=\"quiet\">{_esc(kinds or 'quiet')}</td>")
        for name, view in snap["dynasties"].items():
            entry = view.get("entry") or {}
            if entry.get("accepted"):
                cell = f"{entry.get('attempts', '?')}✓"
            else:
                why = entry.get("refusal") or "—"
                cell = f"{entry.get('attempts', '?')}✗ ({why[:60]})"
            cls = "ok" if entry.get("accepted") else "bad"
            parts.append(f'<td class="{cls}"><span class="quiet">'
                         f'{_esc(name)}</span><br>{_esc(cell)}</td>')
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _strategy(snapshots: list[dict]) -> str:
    parts = ["<h2>Strategy — the behaviour each house is running</h2>"]
    for name, view in snapshots[-1]["dynasties"].items():
        parts.append(
            f"<details open><summary><b>{_esc(name)}</b> "
            f"<span class=\"quiet\">({_esc(view.get('model', ''))})</span></summary>")
        parts.append('<div class="sha-trail">')
        for snap in snapshots:
            b = snap["dynasties"][name]["behaviour"]
            entry = snap["dynasties"][name].get("entry") or {}
            cls = "sha-ok" if entry.get("accepted") else "sha-bad"
            changed = ""
            if snap["round"] > 1:
                prev = snapshots[snap["round"] - 2]["dynasties"][name][
                    "behaviour"]["sha"]
                changed = " sha-new" if prev != b["sha"] else ""
            parts.append(f'<span class="{cls}{changed}" title="round '
                         f'{snap["round"]}">{_esc(b["sha"] or "—")}</span>')
        parts.append("</div>")
        b = view["behaviour"]
        if b.get("state") is not None:
            parts.append(f'<p class="quiet">behaviour state: '
                         f'<code>{_esc(b["state"])}</code></p>')
        parts.append(f'<pre class="lua">{_esc(b.get("source") or "(none)")}</pre>')
        parts.append("</details>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def build_dashboard(snapshots: list[dict], meta: dict) -> str:
    """Assemble the full HTML from per-round snapshots + run metadata."""
    labels = [f"R{s['round']}" for s in snapshots]
    names = list(snapshots[0]["dynasties"].keys()) if snapshots else []

    def collect(fn) -> dict[str, list[Decimal]]:
        return {n: [fn(s, s["dynasties"][n]) for s in snapshots] for n in names}

    wealth = collect(lambda s, v: dynasty_money(v)
                     + dynasty_assets(v, price_table(s["market"])))
    money = collect(lambda s, v: dynasty_money(v))
    needs = collect(lambda s, v: next(
        (Decimal(fd["satisfaction"]) for fd in v.get("needs", [])
         if fd.get("need") == "FOOD"), Decimal("0")))
    symbols = sorted({m["symbol"] for s in snapshots for m in s["market"]
                      if m.get("last_price") is not None})
    prices = {sym: [next((Decimal(m["last_price"]) for m in s["market"]
                          if m["symbol"] == sym), Decimal("0"))
                    for s in snapshots] for sym in symbols}

    houses = " · ".join(
        f"{_esc(n)} ({_esc(snapshots[-1]['dynasties'][n].get('model', ''))})"
        for n in names)
    css = """
      body{font:14px/1.45 -apple-system,'Segoe UI',sans-serif;margin:0;
           background:#0f1115;color:#e5e7eb;padding:28px 34px}
      h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:30px 0 10px;
           border-bottom:1px solid #2a2f3a;padding-bottom:6px}
      .quiet{color:#8b93a3}.meta{color:#8b93a3;margin-bottom:6px}
      table.grid{border-collapse:collapse;margin:8px 0;width:100%}
      table.grid th,table.grid td{border:1px solid #2a2f3a;padding:5px 9px;
           text-align:left;font-size:13px}
      table.grid th{background:#171a21}
      td.num{text-align:right;font-variant-numeric:tabular-nums}
      td.num.strong{font-weight:600}td.name{font-weight:600}
      td.ok{color:#34d399}td.bad{color:#f87171}
      .chart{margin:14px 0}svg{width:100%;max-width:920px;display:block}
      .grid{stroke:#262b36;stroke-width:1}.tick{fill:#8b93a3;font-size:11px}
      .legend{margin-top:6px}.legend span{margin-right:16px;font-size:12px}
      .legend i{display:inline-block;width:10px;height:10px;
           border-radius:2px;margin-right:5px}
      .sha-trail{margin:8px 0}
      .sha-trail span{display:inline-block;font:11px/1.7 ui-monospace,monospace;
           padding:2px 7px;margin:2px 4px 2px 0;border-radius:9px}
      .sha-ok{background:#12321f;color:#6ee7b7}
      .sha-bad{background:#3a1717;color:#fca5a5}
      .sha-new{outline:2px solid #facc15}
      pre.lua{background:#141821;border:1px solid #2a2f3a;border-radius:8px;
           padding:14px;overflow:auto;max-height:420px;font-size:12.5px;
           white-space:pre-wrap}
      details{margin:12px 0;background:#141821;border:1px solid #2a2f3a;
           border-radius:8px;padding:10px 14px}
      summary{cursor:pointer}
      code{color:#93c5fd}
    """
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{_esc(meta.get("title", "econ.me run"))}</title><style>{css}</style>
</head><body>
<h1>{_esc(meta.get("title", "Dynasty run"))}</h1>
<p class="meta">{houses}</p>
<p class="meta">{len(snapshots)} rounds · {_esc(meta.get("ticks_per_round", "?"))
} ticks/round · ticks {_esc(snapshots[0]["ticks"][0] if snapshots else "")}–
{_esc(snapshots[-1]["ticks"][-1] if snapshots else "")} ·
generated {_esc(meta.get("generated", ""))}</p>
{_standings(snapshots)}
{line_chart("Wealth over rounds (money + holdings at last prices)", labels, wealth)}
{line_chart("Money over rounds", labels, money)}
{line_chart("Market prices over rounds", labels, prices)}
{line_chart("FOOD satisfaction over rounds (1.0 = fed)", labels, needs)}
{_activity(snapshots)}
{_strategy(snapshots)}
</body></html>"""


def write_dashboard(path, snapshots: list[dict], meta: dict) -> None:
    Path(path).write_text(build_dashboard(snapshots, meta))
