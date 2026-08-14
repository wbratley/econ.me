"""API tests for the round scheduler -- the platform's batched-tick clock
(docs/game.md §9).

A round = resolve K ticks in a batch. The operator advances the clock
(``POST /admin/rounds/advance``); players observe it (``GET /rounds/current``).
Pace (K) is deployment config (``ECON_TICKS_PER_ROUND``), not a WorldSetting;
the round counter is runtime state in ``round.state``. The round is
authoritative and independent of raw ticks run via the single-tick escape
hatch -- a round is a scheduler batch, not a tick quotient. The engine is
untouched.
"""

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

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
    app.state._test_engine = engine

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


def _current(client, user="u-alice"):
    return client.get("/rounds/current", headers=_auth(user))


def _advance(client):
    return client.post("/admin/rounds/advance", headers=_auth("u-admin"))


# ===========================================================================
# genesis state
# ===========================================================================

def test_current_round_at_genesis(client):
    """Before any advance: 0 rounds resolved, round 1 open, 0 ticks run."""
    s = _current(client).json()
    assert s["round_number"] == 0          # no rounds resolved yet
    assert s["current_round"] == 1         # round 1 is open for submission
    assert s["status"] == "submit"
    assert s["ticks_run"] == 0
    assert s["ticks_per_round"] == 10      # default K
    assert s["ticks_into_round"] == 0


def test_current_round_requires_auth(client):
    assert client.get("/rounds/current").status_code in (401, 403)


# ===========================================================================
# advance
# ===========================================================================

def test_advance_runs_k_ticks_default(client):
    r = _advance(client)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["round_number"] == 1          # round 1 just completed
    assert s["ticks"] == list(range(1, 11))   # ticks 1..10 (default K=10)
    assert s["next_round"] == 2
    assert s["ticks_per_round"] == 10
    # the clock now reflects the completed round
    now = _current(client).json()
    assert now["round_number"] == 1
    assert now["current_round"] == 2       # round 2 open for submission
    assert now["ticks_run"] == 10


def test_advance_respects_env_k(client, monkeypatch):
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "3")
    s = _advance(client).json()
    assert s["ticks"] == [1, 2, 3]
    assert s["ticks_per_round"] == 3
    assert _current(client).json()["ticks_per_round"] == 3


def test_advance_twice(client):
    _advance(client)
    s = _advance(client).json()
    assert s["round_number"] == 2
    assert s["ticks"] == list(range(11, 21))   # second round = ticks 11..20
    now = _current(client).json()
    assert now["round_number"] == 2
    assert now["ticks_run"] == 20


def test_advance_requires_admin(client):
    """Driving the clock is the operator's job, not a player's."""
    r = client.post("/admin/rounds/advance", headers=_auth("u-alice"))
    assert r.status_code == 403


def test_admin_current_round_endpoint(client):
    _advance(client)
    r = client.get("/admin/rounds/current", headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json()["round_number"] == 1


# ===========================================================================
# state persists across requests; round is independent of raw ticks
# ===========================================================================

def test_round_state_persists_across_requests(client):
    """The round counter is durable: a fresh request sees the advanced clock."""
    _advance(client)
    _advance(client)
    assert _current(client).json()["round_number"] == 2


def test_round_independent_of_raw_ticks(client):
    """A round is a scheduler batch, not a tick quotient. Running a single
    raw tick (the low-level escape hatch) moves the tick clock but NOT the
    round counter; the next advance still resolves exactly K ticks."""
    raw = client.post("/admin/ticks", headers=_auth("u-admin"))
    assert raw.status_code == 201
    # one raw tick in, but still in round 1
    s = _current(client).json()
    assert s["round_number"] == 0
    assert s["ticks_run"] == 1
    assert s["ticks_into_round"] == 1
    # advance resolves 10 ticks (2..11) and completes round 1
    adv = _advance(client).json()
    assert adv["round_number"] == 1
    assert adv["ticks"] == list(range(2, 12))


# ===========================================================================
# event aggregation
# ===========================================================================

def test_advance_aggregates_events_bare_world(client):
    """A bare world (no scripts/goods/needs) produces no events per tick."""
    s = _advance(client).json()
    assert s["events"] == 0
    assert s["events_by_type"] == {}


def test_advance_aggregates_events_from_scripts(client):
    """A behaviour script that errors yields one script_error event per tick;
    the round summary counts them by type."""
    engine = app.state._test_engine
    with Session(engine) as session:
        entity = services.create_entity(session, "Boom", EntityType.INDIVIDUAL)
        entity.owner_id = "u-alice"
        services.set_entity_behaviour(
            session, entity, 'error("boom")', owner_id="u-alice",
        )
        session.commit()

    s = _advance(client).json()
    # one erroring behaviour, 10 ticks -> 10 script_error events
    assert s["events_by_type"].get("script_error") == 10
    assert s["events"] >= 10
