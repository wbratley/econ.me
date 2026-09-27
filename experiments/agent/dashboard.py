"""The run dashboard: one self-contained HTML file from the snapshots.

No server, no CDN, no JS framework — inline SVG charts and honest tables,
so the artifact outlives the run: drop it on any disk or browser and the
whole story is there. Data view doctrine follows the platform's: every
number is what the dynasties themselves could see on their own MCP
surface (§13 parity), which is exactly what `multi.run_rounds` snapshots.

Sections: the square (a chat board — every spoken line, live), one
tab per house (state card, holdings ledger, their feed), and the
world tab — final standings; the map — places, roads, and who stands
where, round by round; per-house holdings &amp; conditions by round;
wealth / money / prices / needs charts over
rounds; a round-by-round activity table (attempts, refusals, the round's
event mix); and per-dynasty strategy panels — the latest behaviour
source with the sha trail of every rewrite, so "what is House Llama
doing?" is a tab, not a query. While the run is live (and the
harness passed its world URL), the SSE stream appends says, fights,
deaths and bounced orders to the square and the house feeds between
the round rewrites.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
import urllib.parse
from decimal import Decimal
from pathlib import Path

from .multi import dynasty_assets, dynasty_money, price_table

PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2"]


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt(d: Decimal) -> str:
    q = d.quantize(Decimal("0.01"))
    return f"{q:,}"


# --- condition meters -------------------------------------------------
#
# #213 gave the world's conditions a numeric kill line and the alarm a
# 25% trip point; these give the same climb to the reader. A condition
# bar fills 0 -> incapacitates_at and takes its colour from the same
# fraction the alarm reads: green is genuinely "the platform is
# silent" territory, amber+ is the alarm band, red is dying now.

def _cond_fraction(level: Decimal, kill: str) -> Decimal | None:
    """level / kill line as a fraction — None when the line is absent
    or unparsable (the bar simply does not draw)."""
    try:
        k = Decimal(str(kill))
    except Exception:
        return None
    if k <= 0:
        return None
    return level / k


def _cond_band(frac: Decimal) -> str:
    if frac >= Decimal("0.75"):
        return "crit"
    if frac >= Decimal("0.50"):
        return "hot"
    if frac >= Decimal("0.25"):
        return "warn"
    return "ok"


def _cond_bars(held: dict, kill_lines: dict) -> str:
    """The state card's meter stack: one bar per condition that carries
    a kill line, held level filling toward the line. A zero level draws
    too — an empty green bar reads as full health, which is the scan
    the reader wants (who is LOW)."""
    rows = []
    for sym in sorted(kill_lines):
        frac = _cond_fraction(held.get(sym, Decimal("0")), kill_lines[sym])
        if frac is None:
            continue
        q = held.get(sym, Decimal("0"))
        kill = Decimal(str(kill_lines[sym]))
        band = _cond_band(frac)
        pct = min(max(frac, Decimal("0")), Decimal("1")) * 100
        title = (f"{_esc(sym)} {_fmt(q)} of kill line {_fmt(kill)} "
                 f"({_fmt(frac * 100)}%) — incapacitation at 100%")
        rows.append(
            f'<div class="meter m-{band}" title="{title}">'
            f'<div class="meter-fill" style="width:{pct:.1f}%"></div>'
            f'<span class="meter-lab">{_esc(sym)} {_fmt(q)} / {_fmt(kill)}'
            "</span></div>")
    return ("".join(rows) and '<div class="condmeters">' + "".join(rows)
            + "</div>") or ""


def _heat_cell_class(sym: str, q: Decimal, kill_lines: dict) -> str:
    """The ledger grids' cell colouring: same bands, as a background
    wash on the number, so the round-by-round climb reads as heat."""
    if q == 0:
        return ""
    frac = _cond_fraction(q, kill_lines.get(sym))
    return f" heat-{_cond_band(frac)}" if frac is not None else ""


def _hms(seconds) -> str:
    """75 -> '1:15', 3725 -> '1:02:05' — the run clock, for the header."""
    if seconds is None:
        return ""
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _progress(meta: dict, snapshots: list[dict]) -> str:
    """The run clock: a progress bar, average pace, and (while live) an
    ETA for the finish computed from that pace."""
    done = meta.get("round") or (snapshots[-1]["round"] if snapshots else 0)
    total = meta.get("rounds_total") or done
    elapsed = meta.get("elapsed_s")
    if not total or elapsed is None:
        return ""
    avg = elapsed / max(1, done)
    bar = (f'<div class="pbar"><div style="width:{100 * done / total:.1f}%">'
           f'</div></div>')
    if meta.get("status") == "live":
        remaining = max(0, total - done)
        eta = avg * remaining
        ends = (_dt.datetime.now()
                + _dt.timedelta(seconds=eta)).strftime("%H:%M")
        return (f'{bar}<p class="meta">round {done} of {total} · '
                f'avg {_hms(avg)}/round · ETA ≈{_hms(eta)} to finish '
                f'(ends ≈{ends})</p>')
    return (f'{bar}<p class="meta">{done} of {total} rounds · '
            f'avg {_hms(avg)}/round · total {_hms(elapsed)}</p>')


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
# The map (docs/spatial.md): places and roads drawn from the snapshot's
# world_map — public facts, laid out by the road graph itself (the
# engine has no coordinates, and neither does this drawing).
# ---------------------------------------------------------------------------

def _layout(nodes: list[str], roads: list[tuple[str, str]],
            width: float, height: float) -> dict[str, tuple[float, float]]:
    """A tiny deterministic spring embedding (Fruchterman-Reingold):
    nodes repel, roads attract, init on a circle in sorted-key order,
    fixed iteration count — the same map always draws the same way, so
    round N and round N+1 differ only in who stands where, never in
    where the places are."""
    import math
    n = len(nodes)
    if n <= 1:
        return {v: (width / 2, height / 2) for v in nodes}
    k = 0.55 / math.sqrt(n)              # ideal edge length, scaled to frame
    pos = {v: (math.cos(2 * math.pi * i / n), math.sin(2 * math.pi * i / n))
           for i, v in enumerate(nodes)}
    t = 0.15                             # annealing temperature, cools off
    for _ in range(500):
        disp = {v: [0.0, 0.0] for v in nodes}
        for i, u in enumerate(nodes):
            for v in nodes[i + 1:]:
                dx, dy = pos[u][0] - pos[v][0], pos[u][1] - pos[v][1]
                d = math.sqrt(dx * dx + dy * dy) or 1e-9
                f = k * k / d
                disp[u][0] += dx / d * f; disp[u][1] += dy / d * f
                disp[v][0] -= dx / d * f; disp[v][1] -= dy / d * f
        for a, b in roads:
            dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
            d = math.sqrt(dx * dx + dy * dy) or 1e-9
            f = d / k
            disp[a][0] -= dx / d * f; disp[a][1] -= dy / d * f
            disp[b][0] += dx / d * f; disp[b][1] += dy / d * f
        for v in nodes:
            dx, dy = disp[v]
            d = math.sqrt(dx * dx + dy * dy) or 1e-9
            step = min(d, t)
            pos[v] = (pos[v][0] + dx / d * step, pos[v][1] + dy / d * step)
        t *= 0.997
    xs = [p[0] for p in pos.values()]; ys = [p[1] for p in pos.values()]
    sx = (width - 130) / max(1e-9, max(xs) - min(xs))
    sy = (height - 110) / max(1e-9, max(ys) - min(ys))
    s = min(sx, sy)
    cx, cy = sum(xs) / n, sum(ys) / n
    return {v: (width / 2 + (p[0] - cx) * s, height / 2 - (p[1] - cy) * s)
            for v, p in pos.items()}


def _map(snapshots: list[dict]) -> str:
    """The map section: the latest round's world as an inline SVG —
    places as nodes, roads as edges with their hour costs, every
    entity as a colored marker where it stood at the round's end
    (dynasties in their chart colors, everyone else neutral, the dead
    faded) — then a per-round location strip, the census histogram in
    dashboard form. A run whose world ships no map (or pre-map
    snapshots resumed) renders nothing here."""
    mapped = [s for s in snapshots if s.get("world_map", {}).get("places")]
    if not mapped:
        return ""
    last = mapped[-1]
    wmap = last["world_map"]
    places = {p["key"]: p for p in wmap["places"]}
    roads = [(r["from"], r["to"]) for r in wmap["roads"]]
    width, height = 860, 500
    pos = _layout(sorted(places), roads, width, height)

    # who's who: dynasties wear their chart colors (matched by entity
    # id through the journal entries); everything else is neutral.
    names = list(snapshots[0]["dynasties"].keys())
    ent_color: dict[str, str] = {}
    for i, name in enumerate(names):
        entry = (last["dynasties"].get(name) or {}).get("entry") or {}
        if entry.get("entity"):
            ent_color[entry["entity"]] = PALETTE[i % len(PALETTE)]

    svg = [f'<svg viewBox="0 0 {width} {height}" class="map-svg" role="img">']
    for r in wmap["roads"]:                     # roads first, under nodes
        a, b = r["from"], r["to"]
        if a not in pos or b not in pos:
            continue
        x1, y1 = pos[a]; x2, y2 = pos[b]
        svg.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" '
                   f'y2="{y2:.0f}" class="map-road"/>'
                   f'<text x="{(x1+x2)/2:.0f}" y="{(y1+y2)/2-5:.0f}" '
                   f'class="map-cost" text-anchor="middle">{r["cost_ticks"]}h</text>')
    for key, (x, y) in pos.items():
        name = places[key].get("name") or key
        svg.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="11" class="map-node"/>'
                   f'<text x="{x:.0f}" y="{y+27:.0f}" class="map-place" '
                   f'text-anchor="middle">{_esc(name)}</text>')
        here = [e for e in wmap["entities"] if e["place"] == key]
        for j, e in enumerate(here):            # fan of markers by the node
            mx, my = x + 22 + (j % 2) * 92, y - 14 + (j // 2) * 15
            color = ent_color.get(e["id"], "#9ca3af")
            dead = "" if e["status"] == "active" else " map-dead"
            svg.append(f'<circle cx="{mx:.0f}" cy="{my:.0f}" r="4" '
                       f'fill="{color}" class="{dead.strip()}"/>'
                       f'<text x="{mx+8:.0f}" y="{my+4:.0f}" '
                       f'class="map-ent{dead}">{_esc(e["name"])}</text>')
    svg.append("</svg>")

    # the location strip: every entity ever placed, one row, a column
    # per round — the run-26 census histograms, but live.
    order = []
    for s in mapped:
        for e in s["world_map"].get("entities", []):
            if e["id"] not in [o["id"] for o in order]:
                order.append(e)
    strip = ['<table class="grid map-strip"><tr><th>Who</th>'
             + "".join(f'<th>R{s["round"]}</th>' for s in mapped) + "</tr>"]
    for e in order:
        cells = []
        for s in mapped:
            spot = next((x["place"] for x in s["world_map"]["entities"]
                         if x["id"] == e["id"]), None)
            cells.append(f'<td class="loc">{_esc(spot or "·")}</td>')
        color = ent_color.get(e["id"])
        dot = (f'<i class="lg" style="background:{color}"></i>'
               if color else '<i class="lg"></i>')
        strip.append(f'<tr><td class="name">{dot}{_esc(e["name"])}</td>'
                     + "".join(cells) + "</tr>")
    strip.append("</table>")

    return ('<h2>The map — round ' + str(last["round"]) + '</h2>'
            '<p class="quiet">Places and roads as the world knows them '
            '(laid out by the road graph — no coordinates exist); road '
            'labels are hours. Markers stand where each entity ended '
            'the round; faded markers are the dead.</p>'
            + "".join(svg)
            + '<p class="meta">Location by round — the census strip:</p>'
            + "".join(strip))


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
            if (snap["dynasties"][name].get("entry") or {}).get("kept_old")
            and (snap["dynasties"][name].get("entry") or {}).get("action")
            != "extinct")     # tombstones are not refusals
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


def _house_summaries(snapshots: list[dict]) -> str:
    """Per house, per round: the full holdings breakdown with conditions
    split from inventory — every snapshot already carries each dynasty's
    own MCP view (§13 parity: what the house itself could see). Columns
    are the symbols ever held by anyone, so houses compare row for row;
    zeros render as quiet dots to keep the table readable."""
    names = list(snapshots[0]["dynasties"].keys()) if snapshots else []
    if not names:
        return ""
    conditions = sorted({c for s in snapshots for c in s.get("conditions", [])})
    # kill lines are static per world; the newest snapshot's copy serves
    # every row (resumed pre-#213 runs simply carry none — no wash).
    kl = (snapshots[-1].get("kill_lines") or {}) if snapshots else {}

    def held(view: dict) -> dict[str, Decimal]:
        return {h["symbol"]: Decimal(h["quantity"])
                for h in view.get("holdings", [])}

    commodity = sorted({sym for s in snapshots
                        for v in s["dynasties"].values()
                        for sym, q in held(v).items()
                        if sym not in conditions and sym != "COIN" and q != 0})
    cond_syms = sorted({sym for s in snapshots
                        for v in s["dynasties"].values()
                        for sym, q in held(v).items()
                        if sym in conditions and q != 0})

    parts = ['<h2>Houses — holdings &amp; conditions by round</h2>',
             '<p class="quiet">Each row is the round-end state the house '
             'itself could see. Conditions are held like goods but read as '
             'a state of the holder — the number is the level, not '
             'inventory.</p>']
    for name in names:
        parts.append(
            f'<details open class="hsum"><summary><b>{_esc(name)}</b></summary>'
            f'<div class="hsum-scroll"><table class="grid">')
        head = ('<tr><th>Round</th><th>Money</th>'
                + "".join(f'<th>{_esc(s)}</th>' for s in commodity)
                + "".join(f'<th class="cond-h">{_esc(s)}</th>'
                           for s in cond_syms)
                + "</tr>")
        parts.append(head)
        for snap in snapshots:
            view = snap["dynasties"][name]
            h = held(view)
            row = [f"<tr><td>{snap['round']}</td>",
                   f'<td class="num">{_fmt(dynasty_money(view))}</td>']
            for sym in commodity:
                q = h.get(sym, Decimal("0"))
                cls = "num" if q != 0 else "num quiet"
                cell = _fmt(q) if q != 0 else "·"
                row.append(f'<td class="{cls}">{cell}</td>')
            for sym in cond_syms:
                q = h.get(sym, Decimal("0"))
                cls = "num cond" if q != 0 else "num cond quiet"
                cls += _heat_cell_class(sym, q, kl)
                cell = _fmt(q) if q != 0 else "·"
                row.append(f'<td class="{cls}">{cell}</td>')
            parts.append("".join(row) + "</tr>")
        parts.append("</table></div></details>")
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
            if entry.get("action") == "extinct":
                cell, cls = "† extinct", "extinct"
            elif entry.get("accepted"):
                cell, cls = f"{entry.get('attempts', '?')}✓", "ok"
            else:
                why = entry.get("refusal") or "—"
                cell, cls = f"{entry.get('attempts', '?')}✗ ({why[:60]})", "bad"
            parts.append(f'<td class="{cls}"><span class="quiet">'
                         f'{_esc(name)}</span><br>{_esc(cell)}</td>')
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _world_log(snapshots: list[dict]) -> str:
    """The audit-trail section (§15.3): the world log as readable prose,
    with a round selector and a per-dynasty filter — all inline, so the
    self-contained-HTML doctrine holds (the artifact carries the whole
    story offline). Each snapshot's tail is bounded by its own round."""
    with_log = [s for s in snapshots if s.get("activity")]
    if not with_log:
        return ""
    names = list(snapshots[0]["dynasties"].keys())

    # One collapsible block per round carrying a log, newest first; the
    # latest open. A block's rows merge the world's public facts with
    # every dynasty's own events, newest tick first.
    blocks = []
    for snap in reversed(with_log):
        act = snap["activity"]
        rows = [(r["tick"], "world", r["text"]) for r in act.get("world", [])]
        for name, rlist in act.get("dynasties", {}).items():
            for r in (rlist or []):
                # Witnessed rows (game.md 15.6) are what this house HEARD
                # -- speech and loud facts. Marked so a reader can tell
                # "Ulf said X" (Ulf's own row) from "Bjorn heard X" (the
                # same utterance, witnessed); the tick + text match up.
                heard = " (heard)" if r.get("witnessed") else ""
                rows.append((r["tick"], f"{name}{heard}", r["text"]))
        rows.sort(key=lambda t: -t[0])
        body = "".join(
            f'<tr data-who="{_esc(who)}"><td class="num">{tick}</td>'
            f'<td class="name">{_esc(who)}</td>'
            f'<td>{_esc(text)}</td></tr>'
            for tick, who, text in rows) or (
                '<tr><td colspan=3 class="quiet">a quiet round — no events'
                '</td></tr>')
        blocks.append(
            f'<details class="wlog" data-round="{snap["round"]}"'
            f'{" open" if snap is with_log[-1] else ""}>'
            f'<summary>Round {snap["round"]} world log '
            f'<span class="quiet">({len(rows)} entries)</span></summary>'
            f'<table class="grid wlog-table">'
            f'<tr><th>Tick</th><th>Who</th><th>Action</th></tr>{body}'
            f'</table></details>')

    buttons = "".join(
        f'<button class="wl-btn{" on" if n == "all" else ""}" data-who="{_esc(n)}" onclick="'
        f'wlFilter(this)">{_esc(n)}</button>'
        for n in ["all", "world"] + names)
    script = """
      function wlFilter(btn){
        var who=btn.getAttribute('data-who');
        document.querySelectorAll('.wl-btn').forEach(function(b){
          b.classList.remove('on')});
        btn.classList.add('on');
        document.querySelectorAll('tr[data-who]').forEach(function(r){
          r.style.display=(who==='all'||r.getAttribute('data-who')===who)
            ? '' : 'none'})}
    """
    return ('<h2>World log — the audit trail</h2>'
            '<p class="quiet">Every action rendered as prose (Phase 3b '
            'registry): your log is your events, the world log is public '
            'facts. Filter: '
            f'<span class="wl">{buttons}</span></p>'
            + "".join(blocks) + f'<script>{script}</script>')


