"""The run dashboard: one self-contained HTML file from the snapshots.

No server, no CDN, no JS framework — inline SVG charts and honest tables,
so the artifact outlives the run: drop it on any disk or browser and the
whole story is there. Data view doctrine follows the platform's: every
number is what the dynasties themselves could see on their own MCP
surface (§13 parity), which is exactly what `multi.run_rounds` snapshots.

Sections: final standings; per-house holdings &amp; conditions by
round; wealth / money / prices / needs charts over
rounds; a round-by-round activity table (attempts, refusals, the round's
event mix); and per-dynasty strategy panels — the latest behaviour
source with the sha trail of every rewrite, so "what is House Llama
doing?" is a scroll, not a query.
"""

from __future__ import annotations

import datetime as _dt
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
        for snap in snapshots:
            b = snap["dynasties"][name]["behaviour"]
            entry = snap["dynasties"][name].get("entry") or {}
            if entry.get("action") == "extinct":
                cls = "sha-ext"          # frozen, not refused
            else:
                cls = "sha-ok" if entry.get("accepted") else "sha-bad"
            changed = ""
            if snap["round"] > 1:
                prev = snapshots[snap["round"] - 2]["dynasties"][name][
                    "behaviour"]["sha"]
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

    # live-run header: when meta carries a status, the page says where
    # the run is and (while live) reloads itself, so a served dashboard
    # watched in a browser advances round by round on its own.
    refresh = (f'<meta http-equiv="refresh" content="{int(meta["refresh_s"])}">'
               if meta.get("refresh_s") else "")
    status = ""
    if meta.get("status") == "live":
        status = (f'<p class="meta"><span class="live">● LIVE</span> '
                  f'round {meta.get("round", len(snapshots))} of '
                  f'{meta.get("rounds_total", "?")} · '
                  f'elapsed {_hms(meta.get("elapsed_s"))} · page refreshes '
                  f'every {int(meta["refresh_s"])}s</p>')
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
      .hsum table.grid th{position:sticky;top:0;z-index:1}
      td.cond{color:#fbbf24;font-variant-numeric:tabular-nums}
      th.cond-h{color:#fbbf24}
      td.quiet{color:#4b5563}
    """
    return f"""<!doctype html><html><head><meta charset="utf-8">
{refresh}<title>{_esc(meta.get("title", "econ.me run"))}</title><style>{css}</style>
</head><body>
<h1>{_esc(meta.get("title", "Dynasty run"))}</h1>
<p class="meta">{houses}</p>
{status}
<p class="meta">{len(snapshots)} rounds · {_esc(meta.get("ticks_per_round", "?"))
} ticks/round · ticks {_esc(snapshots[0]["ticks"][0] if snapshots else "")}–
{_esc(snapshots[-1]["ticks"][-1] if snapshots else "")} ·
generated {_esc(meta.get("generated", ""))}</p>
{_standings(snapshots)}
{_house_summaries(snapshots)}
{line_chart("Wealth over rounds (money + holdings at last prices)", labels, wealth)}
{line_chart("Money over rounds", labels, money)}
{line_chart("Market prices over rounds", labels, prices)}
{line_chart("FOOD satisfaction over rounds (1.0 = fed)", labels, needs)}
{_activity(snapshots)}
{_world_log(snapshots)}
{_strategy(snapshots)}
</body></html>"""


def write_dashboard(path, snapshots: list[dict], meta: dict) -> None:
    Path(path).write_text(build_dashboard(snapshots, meta))
