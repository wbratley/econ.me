"""
Stochastic recipes through the HTTP API: admin defines an outcome table,
a user runs the process, and everything an outside auditor needs — the
branch table, the tick's events hash, the process's roll — is readable and
sufficient to verify the outcome.
"""

import pytest
from decimal import Decimal
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ import rng
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
            User(id="u-smith", email="smith@x", name="Smith",
                 provider="test", provider_id="2"),
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


FORGE_SWORD = {
    "code": "FORGE_SWORD", "duration_ticks": 1,
    "inputs": {"IRON": "2", "FORGE": "1"},
    "branches": [
        {"weight": "0.70", "outputs": {"SWORD": "1", "FORGE": "1"}},
        {"weight": "0.25", "outputs": {"SCRAP": "1", "FORGE": "1"},
         "label": "ruined the blank"},
        {"weight": "0.05", "outputs": {"SCRAP": "1"}, "label": "wrecked the forge"},
    ],
}


def test_branched_recipe_round_trip_and_validation(client):
    r = client.post("/admin/recipes", json=FORGE_SWORD, headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    branches = r.json()["branches"]
    assert [b["position"] for b in branches] == [0, 1, 2]
    assert [b["weight"] for b in branches] == ["0.7000", "0.2500", "0.0500"]
    assert branches[2]["label"] == "wrecked the forge"
    assert branches[2]["outputs"] == [{"symbol": "SCRAP", "quantity": "1.0000"}]
    assert r.json()["outputs"] == []

    # branches are public data, like the rest of the recipe
    r = client.get("/recipes/FORGE_SWORD", headers=_auth("u-smith"))
    assert len(r.json()["branches"]) == 3

    r = client.post("/admin/recipes",
                    json={"code": "BAD1", "duration_ticks": 1, "outputs": {"X": "1"},
                          "branches": [{"weight": "1", "outputs": {"Y": "1"}}]},
                    headers=_auth("u-admin"))
    assert r.status_code == 422 and "not both" in r.json()["detail"]
    r = client.post("/admin/recipes",
                    json={"code": "BAD2", "duration_ticks": 1,
                          "branches": [{"weight": "lucky", "outputs": {"Y": "1"}}]},
                    headers=_auth("u-admin"))
    assert r.status_code == 422


def test_outcome_is_auditable_through_the_api(client):
    client.post("/admin/recipes", json=FORGE_SWORD, headers=_auth("u-admin"))
    r = client.post("/entities", json={"name": "Smithy", "entity_type": "business"},
                    headers=_auth("u-smith"))
    entity_id = r.json()["id"]
    for symbol, delta in (("IRON", "2"), ("FORGE", "1")):
        client.post("/admin/holdings",
                    json={"entity_id": entity_id, "symbol": symbol, "delta": delta},
                    headers=_auth("u-admin"))

    r = client.post("/processes", json={"entity_id": entity_id, "recipe": "FORGE_SWORD"},
                    headers=_auth("u-smith"))
    assert r.status_code == 201, r.text
    process = r.json()
    assert process["outcome_branch"] is None  # not rolled yet

    client.post("/admin/ticks", headers=_auth("u-admin"))  # tick 1: still forging
    client.post("/admin/ticks", headers=_auth("u-admin"))  # tick 2: completes

    # the audit, using only what the API serves: tick 1's events hash is the
    # seed, the process id the salt, the recipe's table the odds
    tick1 = client.get("/admin/ticks/1", headers=_auth("u-admin")).json()
    assert tick1["events_hash"] == rng.hash_events(tick1["events"])
    r = client.get("/processes", headers=_auth("u-smith"))
    completed = r.json()[0]
    assert completed["status"] == "completed"
    roll = rng.outcome_roll(tick1["events_hash"], completed["id"])
    assert completed["outcome_roll"] == roll
    recipe = client.get("/recipes/FORGE_SWORD", headers=_auth("u-smith")).json()
    weights = [Decimal(b["weight"]) for b in recipe["branches"]]
    index = rng.weighted_index(roll, weights)
    assert completed["outcome_branch"] == index

    # holdings match the audited branch exactly (catalyst included)
    r = client.get(f"/entities/{entity_id}/holdings", headers=_auth("u-smith"))
    holdings = {h["symbol"]: h["quantity"] for h in r.json()
                if Decimal(h["quantity"]) != 0}
    expected = {o["symbol"]: f'{Decimal(o["quantity"]):.4f}'
                for o in recipe["branches"][index]["outputs"]}
    assert holdings == expected

    # and the tick event tells the same story
    tick2 = client.get("/admin/ticks/2", headers=_auth("u-admin")).json()
    event = next(e for e in tick2["events"] if e["type"] == "process_completed")
    assert event["branch"] == index and event["roll"] == roll