def _strategy(snapshots: list[dict]) -> str:
    parts = ["<h2>Strategy — the behaviour each house is running</h2>"]
    for name, view in snapshots[-1]["dynasties"].items():
        parts.append(
            f"<details open><summary><b>{_esc(name)}</b> "
            f"<span class=\"quiet\">({_esc(view.get('model', ''))})</span></summary>")
        parts.append('<div class="sha-trail">')
        for i, snap in enumerate(snapshots):
            b = snap["dynasties"][name]["behaviour"]
            entry = snap["dynasties"][name].get("entry") or {}
            if entry.get("action") == "extinct":
                cls = "sha-ext"          # frozen, not refused
            else:
                cls = "sha-ok" if entry.get("accepted") else "sha-bad"
            changed = ""
            if i:
                # vs the previous snapshot IN THE LIST, not round
                # arithmetic: under a world clock (§9.2) the world can
                # close rounds the harness never snapshotted (a seat
                # slower than its round), so rounds may skip — the last
                # thing shown is still the right comparator.
                prev = snapshots[i - 1]["dynasties"][name]["behaviour"]["sha"]
                changed = " sha-new" if prev != b["sha"] else ""
            parts.append(f'<span class="{cls}{changed}" title="round '
                         f'{snap["round"]}">{_esc(b["sha"] or "—")}</span>')
        parts.append("</div>")
        diary = [(snap["round"], snap["dynasties"][name].get("entry") or {})
                 for snap in snapshots]
        if any(e.get("thoughts") for _, e in diary):
            parts.append('<h4>strategy diary</h4><div class="diary">')
            for rnd, e in diary:
                if e.get("thoughts"):
                    parts.append(f'<p class="diary-line"><b>R{rnd}</b> '
                                 f'<span class="quiet">'
                                 f'{_esc(e.get("action") or "")}</span> — '
                                 f'{_esc(e["thoughts"])}</p>')
            parts.append('</div>')
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

