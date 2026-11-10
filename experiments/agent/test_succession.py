"""G3: automatic succession — the seat outlives the entity.

What these tests prove is the MECHANISM, offline, through the TestClient
exactly as nim_run.py drives the live server:

  * the genome is the conservative code channel — the last ACCEPTED
    version MINUS the kill-window mutations, recovered from the dead
    body's own script rows (the pure function, no server);
  * a dead seat SUCCEEDS automatically — join the successor through the
    front doors, install the genome (+ the death certificate on file),
    rebind the seat — and the run CONTINUES past the death to its target
    rounds, the successor playing on;
  * the cooldown is the price of death (the successor joins only after
    N rounds the world ran without the house);
  * the all-succeeding escape: when the WHOLE generation dies together
    nobody is left to keep the world running, so the lineages succeed
    together and the world continues — no hang;
  * without succession the dead park (the pre-G3 behaviour is intact).
"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine.models import Base, Entity, EntityStatus, User

from experiments.agent.loop import AgentLoop, McpClient
from experiments.agent.multi import (Dynasty, build_agent_world, run_rounds)
from experiments.agent.succession import (
    Succession, SuccessionError, compute_genome,
)

CLEAN = "ctx.state.note = 'round'"
CLEAN2 = "ctx.state.note = 'again'"

NAMES = ["House One", "House Two", "House Three"]
USER_IDS = ["u-one", "u-two", "u-three"]


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/world.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    app.state._test_engine = engine

    def override_get_session():
        with Session(engine) as session:
            yield session

    def override_get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
        session: Session = Depends(get_session),
    ) -> User:
        user = session.get(User, credentials.credentials)
        if user is None:
            raise HTTPException(401, "User not found")
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    with Session(engine) as s:
        s.add_all([User(id=uid, email=f"{uid}@x", name=n, provider="test",
                        provider_id=uid) for uid, n in zip(USER_IDS, NAMES)])
        s.commit()
        dynasties = [Dynasty(user_id=uid, name=n, model_name=f"test:{uid}",
                             token=uid)
                     for uid, n in zip(USER_IDS, NAMES)]
        build_agent_world(s, dynasties)

    tc = TestClient(app)

    def transport_for(user_id):
        def transport(method, params):
            r = tc.post(
                "/mcp", headers={"Authorization": f"Bearer {user_id}"},
                json={"jsonrpc": "2.0", "id": 1, "method": method,
                      "params": params})
            body = r.json()
            assert "error" not in body, body
            return body["result"]
        return transport

    try:
        yield {
            "transports": {uid: transport_for(uid) for uid in USER_IDS},
            "dynasties": dynasties, "engine": engine, "tc": tc,
            "db_path": tmp_path / "world.db", "out_dir": tmp_path,
        }
    finally:
        app.dependency_overrides.clear()


def make_post(tc):
    """The TestClient front-door client: one Bearer POST, the status and
    the decoded body (the refusal decoded too — the signal, not a crash)."""
    def post(path, token, body=None):
        r = tc.post(path, headers={"Authorization": f"Bearer {token}"},
                   json=body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}
    return post


def make_succession(client, cooldown=0, kill_window=48, ticks_per_round=24):
    return Succession(
        "http://127.0.0.1:0", client["db_path"], client["out_dir"],
        ticks_per_round=ticks_per_round, kill_window=kill_window,
        cooldown_rounds=cooldown, post=make_post(client["tc"]))


def make_loops(client, responses, diary=False):
    from experiments.agent.llm import ScriptedModel
    loops = []
    for d, resps in zip(client["dynasties"], responses):
        lp = AgentLoop(McpClient(client["transports"][d.user_id]),
                       ScriptedModel(list(resps)),
                       entity_id=d.entity_id, diary=diary)
        loops.append((d, lp))
    return loops


def kill_house(client, name, tick=24):
    d = next(d for d in client["dynasties"] if d.name == name)
    with Session(client["engine"]) as s:
        e = s.get(Entity, d.entity_id)
        e.status = EntityStatus.INCAPACITATED
        e.incapacitated_tick = tick
        s.commit()


def write_cert(client, name, condition="HUNGER"):
    cert = {
        "house": name, "model": "test",
        "death": {"tick": 24, "day": 1, "hour": 0, "condition": condition,
                  "quantity": "15.0000", "domain": "food"},
        "vitals": [{"round": 1,
                    "conditions": {"HUNGER": "14.9"}, "coin": "0.5",
                    "wood": "1", "jerky": "0"}],
        "shelf": {"JERKY": {"ask": "2.99"}},
    }
    p = client["out_dir"] / f"death-cert-{name.lower().replace(' ', '-')}.json"
    p.write_text(json.dumps(cert))


# ===========================================================================
# The genome: the conservative code channel (pure, no server)
# ===========================================================================

def _mk_world(tmp_path, entity_id, sources, death_tick, owner="u-a"):
    """A bare world.db: the dead body's script rows + its entity row."""
    db = sqlite3.connect(str(tmp_path / "world.db"))
    db.execute("CREATE TABLE scripts "
               "(entity_id TEXT, source TEXT, created_at TEXT)")
    db.execute("CREATE TABLE entities "
               "(id TEXT, name TEXT, owner_id TEXT, status TEXT, "
               "incapacitated_tick INTEGER)")
    for i, src in enumerate(sources):
        db.execute("INSERT INTO scripts VALUES (?,?,?)",
                   (entity_id, src, str(i)))
    db.execute("INSERT INTO entities VALUES (?,?,?,?,?)",
               (entity_id, "House A", owner, "INCAPACITATED", death_tick))
    db.commit()
    db.close()
    return tmp_path / "world.db"


