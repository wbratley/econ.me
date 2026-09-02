"""script_archive: bank the authored scripts and survival stats of a
finished run (champions program, step a). The tool is offline
bookkeeping -- read a run dir, write revision files + one ledger line
per house -- so the test builds a minimal world.db (the schema slice
the tool reads: entities/scripts/ticks) and checks the ledger row,
the death/usage stats, and that every revision lands on disk."""

import json
import sqlite3

from experiments.agent.script_archive import archive_run, write_ledger


def _run_dir(tmp_path):
    run = tmp_path / "stone-runX"
    run.mkdir()
    db = sqlite3.connect(run / "world.db")
    db.executescript("""
        create table entities (id text primary key, name text, status text);
        create table scripts (id text, entity_id text, name text,
            lineage_id text, script_type text, source text, is_active int,
            created_at text, updated_at text);
        create table ticks (number int, events text);
    """)
    db.execute("insert into entities values ('h1','House Harald','ACTIVE')")
    db.execute("insert into entities values ('h2','House Ivar','INCAPACITATED')")
    db.execute("insert into entities values ('w1','Wolf Pack I','INCAPACITATED')")
    # two revisions for Harald: first authored, second (active) final
    db.execute("insert into scripts values ('s1','h1','behaviour','L1','lua',"
               "'-- v1 gather',0,'t1','t1')")
    db.execute("insert into scripts values ('s2','h1','behaviour','L1','lua',"
               "'-- v2 pace',1,'t2','t2')")
    db.execute("insert into scripts values ('s3','h2','behaviour','L2','lua',"
               "'-- ivar starter',1,'t3','t3')")
    ev = [
        [{"type": "start_process", "entity_id": "h1",
          "params": {"recipe": "EAT_BERRIES"}}],
        [{"type": "start_process", "entity_id": "h1",
          "params": {"recipe": "EAT_BERRIES"}}],
        [{"type": "start_process", "entity_id": "w1",
          "params": {"recipe": "HUNT"}}],
        [{"type": "entity_incapacitated", "entity_id": "h2",
          "condition": "HUNGER", "quantity": 15.0}],
    ]
    for n, blob in enumerate(ev, 1):
        db.execute("insert into ticks values (?, ?)", (n, json.dumps(blob)))
    db.commit()
    db.close()
    (run / "journal-house-harald.jsonl").write_text(
        json.dumps({"round": 1, "kept_old": False}) + "\n"
        + json.dumps({"round": 2, "kept_old": True}) + "\n")
    return run


def test_archive_run_banks_revisions_stats_and_ledger(tmp_path):
    run = _run_dir(tmp_path)
    out = tmp_path / "archive"
    rows = archive_run(run, out)
    # only houses -- the wolf is not banked
    assert [r["house"] for r in rows] == ["House Harald", "House Ivar"]
    harald, ivar = rows
    # every revision on disk, final flagged by ledger position
    files = sorted((out / "stone-runX" / "harald").glob("rev*.lua"))
    assert [f.read_text() for f in files] == ["-- v1 gather", "-- v2 pace"]
    assert harald["revisions"] == 2 and harald["final_rev"] == 2
    assert harald["status"] == "ACTIVE" and harald["died_of"] is None
    assert harald["max_tick"] == 4 and harald["max_day"] == "d1h03"
    # usage counts only the house's own processes; journal fresh/kept
    assert harald["usage"] == {"EAT_BERRIES": 2}
    assert harald["journal"] == {"rounds": 2, "kept_old": 1, "fresh": 1}
    # the death: day/hour from the tick, cause from the condition
    assert ivar["died"] == "d1h03" and ivar["died_of"] == "HUNGER"
    assert ivar["usage"] == {}
    # ledger write is replace-per-run: re-archiving does not duplicate
    path = write_ledger(out, rows)
    assert len(path.read_text().splitlines()) == 2
    write_ledger(out, rows)
    assert len(path.read_text().splitlines()) == 2