# ---------------------------------------------------------------------------
# The chat board + the tabs (v2: the square, the houses, the world)
# ---------------------------------------------------------------------------

def _chat_history(snapshots: list[dict]) -> list[dict]:
    """The say ledger: every spoken line, once, attributed. The world
    log carries each say twice over — the speaker's own row plus a
    "(heard)" row in every witness's log (§15.6) — so the board
    dedupes on (tick, text) and attributes from the own row when one
    exists. The post's merchant bark never has an own row (no dynasty
    journals it): a say whose text opens with POST: is the counter
    speaking. Ascending, capped at the last 400 lines."""
    seen: set[tuple[int, str]] = set()
    out: list[dict] = []
    for snap in snapshots:
        act = snap.get("activity") or {}
        rows: list[tuple[int, str, str, bool]] = []
        own: dict[tuple[int, str], str] = {}
        for house, rlist in (act.get("dynasties") or {}).items():
            for r in (rlist or []):
                if "says:" not in r["text"]:
                    continue
                heard = bool(r.get("witnessed"))
                rows.append((r["tick"], house, r["text"], heard))
                if not heard:
                    own[(r["tick"], r["text"])] = house
        for tick, house, text, heard in rows:
            key = (tick, text)
            if key in seen:
                continue
            speaker = ("" if heard else house) or own.get(key, "")
            m = re.match(r'says: "(.*)"$', text, re.S)
            body = m.group(1) if m else text
            if not speaker:
                speaker = "POST" if body.startswith("POST") else "—"
            seen.add(key)
            out.append({"t": tick, "who": speaker, "text": body,
                        "k": "say"})
    out.sort(key=lambda r: r["t"])
    return out[-400:]


