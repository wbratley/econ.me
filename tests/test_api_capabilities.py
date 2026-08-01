"""API-layer capability flow (docs/actors.md step 1).

An entity starts with no capabilities and cannot issue money through
/intents. An admin grants the monetary_authority capability via the admin
endpoint; the same intent then clears. Granting is admin-gated, and unknown
capability names are rejected.
"""
import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine.capabilities import MONETARY_AUTHORITY
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
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def _issue_intent(entity_id, account_id):
    return [{
        "entity_id": entity_id,
        "type": "issue_money",
        "params": {"account_id": account_id, "amount": "500", "reference": "iss"},
    }]


def test_issue_money_blocked_until_capability_granted(client):
    # a bank entity alice owns, with a USD account
    bank = client.post("/entities", json={"name": "Alice Bank", "entity_type": "bank"},
                       headers=_auth("u-alice")).json()
    acct = client.post(f"/entities/{bank['id']}/accounts",
                       json={"currency": "USD", "initial_balance": "0"},
                       headers=_auth("u-alice")).json()

    # no capability yet -> rejected by the engine gate
    r = client.post("/intents", json=_issue_intent(bank["id"], acct["id"]),
                    headers=_auth("u-alice"))
    assert r.status_code == 200
    out = r.json()[0]
    assert out["status"] == "rejected"
    assert "monetary_authority" in out["reason"]

    # alice cannot grant herself the capability (not admin)
    r = client.patch(f"/admin/entities/{bank['id']}",
                     json={"capabilities": [MONETARY_AUTHORITY]},
                     headers=_auth("u-alice"))
    assert r.status_code == 403

    # admin grants it
    r = client.patch(f"/admin/entities/{bank['id']}",
                     json={"capabilities": [MONETARY_AUTHORITY]},
                     headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    assert MONETARY_AUTHORITY in r.json()["capabilities"]

    # same intent now clears
    r = client.post("/intents", json=_issue_intent(bank["id"], acct["id"]),
                    headers=_auth("u-alice"))
    assert r.status_code == 200
    out = r.json()[0]
    assert out["status"] == "applied", out


def test_admin_rejects_unknown_capability(client):
    bank = client.post("/entities", json={"name": "X", "entity_type": "bank"},
                       headers=_auth("u-admin")).json()
    r = client.patch(f"/admin/entities/{bank['id']}",
                     json={"capabilities": ["definitely_not_real"]},
                     headers=_auth("u-admin"))
    assert r.status_code == 422
