"""
End-to-end production flow through the HTTP API: admin defines a recipe and
grants raw materials, a user starts a process, admin ticks past the duration,
and the outputs appear in the user's holdings.
"""

import pytest
from decimal import Decimal
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
            User(id="u-baker", email="baker@x", name="Baker",
                 provider="test", provider_id="2"),
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def test_full_production_flow(client):
    # Admin defines the recipe.
    r = client.post("/admin/recipes",
                    json={"code": "bake_bread", "name": "Bake bread", "duration_ticks": 2,
                          "inputs": {"flour": "2"}, "outputs": {"bread": "3"}},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    assert r.json()["code"] == "BAKE_BREAD"
    assert r.json()["inputs"] == [{"symbol": "FLOUR", "quantity": "2.0000"}]

    # Recipes are public data.
    r = client.get("/recipes", headers=_auth("u-baker"))
    assert [rec["code"] for rec in r.json()] == ["BAKE_BREAD"]

    # Baker sets up an entity; admin grants flour.
    r = client.post("/entities", json={"name": "Bakery", "entity_type": "business"},
                    headers=_auth("u-baker"))
    entity_id = r.json()["id"]
    r = client.post("/admin/holdings",
                    json={"entity_id": entity_id, "symbol": "FLOUR", "delta": "10"},
                    headers=_auth("u-admin"))
    assert r.status_code == 200, r.text

    # Baker starts the process (before tick 1 -> completes tick 3).
    r = client.post("/processes", json={"entity_id": entity_id, "recipe": "BAKE_BREAD"},
                    headers=_auth("u-baker"))
    assert r.status_code == 201, r.text
    process = r.json()
    assert process["status"] == "running"
    assert process["started_tick"] == 1
    assert process["completes_tick"] == 3

    # Inputs consumed immediately.
    r = client.get(f"/entities/{entity_id}/holdings", headers=_auth("u-baker"))
    assert [(h["symbol"], h["quantity"]) for h in r.json()] == [("FLOUR", "8.0000")]

    # Two ticks: still baking. Third tick: bread.
    for expected_bread in (None, None, "3.0000"):
        client.post("/admin/ticks", headers=_auth("u-admin"))
        r = client.get(f"/entities/{entity_id}/holdings", headers=_auth("u-baker"))
        bread = {h["symbol"]: h["quantity"] for h in r.json()}.get("BREAD")
        assert bread == expected_bread

    r = client.get("/processes", headers=_auth("u-baker"))
    assert [p["status"] for p in r.json()] == ["completed"]

    # The completion landed as a tick event.
    r = client.get("/admin/ticks/3", headers=_auth("u-admin"))
    assert any(e["type"] == "process_completed" for e in r.json()["events"])


def test_production_auth_and_validation(client):
    client.post("/admin/recipes",
                json={"code": "FORAGE", "duration_ticks": 1, "outputs": {"BERRIES": "1"}},
                headers=_auth("u-admin"))
    r = client.post("/entities", json={"name": "Camp", "entity_type": "individual"},
                    headers=_auth("u-baker"))
    entity_id = r.json()["id"]

    # Non-admin cannot define recipes; duplicates are 409.
    r = client.post("/admin/recipes",
                    json={"code": "X", "duration_ticks": 1, "outputs": {"Y": "1"}},
                    headers=_auth("u-baker"))
    assert r.status_code == 403
    r = client.post("/admin/recipes",
                    json={"code": "FORAGE", "duration_ticks": 1, "outputs": {"B": "1"}},
                    headers=_auth("u-admin"))
    assert r.status_code == 409

    # Can't start a process on an entity you don't own, or with a bad recipe.
    r = client.post("/processes", json={"entity_id": entity_id, "recipe": "FORAGE"},
                    headers=_auth("u-admin"))
    assert r.status_code == 404
    r = client.post("/processes", json={"entity_id": entity_id, "recipe": "NOPE"},
                    headers=_auth("u-baker"))
    assert r.status_code == 422

    # Deactivated recipes reject new processes.
    r = client.patch("/admin/recipes/FORAGE", json={"is_active": False},
                     headers=_auth("u-admin"))
    assert r.json()["is_active"] is False
    r = client.post("/processes", json={"entity_id": entity_id, "recipe": "FORAGE"},
                    headers=_auth("u-baker"))
    assert r.status_code == 422

    # Cancel: owner only, once only.
    client.patch("/admin/recipes/FORAGE", json={"is_active": True}, headers=_auth("u-admin"))
    r = client.post("/processes", json={"entity_id": entity_id, "recipe": "FORAGE"},
                    headers=_auth("u-baker"))
    process_id = r.json()["id"]
    r = client.post(f"/processes/{process_id}/cancel", headers=_auth("u-admin"))
    assert r.status_code == 404
    r = client.post(f"/processes/{process_id}/cancel", headers=_auth("u-baker"))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = client.post(f"/processes/{process_id}/cancel", headers=_auth("u-baker"))
    assert r.status_code == 422