def _house_tab(snapshots: list[dict], name: str) -> str:
    """One house, one tab: the state card (what they hold, where they
    stand, how they burn), the per-round holdings ledger, and the
    house's own feed — its doings plus what it heard (§15.6), oldest
    at top so the live tail appends in reading order."""
    last = snapshots[-1]["dynasties"][name]
    lb = last.get("leaderboard") or {}
    eid = str((last.get("entry") or {}).get("entity") or "")

    def held(view: dict) -> dict[str, Decimal]:
        return {h["symbol"]: Decimal(h["quantity"])
                for h in view.get("holdings", [])}

    conditions = sorted({c for s in snapshots for c in s.get("conditions", [])})
    kl = (snapshots[-1].get("kill_lines") or {}) if snapshots else {}
    h = held(last)
    money = dynasty_money(last)
    food = next((fd.get("satisfaction") for fd in last.get("needs", [])
                 if fd.get("need") == "FOOD"), None)
    place = ""
    for s in reversed(snapshots):
        wm = s.get("world_map") or {}
        row = next((x for x in wm.get("entities", [])
                    if x.get("name") == name), None)
        if row:
            place = str(row.get("place") or "")
            break
    inv = sorted((s, q) for s, q in h.items()
                 if q != 0 and s not in conditions and s != "COIN")
    conds = sorted((s, q) for s, q in h.items()
                   if q != 0 and s in conditions)
    chips = " ".join(f'<span class="chip">{_fmt(q)} {_esc(s)}</span>'
                     for s, q in inv) or '<span class="quiet">empty</span>'
    condchips = (" ".join(f'<span class="chip cond">{_esc(s)} {_fmt(q)}</span>'
                          for s, q in conds))

    commodity = sorted({sym for s in snapshots
                        for sym, q in held(s["dynasties"][name]).items()
                        if sym not in conditions and sym != "COIN" and q != 0})
    cond_syms = sorted({sym for s in snapshots
                        for sym, q in held(s["dynasties"][name]).items()
                        if sym in conditions and q != 0})
    parts = [f'<div class="card"><h3>{_esc(name)} '
             f'<span class="quiet">{_esc(last.get("model", ""))}</span></h3>'
             f'<p>status <b>{_esc(lb.get("status", "?"))}</b>'
             f' · money <b class="num">{_fmt(money)}</b>'
             + (f' · FOOD <b>{food}</b>' if food is not None else "")
             + (f' · at <b>{_esc(place)}</b>' if place else "")
             + '</p><p>' + chips
             + (f' <span class="quiet">conditions:</span> {condchips}'
                if condchips else "") + "</p>"
             + _cond_bars(h, kl)
             + "</div>",
             '<div class="hsum-scroll"><table class="grid">',
             '<tr><th>Round</th><th>Money</th>'
             + "".join(f"<th>{_esc(s)}</th>" for s in commodity)
             + "".join(f'<th class="cond-h">{_esc(s)}</th>' for s in cond_syms)
             + "</tr>"]
    for snap in snapshots:
        hh = held(snap["dynasties"][name])
        row = [f"<tr><td>{snap['round']}</td>",
               f'<td class="num">{_fmt(dynasty_money(snap["dynasties"][name]))}</td>']
        for sym in commodity:
            q = hh.get(sym, Decimal("0"))
            row.append(f'<td class="num">{_fmt(q) if q else "·"}</td>')
        for sym in cond_syms:
            q = hh.get(sym, Decimal("0"))
            row.append(f'<td class="num cond{_heat_cell_class(sym, q, kl)}">'
                       f'{_fmt(q) if q else "·"}</td>')
        parts.append("".join(row) + "</tr>")
    parts.append("</table></div>")

    feed: list[tuple[int, str, bool]] = []
    for snap in snapshots:
        for r in ((snap.get("activity") or {}).get("dynasties")
                  or {}).get(name) or []:
            feed.append((r["tick"], r["text"], bool(r.get("witnessed"))))
    feed.sort(key=lambda t: t[0])
    feed = feed[-400:]
    feed_html = "".join(
        f'<div class="feed-line{" fl-heard" if hrd else ""}">'
        f"t{t} · {_esc(x)}</div>" for t, x, hrd in feed
    ) or '<div class="feed-line quiet">a quiet house</div>'
    attr = f' data-feed="{_esc(eid)}"' if eid else ""
    parts.append('<h3>Their round — own doings and what they heard</h3>'
                 f'<div class="feed"{attr}>{feed_html}</div>')
    return "".join(parts)


