#!/usr/bin/env python
"""Keep the authored scripts of finished stone-runs (the champions program, step a).

Run 23 ended with House Harald alive at the 40-round clock -- the first
dynasty to outlast a run in the wolves era -- and the champion-scripts
program wants the artifacts banked before run dirs are pruned: every
house script revision from world.db (scripts.source, its lineage), the
survival stats (died day / died of, recipe usage, journal
fresh-vs-kept), and the sources on disk so a later run can seed
winners. Script quality is not monotone -- run 22's Lagertha peaked
around r12 and spiralled into axe-selling by r18 -- so revisions are
kept, not just finals. Offline bookkeeping only: nothing here touches
a live run or steers a model.

  .venv/bin/python -m experiments.agent.script_archive RUN_DIR [RUN_DIR...]
  .venv/bin/python -m experiments.agent.script_archive --all   # every stone-run* under ~/econ-runs

Writes OUT/<run>/<house>/revNNN.lua and one ledger.jsonl line per
(run, house); re-archiving a run replaces its lines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DEFAULT_ROOT = Path.home() / "econ-runs"
DEFAULT_OUT = DEFAULT_ROOT / "script-archive"
LEDGER = "ledger.jsonl"


def dh(tick: int) -> str:
    """Run-clock display: tick 1 = d1h00."""
    return f"d{(tick - 1) // 24 + 1}h{(tick - 1) % 24:02d}"


def _slug(name: str) -> str:
    return name.split()[-1].lower()


def _journal(run_dir: Path, house: str) -> dict:
    """Diary lines per house: rounds authored, fresh vs kept-old."""
    path = run_dir / f"journal-house-{_slug(house)}.jsonl"
    if not path.exists():
        return {"rounds": 0, "kept_old": 0, "fresh": 0}
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    kept = sum(1 for j in lines if j.get("kept_old"))
    return {"rounds": len(lines), "kept_old": kept, "fresh": len(lines) - kept}


def _scan_ticks(db: sqlite3.Connection) -> tuple[dict, dict, int]:
    """One pass over ticks.events -> ({eid: death}, {eid: usage Counter}, max tick)."""
    deaths: dict[str, tuple[int, str]] = {}
    usage: dict[str, Counter] = {}
    max_tick = 0
    for number, blob in db.execute("select number, events from ticks order by number"):
        max_tick = max(max_tick, number)
        for ev in json.loads(blob or "[]"):
            eid = ev.get("entity_id")
            if ev.get("type") == "entity_incapacitated" and eid and eid not in deaths:
                deaths[eid] = (number, ev.get("condition", "?"))
            elif ev.get("type") == "start_process" and eid:
                usage.setdefault(eid, Counter())[
                    ev.get("params", {}).get("recipe", "?")] += 1
    return deaths, usage, max_tick


def archive_run(run_dir: Path, out_dir: Path = DEFAULT_OUT) -> list[dict]:
    """Bank one run's house scripts + stats; returns the ledger rows."""
    db = sqlite3.connect(f"file:{run_dir / 'world.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    deaths, usage, max_tick = _scan_ticks(db)
    rows: list[dict] = []
    for house in db.execute(
        "select id, name, status from entities where name like 'House %' order by name"
    ):
        revs = db.execute(
            "select name, lineage_id, script_type, source, is_active,"
            " created_at, updated_at from scripts where entity_id = ?"
            " order by created_at, rowid",
            (house["id"],),
        ).fetchall()
        rev_dir = out_dir / run_dir.name / _slug(house["name"])
        rev_dir.mkdir(parents=True, exist_ok=True)
        final = None
        last = None
        for i, rev in enumerate(revs, 1):
            source = rev["source"] or ""
            (rev_dir / f"rev{i:03d}.lua").write_text(source)
            last = (i, source, rev["script_type"])
            if rev["is_active"]:
                final = last
        final = final or last
        if final is None:  # no rows at all: nothing to bank
            continue
        death = deaths.get(house["id"])
        rows.append({
            "run": run_dir.name,
            "house": house["name"],
            "status": house["status"],
            "max_tick": max_tick,
            "max_day": dh(max_tick),
            "died": dh(death[0]) if death else None,
            "died_of": death[1] if death else None,
            "revisions": len(revs),
            "final_rev": final[0],
            "script_type": final[2],
            "final_sha": hashlib.sha256(final[1].encode()).hexdigest()[:12],
            "final_lines": final[1].count("\n") + 1,
            "usage": dict(sorted(usage.get(house["id"], {}).items())),
            "journal": _journal(run_dir, house["name"]),
            "dir": str(rev_dir),
        })
    db.close()
    return rows


def write_ledger(out_dir: Path, rows: list[dict]) -> Path:
    """Append rows, replacing any prior lines for the same runs."""
    path = out_dir / LEDGER
    keep = []
    if path.exists():
        runs = {r["run"] for r in rows}
        keep = [l for l in path.read_text().splitlines()
                if l.strip() and json.loads(l)["run"] not in runs]
    path.write_text("\n".join(keep + [json.dumps(r) for r in rows]) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", nargs="*", type=Path,
                    help="finished run dirs (containing world.db)")
    ap.add_argument("--all", action="store_true",
                    help=f"archive every stone-run* dir under {DEFAULT_ROOT}")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    dirs = [d for d in (sorted(DEFAULT_ROOT.glob("stone-run*"))
                        if args.all else args.run_dirs)
            if (d / "world.db").exists()]
    if not dirs:
        print("no run dirs with a world.db", file=sys.stderr)
        return 1
    rows: list[dict] = []
    for d in dirs:
        got = archive_run(d, args.out)
        names = ", ".join(f"{r['house']} {r['status']}" for r in got) or "none"
        print(f"{d.name}: {len(got)} houses ({names})")
        rows += got
    if rows:
        path = write_ledger(args.out, rows)
        print(f"ledger: {path} ({sum(1 for l in path.read_text().splitlines() if l.strip())} lines)")
        by_days = sorted(rows, key=lambda r: (r["status"] == "ACTIVE",
                                              int((r["died"] or r["max_day"])[1:].split("h")[0])), reverse=True)
        print("\nby survival:")
        for r in by_days:
            days = (r["died"] or r["max_day"]).split("h")[0]
            print(f"  {days:>5} {r['house']:15} {r['run']:28} "
                  f"{r['died_of'] or 'ALIVE'}  revs={r['revisions']} "
                  f"fresh={r['journal']['fresh']}/{r['journal']['rounds']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
