"""The multi-agent run over the real surface: three dynasties, the
readiness gate, per-round snapshots, and the dashboard — offline, with
ScriptedModels, through the TestClient exactly as nim_run.py drives it
through httpx (same JSON-RPC bytes). What these tests prove is the
ORCHESTRATION: the world builds with owned seats, rounds resolve on the
final consent, snapshots carry the parity view per dynasty, a dead model
never stops the world, and the dashboard tells the story from the
snapshots alone.
"""

import hashlib
import json
from decimal import Decimal

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine.models import Base, User

from experiments.agent.dashboard import build_dashboard
from experiments.agent.llm import ScriptedModel
from experiments.agent.loop import AgentLoop, McpClient
from experiments.agent.multi import (
    Dynasty, build_agent_world, dynasty_assets, dynasty_money, price_table,
    run_rounds,
)

CLEAN = "ctx.state.note = 'round'"
CLEAN2 = "ctx.state.note = 'again'"

# the shared survival starter every seat now runs on round 0
import experiments.world.scenario as _scenario
_starter_sha = hashlib.sha256(
    _scenario._read_lua("starter.lua").encode()).hexdigest()

NAMES = ["House One", "House Two", "House Three"]
USER_IDS = ["u-one", "u-two", "u-three"]


@pytest.fixture
def client(tmp_path):
    # A FILE-backed DB, like the live server: parallel rounds issue
    # concurrent requests, and StaticPool's one shared :memory: connection
    # serializes mid-query ("tuple index out of range"). Separate
    # connections let SQLite's own locking do the coordinating.
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
        yield {"transports": {uid: transport_for(uid) for uid in USER_IDS},
               "dynasties": dynasties, "engine": engine}
    finally:
        app.dependency_overrides.clear()


def make_loops(fixture, responses_per_agent, monkeypatch=None, rounds_k=None):
    if monkeypatch is not None:
        monkeypatch.setenv("ECON_TICKS_PER_ROUND", str(rounds_k or 2))
    loops = []
    for d, responses in zip(fixture["dynasties"], responses_per_agent):
        lp = AgentLoop(McpClient(fixture["transports"][d.user_id]),
                       ScriptedModel(list(responses)),
                       entity_id=d.entity_id)
        loops.append((d, lp))
    return loops


# ===========================================================================
# The world
# ===========================================================================

def test_world_builds_with_owned_seats_and_readiness_gate(client):
    with Session(client["engine"]) as s:
        from econengine.models import Entity, WorldSetting
        for d in client["dynasties"]:
            e = s.get(Entity, d.entity_id)
            assert e is not None and e.owner_id == d.user_id
            assert e.status.value == "active"
        gate = s.get(WorldSetting, "round.gate")
        assert gate.value["mode"] == "readiness"
    # every seat sees the markets and the tiered libs (parity surface)
    mcp = McpClient(client["transports"][USER_IDS[0]])
    symbols = {m["symbol"] for m in mcp.call("market_prices")}
    assert {"GRAIN", "ORE", "IRON"} <= symbols
    libs = mcp.call("get_script_libraries")
    assert libs["world"] and libs["pack"]


def test_seats_start_identical(client):
    """No primed roles: every house starts with the same money, the same
    parcel bundle (FARM + FORGE + an ORE seam), both live unlocks, and
    the same survival starter — identical hands, so anything the run
    shows later was played, not dealt."""
    from sqlalchemy import select

    from econengine.models import Account, Parcel

    with Session(client["engine"]) as s:
        for d in client["dynasties"]:
            money = s.execute(select(Account.balance).where(
                Account.entity_id == d.entity_id)).scalar_one()
            assert Decimal(money) == Decimal("500")
            parcels = s.execute(select(Parcel).where(
                Parcel.owner_id == d.entity_id)).scalars().all()
            assert len(parcels) == 1
            parcel = parcels[0]
            assert {f.facility_type for f in parcel.facilities} == {
                "FARM", "FORGE"}
            assert [(dep.symbol, dep.capacity, dep.regen_per_tick)
                    for dep in parcel.deposits] == [
                ("ORE", Decimal("100"), Decimal("2"))]
    for uid, d in zip(USER_IDS, client["dynasties"]):
        state = McpClient(client["transports"][uid]).call(
            "entity_state", {"entity_id": d.entity_id})
        unlocks = set(state.get("unlocks", []))
        assert {"FARMING", "SMELTING"} <= unlocks, unlocks
        behaviour = McpClient(client["transports"][uid]).call(
            "get_behaviour", {"entity_id": d.entity_id})["source"]
        assert hashlib.sha256(behaviour.encode()).hexdigest() \
            == _starter_sha


# ===========================================================================
# The run
# ===========================================================================

