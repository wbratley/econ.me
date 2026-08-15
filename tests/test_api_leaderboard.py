"""Leaderboard tests (docs/game.md §14.5; Phase 2c).

The invariants under test:

  * **A pure platform read** -- the standings derive entirely from engine
    tables plus the immutable registers; nothing here writes.
  * **Money agrees with the observer** (§14.2): the row's money is the
    same definition ``accumulate`` judged -- ACTIVE entities only -- so
    the leaderboard can never disagree with a stamped win.
  * **Dynasties, not accounts**: rows come from ``Entity.owner_id``;
    server-owned entities are invisible; a never-joined player has no row.
  * **Public facts only** (§13): standings-level columns, no per-dynasty
    detail -- and the MCP tool returns exactly the REST payload.
  * **Deterministic ranking**: epoch wins desc, money desc, user id asc.
"""

import json
from decimal import Decimal

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
from econengine.models import (
    Base,
    Entity,
    EntityStatus,
    EntityType,
    Technology,
    Unlock,
    User,
    WorldSetting,
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
            User(id="u-carol", email="carol@x", name="Carol",
                 provider="test", provider_id="4"),
        ])
        session.commit()

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {user_id}"}


def _session() -> Session:
    return Session(app.state._test_engine)


def _seed_entity(owner_id: str | None, name: str = "E", *,
                 balance: Decimal = Decimal("100"),
                 birth_tick: int | None = None,
                 status: EntityStatus = EntityStatus.ACTIVE) -> str:
    """A seeded entity; returns its id. ORM handles must not outlive their
    session, so everything is committed inside. ``birth_tick`` is assigned
    verbatim (None forces NULL = 'predates age tracking', Step 6a)."""
    with _session() as session:
        entity = services.create_entity(session, name, EntityType.INDIVIDUAL)
        entity.owner_id = owner_id
        entity.birth_tick = birth_tick
        entity.status = status
        if balance:
            services.create_account(session, entity, "USD", balance)
        session.commit()
        return entity.id


def _set_setting(key: str, value) -> None:
    with _session() as session:
        row = session.get(WorldSetting, key)
        if row is None:
            session.add(WorldSetting(key=key, value=value))
        else:
            row.value = value
        session.commit()


def _start_running_epoch(number: int = 1) -> None:
    _set_setting("epoch.state", {
        "number": number, "condition": {"code": "grow", "params": {"threshold": 3}},
        "started_tick": 0, "ended_tick": None, "winner_user_ids": [],
    })


def _get(client, user_id: str = "u-alice") -> dict:
    r = client.get("/leaderboard", headers=_auth(user_id))
    assert r.status_code == 200, r.text
    return r.json()


def _row(body: dict, user_id: str) -> dict:
    return next(r for r in body["rows"] if r["user_id"] == user_id)


# ---------------------------------------------------------------------------
# Access + emptiness
# ---------------------------------------------------------------------------

def test_leaderboard_requires_auth(client):
    r = client.get("/leaderboard")
    assert r.status_code in (401, 403)


def test_empty_world_has_no_rows(client):
    body = _get(client)
    assert body["rows"] == []
    assert body["epoch_number"] == 0 and body["epoch_running"] is False


def test_never_joined_player_has_no_row(client):
    _seed_entity("u-alice", "A")
    body = _get(client, user_id="u-bob")  # bob owns nothing
    assert [r["user_id"] for r in body["rows"]] == ["u-alice"]


def test_server_owned_entities_are_invisible(client):
    _seed_entity(None, "Wilderness")  # owner_id NULL: the world's own
    body = _get(client)
    assert body["rows"] == []


# ---------------------------------------------------------------------------
# Row content: money / entities / oldest_age / unlocks / wins
# ---------------------------------------------------------------------------

