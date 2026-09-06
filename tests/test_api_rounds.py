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

import json
import time

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


# ===========================================================================
# The deadline backstop (M2a -- the always-on host)
# ===========================================================================

def _make_eligible(user_id="u-alice"):
    """Give a user an ACTIVE owned entity -- a voice in the gate."""
    engine = app.state._test_engine
    with Session(engine) as session:
        entity = services.create_entity(session, "House Test",
                                        EntityType.INDIVIDUAL)
        entity.owner_id = user_id
        session.commit()


def _set_gate(client, mode):
    r = client.put("/admin/rounds/gate", json={"mode": mode},
                   headers=_auth("u-admin"))
    assert r.status_code == 200, r.text


def test_round_deadline_env_parsing(monkeypatch):
    from econ.api.rounds import round_deadline_s

    monkeypatch.delenv("ECON_ROUND_DEADLINE_S", raising=False)
    assert round_deadline_s() == 0.0            # off by default
    monkeypatch.setenv("ECON_ROUND_DEADLINE_S", "0")
    assert round_deadline_s() == 0.0
    monkeypatch.setenv("ECON_ROUND_DEADLINE_S", "-5")
    assert round_deadline_s() == 0.0            # bad values never arm it
    monkeypatch.setenv("ECON_ROUND_DEADLINE_S", "nine hundred")
    assert round_deadline_s() == 0.0
    monkeypatch.setenv("ECON_ROUND_DEADLINE_S", "900")
    assert round_deadline_s() == 900.0


def test_maybe_auto_advance_anchors_then_fires(client, monkeypatch):
    """First sight of a round anchors its window (no advance); the round
    closes exactly when opened_at + deadline has passed; the next round's
    window opens fresh."""
    from econ.api.rounds import _read_register, maybe_auto_advance

    monkeypatch.setenv("ECON_ROUND_DEADLINE_S", "60")
    _set_gate(client, "readiness")
    _make_eligible()
    engine = app.state._test_engine

    with Session(engine) as session:
        # first look: anchor, don't punish
        assert maybe_auto_advance(session, now=1000.0) is None
        session.commit()
        assert _read_register(session) == (1, [], 1000.0)
        # one second early: nothing
        assert maybe_auto_advance(session, now=1059.9) is None
        session.rollback()
        # past the deadline: close round 1
        summary = maybe_auto_advance(session, now=1061.0)
        session.commit()
    assert summary["round_number"] == 1
    assert summary["next_round"] == 2
    # the new window's anchor is real wall-clock (only the backstop's
    # decision took the injected now)
    assert summary["next_opened_at"] == pytest.approx(time.time(), abs=60)
    with Session(engine) as session:
        assert _read_register(session)[0] == 2   # round 2's window opened


def test_maybe_auto_advance_noop_matrix(client, monkeypatch):
    from econ.api.rounds import maybe_auto_advance

    _make_eligible()
    engine = app.state._test_engine

    def _anchored_past(round_no=1):
        from econengine.models import WorldSetting
        with Session(engine) as session:
            session.get(WorldSetting, "round.readiness", with_for_update=True)
            session.execute(
                WorldSetting.__table__.insert().values(
                    key="round.readiness",
                    value={"round": round_no, "ready": [],
                           "opened_at": 0.0}))
            session.commit()

    # backstop off (default): nobody closes anything
    monkeypatch.delenv("ECON_ROUND_DEADLINE_S", raising=False)
    _anchored_past()
    with Session(engine) as session:
        assert maybe_auto_advance(session, now=99999.0) is None
        session.rollback()

    # armed but operator mode: consent or the operator, not the clock
    monkeypatch.setenv("ECON_ROUND_DEADLINE_S", "60")
    with Session(engine) as session:
        assert maybe_auto_advance(session, now=99999.0) is None
        session.rollback()

    # readiness mode, elapsed, but nobody eligible (extinction): the
    # clock halts rather than ticking a corpse world
    _set_gate(client, "readiness")
    from econengine.models import Entity, EntityStatus
    with Session(engine) as session:
        for e in session.query(Entity).all():
            e.status = EntityStatus.INCAPACITATED
        session.commit()
    with Session(engine) as session:
        assert maybe_auto_advance(session, now=99999.0) is None
        session.rollback()


def test_advance_summary_carries_next_opened_at(client):
    s = _advance(client).json()
    assert isinstance(s["next_opened_at"], float)
    assert s["next_opened_at"] > 0


