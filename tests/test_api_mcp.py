"""Tests for the MCP player interface -- the agent surface (Phase 1, game.md
§11; the observability decision in §13).

Two files implement it: ``econ/api/routers/mcp.py`` (a hand-rolled stateless
MCP Streamable HTTP server: JSON-RPC 2.0 over POST /mcp, plain JSON replies)
and ``econ/api/mcp_tools.py`` (the tool surface). Auth is the existing bearer
scheme; every tool is a thin wrapper over the same platform paths REST
serves.

The §13 resolution under test: **the agent sees exactly what its own
behaviour script sees** -- the event digest filters to ``entity_id == own``,
the same filter the engine applies when feeding BEHAVIOUR scripts each tick.
No omniscience.
"""

import json

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine
from decimal import Decimal

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine import services
from econengine.models import Base, EntityType, Market, User


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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
            raise HTTPException(status_code=401, detail="User not found")
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    with Session(engine) as session:
        session.add_all([
            User(id="u-admin", email="admin@x", name="Admin",
                 provider="test", provider_id="1", is_admin=True),
            User(id="u-alice", email="alice@x", name="Alice",
                 provider="test", provider_id="2"),
            User(id="u-bob", email="bob@x", name="Bob",
                 provider="test", provider_id="3"),
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def rpc(client, method, params=None, id=1, user="u-alice"):
    return client.post(
        "/mcp", headers=_auth(user),
        json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}},
    )


def call_tool(client, name, arguments=None, user="u-alice"):
    r = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, user=user)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    return body["result"]


def call_json(client, name, arguments=None, user="u-alice"):
    """A successful tool call, with its text content parsed back to JSON."""
    result = call_tool(client, name, arguments, user=user)
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


# ===========================================================================
# Protocol: initialize / listing / framing / errors
# ===========================================================================

def test_initialize_echoes_supported_version(client):
    r = rpc(client, "initialize", {"protocolVersion": "2025-03-26"}).json()
    result = r["result"]
    assert result["protocolVersion"] == "2025-03-26"  # echo what we support
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "econ.me"


def test_initialize_unsupported_version_gets_server_latest(client):
    r = rpc(client, "initialize", {"protocolVersion": "1999-01-01"}).json()
    assert r["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_exposes_the_player_surface(client):
    r = rpc(client, "tools/list").json()
    tools = {t["name"]: t for t in r["result"]["tools"]}
    assert set(tools) == {
        "join", "my_entities", "entity_state", "entity_events",
        "get_behaviour", "get_script_libraries", "set_behaviour",
        "round_state", "epoch_state", "governance_current",
        "market_prices", "leaderboard",
    }
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"
        assert t["description"]


def test_ping(client):
    assert rpc(client, "ping").json()["result"] == {}


def test_notification_gets_202(client):
    r = client.post(
        "/mcp", headers=_auth("u-alice"),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert r.status_code == 202


def test_unknown_method_is_32601(client):
    err = rpc(client, "resources/list").json()["error"]
    assert err["code"] == -32601


def test_unknown_tool_is_32602(client):
    err = rpc(client, "tools/call", {"name": "nope", "arguments": {}}).json()["error"]
    assert err["code"] == -32602


def test_parse_error(client):
    r = client.post("/mcp", headers=_auth("u-alice"), content=b"{bad json")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


def test_batch_of_request_and_notification(client):
    r = client.post("/mcp", headers=_auth("u-alice"), json=[
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 7, "method": "ping"},
    ])
    assert r.status_code == 200
    responses = r.json()
    assert len(responses) == 1
    assert responses[0]["id"] == 7 and responses[0]["result"] == {}


def test_get_mcp_is_405(client):
    assert client.get("/mcp", headers=_auth("u-alice")).status_code == 405


def test_mcp_requires_auth(client):
    assert client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    ).status_code in (401, 403)


# ===========================================================================
# Tools: joining and the dynasty view
# ===========================================================================

def test_join_creates_founder(client):
    joined = call_json(client, "join")
    assert joined["entity"]["entity_type"] == "individual"
    assert joined["account"]["balance"] == "0.0000"  # default join config
    mine = call_json(client, "my_entities")
    assert [e["id"] for e in mine] == [joined["entity"]["id"]]


def test_entity_state_snapshot(client):
    joined = call_json(client, "join")
    state = call_json(client, "entity_state", {"entity_id": joined["entity"]["id"]})
    assert state["entity"]["id"] == joined["entity"]["id"]
    assert state["entity"]["is_fixed"] is False
    assert set(state) == {
        "entity", "accounts", "holdings", "needs", "processes",
        "parcels", "unlocks", "behaviour",
    }
    assert len(state["accounts"]) == 1
    assert state["behaviour"] is None  # no starter configured


def test_entity_state_is_ownership_gated(client):
    joined = call_json(client, "join", user="u-alice")
    # bob asks for alice's entity: a tool error, and no info leak
    result = call_tool(client, "entity_state",
                       {"entity_id": joined["entity"]["id"]}, user="u-bob")
    assert result["isError"] is True
    assert "not yours" in result["content"][0]["text"]


# ===========================================================================
# Behaviour: read / write via the autonomy path
# ===========================================================================

def test_set_and_get_behaviour_roundtrip(client):
    joined = call_json(client, "join")
    eid = joined["entity"]["id"]
    call_json(client, "set_behaviour",
              {"entity_id": eid, "source": "-- idle", "description": "do nothing"})
    src = call_json(client, "get_behaviour", {"entity_id": eid})
    assert src["source"] == "-- idle"
    assert src["description"] == "do nothing"


