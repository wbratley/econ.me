"""Goods API: admin-managed definitions, public reads, validation."""

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


def test_goods_crud(client):
    r = client.post("/admin/goods",
                    json={"symbol": "labor", "name": "Labor",
                          "decay_per_tick": "0", "auto_issue_quantity": "8",
                          "auto_issue_entity_type": "individual"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    good = r.json()
    assert good["symbol"] == "LABOR"
    assert good["auto_issue_quantity"] == "8.0000"
    assert good["auto_issue_entity_type"] == "individual"

    # Duplicate symbol conflicts.
    r = client.post("/admin/goods", json={"symbol": "LABOR"}, headers=_auth("u-admin"))
    assert r.status_code == 409

    # Goods are public data.
    r = client.get("/goods", headers=_auth("u-user"))
    assert [g["symbol"] for g in r.json()] == ["LABOR"]
    r = client.get("/goods/labor", headers=_auth("u-user"))
    assert r.status_code == 200 and r.json()["symbol"] == "LABOR"
    assert client.get("/goods/GOLD", headers=_auth("u-user")).status_code == 404

    # Patch updates properties; an explicit null clears the type filter.
    r = client.patch("/admin/goods/LABOR",
                     json={"decay_per_tick": "0.5", "auto_issue_entity_type": None},
                     headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert r.json()["decay_per_tick"] == "0.5000"
    assert r.json()["auto_issue_entity_type"] is None

    # An omitted field is untouched.
    r = client.patch("/admin/goods/LABOR", json={"name": "Toil"}, headers=_auth("u-admin"))
    assert r.json()["name"] == "Toil"
    assert r.json()["decay_per_tick"] == "0.5000"


def test_goods_validation(client):
    r = client.post("/admin/goods", json={"symbol": "X", "decay_per_tick": "1.5"},
                    headers=_auth("u-admin"))
    assert r.status_code == 422
    r = client.post("/admin/goods", json={"symbol": "X", "auto_issue_quantity": "-1"},
                    headers=_auth("u-admin"))
    assert r.status_code == 422
    r = client.post("/admin/goods", json={"symbol": "X", "decay_per_tick": "nope"},
                    headers=_auth("u-admin"))
    assert r.status_code == 422

    client.post("/admin/goods", json={"symbol": "X"}, headers=_auth("u-admin"))
    r = client.patch("/admin/goods/X", json={"decay_per_tick": "2"}, headers=_auth("u-admin"))
    assert r.status_code == 422
    r = client.patch("/admin/goods/X", json={"auto_issue_quantity": "-1"}, headers=_auth("u-admin"))
    assert r.status_code == 422


def test_goods_admin_only_writes(client):
    r = client.post("/admin/goods", json={"symbol": "LABOR"}, headers=_auth("u-user"))
    assert r.status_code == 403
    r = client.patch("/admin/goods/LABOR", json={"name": "x"}, headers=_auth("u-user"))
    assert r.status_code == 403