def _journal(tmp_path, entries):
    """entries: a list of (round, sha, accepted)."""
    p = tmp_path / "journal-house-a.jsonl"
    lines = []
    for rnd, sha, accepted in entries:
        lines.append(json.dumps({"round": rnd, "source_sha": sha,
                                 "accepted": accepted}))
    p.write_text("\n".join(lines) + "\n")
    return p


def test_compute_genome_rolls_back_the_kill_window(tmp_path):
    """Inherit the last accepted version MINUS every version that landed
    inside the kill window [death - W, death]. v1 is safely before the
    window (inherited); v2 and v3 landed in it (the prime suspects —
    rolled back)."""
    v1, v2, v3 = "return 1", "return 2", "return 3"
    db = _mk_world(tmp_path, "E", [v1, v2, v3], death_tick=60)
    # v1 installed round 1 (tick 1), v2 round 2 (tick 25), v3 round 3
    # (tick 49); death tick 60, W=48 -> window [12, 60]: v2, v3 inside.
    j = _journal(tmp_path, [(1, "a" * 16, True), (2, "b" * 16, True),
                            (3, "c" * 16, True)])
    # the journal shas must match the script sources' shas for recovery
    import hashlib
    shas = [hashlib.sha256(s.encode()).hexdigest()[:16] for s in (v1, v2, v3)]
    j = _journal(tmp_path, [(1, shas[0], True), (2, shas[1], True),
                            (3, shas[2], True)])
    g = compute_genome(db, j, "E", death_tick=60, ticks_per_round=24,
                       kill_window=48)
    assert g["genome_src"] == v1          # the last SAFE version
    assert g["rolled_back"] == 2          # v2, v3 were the suspects
    assert g["n_versions"] == 3


def test_compute_genome_inherits_the_clean_tail(tmp_path):
    """No version inside the window: the whole accepted tail is clean,
    inherit the last one, nothing rolled back."""
    v1, v2 = "return 1", "return 2"
    db = _mk_world(tmp_path, "E", [v1, v2], death_tick=100)
    import hashlib
    shas = [hashlib.sha256(s.encode()).hexdigest()[:16] for s in (v1, v2)]
    # both installed long before the window [52, 100]
    j = _journal(tmp_path, [(1, shas[0], True), (2, shas[1], True)])
    g = compute_genome(db, j, "E", death_tick=100, ticks_per_round=24,
                       kill_window=48)
    assert g["genome_src"] == v2
    assert g["rolled_back"] == 0


def test_compute_genome_falls_back_to_last_script(tmp_path):
    """No journal (a line that never journaled): inherit the last script
    row — the cold start, never a crash."""
    v1, v2 = "return 1", "return 2"
    db = _mk_world(tmp_path, "E", [v1, v2], death_tick=10)
    g = compute_genome(db, tmp_path / "missing.jsonl", "E", death_tick=10)
    assert g["genome_src"] == v2