# Client-side, literal except for two injected values (the seeded chat
# history and the speaker→colour map) — a module-level template so the
# JS stays readable without f-string brace-escaping. It runs on every
# page (live or finished): the board renders the embedded history, and
# the live script (when present) appends through window.__chatLine.
_CHAT_JS = """
(function () {
  var CHAT = __CHAT__;
  var COLORS = __COLORS__;
  var board = document.getElementById('chat-log');
  if (!board) return;
  function stamp(t) {
    var h = (t - 1) % 24, d = Math.floor((t - 1) / 24) + 1;
    return 'd' + d + ' h' + String(h).padStart(2, '0');
  }
  function line(o) {
    var near = board.scrollHeight - board.scrollTop
             - board.clientHeight < 60;
    var d = document.createElement('div');
    d.className = 'chat-line ' + (o.kind || 'say');
    var s = document.createElement('span');
    s.className = 'chat-stamp';
    s.textContent = stamp(o.t || 0);
    d.appendChild(s);
    if (o.who) {
      var w = document.createElement('span');
      w.className = 'chat-who ' + (COLORS[o.who] || 'cx');
      w.textContent = o.who;
      d.appendChild(w);
    }
    var x = document.createElement('span');
    x.className = 'chat-text';
    x.textContent = o.text || '';
    d.appendChild(x);
    board.appendChild(d);
    while (board.children.length > 400) board.removeChild(board.firstChild);
    if (near) board.scrollTop = board.scrollHeight;
  }
  window.__chatLine = line;
  CHAT.forEach(function (c) { line(c); });
})();
(function () {
  // The page rewrites itself every round while live — the reader's
  // place in it must survive the rewrite (localStorage, not the URL,
  // so the artifact stays portable).
  function activate(id, save) {
    document.querySelectorAll('.tab').forEach(function (b) {
      b.classList.toggle('on', b.getAttribute('data-tab') === id); });
    document.querySelectorAll('.tabpane').forEach(function (p) {
      p.classList.toggle('on', p.id === 'pane-' + id); });
    if (save) { try { localStorage.setItem('econtab', id); } catch (e) {} }
  }
  var tabs = document.querySelectorAll('.tab');
  Array.prototype.forEach.call(tabs, function (b) {
    b.onclick = function () { activate(b.getAttribute('data-tab'), true); };
  });
  var saved = null;
  try { saved = localStorage.getItem('econtab'); } catch (e) {}
  if (!(saved && document.getElementById('pane-' + saved))) saved = 'chat';
  activate(saved, false);
})();
"""


# ---------------------------------------------------------------------------
# The live world panel (§9.2's audience)
# ---------------------------------------------------------------------------