def test_money_sums_the_dynasty_and_ranks_by_it(client):
    _seed_entity("u-alice", "A1", balance=Decimal("100"))
    _seed_entity("u-alice", "A2", balance=Decimal("50"))
    _seed_entity("u-bob", "B1", balance=Decimal("200"))
    body = _get(client)
    # bob (200) outranks alice (150); money is an exact string.
    assert [r["user_id"] for r in body["rows"]] == ["u-bob", "u-alice"]
    assert Decimal(_row(body, "u-alice")["money"]) == Decimal("150")
    assert Decimal(_row(body, "u-bob")["money"]) == Decimal("200")


def test_money_counts_active_entities_only(client):
    """The same definition the observer's accumulate judges (§14.2): an
    incapacitated entity's balances leave the dynasty's money."""
    _seed_entity("u-alice", "A-live", balance=Decimal("100"))
    _seed_entity("u-alice", "A-dead", balance=Decimal("999"),
                 status=EntityStatus.INCAPACITATED)
    body = _get(client)
    row = _row(body, "u-alice")
    assert Decimal(row["money"]) == Decimal("100")
    assert row["entities_active"] == 1 and row["entities_total"] == 2


def _advance_ticks(client, n: int = 1) -> None:
    """Raw ticks via the low-level escape hatch (rounds not needed here)."""
    for _ in range(n):
        r = client.post("/admin/ticks", headers=_auth("u-admin"))
        assert r.status_code == 201, r.text


def test_oldest_age_uses_the_oldest_tracked_member(client):
    _seed_entity("u-alice", "A-old", birth_tick=2)
    _seed_entity("u-alice", "A-young", birth_tick=7)
    _seed_entity("u-bob", "B-old", birth_tick=4)
    _advance_ticks(client, n=10)  # latest tick 10
    body = _get(client)
    assert _row(body, "u-alice")["oldest_age"] == 10 - 2  # the older member
    assert _row(body, "u-bob")["oldest_age"] == 10 - 4


def test_oldest_age_skips_untracked_members(client):
    """NULL birth_tick means 'predates tracking' (Step 6a): such members
    have no honest age and must not zero out the lineage."""
    _seed_entity("u-alice", "A-ancient", birth_tick=None)
    _advance_ticks(client, n=3)
    body = _get(client)
    assert _row(body, "u-alice")["oldest_age"] is None


def test_unlocks_are_distinct_and_entity_scoped(client):
    with _session() as session:
        a1 = _seed_entity("u-alice", "A1")
        a2 = _seed_entity("u-alice", "A2")
        _seed_entity("u-bob", "B1")  # bob owns no unlocks
        session.add_all([
            Technology(id="t-farm", code="FARMING", scope="entity"),
            Technology(id="t-smelt", code="SMELTING", scope="world"),
        ])
        session.flush()
        # Alice: two entities, one shared tech + one solo -> 2 distinct.
        session.add_all([
            Unlock(technology_id="t-farm", entity_id=a1, unlocked_tick=1),
            Unlock(technology_id="t-farm", entity_id=a2, unlocked_tick=1),
            Unlock(technology_id="t-smelt", entity_id=a2, unlocked_tick=2),
        ])
        # A world-scope unlock (entity NULL) belongs to the world.
        session.add(Unlock(technology_id="t-smelt", entity_id=None, unlocked_tick=3))
        session.commit()
    body = _get(client)
    assert _row(body, "u-alice")["unlocks"] == 2
    assert _row(body, "u-bob")["unlocks"] == 0


def test_epoch_wins_count_stamps_across_epochs(client):
    _set_setting("victory.stamps", [
        {"epoch": 1, "user_id": "u-alice", "tick": 5, "code": "grow", "value": 3},
        {"epoch": 2, "user_id": "u-alice", "tick": 9, "code": "accumulate", "value": "5000"},
        {"epoch": 2, "user_id": "u-bob", "tick": 9, "code": "accumulate", "value": "5000"},  # co-winner
    ])
    _seed_entity("u-alice", "A1", balance=Decimal("1"))
    _seed_entity("u-bob", "B1", balance=Decimal("999999"))  # bob richer
    body = _get(client)
    # Wins rank above money (§14.5): alice (2) first despite bob's fortune.
    assert [r["user_id"] for r in body["rows"]] == ["u-alice", "u-bob"]
    assert _row(body, "u-alice")["epoch_wins"] == 2
    assert _row(body, "u-bob")["epoch_wins"] == 1