def test_compute_genome_refuses_a_body_with_no_script(tmp_path):
    db = sqlite3.connect(str(tmp_path / "world.db"))
    db.execute("CREATE TABLE scripts "
               "(entity_id TEXT, source TEXT, created_at TEXT)")
    db.execute("CREATE TABLE entities "
               "(id TEXT, name TEXT, owner_id TEXT, status TEXT, "
               "incapacitated_tick INTEGER)")
    db.commit()
    db.close()
    with pytest.raises(SuccessionError, match="no script to inherit"):
        compute_genome(tmp_path / "world.db",
                       tmp_path / "missing.jsonl", "E", death_tick=10)


# ===========================================================================
# succeed(): the successor's genome is installed through the front doors
# ===========================================================================

def test_succeed_installs_the_inherited_genome(client):
    """A dead body's successor inherits the last accepted version, with
    the death certificate riding the genome's leading comment block —
    installed through POST /join + POST /behaviour (the lint gate)."""
    # one authored round: House Two's accepted version is CLEAN
    loops = make_loops(client, [[CLEAN], [CLEAN], [CLEAN]])
    run_rounds(loops, 1, client["out_dir"])
    kill_house(client, "House Two", tick=24)
    write_cert(client, "House Two")

    d = next(d for d in client["dynasties"] if d.name == "House Two")
    before = {e.id for e in
              _entities(client, d.user_id)}
    succ_id = make_succession(client, cooldown=0).succeed(d)

    assert succ_id not in before            # a NEW body, not the corpse
    with Session(client["engine"]) as s:
        e = s.get(Entity, succ_id)
        assert e.owner_id == d.user_id
        assert e.status == EntityStatus.ACTIVE
        assert e.name == "House Two II"
    mcp = McpClient(client["transports"][d.user_id])
    src = mcp.call("get_behaviour", {"entity_id": succ_id})["source"]
    assert "INHERITED MEMORY" in src        # the certificate preamble
    assert "HUNGER" in src                 # ...naming the parent's death
    assert CLEAN in src                    # the inherited genome


def test_succeed_without_certificate_inherits_cold(client):
    """No certificate on file: the code channel alone, the preamble says
    so — never a crash, never a fabricated memory."""
    loops = make_loops(client, [[CLEAN], [CLEAN], [CLEAN]])
    run_rounds(loops, 1, client["out_dir"])
    kill_house(client, "House Two", tick=24)
    d = next(d for d in client["dynasties"] if d.name == "House Two")
    succ_id = make_succession(client, cooldown=0).succeed(d)
    mcp = McpClient(client["transports"][d.user_id])
    src = mcp.call("get_behaviour", {"entity_id": succ_id})["source"]
    assert "inherit the genome cold" in src
    assert CLEAN in src


def test_succeed_refuses_a_live_body(client):
    """A body that is not dead is not succeeded: succeed() is a no-op
    that returns the (live) entity it was handed."""
    d = next(d for d in client["dynasties"] if d.name == "House One")
    assert make_succession(client).succeed(d) == d.entity_id


def _entities(client, user_id):
    with Session(client["engine"]) as s:
        from sqlalchemy import select
        from econengine.models import Entity as E
        return list(s.execute(
            select(E).where(E.owner_id == user_id)).scalars().all())


# ===========================================================================
# The live loop: a dead seat succeeds and the run continues
# ===========================================================================

def test_dead_seat_succeeds_and_the_run_continues(client, monkeypatch,
                                                 tmp_path):
    """The G3 core: House Two's body dies mid-run; its SEAT succeeds
    automatically (join the successor, install the genome, rebind) and
    the run CONTINUES to its target rounds — the successor playing on,
    the other houses never held back by the dead one."""
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "2")
    # one clean round so the lineages have an authored genome to inherit
    loops = make_loops(client, [[CLEAN]] * 3)
    prior = run_rounds(loops, 1, tmp_path)
    assert [p["round"] for p in prior] == [1]
    kill_house(client, "House Two", tick=2)

    # a fresh process resumes with succession armed: the dead seat
    # succeeds and plays on
    loops2 = make_loops(client, [[CLEAN2] * 4] * 3)
    snaps = run_rounds(loops2, 3, tmp_path, start_round=2, snapshots=prior,
                       succession=make_succession(client, cooldown=0))
    assert [p["round"] for p in snaps] == [1, 2, 3]
    # the successor exists, is House Two's, and is ACTIVE
    d = next(d for d in client["dynasties"] if d.name == "House Two")
    with Session(client["engine"]) as s:
        owned = list(s.execute(
            __import__("sqlalchemy").select(Entity)
            .where(Entity.owner_id == d.user_id)).scalars().all())
    assert len(owned) == 2                  # the corpse + the successor
    assert d.entity_id in {e.id for e in owned}
    assert d.entity_id != prior[0]["dynasties"]["House Two"]["entry"]["entity"]
    # the successor played the later rounds (not a wall of tombstones)
    late = [snaps[i]["dynasties"]["House Two"]["entry"] for i in (1, 2)]
    assert any(e.get("action") != "extinct" for e in late)