# Client-side, literal except for three injected values (URL, name map,
# ticks-per-round) — kept as a module-level template so the JS stays
# readable without f-string brace-escaping.
_LIVE_JS = """
(function () {
  var PORT = __PORT__, NAMES = __NAMES__, K = __K__;
  // The stream target follows the PAGE's hostname, not the harness's:
  // a LAN browser on http://192.168.x.y:8130/ must reach the world SSE
  // at 192.168.x.y:8925 — 127.0.0.1 only pulses for a browser on the
  // host itself (run 47: the LAN panel stayed frozen all day). Only
  // the world server's PORT comes from the meta; scheme + host ride
  // location (an empty port means the world sat on :80/:443).
  var LIVE = location.protocol + '//' + location.hostname
           + (PORT ? ':' + PORT : '');
  var el = function (id) { return document.getElementById(id); };
  var chat = window.__chatLine || function () {};
  var SEEN = {};
  var cur = 0;
  function name(id) {
    return !id ? '?' : (NAMES[id] || ('…' + String(id).slice(-6)));
  }
  // The two live surfaces: the chat board (Chat tab) and the per-house
  // feeds (House tabs). Both autoscroll only when the reader is already
  // at the bottom — scrolling up to read pins the view.
  function push(box, div) {
    var near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    box.appendChild(div);
    while (box.children.length > 400) box.removeChild(box.firstChild);
    if (near) box.scrollTop = box.scrollHeight;
  }
  function chatLine(t, who, text, kind) {
    chat({ t: t, who: who, text: text, kind: kind });
  }
  function feed(ids, text, cls) {
    Array.prototype.slice.call(
      document.querySelectorAll('[data-feed]')).forEach(function (f) {
        if (ids.indexOf(f.getAttribute('data-feed')) < 0) return;
        var d = document.createElement('div');
        d.className = 'feed-line' + (cls ? ' ' + cls : '');
        d.textContent = text;
        push(f, d);
      });
  }
  function flash(cls) {
    var p = document.getElementById('lv');
    p.classList.remove('flash-combat', 'flash-death');
    if (cls) { void p.offsetWidth; p.classList.add(cls); }
  }
  function setTick(t, hour) {
    el('lv-tick').textContent = 'tick ' + t;
    var h = (hour != null) ? hour : ((t - 1) % 24);
    var day = Math.floor((t - 1) / 24) + 1;
    el('lv-hour').textContent = 'day ' + day + ', h'
      + String(h).padStart(2, '0') + (h < 6 || h >= 20 ? ' ☾' : ' ☀');
    if (K) el('lv-round').textContent =
      'round ' + (Math.floor((t - 1) / K) + 1);
  }
  function observable(e, t) {
    cur = t;
    if (e.type === 'say') {
      var txt = String(e.text || '').slice(0, 300);
      var key = t + '\\u0000' + txt;   // dedupe vs embedded history
      if (SEEN[key]) return;
      SEEN[key] = 1;
      chatLine(t, name(e.entity_id), txt, 'say');
      feed([e.entity_id], 'said: "' + txt + '"', 'fl-say');
      return;
    }
    if (e.type === 'combat') {
      flash('flash-combat');
      var c = '⚔ ' + name(e.entity_id) + ' → ' + name(e.target_id)
        + (e.hit ? ' hit −' + e.damage : (e.deterred ? ' deterred'
        : ' missed'));
      chatLine(t, '', c, 'combat');
      feed([e.entity_id, e.target_id], c, 'fl-combat');
      return;
    }
    if (e.type === 'entity_incapacitated') {
      flash('flash-death');
      var d = '☠ ' + name(e.entity_id) + ' falls ('
        + (e.condition || '?') + ')';
      chatLine(t, '', d, 'death');
      feed([e.entity_id], d, 'fl-death');
      return;
    }
    if (e.type === 'order_cancelled') {
      var o = '✗ ' + name(e.entity_id) + "'s order bounced at "
        + (e.market || '?') + ' (' + (e.reason || '?') + ')';
      chatLine(t, '', o, 'quiet');
      feed([e.entity_id], o, 'fl-quiet');
      return;
    }
  }
  var es = new EventSource(LIVE + '/rounds/events');
  es.addEventListener('hello', function (m) {
    var h = JSON.parse(m.data);
    el('lv-stream').textContent = h.world_tick_seconds
      ? 'clock ' + h.world_tick_seconds + 's/tick' : 'clock off';
    if (h.ticks_run) setTick(h.ticks_run, null);
  });
  es.addEventListener('tick', function (m) {
    var d = JSON.parse(m.data);
    setTick(d.tick, d.hour);
    (d.observables || []).forEach(function (e) { observable(e, d.tick); });
    (d.closed && d.closed.eliminations || []).forEach(function (x) {
      flash('flash-death');
      chatLine(cur, '', '☠ ' + name(x.user_id) + ' eliminated', 'death');
    });
  });
  es.addEventListener('round_closed', function (m) {
    chatLine(cur, '', '— round '
      + JSON.parse(m.data).round_number + ' closed —', 'round');
  });
  es.addEventListener('round_opened', function (m) {
    el('lv-round').textContent = 'round ' + JSON.parse(m.data).round;
  });
  es.onopen = function () { el('lv-stream').className = 'quiet'; };
  es.onerror = function () {
    el('lv-stream').textContent = 'stream lost — reconnecting…';
  };
})();
"""


