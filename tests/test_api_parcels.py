"""Parcels through the HTTP API: admin lays down genesis land, the registry
is public, ownership moves only by owner intent (or admin grant), and the
farming loop — claim, build, grow — runs end to end."""

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
            User(id="u-alice", email="alice@x", name="Alice",
                 provider="test", provider_id="2"),
            User(id="u-bob", email="bob@x", name="Bob",
                 provider="test", provider_id="3"),
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def make_entity(client, user, name="Farm Co"):
    r = client.post("/entities", json={"name": name, "entity_type": "individual"},
                    headers=_auth(user))
    return r.json()["id"]


def make_parcel(client, parcel_type="FIELD", owner_entity_id=None, **extra):
    r = client.post("/admin/parcels",
                    json={"parcel_type": parcel_type,
                          "owner_entity_id": owner_entity_id, **extra},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_admin_lays_down_genesis_land(client):
    parcel_id = make_parcel(client, "field", name="North field",
                            region_id="R1", extent_ref="chunk:0,0")
    r = client.post(f"/admin/parcels/{parcel_id}/facilities",
                    json={"facility_type": "farm"}, headers=_auth("u-admin"))
    assert r.status_code == 201
    r = client.post(f"/admin/parcels/{parcel_id}/deposits",
                    json={"symbol": "soil", "quantity": "10", "capacity": "10",
                          "regen_per_tick": "0.1"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201
    parcel = r.json()
    assert parcel["parcel_type"] == "FIELD" and parcel["owner_id"] is None
    assert parcel["facilities"][0]["facility_type"] == "FARM"
    assert parcel["facilities"][0]["built_tick"] is None  # genesis
    assert parcel["deposits"] == [{"symbol": "SOIL", "quantity": "10.0000",
                                   "capacity": "10.0000", "regen_per_tick": "0.1000"}]

    # duplicate deposit and bad quantities are refused
    r = client.post(f"/admin/parcels/{parcel_id}/deposits",
                    json={"symbol": "SOIL", "quantity": "1"}, headers=_auth("u-admin"))
    assert r.status_code == 409
    r = client.post(f"/admin/parcels/{parcel_id}/deposits",
                    json={"symbol": "IRON", "quantity": "lots"}, headers=_auth("u-admin"))
    assert r.status_code == 422

    # non-admins cannot make land
    r = client.post("/admin/parcels", json={"parcel_type": "LOT"},
                    headers=_auth("u-alice"))
    assert r.status_code == 403


def test_registry_is_public_and_filterable(client):
    alice = make_entity(client, "u-alice")
    make_parcel(client, "FIELD", region_id="R1", owner_entity_id=alice)
    make_parcel(client, "LOT", region_id="R2")

    r = client.get("/parcels", headers=_auth("u-bob"))  # anyone can read
    assert r.status_code == 200 and len(r.json()) == 2
    r = client.get("/parcels", params={"region_id": "R1"}, headers=_auth("u-bob"))
    assert [p["region_id"] for p in r.json()] == ["R1"]
    r = client.get("/parcels", params={"owner_entity_id": alice}, headers=_auth("u-bob"))
    assert [p["owner_id"] for p in r.json()] == [alice]


def test_transfer_by_owner_intent_only(client):
    alice = make_entity(client, "u-alice")
    bob = make_entity(client, "u-bob", "Bob's")
    parcel_id = make_parcel(client, "LOT", owner_entity_id=alice)

    # bob cannot move alice's land (indistinguishable from not-found)
    r = client.post(f"/parcels/{parcel_id}/transfer", json={"to_entity_id": bob},
                    headers=_auth("u-bob"))
    assert r.status_code == 404

    r = client.post(f"/parcels/{parcel_id}/transfer", json={"to_entity_id": bob},
                    headers=_auth("u-alice"))
    assert r.status_code == 200 and r.json()["owner_id"] == bob


def test_admin_grant_assigns_and_revokes(client):
    alice = make_entity(client, "u-alice")
    parcel_id = make_parcel(client, "LOT")
    r = client.post(f"/admin/parcels/{parcel_id}/grant", json={"to_entity_id": alice},
                    headers=_auth("u-admin"))
    assert r.status_code == 200 and r.json()["owner_id"] == alice
    r = client.post(f"/admin/parcels/{parcel_id}/grant", json={},
                    headers=_auth("u-admin"))
    assert r.status_code == 200 and r.json()["owner_id"] is None


def test_farming_loop_end_to_end(client):
    """The build-order acceptance loop: claim a field, build a farm on it,
    grow wheat at the farm, wait ticks, harvest."""
    alice = make_entity(client, "u-alice")
    parcel_id = make_parcel(client, "FIELD", owner_entity_id=alice)
    client.post("/admin/holdings",
                json={"entity_id": alice, "symbol": "TIMBER", "delta": "10"},
                headers=_auth("u-admin"))
    client.post("/admin/holdings",
                json={"entity_id": alice, "symbol": "SEED", "delta": "5"},
                headers=_auth("u-admin"))

    r = client.post("/admin/recipes",
                    json={"code": "BUILD_FARM", "duration_ticks": 1,
                          "inputs": {"TIMBER": "10"}, "builds_facility": "FARM"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    assert r.json()["builds_facility"] == "FARM"
    r = client.post("/admin/recipes",
                    json={"code": "GROW_WHEAT", "duration_ticks": 2,
                          "inputs": {"SEED": "1"}, "outputs": {"WHEAT": "8"},
                          "requires_facility": "FARM"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text

    # growing needs the farm, which does not exist yet
    r = client.post("/processes",
                    json={"entity_id": alice, "recipe": "GROW_WHEAT",
                          "parcel_id": parcel_id},
                    headers=_auth("u-alice"))
    assert r.status_code == 422 and "no free FARM" in r.json()["detail"]
    # and a parcel-bound recipe cannot be started unbound
    r = client.post("/processes", json={"entity_id": alice, "recipe": "GROW_WHEAT"},
                    headers=_auth("u-alice"))
    assert r.status_code == 422 and "bound to a parcel" in r.json()["detail"]

    r = client.post("/processes",
                    json={"entity_id": alice, "recipe": "BUILD_FARM",
                          "parcel_id": parcel_id},
                    headers=_auth("u-alice"))
    assert r.status_code == 201 and r.json()["parcel_id"] == parcel_id
    client.post("/admin/ticks", headers=_auth("u-admin"))  # tick 1: building
    client.post("/admin/ticks", headers=_auth("u-admin"))  # tick 2: farm goes up

    r = client.post("/processes",
                    json={"entity_id": alice, "recipe": "GROW_WHEAT",
                          "parcel_id": parcel_id},
                    headers=_auth("u-alice"))
    assert r.status_code == 201, r.text
    for _ in range(3):  # started before tick 3, completes at tick 5
        client.post("/admin/ticks", headers=_auth("u-admin"))

    r = client.get(f"/parcels/{parcel_id}", headers=_auth("u-bob"))
    assert r.json()["facilities"][0]["facility_type"] == "FARM"
    assert r.json()["facilities"][0]["built_tick"] == 2
    r = client.get(f"/entities/{alice}/holdings", headers=_auth("u-alice"))
    holdings = {h["symbol"]: h["quantity"] for h in r.json()}
    assert holdings["WHEAT"] == "8.0000"