def test_rounds_resolve_on_final_consent_and_snapshot(client, monkeypatch, tmp_path):
    loops = make_loops(client, [
        [CLEAN, CLEAN2], [CLEAN, CLEAN2], [CLEAN, CLEAN2]], monkeypatch)
    snapshots = run_rounds(loops, 2, tmp_path)

    assert [s["round"] for s in snapshots] == [1, 2]
    assert snapshots[0]["ticks"] == [1, 2]
    assert snapshots[1]["ticks"] == [3, 4]
    # three dynasty views per snapshot, each with its leaderboard row
    for snap in snapshots:
        assert set(snap["dynasties"]) == set(NAMES)
        for view in snap["dynasties"].values():
            assert view["leaderboard"]["user_id"] in USER_IDS
            assert view["behaviour"]["sha"]
    # money is conserved (trades transfer, nothing mints): the endowments
    first, last = snapshots[0], snapshots[-1]
    total0 = sum(dynasty_money(v) for v in first["dynasties"].values())
    total1 = sum(dynasty_money(v) for v in last["dynasties"].values())
    assert total1 == total0 == Decimal("1500")
    # files on disk, one per round
    names = sorted(p.name for p in tmp_path.glob("round-*.json"))
    assert names == ["round-01.json", "round-02.json"]
    assert json.loads((tmp_path / "round-02.json").read_text())["round"] == 2


def test_dead_model_never_stops_the_world(client, monkeypatch, tmp_path):
    """One dynasty's model hard-fails every round: the run continues, the
    failure is journaled into the snapshot, and the round still resolves
    (the failing house readies too — consent, not competence)."""
    class ExplodingModel:
        name = "test:dead"

        def complete(self, system, user):
            raise RuntimeError("provider is down")

    loops = []
    for d in client["dynasties"]:
        model = (ExplodingModel() if d.user_id == USER_IDS[1]
                 else ScriptedModel([CLEAN, CLEAN2]))
        loops.append((d, AgentLoop(McpClient(client["transports"][d.user_id]),
                                   model, entity_id=d.entity_id)))
    snapshots = run_rounds(loops, 2, tmp_path)

    assert [s["round"] for s in snapshots] == [1, 2]
    entry = snapshots[0]["dynasties"]["House Two"]["entry"]
    assert entry["kept_old"] and "provider is down" in entry["refusal"]
    # the other two houses played on
    assert snapshots[0]["dynasties"]["House One"]["entry"]["accepted"]
    assert snapshots[0]["dynasties"]["House Three"]["entry"]["accepted"]
    # the dead house kept its starter behaviour
    src = snapshots[1]["dynasties"]["House Two"]["behaviour"]["source"]
    assert "ctx" in src or "--" in src          # still the wired starter


def test_decisions_overlap_in_time(client, tmp_path):
    """The houses' model calls run concurrently: every model waits on a
    barrier ALL of them must reach while its own call is in flight —
    sequential cycles would break the barrier (timeout) and the houses
    would journal failures instead of accepting."""
    import threading

    gate = threading.Barrier(3, timeout=10)

    class BarrierModel:
        name = "test:barrier"

        def complete(self, system, user):
            gate.wait()                 # only passes if 3 calls overlap
            return CLEAN

    loops = [(d, AgentLoop(McpClient(client["transports"][d.user_id]),
                           BarrierModel(), entity_id=d.entity_id))
             for d in client["dynasties"]]
    snapshots = run_rounds(loops, 1, tmp_path)

    for name in NAMES:
        assert snapshots[0]["dynasties"][name]["entry"]["accepted"]


# ===========================================================================
# The dashboard
# ===========================================================================

def test_dashboard_tells_the_story(client, monkeypatch, tmp_path):
    loops = make_loops(client, [
        [CLEAN, CLEAN2], [CLEAN, CLEAN2], [CLEAN, CLEAN2]], monkeypatch)
    snapshots = run_rounds(loops, 2, tmp_path)

    page = build_dashboard(snapshots, {
        "title": "test run", "ticks_per_round": 2, "generated": "now"})
    for name in NAMES:
        assert name in page
    assert "Final standings" in page and page.count("<svg") >= 3
    assert "Wealth over rounds" in page and "Market prices" in page
    assert "(no data)" in page            # prices with zero trades: quiet, not broken
    # regression (nim-run3): a market unpriced in early snapshots that
    # trades later must chart zeros before the first print — not crash
    # on Decimal(None)
    for m in snapshots[-1]["market"]:
        if m["symbol"] == "GRAIN":
            m["last_price"] = "0.42"
    page = build_dashboard(snapshots, {
        "title": "test run", "ticks_per_round": 2, "generated": "now"})
    assert "GRAIN" in page and "0.42" in page
    assert "FOOD satisfaction" in page
    assert "R1" in page and "R2" in page
    assert page.count("500.00") >= 3        # three identical seats
    assert "ctx.state.note" in page         # latest behaviour source shown
    # strategy trail: one sha chip per round per dynasty
    assert page.count("sha-") >= 2 * 3


def test_price_table_and_assets_value_holdings(client, monkeypatch, tmp_path):
    loops = make_loops(client, [[CLEAN], [CLEAN], [CLEAN]], monkeypatch)
    snapshots = run_rounds(loops, 1, tmp_path)
    snap = snapshots[0]
    prices = price_table(snap["market"])
    for view in snap["dynasties"].values():
        assets = dynasty_assets(view, prices)
        assert assets >= 0          # GRAIN buffers are valued once GRAIN trades
