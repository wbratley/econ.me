"""Needs API: admin-managed definitions, public reads, per-entity states."""

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
            User(id="u-user", email="user@x", name="User",
                 provider="test", provider_id="2"),
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def test_needs_crud(client):
    r = client.post("/admin/needs",
                    json={"code": "food", "name": "Food", "entity_type": "individual",
                          "quantity_per_tick": "2", "priority": 1,
                          "satisfiers": ["fish", "BREAD", "fish"]},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    need = r.json()
    assert need["code"] == "FOOD"
    assert need["quantity_per_tick"] == "2.0000"
    assert need["satisfiers"] == ["BREAD", "FISH"]  # upper, sorted, deduped
    assert need["entity_type"] == "individual"

    # Duplicate code conflicts.
    r = client.post("/admin/needs",
                    json={"code": "FOOD", "quantity_per_tick": "1", "satisfiers": ["BREAD"]},
                    headers=_auth("u-admin"))
    assert r.status_code == 409

    # Needs are public data.
    r = client.get("/needs", headers=_auth("u-user"))
    assert [n["code"] for n in r.json()] == ["FOOD"]
    r = client.get("/needs/food", headers=_auth("u-user"))
    assert r.status_code == 200 and r.json()["code"] == "FOOD"
    assert client.get("/needs/SLEEP", headers=_auth("u-user")).status_code == 404

    # Patch updates parameters; satisfiers replace wholesale.
    r = client.patch("/admin/needs/FOOD",
                     json={"quantity_per_tick": "3", "priority": 0, "satisfiers": ["stew"]},
                     headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert r.json()["quantity_per_tick"] == "3.0000"
    assert r.json()["priority"] == 0
    assert r.json()["satisfiers"] == ["STEW"]

    # An omitted field is untouched; deactivation works.
    r = client.patch("/admin/needs/FOOD", json={"is_active": False}, headers=_auth("u-admin"))
    assert r.json()["is_active"] is False
    assert r.json()["satisfiers"] == ["STEW"]


def test_needs_validation(client):
    r = client.post("/admin/needs",
                    json={"code": "X", "quantity_per_tick": "0", "satisfiers": ["BREAD"]},
                    headers=_auth("u-admin"))
    assert r.status_code == 422
    r = client.post("/admin/needs",
                    json={"code": "X", "quantity_per_tick": "1", "satisfiers": []},
                    headers=_auth("u-admin"))
    assert r.status_code == 422
    r = client.post("/admin/needs",
                    json={"code": "X", "quantity_per_tick": "nope", "satisfiers": ["BREAD"]},
                    headers=_auth("u-admin"))
    assert r.status_code == 422

    client.post("/admin/needs",
                json={"code": "X", "quantity_per_tick": "1", "satisfiers": ["BREAD"]},
                headers=_auth("u-admin"))
    r = client.patch("/admin/needs/X", json={"quantity_per_tick": "-1"}, headers=_auth("u-admin"))
    assert r.status_code == 422
    r = client.patch("/admin/needs/X", json={"satisfiers": []}, headers=_auth("u-admin"))
    assert r.status_code == 422


def test_needs_admin_only_writes(client):
    r = client.post("/admin/needs",
                    json={"code": "FOOD", "quantity_per_tick": "1", "satisfiers": ["BREAD"]},
                    headers=_auth("u-user"))
    assert r.status_code == 403
    r = client.patch("/admin/needs/FOOD", json={"name": "x"}, headers=_auth("u-user"))
    assert r.status_code == 403


def test_entity_need_states(client):
    client.post("/admin/needs",
                json={"code": "FOOD", "quantity_per_tick": "1", "satisfiers": ["BREAD"],
                      "entity_type": "individual"},
                headers=_auth("u-admin"))
    r = client.post("/entities", json={"name": "Me", "entity_type": "individual"},
                    headers=_auth("u-user"))
    entity_id = r.json()["id"]

    # No consumption pass yet: no states.
    r = client.get(f"/entities/{entity_id}/needs", headers=_auth("u-user"))
    assert r.status_code == 200 and r.json() == []

    client.post("/admin/ticks", headers=_auth("u-admin"))

    r = client.get(f"/entities/{entity_id}/needs", headers=_auth("u-user"))
    assert r.json() == [{"need": "FOOD", "satisfaction": "0.0000", "updated_tick": 1}]

    # Another user's entity is invisible.
    assert client.get(f"/entities/{entity_id}/needs", headers=_auth("u-admin")).status_code == 404
