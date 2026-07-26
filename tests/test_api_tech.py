"""Tech API: admin-managed technologies, unlock grants, public reads, and
recipe gating fields."""

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econ.models import Base, User


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


def test_technology_crud(client):
    r = client.post("/admin/technologies",
                    json={"code": "fire", "name": "Fire", "scope": "world"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    assert r.json()["code"] == "FIRE"
    assert r.json()["scope"] == "world"

    r = client.post("/admin/technologies",
                    json={"code": "smelting", "prerequisites": ["FIRE", "fire"]},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    tech = r.json()
    assert tech["prerequisites"] == ["FIRE"]  # upper, deduped
    assert tech["scope"] == "entity"  # the default

    # Duplicates conflict; unknown prerequisites are rejected.
    r = client.post("/admin/technologies", json={"code": "FIRE"}, headers=_auth("u-admin"))
    assert r.status_code == 409
    r = client.post("/admin/technologies",
                    json={"code": "BAD", "prerequisites": ["WARP"]},
                    headers=_auth("u-admin"))
    assert r.status_code == 422

    # Technologies are public data; admin routes are not.
    r = client.get("/technologies", headers=_auth("u-user"))
    assert [t["code"] for t in r.json()] == ["FIRE", "SMELTING"]
    assert client.get("/technologies/WARP", headers=_auth("u-user")).status_code == 404
    r = client.post("/admin/technologies", json={"code": "X"}, headers=_auth("u-user"))
    assert r.status_code == 403

    r = client.patch("/admin/technologies/FIRE",
                     json={"name": "Mastery of fire", "is_active": False},
                     headers=_auth("u-admin"))
    assert r.json()["name"] == "Mastery of fire"
    assert r.json()["is_active"] is False


def test_grant_and_entity_unlocks(client):
    client.post("/admin/technologies", json={"code": "FIRE", "scope": "world"},
                headers=_auth("u-admin"))
    client.post("/admin/technologies", json={"code": "SMELTING", "prerequisites": ["FIRE"]},
                headers=_auth("u-admin"))
    entity_id = client.post("/entities", json={"name": "Alice", "entity_type": "individual"},
                            headers=_auth("u-user")).json()["id"]

    # The DAG binds admin grants too.
    r = client.post("/admin/technologies/SMELTING/grant", json={"entity_id": entity_id},
                    headers=_auth("u-admin"))
    assert r.status_code == 422
    assert "requires FIRE" in r.json()["detail"]

    r = client.post("/admin/technologies/FIRE/grant", json={"entity_id": entity_id},
                    headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert r.json()["technology"] == "FIRE"
    assert r.json()["entity_id"] is None  # world-scoped: held by everyone

    r = client.post("/admin/technologies/SMELTING/grant", json={"entity_id": entity_id},
                    headers=_auth("u-admin"))
    assert r.status_code == 200 and r.json()["entity_id"] == entity_id

    # Granting twice conflicts; unknown targets 404.
    r = client.post("/admin/technologies/FIRE/grant", json={"entity_id": entity_id},
                    headers=_auth("u-admin"))
    assert r.status_code == 409
    r = client.post("/admin/technologies/WARP/grant", json={"entity_id": entity_id},
                    headers=_auth("u-admin"))
    assert r.status_code == 404
    r = client.post("/admin/technologies/FIRE/grant", json={"entity_id": "nope"},
                    headers=_auth("u-admin"))
    assert r.status_code == 404

    # The owner sees own + world unlocks; strangers see nothing.
    r = client.get(f"/entities/{entity_id}/unlocks", headers=_auth("u-user"))
    assert [(u["technology"], u["entity_id"]) for u in r.json()] == [
        ("FIRE", None), ("SMELTING", entity_id),
    ]
    assert client.get(f"/entities/{entity_id}/unlocks",
                      headers=_auth("u-admin")).status_code == 200  # admin may look
    other = client.post("/entities", json={"name": "B", "entity_type": "individual"},
                        headers=_auth("u-admin")).json()["id"]
    assert client.get(f"/entities/{other}/unlocks",
                      headers=_auth("u-user")).status_code == 404


def test_recipe_gating_fields(client):
    client.post("/admin/technologies", json={"code": "SMITHING"}, headers=_auth("u-admin"))
    r = client.post("/admin/recipes",
                    json={"code": "study", "duration_ticks": 2,
                          "inputs": {"LABOR": "3"}, "unlocks": ["smithing"]},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    assert r.json()["unlocks"] == ["SMITHING"]
    assert r.json()["outputs"] == []  # pure research: an unlock is the output

    r = client.post("/admin/recipes",
                    json={"code": "forge", "duration_ticks": 1,
                          "inputs": {"IRON": "1"}, "outputs": {"SWORD": "1"},
                          "requires": ["SMITHING"]},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    assert r.json()["requires"] == ["SMITHING"]

    # Unknown technologies and empty recipes are rejected.
    r = client.post("/admin/recipes",
                    json={"code": "BAD", "duration_ticks": 1, "outputs": {"X": "1"},
                          "requires": ["WARP"]},
                    headers=_auth("u-admin"))
    assert r.status_code == 422
    r = client.post("/admin/recipes",
                    json={"code": "EMPTY", "duration_ticks": 1, "inputs": {"X": "1"}},
                    headers=_auth("u-admin"))
    assert r.status_code == 422
