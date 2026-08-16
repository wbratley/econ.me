"""API tests for the readiness gate (docs/game.md §9.1) -- rounds close by
player consent.

The clock has two drivers: the operator's advance (always available, the
override) and, in ``readiness`` mode, the players themselves -- the final
ready resolves the round in-request. Eligibility is one test: a user owns
>= 1 ACTIVE entity (spectators and eliminated dynasties have no agency, so
they can neither block nor signal). The register is a WorldSetting
(``round.readiness``), reset by every advance; the server derives the round
number on every write, so an advance racing a straggler POST is harmless.
The submit window is information-static, which is why public readiness
leaks nothing that matters (§9.1).
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
from econengine.models import Base, Entity, EntityType, User, WorldSetting


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "2")   # small batches, fast
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
            User(id="u-bob", email="bob@x", name="Bob",
                 provider="test", provider_id="3"),
            User(id="u-carol", email="carol@x", name="Carol",   # spectator
                 provider="test", provider_id="4"),
        ])
        session.flush()
        session.add_all([
            Entity(name="Alice's Firm", entity_type=EntityType.INDIVIDUAL,
                   owner_id="u-alice"),
            Entity(name="Bob's Firm", entity_type=EntityType.INDIVIDUAL,
                   owner_id="u-bob"),
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def _current(client, user="u-alice"):
    return client.get("/rounds/current", headers=_auth(user)).json()


def _ready(client, user):
    return client.post("/rounds/ready", headers=_auth(user))


def _gate_on(client):
    r = client.put("/admin/rounds/gate", json={"mode": "readiness"},
                   headers=_auth("u-admin"))
    assert r.status_code == 200, r.text


# ===========================================================================
# the gate's public face
# ===========================================================================

def test_readiness_block_defaults_to_operator_mode(client):
    s = _current(client)
    r = s["readiness"]
    assert r["mode"] == "operator"          # default: existing worlds unchanged
    assert r["round"] == 1
    assert r["ready"] == 0
    assert r["eligible"] == 2               # alice + bob own ACTIVE entities
    assert r["ready_users"] == []


def test_ready_endpoint_requires_auth(client):
    assert client.post("/rounds/ready").status_code in (401, 403)


# ===========================================================================
# operator mode: readiness records but never fires
# ===========================================================================

def test_operator_mode_ready_records_without_firing(client):
    r = _ready(client, "u-alice")
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is None     # the operator is still the clock
    s = _current(client)
    assert s["readiness"]["ready"] == 1
    assert s["readiness"]["ready_users"] == ["u-alice"]
    assert s["ticks_run"] == 0             # nothing resolved


def test_operator_mode_everyone_ready_still_waits_for_admin(client):
    _ready(client, "u-alice")
    r = _ready(client, "u-bob")
    assert r.status_code == 200             # even 2/2: mode decides, not count
    assert r.json()["resolved"] is None
    assert _current(client)["round_number"] == 0


# ===========================================================================
# readiness mode: the final ready resolves the round
# ===========================================================================

def test_final_ready_resolves_round_in_request(client):
    _gate_on(client)
    r1 = _ready(client, "u-alice")
    assert r1.status_code == 200            # not final: consent recorded only
    assert r1.json()["resolved"] is None
    assert _current(client)["ticks_run"] == 0

    r2 = _ready(client, "u-bob")
    assert r2.status_code == 201, r2.text   # the final ready resolved it
    summary = r2.json()["resolved"]
    assert summary["round_number"] == 1
    assert summary["ticks"] == [1, 2]       # K=2
    assert summary["next_round"] == 2

    s = _current(client)
    assert s["round_number"] == 1           # the clock moved
    assert s["current_round"] == 2
    assert s["ticks_run"] == 2
    # the register reset for the new round -- consents are historical
    assert s["readiness"]["round"] == 2
    assert s["readiness"]["ready"] == 0
    assert s["readiness"]["ready_users"] == []


def test_ready_is_idempotent(client):
    _gate_on(client)
    _ready(client, "u-alice")
    r = _ready(client, "u-alice")           # same user, again
    assert r.status_code == 200             # still not final (1 of 2)
    assert _current(client)["readiness"]["ready"] == 1


def test_unready_blocks_the_gate_until_re_ready(client):
    _gate_on(client)
    _ready(client, "u-alice")
    d = client.delete("/rounds/ready", headers=_auth("u-alice"))
    assert d.status_code == 200
    assert _current(client)["readiness"]["ready"] == 0   # withdrawn

    r = _ready(client, "u-bob")             # bob alone: 1 of 2, no fire
    assert r.status_code == 200
    r = _ready(client, "u-alice")           # now complete again -> fires
    assert r.status_code == 201
    assert _current(client)["round_number"] == 1


def test_unready_is_noop_after_round_resolved(client):
    _gate_on(client)
    _ready(client, "u-alice")
    _ready(client, "u-bob")                 # fires the round
    d = client.delete("/rounds/ready", headers=_auth("u-alice"))
    assert d.status_code == 200
    # readiness is historical: round 2 is open, nobody ready, clock unmoved
    assert _current(client)["readiness"] == {
        "mode": "readiness", "round": 2, "ready": 0,
        "eligible": 2, "ready_users": [],
    }
    assert _current(client)["round_number"] == 1


# ===========================================================================
# eligibility: one test -- owns >= 1 ACTIVE entity
# ===========================================================================

def test_spectator_cannot_ready(client):
    r = _ready(client, "u-carol")
    assert r.status_code == 409             # no ACTIVE entity: no agency
    assert "ACTIVE entity" in r.json()["detail"] or "active" in r.json()["detail"]
    assert _current(client)["readiness"]["ready"] == 0


def test_operator_without_entities_cannot_ready(client):
    r = _ready(client, "u-admin")
    assert r.status_code == 409             # the referee is not a player
    assert _current(client)["readiness"]["eligible"] == 2


def test_eligible_set_tracks_entity_ownership(client, monkeypatch):
    """A user becomes eligible the moment they own an ACTIVE entity; a
    dynasty whose entities are all incapacitated loses its voice."""
    r = _ready(client, "u-carol")
    assert r.status_code == 409

    from econengine.models import EntityStatus
    with Session(app.state._test_engine) as session:
        session.add(Entity(name="Carol's Firm", entity_type=EntityType.INDIVIDUAL,
                           owner_id="u-carol"))
        session.commit()
    assert _current(client)["readiness"]["eligible"] == 3

    with Session(app.state._test_engine) as session:
        session.query(Entity).filter(Entity.owner_id == "u-carol").update(
            {"status": EntityStatus.INCAPACITATED})
        session.commit()
    assert _current(client)["readiness"]["eligible"] == 2


# ===========================================================================
# the operator override and the register's lifecycle
# ===========================================================================

def test_admin_advance_still_works_in_readiness_mode(client):
    _gate_on(client)
    r = client.post("/admin/rounds/advance", headers=_auth("u-admin"))
    assert r.status_code == 201             # the override, not the clock
    assert r.json()["round_number"] == 1


def test_advance_resets_readiness_register(client):
    _ready(client, "u-alice")               # consent for round 1 (operator mode)
    client.post("/admin/rounds/advance", headers=_auth("u-admin"))
    s = _current(client)
    assert s["readiness"]["round"] == 2     # reset for the now-open round
    assert s["readiness"]["ready"] == 0
    assert s["readiness"]["ready_users"] == []


def test_stale_register_does_not_phantom_fire(client):
    """A register left over from an older round reads as empty, and a
    straggler POST normalizes to the round open now (the advance race)."""
    _gate_on(client)
    with Session(app.state._test_engine) as session:
        session.merge(WorldSetting(          # stale: claims round 7
            key="round.readiness",
            value={"round": 7, "ready": ["u-alice", "u-bob"]}))
        session.commit()
    # the pure read normalizes in-memory (GETs never persist)
    s = _current(client)
    assert s["readiness"]["round"] == 1
    assert s["readiness"]["ready"] == 0

    r = _ready(client, "u-bob")             # 1 of 2 -- stale readies dropped
    assert r.status_code == 200
    assert r.json()["readiness"]["ready_users"] == ["u-bob"]


def test_empty_eligible_set_never_blocks_genesis(client):
    """A world with no players (operator bootstrap): the gate does not block
    -- admin advance is the only clock (§9.1)."""
    with Session(app.state._test_engine) as session:
        session.query(Entity).delete()
        session.commit()
    _gate_on(client)
    assert _current(client)["readiness"]["eligible"] == 0
    r = client.post("/admin/rounds/advance", headers=_auth("u-admin"))
    assert r.status_code == 201


# ===========================================================================
# gate mode administration
# ===========================================================================

def test_gate_mode_defaults_to_operator(client):
    r = client.get("/admin/rounds/gate", headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json()["mode"] == "operator"


def test_gate_mode_put_and_read(client):
    r = client.put("/admin/rounds/gate", json={"mode": "readiness"},
                   headers=_auth("u-admin"))
    assert r.status_code == 200
    assert r.json()["mode"] == "readiness"
    assert client.get("/admin/rounds/gate",
                      headers=_auth("u-admin")).json()["mode"] == "readiness"
    assert _current(client)["readiness"]["mode"] == "readiness"


def test_gate_mode_rejects_unknown_mode(client):
    r = client.put("/admin/rounds/gate", json={"mode": "chaos"},
                   headers=_auth("u-admin"))
    assert r.status_code == 422


def test_gate_mode_requires_admin(client):
    r = client.put("/admin/rounds/gate", json={"mode": "readiness"},
                   headers=_auth("u-alice"))
    assert r.status_code == 403
