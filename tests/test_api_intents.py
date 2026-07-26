"""Machine-client intent API (design.md §4.5): POST /intents resolves a
batch of intents through the same scripting.resolve_intent dispatcher
Lua scripts and the tick engine already share, instead of the human
per-action REST endpoints."""

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine.models import Base, User


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

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
            User(id="u-mallory", email="mallory@x", name="Mallory",
                 provider="test", provider_id="3"),
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def _make_entity(client, owner, name="Alice Co"):
    r = client.post("/entities", json={"name": name, "entity_type": "individual"},
                    headers=_auth(owner))
    assert r.status_code == 201, r.text
    return r.json()


def _make_account(client, owner, entity_id, currency="USD", initial_balance="1000"):
    r = client.post(f"/entities/{entity_id}/accounts",
                    json={"currency": currency, "initial_balance": initial_balance},
                    headers=_auth(owner))
    assert r.status_code == 201, r.text
    return r.json()


def test_batch_applies_owned_entity_intents(client):
    alice = _make_entity(client, "u-alice")
    a = _make_account(client, "u-alice", alice["id"])
    bob = _make_entity(client, "u-alice", "Bob")
    b = _make_account(client, "u-alice", bob["id"], initial_balance="0")

    client.post("/admin/recipes",
                json={"code": "FORAGE", "duration_ticks": 1, "outputs": {"BERRIES": "1"}},
                headers=_auth("u-admin"))

    r = client.post("/intents", json=[
        {"entity_id": alice["id"], "type": "transfer",
         "params": {"from_account_id": a["id"], "to_account_id": b["id"],
                    "amount": "100", "reference": "rent"}},
        {"entity_id": alice["id"], "type": "start_process",
         "params": {"recipe": "FORAGE"}},
    ], headers=_auth("u-alice"))

    assert r.status_code == 200, r.text
    results = r.json()
    assert [item["status"] for item in results] == ["applied", "applied"]
    assert results[0]["type"] == "transfer"
    assert results[1]["process_id"]

    r = client.get(f"/entities/{alice['id']}/accounts/{a['id']}/transactions",
                    headers=_auth("u-alice"))
    assert any(t["amount"] == "100.0000" and t["tx_type"] == "debit" for t in r.json())


def test_batch_rejects_entity_the_caller_does_not_own(client):
    alice = _make_entity(client, "u-alice")
    a = _make_account(client, "u-alice", alice["id"])

    r = client.post("/intents", json=[
        {"entity_id": alice["id"], "type": "transfer",
         "params": {"from_account_id": a["id"], "to_account_id": a["id"],
                    "amount": "1", "reference": "steal"}},
    ], headers=_auth("u-mallory"))

    assert r.status_code == 200
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert result["reason"] == "entity not found"


def test_batch_rejects_unknown_intent_type(client):
    alice = _make_entity(client, "u-alice")

    r = client.post("/intents", json=[
        {"entity_id": alice["id"], "type": "teleport", "params": {}},
    ], headers=_auth("u-alice"))

    assert r.status_code == 200
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert "teleport" in result["reason"]


def test_batch_isolates_a_failing_intent_from_the_rest(client):
    alice = _make_entity(client, "u-alice")
    a = _make_account(client, "u-alice", alice["id"], initial_balance="50")
    bob = _make_entity(client, "u-alice", "Bob")
    b = _make_account(client, "u-alice", bob["id"], initial_balance="0")

    r = client.post("/intents", json=[
        {"entity_id": alice["id"], "type": "transfer",
         "params": {"from_account_id": a["id"], "to_account_id": b["id"],
                    "amount": "999999", "reference": "too much"}},
        {"entity_id": alice["id"], "type": "transfer",
         "params": {"from_account_id": a["id"], "to_account_id": b["id"],
                    "amount": "10", "reference": "fine"}},
    ], headers=_auth("u-alice"))

    assert r.status_code == 200
    results = r.json()
    assert results[0]["status"] == "rejected"
    assert results[1]["status"] == "applied"

    r = client.get(f"/entities/{bob['id']}/accounts/{b['id']}/transactions",
                    headers=_auth("u-alice"))
    assert [t["amount"] for t in r.json()] == ["10.0000"]
