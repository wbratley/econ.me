"""Admin compute-budget endpoints (entity_tick_compute_budget_ms, votable
world data — design.md §4.5 compute budgets)."""

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


def test_compute_budget_defaults_to_unlimited(client):
    r = client.get("/admin/compute-budget", headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json() == {"budget_ms": None}


def test_compute_budget_set_and_read_back(client):
    r = client.put("/admin/compute-budget", json={"budget_ms": 50}, headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json() == {"budget_ms": 50}

    r = client.get("/admin/compute-budget", headers=_auth("u-admin"))
    assert r.json() == {"budget_ms": 50}


def test_compute_budget_null_clears_it(client):
    client.put("/admin/compute-budget", json={"budget_ms": 50}, headers=_auth("u-admin"))
    r = client.put("/admin/compute-budget", json={"budget_ms": None}, headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json() == {"budget_ms": None}
    assert client.get("/admin/compute-budget", headers=_auth("u-admin")).json() == {"budget_ms": None}


def test_compute_budget_is_admin_only(client):
    assert client.get("/admin/compute-budget", headers=_auth("u-user")).status_code == 403
    assert client.put("/admin/compute-budget", json={"budget_ms": 10},
                      headers=_auth("u-user")).status_code == 403