def test_set_behaviour_requires_source(client):
    joined = call_json(client, "join")
    result = call_tool(client, "set_behaviour", {"entity_id": joined["entity"]["id"]})
    assert result["isError"] is True
    assert "source" in result["content"][0]["text"]


def test_set_behaviour_refuses_fixed_entities(client):
    engine = app.state._test_engine
    with Session(engine) as session:
        entity = services.create_entity(session, "WorldPhysics", EntityType.INDIVIDUAL)
        entity.owner_id = "u-alice"
        entity.is_fixed = True
        session.commit()
        eid = entity.id
    result = call_tool(client, "set_behaviour", {"entity_id": eid, "source": "-- x"})
    assert result["isError"] is True
    assert "fixed" in result["content"][0]["text"]


# ===========================================================================
# §13: the per-entity event digest -- no omniscience
# ===========================================================================

def test_event_digest_shows_only_own_events(client, monkeypatch):
    """Alice's erroring script emits script_error events each tick; bob's
    digest of the same ticks shows none of them -- the agent sees exactly
    what its own behaviour script sees (same entity_id filter as tick.py)."""
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "2")
    a = call_json(client, "join", user="u-alice")["entity"]["id"]
    call_json(client, "join", user="u-bob")
    call_json(client, "set_behaviour",
              {"entity_id": a, "source": 'error("boom")'}, user="u-alice")
    client.post("/admin/rounds/advance", headers=_auth("u-admin"))

    digest = call_json(client, "entity_events", {"entity_id": a}, user="u-alice")
    ticks = digest["ticks"]
    assert len(ticks) == 2
    types = [e["type"] for t in ticks for e in t["events"]]
    assert types.count("script_error") == 2  # K ticks of a failing script
    for t in ticks:
        for e in t["events"]:
            assert e["entity_id"] == a  # own events only

    # bob's digest of the same ticks: none of alice's events
    bob = call_json(client, "my_entities", user="u-bob")[0]["id"]
    bob_digest = call_json(client, "entity_events", {"entity_id": bob}, user="u-bob")
    for t in bob_digest["ticks"]:
        for e in t["events"]:
            assert e["entity_id"] != a


def test_event_digest_bounds_last_ticks(client):
    joined = call_json(client, "join")
    eid = joined["entity"]["id"]
    for bad in (0, 51):
        result = call_tool(client, "entity_events",
                           {"entity_id": eid, "last_ticks": bad})
        assert result["isError"] is True


# ===========================================================================
# World facts: round clock + market prices
# ===========================================================================

def test_round_state_tracks_rest_advances(client, monkeypatch):
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "1")
    call_json(client, "join")  # a player is watching the clock
    client.post("/admin/rounds/advance", headers=_auth("u-admin"))
    s = call_json(client, "round_state")
    assert s["round_number"] == 1
    assert s["current_round"] == 2
    assert s["ticks_run"] == 1


def test_market_prices_reports_last_trade(client):
    engine = app.state._test_engine
    with Session(engine) as session:
        session.add_all([
            Market(symbol="WHEAT", currency="USD", last_price=Decimal("2.5000")),
            Market(symbol="IRON", currency="USD", last_price=None),
        ])
        session.commit()
    prices = {m["symbol"]: m for m in call_json(client, "market_prices")}
    assert prices["WHEAT"]["last_price"] == "2.5000"
    assert prices["IRON"]["last_price"] is None
    assert prices["WHEAT"]["currency"] == "USD"


# ===========================================================================
# §6: the script vocabulary tiers -- agent-authorable behaviours
# ===========================================================================

def test_get_script_libraries_exposes_all_tiers(client):
    """The tiers under every behaviour: engine std (source + fingerprint,
    always), world/pack libs when installed. Authoring from scratch means
    reading these; guessing is the nil-call trap."""
    tiers = call_json(client, "get_script_libraries")
    assert "amount_str" in tiers["std"]["source"]
    assert len(tiers["std"]["fingerprint"]) == 16
    assert tiers["world"] is None and tiers["pack"] is None

    from econengine import scripting
    engine = app.state._test_engine
    lib = "local t = {} function t.tag() return 'demo' end return t"
    with Session(engine) as session:
        scripting.set_world_lib(session, lib)
        session.commit()

    tiers = call_json(client, "get_script_libraries")
    assert tiers["world"] == lib


def test_set_behaviour_lint_refuses_and_warns(client):
    """Phase 3 on the agent surface: the nil-call trap is an isError with
    the finding in the text; a state-dependent script is accepted with
    `lint_warnings` in the result. An agent that reads the error can fix
    the typo in one round-trip -- the pre-tier era made it a zombie."""
    joined = call_json(client, "join", user="u-alice")
    eid = joined["entity"]["id"]

    result = call_tool(client, "set_behaviour",
                       {"entity_id": eid, "source": "local fills = settle_last_orders()"},
                       user="u-alice")
    assert result["isError"] is True
    assert "settle_last_orders" in result["content"][0]["text"]

    ok = call_json(client, "set_behaviour",
                   {"entity_id": eid,
                    "source": "ctx.state.hunger = ctx.state.hunger + 1"},
                   user="u-alice")
    assert ok["status"] == "active"
    assert len(ok["lint_warnings"]) == 1 and ok["lint_warnings"][0].startswith("smoke-run:")

    clean = call_json(client, "set_behaviour",
                      {"entity_id": eid, "source": "ctx.state.set = std.amount_str(1)"},
                      user="u-alice")
    assert clean["lint_warnings"] == []