def test_ties_break_by_money_then_user_id(client):
    _set_setting("victory.stamps", [
        {"epoch": 1, "user_id": "u-alice", "tick": 5, "code": "grow", "value": 3},
        {"epoch": 1, "user_id": "u-bob", "tick": 5, "code": "grow", "value": 3},
    ])
    _seed_entity("u-bob", "B1", balance=Decimal("10"))
    _seed_entity("u-alice", "A1", balance=Decimal("10"))
    _seed_entity("u-carol", "C1", balance=Decimal("10"))
    body = _get(client)
    # alice/bob tie on wins+money -> user id asc; carol (0 wins) last.
    assert [r["user_id"] for r in body["rows"]] == ["u-alice", "u-bob", "u-carol"]


# ---------------------------------------------------------------------------
# Status: active / eliminated / extinct
# ---------------------------------------------------------------------------

def test_status_active_by_default(client):
    _seed_entity("u-alice", "A1")
    assert _row(_get(client), "u-alice")["status"] == "active"


def test_status_eliminated_this_epoch_only_while_running(client):
    _seed_entity("u-alice", "A1", status=EntityStatus.INCAPACITATED)
    _start_running_epoch(number=3)
    _set_setting("epoch.eliminations", [
        {"epoch": 3, "user_id": "u-alice", "tick": 4},
    ])
    body = _get(client)
    row = _row(body, "u-alice")
    assert row["status"] == "eliminated"
    assert body["epoch_number"] == 3 and body["epoch_running"] is True

    # Epoch ends: the register becomes historical (§14.3) -- the dynasty is
    # dead but no longer eliminated-in-a-running-epoch.
    _set_setting("epoch.state", {
        "number": 3, "condition": {"code": "grow", "params": {"threshold": 3}},
        "started_tick": 0, "ended_tick": 9, "winner_user_ids": [],
    })
    assert _row(_get(client), "u-alice")["status"] == "extinct"


def test_status_extinct_for_dead_dynasty_outside_running_epoch(client):
    """Dead in an *earlier* epoch (or before any epoch): not eliminated
    now -- the epoch boundary is the fresh start (§14.3)."""
    _seed_entity("u-alice", "A1", status=EntityStatus.INCAPACITATED)
    _start_running_epoch(number=2)
    _set_setting("epoch.eliminations", [
        {"epoch": 1, "user_id": "u-alice", "tick": 4},  # last epoch's stamp
    ])
    assert _row(_get(client), "u-alice")["status"] == "extinct"


def test_active_beats_a_stale_elimination_stamp(client):
    """A living dynasty is never mislabelled by a stale stamp (a spawn
    into the owner's dynasty after elimination)."""
    _seed_entity("u-alice", "A-live")
    _start_running_epoch(number=1)
    _set_setting("epoch.eliminations", [
        {"epoch": 1, "user_id": "u-alice", "tick": 4},
    ])
    assert _row(_get(client), "u-alice")["status"] == "active"


# ---------------------------------------------------------------------------
# MCP: same payload, same gates
# ---------------------------------------------------------------------------

def _rpc(client, method: str, params: dict, user_id: str = "u-alice"):
    return client.post("/mcp", headers=_auth(user_id),
                       json={"jsonrpc": "2.0", "id": 1, "method": method,
                             "params": params})


def test_mcp_leaderboard_matches_rest(client):
    _seed_entity("u-alice", "A1", balance=Decimal("100"))
    _seed_entity("u-bob", "B1", balance=Decimal("200"))
    rest = _get(client, user_id="u-bob")
    r = _rpc(client, "tools/call",
             {"name": "leaderboard", "arguments": {}}, user_id="u-bob").json()
    assert not r["result"].get("isError")
    mcp = json.loads(r["result"]["content"][0]["text"])
    assert mcp == rest
