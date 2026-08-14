"""Epoch + victory observer tests (docs/game.md §7, §14; Phase 2a).

The invariants under test:

  * **A win is an engine-witnessed fact, never a vote** -- stamps appear
    only from real crossings inside round resolution, and only the
    observer writes them.
  * **Per-tick evaluation** (§14.2): a crossing that dips back below
    before the batch ends still counts (anti-flash-dump, §7.1).
  * **First crossing ends the epoch**; same-tick ties co-stamp.
  * **Elimination means something** (§14.3): eliminated players cannot
    rejoin until the next epoch -- but a player dead *before* an epoch
    began never took part in it and may join immediately.
"""

from decimal import Decimal

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.epochs import (
    get_epoch_state,
    get_stamps,
    observe_tick,
    scan_eliminations,
)
from econ.api.main import app
from econengine import services
from econengine.models import (
    Base,
    Entity,
    EntityStatus,
    EntityType,
    Technology,
    Unlock,
    User,
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "1")
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

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def _session() -> Session:
    engine = app.state._test_engine
    return Session(engine)


def _seed_entity(owner_id: str, name: str = "E", *, balance: str = "0",
                 status: EntityStatus = EntityStatus.ACTIVE,
                 incapacitated_tick: int | None = None,
                 birth_tick: int | None = None) -> Entity:
    """An entity owned by a user, with an endowment account."""
    with _session() as session:
        entity = services.create_entity(session, name, EntityType.INDIVIDUAL)
        entity.owner_id = owner_id
        if status is not None:
            entity.status = status
        if incapacitated_tick is not None:
            entity.incapacitated_tick = incapacitated_tick
        if birth_tick is not None:
            entity.birth_tick = birth_tick
        services.create_account(session, entity, "USD", Decimal(balance))
        session.commit()
        return entity


def _start_epoch(code: str, params: dict) -> dict:
    with _session() as session:
        from econ.api.epochs import start_epoch
        state = start_epoch(session, code, params)
        session.commit()
        return state


