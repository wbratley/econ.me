"""API tests for player onboarding -- ``POST /join`` and the admin
join-config (docs/game.md §6, §12.6; Phase 1).

Join is platform orchestration over engine primitives: it creates a founder
INDIVIDUAL owned by the user, endows an account from the world's
``join.config``, applies the starter BEHAVIOUR if configured, and shares the
spawn fairness gate. It grants no capability and leaves the founder in the
autonomy tier (not fixed), so onboarding and the autonomy path compose.
"""

import pytest
from decimal import Decimal
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
        ])
        session.commit()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def _join(client, user="u-alice"):
    return client.post("/join", headers=_auth(user))


def _join_config(client, **fields):
    return client.put("/admin/join-config", json=fields, headers=_auth("u-admin"))


# ===========================================================================
# the founder the player gets
# ===========================================================================

def test_join_no_config_creates_bare_founder(client):
    """With no join.config, a player still joins -- they get a bare founder
    (INDIVIDUAL, owned, no endowment, no script) they must script themselves."""
    r = _join(client)
    assert r.status_code == 201, r.text
    body = r.json()
    e = body["entity"]
    assert e["entity_type"] == "individual"
    assert e["owner_id"] == "u-alice"
    assert e["capabilities"] == []          # capabilities don't breed (§8)
    assert e["is_monetary_authority"] is False
    assert body["behaviour"] is None        # no starter configured
    acct = body["account"]
    assert acct["currency"] == "USD"        # default currency
    assert Decimal(acct["balance"]) == 0    # default endowment is zero
    # the founder is in the autonomy tier: NOT fixed, so the player can edit
    # its behaviour immediately via POST /entities/{id}/behaviour.


def test_join_with_endowment(client):
    _join_config(client, endowment="500", currency="USD")
    r = _join(client)
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["account"]["balance"]) == 500


def test_join_applies_starter_behaviour(client):
    """A configured starter becomes the founder's active BEHAVIOUR, in the
    entity-scoped autonomy lineage."""
    _join_config(client, starter_behaviour="ctx.state.ran = ctx.tick")
    r = _join(client)
    assert r.status_code == 201, r.text
    b = r.json()["behaviour"]
    entity_id = r.json()["entity"]["id"]
    assert b is not None
    assert b["script_type"] == "behaviour"
    assert b["is_active"] is True
    assert b["entity_id"] == entity_id
    assert b["lineage_id"] == f"behaviour:{entity_id}"


def test_founder_owner_is_the_player(client):
    r = _join(client, "u-bob")
    assert r.json()["entity"]["owner_id"] == "u-bob"
    # only the owner sees it via the entities list.
    assert len(client.get("/entities", headers=_auth("u-bob")).json()) == 1
    assert len(client.get("/entities", headers=_auth("u-alice")).json()) == 0


def test_join_requires_authentication(client):
    assert client.post("/join").status_code in (401, 403)


# ===========================================================================
# safety: caps, capabilities, fixed-tier
# ===========================================================================

def test_join_respects_per_owner_cap(client, monkeypatch):
    monkeypatch.setenv("ECON_MAX_ENTITIES_PER_OWNER", "1")
    assert _join(client).status_code == 201   # first founder: ok
    r = _join(client)                          # second: at the ceiling
    assert r.status_code == 409
    assert "owner" in r.json()["detail"].lower()


def test_join_respects_active_cap(client, monkeypatch):
    monkeypatch.setenv("ECON_MAX_ACTIVE_ENTITIES", "0")
    r = _join(client)
    assert r.status_code == 409
    assert "active" in r.json()["detail"].lower()


def test_join_does_not_grant_capability(client):
    """Joining confers no privilege (§8): a founder starts with an empty
    capability list. SEIZE/LEVY/MONETARY_AUTHORITY arrive only by vote."""
    e = _join(client).json()["entity"]
    assert e["capabilities"] == []


def test_founder_is_editable_via_autonomy_path(client):
    """Onboarding composes with autonomy: a joined founder is immediately
    editable by its owner (POST /entities/{id}/behaviour), proving it lands
    in the autonomy tier, not the immutable one."""
    entity_id = _join(client).json()["entity"]["id"]
    r = client.post(f"/entities/{entity_id}/behaviour",
                    json={"source": "ctx.state.edited = true"},
                    headers=_auth("u-alice"))
    assert r.status_code == 201
    assert r.json()["source"] == "ctx.state.edited = true"


# ===========================================================================
# end-to-end: the starter actually runs next tick
# ===========================================================================

def test_starter_runs_next_tick(client):
    """The whole point: a joined founder's starter behaviour executes as the
    entity on the next tick."""
    _join_config(client, endowment="100",
                 starter_behaviour="ctx.state['ran_on'] = ctx.tick")
    entity_id = _join(client).json()["entity"]["id"]

    # advance one tick (admin-gated clock).
    t = client.post("/admin/ticks", headers=_auth("u-admin"))
    assert t.status_code == 201

    b = client.get(f"/entities/{entity_id}/behaviour", headers=_auth("u-alice"))
    assert b.status_code == 200
    assert b.json()["state"].get("ran_on") == 1   # ran on tick 1


# ===========================================================================
# admin join-config: get / put / merge
# ===========================================================================

def test_join_config_defaults(client):
    r = client.get("/admin/join-config", headers=_auth("u-admin"))
    assert r.status_code == 200
    cfg = r.json()
    assert cfg["endowment"] == "0"
    assert cfg["currency"] == "USD"
    assert cfg["starter_behaviour"] is None


def test_join_config_put_then_get(client):
    r = _join_config(client, endowment="750", currency="EUR",
                     starter_behaviour="-- go")
    assert r.status_code == 200
    cfg = client.get("/admin/join-config", headers=_auth("u-admin")).json()
    assert cfg["endowment"] == "750"
    assert cfg["currency"] == "EUR"
    assert cfg["starter_behaviour"] == "-- go"


def test_join_config_merge_leaves_untouched_fields(client):
    """PUT merges: setting the endowment does not clobber a prior starter."""
    _join_config(client, starter_behaviour="-- original")
    _join_config(client, endowment="300")
    cfg = client.get("/admin/join-config", headers=_auth("u-admin")).json()
    assert cfg["endowment"] == "300"
    assert cfg["starter_behaviour"] == "-- original"


def test_join_config_requires_admin(client):
    """The founder package is operator content, not player-editable: a
    non-admin may not set what joiners start with."""
    r = client.put("/admin/join-config", json={"endowment": "999"},
                   headers=_auth("u-alice"))
    assert r.status_code == 403
