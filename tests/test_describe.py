"""The action registry and the audit-trail reads (Phase 3b, game.md 15.2/15.3).

The property under test: **the registry is total.** A renderer exists
for every event type the engine can emit and every intent type it
dispatches; anything else falls back to a generic render rather than
raising  an unrenderable action is impossible, not a silent gap in the
log. And the reads: your log is your own events; the world's log is
public facts (13).
"""
import json

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine.capabilities import INTENT_CAPABILITIES
from econengine.describe import (
    ENGINE_EVENT_TYPES, FREE_INTENT_TYPES, render_event,
)
from econengine.models import Base, Entity, EntityType, Tick, User


# --- The registry is total -------------------------------------------------


def test_every_engine_outcome_type_renders():
    for etype in ENGINE_EVENT_TYPES:
        text = render_event({"type": etype})
        assert isinstance(text, str) and text.strip()
        # Not the generic fallback: a renderer shaped the sentence.
        assert text != etype.replace("_", " ")


def test_every_intent_type_renders():
    for itype in sorted(set(INTENT_CAPABILITIES) | set(FREE_INTENT_TYPES)):
        text = render_event({"type": itype, "params": {}})
        assert isinstance(text, str) and text.strip()


def test_unknown_event_type_renders_not_raises():
    assert render_event({"type": "a_future_shape", "x": 1}) == "a future shape"


def test_trade_renders_the_fill():
    e = {"type": "trade", "side": "sell", "market": "ORE", "quantity": "2",
         "price": "5", "cost": "10"}
    assert render_event(e, {"ORE": "Iron Ore"}) == "sold 2 Iron Ore for 10 @ 5"


def test_rejected_intent_renders_the_refusal():
    e = {"type": "place_order", "params": {"side": "buy", "symbol": "BREAD",
                                           "quantity": "12", "limit_price": "12"},
         "status": "rejected", "reason": "insufficient funds"}
    assert render_event(e) == (
        "placed a buy order: 12 BREAD @ 12 — refused: insufficient funds"
    )


def test_run9_event_shapes_render():
    """Real shapes lifted from stone-run9's world log."""
    assert render_event({
        "type": "auction", "entity_id": None, "market": "BERRIES",
        "price": "1.8000", "volume": "0.0954", "trades": 1,
    }, {"BERRIES": "Berries"}) == "auction: Berries cleared 0.0954 @ 1.8 (1 trades)"
    assert render_event({
        "type": "need_unmet", "entity_id": "x", "need": "WARMTH",
        "consumed": "0.8813", "required": "1.5000", "satisfaction": "0.5875",
        "condition": "EXPOSURE", "granted": "0.4125",
    }) == "warmth unmet: only 0.8813 of 1.5 — EXPOSURE +0.4125"
    assert render_event({
        "type": "process_completed", "entity_id": "x", "process_id": "p",
        "recipe": "GATHER", "outputs": {"BERRIES": "3"},
    }, {"BERRIES": "Berries"}) == "gather: +3 Berries"
    assert render_event({
        "type": "entity_incapacitated", "entity_id": "x", "condition": "HUNGER",
        "quantity": "15.0279", "threshold": "15.0000",
        "recipient_id": "heir",
    }) == "incapacitated: HUNGER reached 15.0279 (threshold 15); estate applied"


# --- The reads: /activity, /entities/{id}/activity, MCP entity_activity ----


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def api_client(session):
    session.add(User(id="u-alice", email="alice@x", name="Alice",
                     provider="test", provider_id="1"))
    entity = Entity(name="House Test", entity_type=EntityType.INDIVIDUAL,
                    owner_id="u-alice")
    session.add(entity)
    session.flush()
    # Two ticks of a plausible world log: an attributed trade and a
    # refused order for alice's entity; a public auction and decay.
    session.add(Tick(number=1, events=[
        {"type": "trade", "entity_id": entity.id, "side": "buy",
         "market": "ORE", "price": "5", "quantity": "2", "cost": "10"},
        {"type": "auction", "entity_id": None, "market": "ORE",
         "price": "5", "volume": "2", "trades": 1},
    ]))
    session.add(Tick(number=2, events=[
        {"type": "decay", "entity_id": None, "symbol": "ORE",
         "decayed": "4", "holders": 2},
        {"type": "place_order", "entity_id": entity.id,
         "params": {"side": "buy", "symbol": "BREAD", "quantity": "12",
                    "limit_price": "12"},
         "status": "rejected", "reason": "insufficient funds"},
    ]))
    session.commit()
    app.state._test_engine = session.get_bind()

    def override_get_session():
        yield session

    def override_get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
        s: Session = Depends(get_session),
    ) -> User:
        user = s.get(User, credentials.credentials)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user
    try:
        yield TestClient(app), entity
    finally:
        app.dependency_overrides.clear()


def test_entity_activity_is_own_events_rendered(api_client):
    client, entity = api_client
    r = client.get(f"/entities/{entity.id}/activity",
                   headers={"Authorization": "Bearer u-alice"})
    assert r.status_code == 200, r.text
    rows = r.json()["activity"]
    # Newest tick first; only this entity's events  the auction and
    # decay (public facts) do not ride along.
    assert [(row["tick"], row["text"]) for row in rows] == [
        (2, "placed a buy order: 12 BREAD @ 12 — refused: insufficient funds"),
        (1, "bought 2 ORE for 10 @ 5"),
    ]


def test_world_activity_is_public_facts_only(api_client):
    client, _ = api_client
    r = client.get("/activity", headers={"Authorization": "Bearer u-alice"})
    assert r.status_code == 200, r.text
    rows = r.json()["activity"]
    assert [(row["tick"], row["text"]) for row in rows] == [
        (2, "decay: 4 ORE rotted across 2 holders"),
        (1, "auction: ORE cleared 2 @ 5 (1 trades)"),
    ]


def test_entity_activity_scoped_to_owner(api_client):
    client, _ = api_client
    # Bob cannot read alice's log: 404, not 403  the entity does not
    # exist from bob's seat (13).
    session = client.app.dependency_overrides[get_current_user]
    r = client.get("/entities/nonexistent/activity",
                   headers={"Authorization": "Bearer u-alice"})
    assert r.status_code == 404


def test_mcp_entity_activity_is_the_same_read(api_client):
    client, entity = api_client
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "entity_activity",
                   "arguments": {"entity_id": entity.id, "last_ticks": 10}},
    }
    r = client.post("/mcp", headers={"Authorization": "Bearer u-alice"},
                    json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    data = json.loads(body["result"]["content"][0]["text"])
    assert data["entity_id"] == entity.id
    texts = [row["text"] for row in data["activity"]]
    assert "bought 2 ORE for 10 @ 5" in texts
