"""Admin world-lib endpoints (the `world` script namespace, docs/scripting.md
section 3; Phase 1). Operator fiat at world creation -- the same surface the
demo-world bootstrap uses to install its library. The join path needs no
endpoint change: a starter configured WITHOUT library text now works, because
vocabulary arrives from the tiers at run time."""

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

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


ADMIN = {"Authorization": "Bearer u-admin"}
USER = {"Authorization": "Bearer u-user"}

_LIB = "local t = {} function t.tag() return 'demo' end return t"


def test_world_lib_defaults_to_unset(client):
    r = client.get("/admin/world-lib", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {"source": None}


def test_set_read_clear_roundtrip(client):
    r = client.put("/admin/world-lib", headers=ADMIN, json={"source": _LIB})
    assert r.status_code == 200
    assert r.json() == {"source": _LIB}

    r = client.get("/admin/world-lib", headers=ADMIN)
    assert r.json() == {"source": _LIB}

    r = client.delete("/admin/world-lib", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {"source": None}


def test_blank_source_is_normalised_to_unset(client):
    r = client.put("/admin/world-lib", headers=ADMIN, json={"source": "   "})
    assert r.status_code == 200
    assert r.json() == {"source": None}


def test_world_lib_requires_admin(client):
    assert client.get("/admin/world-lib", headers=USER).status_code == 403
    assert client.put("/admin/world-lib", headers=USER,
                      json={"source": _LIB}).status_code == 403


def test_dry_run_validate_uses_the_configured_tiers(client):
    """The /admin/scripts/{id}/validate dry-run must see the same `world`
    the tick loop injects -- a script written against world vocabulary
    validates clean iff the world lib is installed."""
    def _make_script(source):
        r = client.post("/admin/scripts", headers=ADMIN, json={
            "name": "probe", "script_type": "behaviour", "source": source,
        })
        assert r.status_code == 201, r.text
        return r.json()["id"]

    script_id = _make_script("ctx.state.tag = world.tag()")

    # No world lib installed: the dry-run reports the nil-index error.
    r = client.post(f"/admin/scripts/{script_id}/validate", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["ok"] is False

    # Install the lib: the same script now validates clean and would have
    # written the tag into its state.
    client.put("/admin/world-lib", headers=ADMIN, json={"source": _LIB})
    r = client.post(f"/admin/scripts/{script_id}/validate", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["ok"] is True, r.json()["error"]


# ---------------------------------------------------------------------------
# Phase 2: the gate, the pack tier, and tier identity
# ---------------------------------------------------------------------------

def test_put_world_lib_refuses_broken_source(client):
    r = client.put("/admin/world-lib", headers=ADMIN, json={"source": "return 5"})
    assert r.status_code == 400
    assert any("namespace table" in p for p in r.json()["detail"])
    # Nothing was installed.
    assert client.get("/admin/world-lib", headers=ADMIN).json() == {"source": None}


def test_pack_lib_endpoints_roundtrip(client):
    good = "local p = {} function p.f() return 1 end return p"
    r = client.put("/admin/pack-lib", headers=ADMIN, json={"source": good})
    assert r.status_code == 200 and r.json() == {"source": good}
    assert client.get("/admin/pack-lib", headers=ADMIN).json() == {"source": good}

    r = client.put("/admin/pack-lib", headers=ADMIN, json={"source": "local t = {"})
    assert r.status_code == 400

    assert client.delete("/admin/pack-lib", headers=ADMIN).json() == {"source": None}
    assert client.get("/admin/pack-lib", headers=USER).status_code == 403


def test_scripting_tiers_reports_identity(client):
    r = client.get("/admin/scripting-tiers", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert len(body["std"]["fingerprint"]) == 16
    assert body["std"]["matches_pinned"] is True  # unpinned counts as matching
    assert body["world_lib_sha"] is None

    # Install a lib: its sha and a clean gate verdict appear.
    good = "local p = {} function p.f() return 1 end return p"
    client.put("/admin/world-lib", headers=ADMIN, json={"source": good})
    body = client.get("/admin/scripting-tiers", headers=ADMIN).json()
    assert body["world_lib_sha"] and body["gate"]["world_lib"] == []

    assert client.get("/admin/scripting-tiers", headers=USER).status_code == 403


def test_dry_run_validates_with_the_gate_strictness(client):
    """The dry-run applies the install gate's lint: a script that writes an
    undeclared global (a typo class the gate refuses) reports ok=false --
    the endpoint can no longer bless what the gate would reject."""
    r = client.post("/admin/scripts", headers=ADMIN, json={
        "name": "sloppy", "script_type": "behaviour",
        "source": "fctors = {}",
    })
    script_id = r.json()["id"]

    r = client.post(f"/admin/scripts/{script_id}/validate", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "undeclared global" in r.json()["error"]
