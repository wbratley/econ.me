"""Conditions API: condition properties on goods and needs, entity
lifecycle exposure, heirs, and the admin estate rule."""

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


def _make_entity(client, name="Somebody"):
    r = client.post("/admin/entities", json={"name": name, "entity_type": "individual"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    return r.json()


def test_condition_good_roundtrip(client):
    r = client.post("/admin/goods",
                    json={"symbol": "cond-weak", "decay_per_tick": "0.1",
                          "modifies_pattern": "labor-*", "modifies_factor": "0.5",
                          "incapacitates_at": "50"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    good = r.json()
    assert good["modifies_pattern"] == "LABOR-*"
    assert good["modifies_factor"] == "0.5000"
    assert good["incapacitates_at"] == "50.0000"

    # explicit nulls clear the condition properties
    r = client.patch("/admin/goods/COND-WEAK",
                     json={"modifies_pattern": None, "modifies_factor": None,
                           "incapacitates_at": None},
                     headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert r.json()["modifies_pattern"] is None
    assert r.json()["incapacitates_at"] is None

    # pattern without factor is rejected
    r = client.patch("/admin/goods/COND-WEAK", json={"modifies_pattern": "LABOR-*"},
                     headers=_auth("u-admin"))
    assert r.status_code == 422


def test_need_condition_roundtrip(client):
    r = client.post("/admin/needs",
                    json={"code": "food", "quantity_per_tick": "1", "satisfiers": ["bread"],
                          "condition_symbol": "cond-weak", "condition_quantity": "2"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    need = r.json()
    assert need["condition_symbol"] == "COND-WEAK"
    assert need["condition_quantity"] == "2.0000"

    # clearing the symbol clears the pair
    r = client.patch("/admin/needs/FOOD", json={"condition_symbol": None},
                     headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert r.json()["condition_symbol"] is None
    assert r.json()["condition_quantity"] == "0.0000"

    # symbol without a positive quantity is rejected
    r = client.post("/admin/needs",
                    json={"code": "warmth", "quantity_per_tick": "1", "satisfiers": ["wood"],
                          "condition_symbol": "cond-cold"},
                    headers=_auth("u-admin"))
    assert r.status_code == 422


def test_entity_status_and_heir(client):
    person = _make_entity(client, "Person")
    heir = _make_entity(client, "Heir")
    assert person["status"] == "active"
    assert person["incapacitated_tick"] is None

    r = client.patch(f"/admin/entities/{person['id']}", json={"heir_id": heir["id"]},
                     headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert r.json()["heir_id"] == heir["id"]

    r = client.patch(f"/admin/entities/{person['id']}", json={"heir_id": person["id"]},
                     headers=_auth("u-admin"))
    assert r.status_code == 422  # cannot be your own heir
    r = client.patch(f"/admin/entities/{person['id']}", json={"heir_id": "nope"},
                     headers=_auth("u-admin"))
    assert r.status_code == 422

    r = client.patch(f"/admin/entities/{person['id']}", json={"heir_id": None},
                     headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json()["heir_id"] is None


def test_estate_rule_endpoint(client):
    r = client.get("/admin/estate-rule", headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json() == {"policy": "burn", "treasury_entity_id": None}

    r = client.put("/admin/estate-rule", json={"policy": "heir"}, headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json()["policy"] == "heir"

    treasury = _make_entity(client, "Treasury")
    r = client.put("/admin/estate-rule",
                   json={"policy": "treasury", "treasury_entity_id": treasury["id"]},
                   headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json()["treasury_entity_id"] == treasury["id"]

    assert client.put("/admin/estate-rule", json={"policy": "guillotine"},
                      headers=_auth("u-admin")).status_code == 422
    assert client.put("/admin/estate-rule", json={"policy": "treasury"},
                      headers=_auth("u-admin")).status_code == 422

    # admin-only, like every world datum until governance lands
    assert client.get("/admin/estate-rule", headers=_auth("u-user")).status_code == 403
