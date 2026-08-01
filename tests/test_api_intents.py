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


def test_levy_intent_seizes_through_the_api(client):
    """The full HTTP path for enforced collection: a levy-capable
    government (owned by admin) compels money out of a citizen's account
    it does not own, into its treasury. Capability gates at the boundary;
    ownership of the source is bypassed by privilege + rule_ref."""
    # a citizen with a funded account (owned by alice)
    citizen = _make_entity(client, "u-alice", "Citizen")
    ca = _make_account(client, "u-alice", citizen["id"], initial_balance="1000")

    # a government entity owned by admin, granted the levy capability
    r = client.post("/admin/entities",
                    json={"name": "Treasury", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    gov = r.json()
    r = client.patch(f"/admin/entities/{gov['id']}",
                     json={"capabilities": ["levy"]}, headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    ga = _make_account(client, "u-admin", gov["id"], initial_balance="0")

    # admin drives the government to levy the citizen
    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "levy",
         "params": {"from_account_id": ca["id"], "to_account_id": ga["id"],
                    "amount": "300", "rule_ref": "tax:income", "reference": "Q1"}},
    ], headers=_auth("u-admin"))
    assert r.status_code == 200, r.text
    result = r.json()[0]
    assert result["status"] == "applied", result

    citizen_bal = client.get(f"/entities/{citizen['id']}",
                             headers=_auth("u-alice")).json()["accounts"][0]["balance"]
    gov_bal = client.get(f"/entities/{gov['id']}",
                         headers=_auth("u-admin")).json()["accounts"][0]["balance"]
    assert citizen_bal == "700.0000"   # seized
    assert gov_bal == "300.0000"        # collected


def test_levy_intent_rejected_without_capability(client):
    """A government with no levy capability cannot seize through the API."""
    citizen = _make_entity(client, "u-alice", "Citizen")
    ca = _make_account(client, "u-alice", citizen["id"], initial_balance="1000")
    r = client.post("/admin/entities",
                    json={"name": "WeakGov", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    gov = r.json()  # no capabilities granted
    ga = _make_account(client, "u-admin", gov["id"], initial_balance="0")

    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "levy",
         "params": {"from_account_id": ca["id"], "to_account_id": ga["id"],
                    "amount": "300", "rule_ref": "tax:income"}},
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert "levy" in result["reason"]
    # citizen untouched
    citizen_bal = client.get(f"/entities/{citizen['id']}",
                             headers=_auth("u-alice")).json()["accounts"][0]["balance"]
    assert citizen_bal == "1000.0000"


def test_set_fiscal_policy_intent_sets_votable_rate(client):
    """The HTTP path for enacting fiscal policy: a set_fiscal_policy-
    capable government (owned by admin) replaces the votable policy dict.
    Capability gates at the boundary; this is Fork 4B — the power to set
    policy is held by the role, not by a superuser."""
    r = client.post("/admin/entities",
                    json={"name": "Treasury", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    gov = r.json()
    r = client.patch(f"/admin/entities/{gov['id']}",
                     json={"capabilities": ["set_fiscal_policy"]},
                     headers=_auth("u-admin"))
    assert r.status_code == 200

    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "set_fiscal_policy",
         "params": {"policy": '{"rate": "0.10", "rule": "income"}'}},
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "applied", result


def test_set_fiscal_policy_intent_rejected_without_capability(client):
    """A government with no set_fiscal_policy capability cannot enact
    policy through the API."""
    r = client.post("/admin/entities",
                    json={"name": "WeakGov", "entity_type": "government",
                          "owner_id": "u-admin"},
                    headers=_auth("u-admin"))
    gov = r.json()  # no capabilities

    r = client.post("/intents", json=[
        {"entity_id": gov["id"], "type": "set_fiscal_policy",
         "params": {"policy": '{"rate": "0.10"}'}},
    ], headers=_auth("u-admin"))
    result = r.json()[0]
    assert result["status"] == "rejected"
    assert "set_fiscal_policy" in result["reason"]