def _live_panel(meta: dict, snapshots: list[dict]) -> str:
    """The between-rounds view: while the run is LIVE and the harness
    gave the page a world URL, an EventSource on the world's SSE stream
    (/rounds/events, same facts the seats hear, §9.1) drives a live
    header — tick, day/hour with sun/moon, round — and a scrolling log
    of what each tick broadcasts out loud: says, fights, deaths,
    bounced orders, with combat and incapacity flashing the panel.
    Under a world clock (§9.2) the world moves between rounds and the
    dashboard moves with it; the aggregate sections stay exactly where
    they always were (whole-page rewrites after every round). Drops
    off the finished page — the stream belongs to a live world, and a
    post-run artifact must stay self-contained. Entities render as
    houses (the id→name map is built from the snapshots themselves);
    eliminations stamp user ids, so the map carries both keys."""
    if meta.get("status") != "live" or not meta.get("live_url"):
        return ""
    # only the PORT of the harness's world URL survives into the page:
    # the browser resolves the host (see _LIVE_JS) so served-from-any-
    # device views pulse against the same world.
    parsed = urllib.parse.urlparse(str(meta["live_url"]))
    port = parsed.port or ""
    names: dict[str, str] = {}
    for s in snapshots:
        for house, v in s["dynasties"].items():
            eid = (v.get("entry") or {}).get("entity")
            if eid:
                names[str(eid)] = house
                # the run's own user-id scheme (nim_run: u-<slug(name)>)
                names["u-" + re.sub(r"\W+", "-", house.lower()).strip("-")] = house
    js = (_LIVE_JS
          .replace("__PORT__", json.dumps(str(port)))
          .replace("__NAMES__", json.dumps(names))
          .replace("__K__", str(int(meta.get("ticks_per_round") or 0))))
    return ('\n<div id="lv" class="live-panel"><div class="live-head">'
            '<span class="live-dot"></span><span class="live-t">live world</span>'
            '<span id="lv-tick">tick –</span><span id="lv-hour">–</span>'
            '<span id="lv-round">round –</span>'
            '<span id="lv-stream" class="quiet">connecting…</span></div></div>\n'
            f"<script>{js}</script>")


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
                          if m["symbol"] == sym
                          and m["last_price"] is not None), Decimal("0"))
                    for s in snapshots] for sym in symbols}

    houses = " · ".join(
        f"{_esc(n)} ({_esc(snapshots[-1]['dynasties'][n].get('model', ''))})"
        for n in names)

    # the tabbed surface (v2): the square first, one tab per house,
    # the world last — nothing from the old page is lost, it just
    # stops being a scroll.
    chat_js = (_CHAT_JS
               .replace("__CHAT__", json.dumps(_chat_history(snapshots))
                        .replace("</", "<\\/"))
               .replace("__COLORS__", json.dumps(
                   {n: f"c{i % 6}" for i, n in enumerate(names)})))
    tabbar = ('<div class="tabs">'
              '<button class="tab" data-tab="chat">💬 the square</button>'
              + "".join(f'<button class="tab" data-tab="h{i}">{_esc(n)}</button>'
                        for i, n in enumerate(names))
              + '<button class="tab" data-tab="world">🌍 the world</button>'
              '</div>')
    house_panes = "".join(
        f'<div id="pane-h{i}" class="tabpane">{_house_tab(snapshots, n)}</div>'
        for i, n in enumerate(names))

    # live-run header: while live the SSE stream keeps the square and
    # the house feeds current between rounds — the page does NOT reload
    # itself anymore (the 10s meta refresh fought the reader for the
    # scroll bar; aggregates refresh on a manual reload, liveness rides
    # the stream).
    status = ""
    if meta.get("status") == "live":
        status = (f'<p class="meta"><span class="live">● LIVE</span> '
                  f'round {meta.get("round", len(snapshots))} of '
                  f'{meta.get("rounds_total", "?")} · '
                  f'elapsed {_hms(meta.get("elapsed_s"))} · live via SSE,'
                  ' reload for the aggregates</p>')
    elif meta.get("status") == "complete":
        status = (f'<p class="meta"><span class="done">✓ complete</span> '
                  f'{len(snapshots)} rounds in '
                  f'{_hms(meta.get("elapsed_s"))}</p>')
    status += _progress(meta, snapshots)
    css = """
      body{font:14px/1.45 -apple-system,'Segoe UI',sans-serif;margin:0;
           background:#0f1115;color:#e5e7eb;padding:28px 34px}
      h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:30px 0 10px;
           border-bottom:1px solid #2a2f3a;padding-bottom:6px}
      .quiet{color:#8b93a3}.meta{color:#8b93a3;margin-bottom:6px}
      .live{color:#facc15;font-weight:600}.done{color:#34d399;font-weight:600}
      table.grid{border-collapse:collapse;margin:8px 0;width:100%}
      table.grid th,table.grid td{border:1px solid #2a2f3a;padding:5px 9px;
           text-align:left;font-size:13px}
      table.grid th{background:#171a21}
      td.num{text-align:right;font-variant-numeric:tabular-nums}
      td.num.strong{font-weight:600}td.name{font-weight:600}
      td.ok{color:#34d399}td.bad{color:#f87171}td.extinct{color:#8b93a3}
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
      .sha-ext{background:#262b33;color:#8b93a3}
      .sha-new{outline:2px solid #facc15}
      .diary{margin:10px 0 4px}
      .diary-line{margin:4px 0;color:#c7cdd9;font-size:13px}
      .diary-line b{color:#e5e7eb}.diary-line .quiet{font-size:11.5px}
      pre.lua{background:#141821;border:1px solid #2a2f3a;border-radius:8px;
           padding:14px;overflow:auto;max-height:420px;font-size:12.5px;
           white-space:pre-wrap}
      details{margin:12px 0;background:#141821;border:1px solid #2a2f3a;
           border-radius:8px;padding:10px 14px}
      summary{cursor:pointer}
      .wl-btn{background:#171a21;border:1px solid #2a2f3a;border-radius:9px;
           color:#c7cdd9;font-size:12px;padding:3px 10px;margin:0 4px 4px 0;
           cursor:pointer}
      .wl-btn.on{background:#12321f;color:#6ee7b7;border-color:#1d4d33}
      .wlog-table td{font-size:12.5px}
      code{color:#93c5fd}
      .pbar{height:8px;background:#171a21;border:1px solid #2a2f3a;
           border-radius:5px;margin:8px 0 2px;max-width:640px;overflow:hidden}
      .pbar div{height:100%;background:#2563eb}
      .hsum-scroll{max-height:340px;overflow:auto;margin:6px 0}
      .live-panel{border:1px solid #2a2f3a;border-radius:10px;
           background:#141821;margin:14px 0;padding:10px 14px}
      .live-head{display:flex;gap:16px;align-items:baseline;font-size:13px}
      .live-t{color:#facc15;font-weight:600}
      .live-dot{display:inline-block;width:9px;height:9px;border-radius:50%;
           background:#facc15;animation:lv-pulse 2s infinite}
      .live-log{margin-top:8px;max-height:300px;overflow-y:auto;
           font:12.5px/1.6 ui-monospace,SFMono-Regular,monospace;color:#c7cdd9;
           background:#0f1115;border:1px solid #232837;border-radius:8px;
           padding:8px 10px}
      .lv-line{white-space:pre-wrap}
      .lv-say{color:#93c5fd}.lv-combat{color:#fbbf24}
      .lv-death{color:#f87171;font-weight:600}
      .lv-cancel{color:#8b93a3}
      .lv-round{color:#34d399;margin:3px 0}
      @keyframes lv-pulse{0%,100%{opacity:1}50%{opacity:.35}}
      @keyframes lv-flash-amber{0%{background:#3a2a10}100%{background:#141821}}
      @keyframes lv-flash-red{0%{background:#3a1616}100%{background:#141821}}
      .flash-combat{animation:lv-flash-amber 1.2s ease-out}
      .flash-death{animation:lv-flash-red 2s ease-out}
      .hsum table.grid th{position:sticky;top:0;z-index:1}
      td.cond{color:#fbbf24;font-variant-numeric:tabular-nums}
      th.cond-h{color:#fbbf24}
      td.quiet{color:#4b5563}
      .map-svg{max-width:920px}
      .map-road{stroke:#3b4354;stroke-width:2}
      .map-cost{fill:#8b93a3;font-size:11px}
      .map-node{fill:#1d2330;stroke:#5b6478;stroke-width:2}
      .map-place{fill:#c7cdd9;font-size:12.5px;font-weight:600}
      .map-ent{fill:#aab2c2;font-size:11px}
      .map-dead{opacity:.45}
      .map-strip td.loc{font-size:12px;font-variant-numeric:tabular-nums;
           text-align:center;color:#c7cdd9}
      i.lg{display:inline-block;width:9px;height:9px;border-radius:2px;
           margin:0 7px 0 1px;background:#4b5563}
      .tabs{display:flex;gap:6px;flex-wrap:wrap;position:sticky;top:0;
           z-index:9;background:#0f1115;padding:10px 0 8px;
           border-bottom:1px solid #2a2f3a}
      .tab{background:#171a21;border:1px solid #2a2f3a;border-radius:9px;
           color:#c7cdd9;font-size:13px;padding:5px 14px;cursor:pointer}
      .tab.on{background:#1d2330;color:#facc15;border-color:#4b5563}
      .tabpane{display:none;padding-top:4px}
      .tabpane.on{display:block}
      .chat-log{max-width:880px;max-height:72vh;overflow-y:auto;
           background:#0f1115;border:1px solid #232837;border-radius:10px;
           padding:12px 14px;font-size:13.5px}
      .chat-line{margin:7px 0;display:flex;gap:10px;align-items:baseline}
      .chat-stamp{font:11px ui-monospace,monospace;color:#4b5563;
           min-width:60px;text-align:right;flex:0 0 auto}
      .chat-who{font-weight:600;flex:0 0 auto}
      .chat-text{white-space:pre-wrap}
      .chat-line.combat .chat-text{color:#fbbf24}
      .chat-line.death .chat-text{color:#f87171;font-weight:600}
      .chat-line.quiet .chat-text{color:#8b93a3}
      .chat-line.round .chat-text{color:#34d399}
      .c0{color:#93c5fd}.c1{color:#6ee7b7}.c2{color:#f9a8d4}.c3{color:#fcd34d}
      .c4{color:#a5b4fc}.c5{color:#fdba74}.cx{color:#e5e7eb}
      .chip{display:inline-block;background:#171a21;
           border:1px solid #2a2f3a;border-radius:9px;padding:2px 9px;
           margin:2px 4px 2px 0;font:12px ui-monospace,monospace;
           color:#c7cdd9}
      .chip.cond{color:#fbbf24;border-color:#3a2a10}
      .condmeters{margin:8px 0 2px}
      .meter{position:relative;height:20px;background:#171a21;
           border:1px solid #2a2f3a;border-radius:6px;margin:4px 0;
           max-width:420px;overflow:hidden}
      .meter-fill{height:100%}
      .m-ok .meter-fill{background:#15803d}
      .m-warn .meter-fill{background:#b45309}
      .m-hot .meter-fill{background:#c2410c}
      .m-crit .meter-fill{background:#b91c1c}
      .m-crit{border-color:#7f1d1d;box-shadow:0 0 0 1px #7f1d1d55}
      .meter-lab{position:absolute;inset:0;display:flex;align-items:center;
           justify-content:center;font:11.5px ui-monospace,monospace;
           color:#e5e7eb;text-shadow:0 1px 2px #000d;pointer-events:none}
      td.heat-ok{background:rgba(21,128,61,.12)}
      td.heat-warn{background:rgba(180,83,9,.18)}
      td.heat-hot{background:rgba(194,65,12,.26)}
      td.heat-crit{background:rgba(185,28,28,.34)}
      .card{background:#141821;border:1px solid #2a2f3a;border-radius:10px;
           padding:12px 16px;margin:10px 0;max-width:920px}
      .card h3{margin:0 0 8px}
      .card p{margin:6px 0}
      .feed{max-width:920px;max-height:60vh;overflow-y:auto;
           background:#0f1115;border:1px solid #232837;border-radius:10px;
           padding:10px 12px;font:12.5px/1.7 ui-monospace,monospace;
           color:#c7cdd9}
      .feed-line{white-space:pre-wrap}
      .fl-heard{color:#8b93a3}
      .fl-say{color:#93c5fd}.fl-combat{color:#fbbf24}
      .fl-death{color:#f87171}.fl-quiet{color:#8b93a3}
    """
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{_esc(meta.get("title", "econ.me run"))}</title><style>{css}</style>
</head><body>
<h1>{_esc(meta.get("title", "Dynasty run"))}</h1>
<p class="meta">{houses}</p>
{status}
{_live_panel(meta, snapshots)}
{tabbar}
<div id="pane-chat" class="tabpane">
<h2>The square — everything said out loud</h2>
<p class="quiet">Spoken lines live here: the seats' controller voice
(SAY:, #208), the post's merchant bark, and what the world broadcasts
in the open — fights, falls, bounced orders, rounds closing. Newest at
the bottom; while the run is live the stream appends between
refreshes.</p>
<div id="chat-log" class="chat-log"></div>
</div>
{house_panes}
<div id="pane-world" class="tabpane">
<p class="meta">{len(snapshots)} rounds · {_esc(meta.get("ticks_per_round", "?"))
} ticks/round · ticks {_esc(snapshots[0]["ticks"][0] if snapshots else "")}–
{_esc(snapshots[-1]["ticks"][-1] if snapshots else "")} ·
generated {_esc(meta.get("generated", ""))}</p>
{_standings(snapshots)}
{_map(snapshots)}
{_house_summaries(snapshots)}
{line_chart("Wealth over rounds (money + holdings at last prices)", labels, wealth)}
{line_chart("Money over rounds", labels, money)}
{line_chart("Market prices over rounds", labels, prices)}
{line_chart("FOOD satisfaction over rounds (1.0 = fed)", labels, needs)}
{_activity(snapshots)}
{_world_log(snapshots)}
{_strategy(snapshots)}
</div>
<script>{chat_js}</script>
</body></html>"""


def write_dashboard(path, snapshots: list[dict], meta: dict) -> None:
    Path(path).write_text(build_dashboard(snapshots, meta))