def _advance(client) -> dict:
    r = client.post("/admin/rounds/advance", headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Lifecycle: start / close / read
# ---------------------------------------------------------------------------

def test_no_epoch_observer_is_inert(client):
    _seed_entity("u-alice", balance="9000")
    summary = _advance(client)
    assert summary["victory_stamps"] == []
    assert summary["eliminations"] == []
    r = client.get("/epochs/current", headers=_auth("u-alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False and body["number"] == 0


def test_start_epoch_requires_admin(client):
    r = client.post("/admin/epochs", headers=_auth("u-alice"),
                    json={"code": "accumulate", "params": {"threshold": 5}})
    assert r.status_code == 403


@pytest.mark.parametrize("code,params,fragment", [
    ("levitate", {}, "unknown victory code"),
    ("accumulate", {}, "threshold"),
    ("accumulate", {"threshold": "-1"}, "positive"),
    ("innovate", {}, "technology"),
    ("innovate", {"technology": "NOPE"}, "unknown technology"),
    ("endure", {}, "ticks"),
    ("grow", {"threshold": "x"}, "threshold"),
])
def test_start_epoch_validates_condition(client, code, params, fragment):
    r = client.post("/admin/epochs", headers=_auth("u-admin"),
                    json={"code": code, "params": params})
    assert r.status_code == 422
    assert fragment in r.json()["detail"]


def test_start_epoch_twice_conflicts(client):
    r = client.post("/admin/epochs", headers=_auth("u-admin"),
                    json={"code": "grow", "params": {"threshold": 3}})
    assert r.status_code == 201
    r = client.post("/admin/epochs", headers=_auth("u-admin"),
                    json={"code": "grow", "params": {"threshold": 3}})
    assert r.status_code == 409


def test_close_epoch_without_winner(client):
    _start_epoch("grow", {"threshold": 3})
    r = client.post("/admin/epochs/close", headers=_auth("u-admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["winner_user_ids"] == []
    # A new epoch may now begin (the boundary is the fresh start).
    r = client.post("/admin/epochs", headers=_auth("u-admin"),
                    json={"code": "accumulate", "params": {"threshold": "100"}})
    assert r.status_code == 201 and r.json()["number"] == 2


# ---------------------------------------------------------------------------
# accumulate
# ---------------------------------------------------------------------------

def test_accumulate_crossing_stamps_and_ends_epoch(client):
    _seed_entity("u-alice", "A1", balance="3000")
    _seed_entity("u-alice", "A2", balance="3000")   # dynasty total 6000
    _seed_entity("u-bob", "B1", balance="4000")     # below threshold
    _start_epoch("accumulate", {"threshold": "5000"})

    summary = _advance(client)
    stamps = summary["victory_stamps"]
    assert len(stamps) == 1
    s = stamps[0]
    assert s["user_id"] == "u-alice" and s["code"] == "accumulate"
    assert s["epoch"] == 1 and s["tick"] == summary["ticks"][-1]
    assert Decimal(s["value"]) == Decimal("6000")

    state = get_epoch_state(_session())
    assert state["ended_tick"] == s["tick"]
    assert state["winner_user_ids"] == ["u-alice"]

    # The register is immutable-by-path: only the observer appends, and the
    # epoch is over, so further rounds stamp nothing.
    assert _advance(client)["victory_stamps"] == []
    assert len(get_stamps(_session())) == 1


def test_accumulate_same_tick_tie_co_stamps(client):
    _seed_entity("u-alice", balance="6000")
    _seed_entity("u-bob", balance="5500")
    _start_epoch("accumulate", {"threshold": "5000"})

    summary = _advance(client)
    assert sorted(s["user_id"] for s in summary["victory_stamps"]) == ["u-alice", "u-bob"]
    state = get_epoch_state(_session())
    assert sorted(state["winner_user_ids"]) == ["u-alice", "u-bob"]


def test_accumulate_flash_dump_still_counts(client):
    """The §7.1 defence, operationalized: evaluated per tick, a dynasty that
    dips back below before the batch ends still holds its first crossing."""
    _seed_entity("u-alice", balance="1000")

    with _session() as session:
        from sqlalchemy import select
        from econ.api.epochs import start_epoch
        from econengine.models import Account
        start_epoch(session, "accumulate", {"threshold": "5000"})
        session.commit()

        acc = session.execute(
            select(Account).where(Account.currency == "USD")
        ).scalar_one()  # the only account in this bare world

        # tick 1: below threshold -- no stamp
        assert observe_tick(session, 1) == []
        # Alice is paid 5000 mid-batch (a sale, a grant -- the observer
        # does not care where money came from, only that it is hers).
        acc.balance = Decimal("6000")
        session.flush()
        # tick 2: above -- stamped, at tick 2
        stamps = observe_tick(session, 2)
        assert len(stamps) == 1 and stamps[0]["tick"] == 2
        # tick 3: the dump -- money leaves; the epoch is already over, the
        # stamp stands forever (append-only, no un-stamping).
        acc.balance = Decimal("10")
        session.flush()
        assert observe_tick(session, 3) == []
        assert len(get_stamps(session)) == 1


# ---------------------------------------------------------------------------
# innovate / grow / endure
# ---------------------------------------------------------------------------

def test_innovate_crossing_on_entity_unlock(client):
    with _session() as session:
        tech = Technology(code="STEELMAKING", name="Steel")
        session.add(tech)
        session.commit()
        alice = services.create_entity(session, "A", EntityType.INDIVIDUAL)
        alice.owner_id = "u-alice"
        session.add(Unlock(technology_id=tech.id, entity_id=alice.id, unlocked_tick=1))
        session.commit()

    _start_epoch("innovate", {"technology": "STEELMAKING"})
    summary = _advance(client)
    stamps = summary["victory_stamps"]
    assert len(stamps) == 1
    assert stamps[0]["user_id"] == "u-alice"
    assert stamps[0]["value"] == "STEELMAKING"

    # A world-scope unlock (entity NULL) belongs to the world, not a dynasty.
    with _session() as session:
        from sqlalchemy import delete, select
        from econ.api.epochs import start_epoch
        session.execute(delete(Unlock))
        session.commit()
        tech = session.execute(select(Technology)).scalar_one()
        session.add(Unlock(technology_id=tech.id, entity_id=None, unlocked_tick=1))
        session.commit()
        start_epoch(session, "innovate", {"technology": "STEELMAKING"})  # epoch 2
        session.commit()
    assert _advance(client)["victory_stamps"] == []


def test_grow_crossing_counts_active_entities(client):
    _seed_entity("u-alice", "A1")
    _seed_entity("u-alice", "A2")
    _seed_entity("u-alice", "A3", status=EntityStatus.INCAPACITATED,
                 incapacitated_tick=1)  # dead members do not count
    _seed_entity("u-bob", "B1")
    _start_epoch("grow", {"threshold": 2})

    summary = _advance(client)
    stamps = summary["victory_stamps"]
    assert len(stamps) == 1
    assert stamps[0]["user_id"] == "u-alice" and stamps[0]["value"] == 2


def test_endure_crossing_after_declared_ticks(client):
    _seed_entity("u-alice")
    _start_epoch("endure", {"ticks": 2})

    summary = _advance(client)  # tick 1: endured 1 -- no
    assert summary["victory_stamps"] == []
    summary = _advance(client)  # tick 2: endured 2 -- crossing
    stamps = summary["victory_stamps"]
    assert len(stamps) == 1
    assert stamps[0]["user_id"] == "u-alice" and stamps[0]["value"] == 2


def test_endure_ignores_ticks_before_the_epoch_started(client):
    _seed_entity("u-alice")
    _advance(client)  # a round runs before any epoch exists
    _advance(client)
    _start_epoch("endure", {"ticks": 1})  # started_tick = 2

    summary = _advance(client)  # tick 3: endured 1 -- crossing
    stamps = summary["victory_stamps"]
    assert len(stamps) == 1 and stamps[0]["tick"] == 3


def test_no_epoch_ended_epoch_never_stamps(client):
    _seed_entity("u-alice", balance="9000")
    _start_epoch("accumulate", {"threshold": "5000"})
    with _session() as session:
        from econ.api.epochs import close_epoch
        close_epoch(session)
        session.commit()
    assert _advance(client)["victory_stamps"] == []


# ---------------------------------------------------------------------------
# Eliminations + the §14.3 rejoin check
# ---------------------------------------------------------------------------

def test_elimination_stamped_and_blocks_rejoin_until_next_epoch(client):
    _seed_entity("u-alice", "A1", status=EntityStatus.INCAPACITATED,
                 incapacitated_tick=1, birth_tick=1)
    _start_epoch("grow", {"threshold": 99})  # nobody can win this

    summary = _advance(client)
    elims = summary["eliminations"]
    assert len(elims) == 1
    assert elims[0]["user_id"] == "u-alice" and elims[0]["epoch"] == 1

    # Join refused while the epoch runs (REST and MCP paths).
    r = client.post("/join", headers=_auth("u-alice"))
    assert r.status_code == 409
    assert "next epoch" in r.json()["detail"]

    r = client.post("/mcp", headers=_auth("u-alice"), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "join", "arguments": {}},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["isError"] is True

    # Not stamped twice for the same epoch (append-only, deduplicated).
    assert _advance(client)["eliminations"] == []

    # The epoch boundary is the fresh start.
    client.post("/admin/epochs/close", headers=_auth("u-admin"))
    r = client.post("/join", headers=_auth("u-alice"))
    assert r.status_code == 201


def test_player_dead_before_epoch_may_join_immediately(client):
    """Participation is reconstructed from immutable columns: an entity that
    died before the epoch began is not in-epoch participation, so the owner
    is never stamped and may rejoin at once."""
    _seed_entity("u-alice", "A1", status=EntityStatus.INCAPACITATED,
                 incapacitated_tick=1, birth_tick=1)
    _advance(client)  # tick 1 kills the dynasty (pre-epoch)
    _start_epoch("grow", {"threshold": 99})

    summary = _advance(client)  # tick 2, epoch running, alice long dead
    assert summary["eliminations"] == []

    r = client.post("/join", headers=_auth("u-alice"))
    assert r.status_code == 201


def test_living_dynasty_is_never_eliminated(client):
    _seed_entity("u-alice", balance="1")
    _start_epoch("grow", {"threshold": 99})
    assert _advance(client)["eliminations"] == []


def test_elimination_scan_direct_requires_running_epoch(client):
    with _session() as session:
        assert scan_eliminations(session, 1) == []


# ---------------------------------------------------------------------------
# Player-facing reads + MCP epoch_state
# ---------------------------------------------------------------------------

def test_player_epoch_view(client):
    _seed_entity("u-alice", balance="6000")
    _start_epoch("accumulate", {"threshold": "5000"})
    _advance(client)

    r = client.get("/epochs/current", headers=_auth("u-alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["number"] == 1
    assert body["winner_user_ids"] == ["u-alice"]
    assert body["eliminated_this_epoch"] is False
    assert body["condition"] == {
        "code": "accumulate", "params": {"threshold": "5000"},
    }


def test_epoch_view_requires_auth(client):
    assert client.get("/epochs/current").status_code == 401


def test_mcp_epoch_state_tool(client):
    # No epoch declared yet.
    r = client.post("/mcp", headers=_auth("u-alice"), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "epoch_state", "arguments": {}},
    })
    body = r.json()["result"]
    assert not body.get("isError")
    import json
    payload = json.loads(body["content"][0]["text"])
    assert payload["running"] is False and payload["number"] == 0

    # Declared, then won by alice; bob (alive) and alice see their own view.
    _seed_entity("u-alice", balance="6000")
    _start_epoch("accumulate", {"threshold": "5000"})
    _advance(client)
    for uid in ("u-alice", "u-bob"):
        r = client.post("/mcp", headers=_auth(uid), json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "epoch_state", "arguments": {}},
        })
        payload = json.loads(r.json()["result"]["content"][0]["text"])
        assert payload["winner_user_ids"] == ["u-alice"]
        assert payload["you_are_eliminated"] is False


def test_mcp_tools_list_includes_epoch_state(client):
    r = client.post("/mcp", headers=_auth("u-alice"), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    })
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "epoch_state" in names