def test_final_ready_publishes_on_the_event_stream(client, monkeypatch):
    """A consent resolve broadcasts readiness + round_closed +
    round_opened (with the deadline epoch when armed) -- the same shape
    the deadline scheduler broadcasts."""
    from econ.api import events as events_mod

    seen = []

    def spy(event, data):
        seen.append((event, data))

    monkeypatch.setattr(events_mod, "publish", spy)
    monkeypatch.setenv("ECON_ROUND_DEADLINE_S", "900")
    _set_gate(client, "readiness")
    _make_eligible()

    r = client.post("/rounds/ready", headers=_auth("u-alice"))
    assert r.status_code == 201, r.text
    assert r.json()["resolved"] is not None

    kinds = [e for e, _ in seen]
    assert kinds == ["readiness", "round_closed", "round_opened"]
    closed = next(d for e, d in seen if e == "round_closed")
    assert closed["round_number"] == 1 and closed["next_round"] == 2
    opened = next(d for e, d in seen if e == "round_opened")
    assert opened["round"] == 2
    assert opened["deadline_epoch"] == pytest.approx(
        r.json()["resolved"]["next_opened_at"] + 900.0, rel=1e-3)


def test_sse_stream_hello_then_events(client):
    """On connect the stream states where the world is (hello snapshot),
    then forwards pub/sub events -- readable by a plain HTTP client.

    Driven at the ASGI level: an SSE stream never ends, so a buffered
    test transport (httpx ASGITransport awaits the whole app call) would
    hang; a raw scope/receive/send lets the test read frames as the
    server yields them."""
    import asyncio

    from econ.api import events

    def run() -> list:
        async def main() -> list:
            sent: asyncio.Queue = asyncio.Queue()
            scope = {"type": "http", "asgi": {"version": "3.0"},
                     "http_version": "1.1", "method": "GET",
                     "scheme": "http", "path": "/rounds/events",
                     "raw_path": b"/rounds/events", "query_string": b"",
                     "root_path": "", "headers": [], "client": ("t", 0),
                     "server": ("t", 80)}
            disconnect = asyncio.Event()

            async def receive():
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                await sent.put(message)

            task = asyncio.create_task(app(scope, receive, send))
            try:
                start = await asyncio.wait_for(sent.get(), 5)
                assert start["type"] == "http.response.start"
                assert start["status"] == 200
                hdrs = {k.decode().lower(): v.decode()
                        for k, v in start.get("headers", [])}
                assert hdrs["content-type"].startswith("text/event-stream")

                async def frame():
                    msg = await asyncio.wait_for(sent.get(), 5)
                    assert msg["type"] == "http.response.body"
                    return msg.get("body", b"").decode()

                hello_frame = await frame()      # yielded before any await
                lines = hello_frame.splitlines()
                assert lines[0] == "event: hello"
                hello = json.loads(lines[1][len("data: "):])
                assert hello["type"] == "hello" and hello["current_round"] == 1

                events.publish("round_opened",
                               {"round": 5, "deadline_epoch": None})
                got = ""
                while "event: round_opened" not in got:
                    got += await frame()
                assert (f'data: {json.dumps({"round": 5, "deadline_epoch": None})}'
                        in got)
                return []
            finally:
                disconnect.set()
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        return asyncio.run(main())

    run()


def test_admin_token_mint(client):
    """POST /admin/tokens mints a bearer JWT for an existing user --
    the over-the-wire seat join (M2a). Admin-only; unknown user 404."""
    from econ.api.auth import decode_token

    r = client.post("/admin/tokens", json={"user_id": "u-alice"},
                    headers=_auth("u-admin"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_id"] == "u-alice" and body["token"]
    assert body["expires_minutes"] >= 1
    payload = decode_token(body["token"])
    assert payload is not None and payload["sub"] == "u-alice"

    assert client.post("/admin/tokens", json={"user_id": "u-alice"},
                       headers=_auth("u-alice")).status_code == 403
    assert client.post("/admin/tokens", json={"user_id": "nobody"},
                       headers=_auth("u-admin")).status_code == 404


def test_deadline_poll_closes_a_stale_round(client, monkeypatch):
    """The scheduler's one tick: an anchored, elapsed, readiness-mode
    round with players alive gets closed and committed."""
    from econ.api.main import _deadline_poll_once
    from econengine.models import WorldSetting

    monkeypatch.setenv("ECON_ROUND_DEADLINE_S", "60")
    _set_gate(client, "readiness")
    _make_eligible()
    engine = app.state._test_engine
    with Session(engine) as session:              # a window opened long ago
        session.get(WorldSetting, "round.readiness", with_for_update=True)
        session.execute(
            WorldSetting.__table__.insert().values(
                key="round.readiness",
                value={"round": 1, "ready": [], "opened_at": 0.0}))
        session.commit()

    summary = _deadline_poll_once()
    assert summary is not None
    assert summary["round_number"] == 1
    # and it committed: the clock moved
    assert _current(client).json()["round_number"] == 1