def test_succession_cooldown_delays_the_successor(client, monkeypatch,
                                                 tmp_path):
    """The price of death: with a 2-round cooldown the successor joins
    only after the world ran two rounds WITHOUT the house — its first
    authored turn is a full cooldown later, not the very next round."""
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "2")
    loops = make_loops(client, [[CLEAN]] * 3)
    prior = run_rounds(loops, 1, tmp_path)
    kill_house(client, "House Two", tick=2)

    loops2 = make_loops(client, [[CLEAN2] * 6] * 3)
    snaps = run_rounds(loops2, 5, tmp_path, start_round=2, snapshots=prior,
                       succession=make_succession(client, cooldown=2))
    # the world kept running through the cooldown (the other two houses
    # hold the gate); the successor is born, then plays on
    assert [p["round"] for p in snaps] == [1, 2, 3, 4, 5]
    d = next(d for d in client["dynasties"] if d.name == "House Two")
    with Session(client["engine"]) as s:
        owned = list(s.execute(
            __import__("sqlalchemy").select(Entity)
            .where(Entity.owner_id == d.user_id)).scalars().all())
    assert len(owned) == 2                  # exactly one successor


def test_without_succession_the_dead_park(client, monkeypatch, tmp_path):
    """Regression: with succession OFF, a dead seat parks for good and
    the run ends at total extinction — the pre-G3 behaviour is intact
    (the manual operator's job is unchanged)."""
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "2")
    loops = make_loops(client, [[CLEAN]] * 3)
    prior = run_rounds(loops, 1, tmp_path)
    kill_house(client, "House Two", tick=2)
    # everyone else keeps playing; House Two parks (no succession)
    loops2 = make_loops(client, [[CLEAN2] * 4] * 3)
    snaps = run_rounds(loops2, 3, tmp_path, start_round=2, snapshots=prior)
    # the run ends when House Two is the only one left to act — with
    # succession off it parks, so the other two carry it to round 3
    assert [p["round"] for p in snaps] == [1, 2, 3]
    d = next(d for d in client["dynasties"] if d.name == "House Two")
    with Session(client["engine"]) as s:
        owned = list(s.execute(
            __import__("sqlalchemy").select(Entity)
            .where(Entity.owner_id == d.user_id)).scalars().all())
    assert len(owned) == 1                  # no successor was joined


def test_whole_generation_dying_succeeds_together(client, monkeypatch,
                                                 tmp_path):
    """The all-succeeding escape: the WHOLE generation dies in the same
    breath — nobody is left to keep the world running, so a round-counted
    cooldown would hang. The lineages succeed together and the world
    continues with the new generation; the run reaches its target."""
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "2")
    loops = make_loops(client, [[CLEAN]] * 3)
    prior = run_rounds(loops, 1, tmp_path)
    for name in NAMES:
        kill_house(client, name, tick=2)

    loops2 = make_loops(client, [[CLEAN2] * 5] * 3)
    snaps = run_rounds(loops2, 4, tmp_path, start_round=2, snapshots=prior,
                       succession=make_succession(client, cooldown=2))
    assert [p["round"] for p in snaps] == [1, 2, 3, 4]
    # every line succeeded: each user owns the corpse + one successor
    for d in client["dynasties"]:
        with Session(client["engine"]) as s:
            owned = list(s.execute(
                __import__("sqlalchemy").select(Entity)
                .where(Entity.owner_id == d.user_id)).scalars().all())
        assert len(owned) == 2, d.name
