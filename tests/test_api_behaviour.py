"""API tests for the ownership-gated autonomy path
(``POST /entities/{id}/behaviour``; docs/game.md §6)."""

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine import services
from econengine.models import Base, EntityType, User


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
    app.state._test_engine = engine

    with Session(engine) as session:
        session.add_all([
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


def _make_entity(client, owner, name="Alice Co"):
    r = client.post("/entities", json={"name": name, "entity_type": "individual"},
                    headers=_auth(owner))
    assert r.status_code == 201, r.text
    return r.json()


def _stamp(entity_id, **fields):
    """Operator content-time action: mutate an entity directly on the shared
    engine (is_fixed has no player endpoint)."""
    from econengine.models import Entity
    with Session(app.state._test_engine) as s:
        e = s.get(Entity, entity_id)
        for k, v in fields.items():
            setattr(e, k, v)
        s.commit()


# ---------------------------------------------------------------------------
# POST /entities/{id}/behaviour  (the autonomy write)
# ---------------------------------------------------------------------------

def test_owner_sets_behaviour(client):
    alice = _make_entity(client, "u-alice")
    r = client.post(f"/entities/{alice['id']}/behaviour",
                    json={"source": "ctx.state.ran = ctx.tick"},
                    headers=_auth("u-alice"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["script_type"] == "behaviour"
    assert body["is_active"] is True
    assert body["entity_id"] == alice["id"]
    assert body["lineage_id"] == f"behaviour:{alice['id']}"


def test_non_owner_is_404(client):
    """Ownership hides existence: a non-owner gets 404, not 403 (so the
    entity's mere existence is not leaked)."""
    alice = _make_entity(client, "u-alice")
    r = client.post(f"/entities/{alice['id']}/behaviour",
                    json={"source": "-- nope"},
                    headers=_auth("u-bob"))
    assert r.status_code == 404


def test_unauthenticated_is_401(client):
    alice = _make_entity(client, "u-alice")
    r = client.post(f"/entities/{alice['id']}/behaviour",
                    json={"source": "-- nope"})
    assert r.status_code in (401, 403)


def test_fixed_entity_is_409(client):
    """The immutable tier: a fixed entity is refused (409), even by its
    owner. ``is_fixed`` is operator-set at content time (no player endpoint
    flips it)."""
    alice = _make_entity(client, "u-alice")
    # sanity: editable when NOT fixed
    r = client.post(f"/entities/{alice['id']}/behaviour",
                    json={"source": "-- x"}, headers=_auth("u-alice"))
    assert r.status_code == 201

    _stamp(alice["id"], is_fixed=True)
    r = client.post(f"/entities/{alice['id']}/behaviour",
                    json={"source": "-- again"}, headers=_auth("u-alice"))
    assert r.status_code == 409
    assert "fixed" in r.json()["detail"].lower()


def test_successive_edits_version_and_retire(client):
    alice = _make_entity(client, "u-alice")
    first = client.post(f"/entities/{alice['id']}/behaviour",
                        json={"source": "-- v1"}, headers=_auth("u-alice")).json()
    second = client.post(f"/entities/{alice['id']}/behaviour",
                         json={"source": "-- v2"}, headers=_auth("u-alice")).json()
    assert first["name"].endswith("#1")
    assert second["name"].endswith("#2")
    # GET returns the latest active one.
    got = client.get(f"/entities/{alice['id']}/behaviour", headers=_auth("u-alice"))
    assert got.status_code == 200
    assert got.json()["source"] == "-- v2"


# ---------------------------------------------------------------------------
# GET /entities/{id}/behaviour  (read the active behaviour)
# ---------------------------------------------------------------------------

def test_get_behaviour_when_none_is_404(client):
    alice = _make_entity(client, "u-alice")
    r = client.get(f"/entities/{alice['id']}/behaviour", headers=_auth("u-alice"))
    assert r.status_code == 404


def test_get_behaviour_non_owner_404(client):
    alice = _make_entity(client, "u-alice")
    client.post(f"/entities/{alice['id']}/behaviour",
                json={"source": "-- x"}, headers=_auth("u-alice"))
    r = client.get(f"/entities/{alice['id']}/behaviour", headers=_auth("u-bob"))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Phase 3: submit-time strictness on the autonomy path
# ---------------------------------------------------------------------------

def test_nil_call_trap_is_refused_at_submit(client):
    """The zombie trap (a helper that is not injected) is a 400 with the
    finding in hand -- and the entity's current behaviour is untouched."""
    alice = _make_entity(client, "u-alice")
    client.post(f"/entities/{alice['id']}/behaviour",
                json={"source": "-- healthy"}, headers=_auth("u-alice"))

    r = client.post(f"/entities/{alice['id']}/behaviour",
                    json={"source": "local fills = settle_last_orders()"},
                    headers=_auth("u-alice"))
    assert r.status_code == 400
    assert any("settle_last_orders" in p for p in r.json()["detail"])

    got = client.get(f"/entities/{alice['id']}/behaviour", headers=_auth("u-alice"))
    assert got.json()["source"] == "-- healthy"


def test_syntax_error_is_refused_at_submit(client):
    alice = _make_entity(client, "u-alice")
    r = client.post(f"/entities/{alice['id']}/behaviour",
                    json={"source": "local t = {"}, headers=_auth("u-alice"))
    assert r.status_code == 400
    assert any(p.startswith("syntax:") for p in r.json()["detail"])


def test_state_dependent_script_accepted_with_warnings(client):
    """A script that errors only on the synthetic ctx is accepted; the
    warning is on the response for the player to look at."""
    alice = _make_entity(client, "u-alice")
    r = client.post(f"/entities/{alice['id']}/behaviour",
                    json={"source": "ctx.state.hunger = ctx.state.hunger + 1"},
                    headers=_auth("u-alice"))
    assert r.status_code == 201
    body = r.json()
    assert body["source"].startswith("ctx.state.hunger")
    assert len(body["warnings"]) == 1 and body["warnings"][0].startswith("smoke-run:")


def test_clean_script_has_empty_warnings(client):
    alice = _make_entity(client, "u-alice")
    r = client.post(f"/entities/{alice['id']}/behaviour",
                    json={"source": "ctx.state.set = std.amount_str(1.5)"},
                    headers=_auth("u-alice"))
    assert r.status_code == 201
    assert r.json()["warnings"] == []
