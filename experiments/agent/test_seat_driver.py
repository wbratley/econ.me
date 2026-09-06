"""The seat kit driver (M2b): the always-on world's client half.

What these tests prove, in order: the SSE parser turns the round
stream's bytes into events; the driver takes exactly ONE turn per
round (dedupe by round number across hello and round_opened — the
reconnect storm costs nothing); its own final consent resolves the
round (a lone seat paces the world, pure MCP); the catch-up guard
re-cycles when the round moves mid-turn (a deadline or an operator's
advance during the model call) and never attaches consent to a round
the seat didn't play; the dead get a tombstone and no model call; the
workspace scaffold carries the manual and never the token.

Everything runs against the real FastAPI app through the TestClient —
the same JSON-RPC bytes a live uvicorn would see — with the SSE stream
INJECTED (a list of frames) because the driver's contract is event
handling, and httpx's ASGI transport cannot consume an infinite
stream synchronously anyway.
"""

import json
from pathlib import Path

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine.models import Base, User

from experiments.agent.loop import AgentLoop, McpClient
from experiments.agent.multi import Dynasty, build_agent_world
from experiments.agent.seat_driver import (
    SeatDriver, SseParser, init_workspace,
)

CLEAN = "ctx.state.note = 'round'"
CLEAN2 = "ctx.state.note = 'again'"


@pytest.fixture
def client(tmp_path):
    """One dynasty's world (readiness gate) + an admin, over the real
    app: the driver under test talks MCP exactly as it would to a live
    uvicorn."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/world.db",
        connect_args={"check_same_thread": False},
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
            raise HTTPException(401, "User not found")
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = override_get_current_user

    with Session(engine) as s:
        s.add(User(id="u-admin", email="admin@x", name="Operator",
                   provider="test", provider_id="0", is_admin=True))
        s.add(User(id="u-one", email="u-one@x", name="House One",
                   provider="test", provider_id="1"))
        s.commit()
        dynasties = [Dynasty(user_id="u-one", name="House One",
                             model_name="test:u-one", token="u-one")]
        build_agent_world(s, dynasties)

    tc = TestClient(app)

    def transport_for(user_id):
        def transport(method, params):
            r = tc.post(
                "/mcp", headers={"Authorization": f"Bearer {user_id}"},
                json={"jsonrpc": "2.0", "id": 1, "method": method,
                      "params": params})
            body = r.json()
            assert "error" not in body, body
            return body["result"]
        return transport

    try:
        yield {"transports": {"u-one": transport_for("u-one"),
                              "u-admin": transport_for("u-admin")},
               "dynasties": dynasties, "engine": engine}
    finally:
        app.dependency_overrides.clear()


def make_driver(fixture, responses, tmp_path, **kw):
    from experiments.agent.llm import ScriptedModel
    model = ScriptedModel(list(responses))
    d = fixture["dynasties"][0]
    lp = AgentLoop(McpClient(fixture["transports"][d.user_id]), model,
                   entity_id=d.entity_id,
                   journal_path=str(tmp_path / "journal.jsonl"))
    return SeatDriver(lp, d.name, tmp_path, **kw), model


def round_state(fixture):
    return fixture["transports"]["u-one"](
        "tools/call", {"name": "round_state", "arguments": {}})


def decoded(fixture, result):
    return json.loads(result["content"][0]["text"])


# ===========================================================================
# The SSE parser
# ===========================================================================

def test_sse_parser_frames_and_heartbeats():
    p = SseParser()
    assert p.feed("event: hello") is None
    assert p.feed('data: {"current_round": 3}') is None
    assert p.feed(": keepalive") is None            # comment mid-frame
    ev = p.feed("")
    assert ev == ("hello", {"current_round": 3})
    assert p.feed("") is None                       # nothing accumulated
    assert p.feed(": keepalive") is None
    assert p.feed("") is None                       # bare heartbeat frame


def test_sse_parser_multiline_data_and_reset():
    p = SseParser()
    assert p.feed("event: round_opened") is None
    assert p.feed('data: {"round": 4,') is None
    assert p.feed('data:   "deadline_epoch": 1900.0}') is None
    ev = p.feed("")
    assert ev[0] == "round_opened"
    assert ev[1] == {"round": 4, "deadline_epoch": 1900.0}
    # the next frame starts clean
    assert p.feed("event: readiness") is None
    assert p.feed('data: {"round": 4}') is None
    assert p.feed("") == ("readiness", {"round": 4})


def test_sse_parser_non_json_passes_through():
    p = SseParser()
    p.feed("event: odd")
    assert p.feed("data: just text") is None
    assert p.feed("") == ("odd", "just text")


def test_sse_parser_event_with_no_data_is_dropped():
    p = SseParser()
    p.feed("event: ping")
    assert p.feed("") is None


# ===========================================================================
# One turn per round, and the turn resolves the round
# ===========================================================================

def test_driver_hello_takes_turn_and_own_consent_resolves(client, tmp_path):
    driver, model = make_driver(client, [CLEAN], tmp_path, max_rounds=1)
    driver.run(lambda: iter([("hello", {"current_round": 1})]))
    assert driver.turns == 1
    assert driver.last_round == 1
    assert len(model.calls) == 1
    # the lone seat's final ready resolved round 1 in-request
    state = decoded(client, round_state(client))
    assert state["current_round"] == 2
    assert state["round_number"] == 1
    lines = [json.loads(l) for l in
             (tmp_path / "driver.jsonl").read_text().splitlines()]
    assert any(l["kind"] == "resolved" and l["round"] == 1 for l in lines)


def test_driver_dedupes_round_events(client, tmp_path):
    # hello + replayed round_opened for the SAME round = one cycle; the
    # second round_opened (2) is the second turn; then --max-rounds stops
    driver, model = make_driver(client, [CLEAN, CLEAN2], tmp_path,
                                max_rounds=2)
    frames = [
        ("hello", {"current_round": 1}),
        ("round_opened", {"round": 1}),            # replay: no second turn
        ("round_closed", {"round_number": 1, "eliminations": []}),
        ("round_opened", {"round": 2, "deadline_epoch": None}),
        ("round_opened", {"round": 2}),            # stopped already
    ]
    driver.run(lambda: iter(frames))
    assert driver.turns == 2
    assert driver.last_round == 2
    assert len(model.calls) == 2
    # the cycle journal carries both rounds; round 2's script is CLEAN2
    entries = [json.loads(l) for l in
               (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert [e["round"] for e in entries] == [1, 2]
    state = decoded(client, round_state(client))
    assert state["current_round"] == 3
    # round_closed frames journal too — the driver's own world history
    dlines = [json.loads(l) for l in
              (tmp_path / "driver.jsonl").read_text().splitlines()]
    assert any(l["kind"] == "round_closed" and l["round"] == 1
               for l in dlines)


# ===========================================================================
# The catch-up guard: the round moved mid-turn
# ===========================================================================

def test_driver_catches_up_when_round_moves_mid_cycle(client, tmp_path):
    """The deadline/operative advance during the model call: the cycle's
    observation says round 1, but by submission the world is on round
    2 — the driver re-cycles for 2 and consents THERE, never for the
    round it didn't play."""
    from experiments.agent.llm import ScriptedModel

    advance_calls = []

    # admin advance is a REST route, not an MCP tool: the model's call
    # fires it mid-turn (the operator/deadline path resolving round 1
    # while "the model thinks")
    class RestAdvanceModel(ScriptedModel):
        name = "rest-advance"
        def complete(self, system, user):
            reply = super().complete(system, user)
            if not advance_calls:
                advance_calls.append(True)
                tc = TestClient(app)
                r = tc.post("/admin/rounds/advance",
                            headers={"Authorization": "Bearer u-admin"})
                assert r.status_code == 201, r.text
            return reply

    model = RestAdvanceModel([CLEAN, CLEAN2])
    d = client["dynasties"][0]
    lp = AgentLoop(McpClient(client["transports"][d.user_id]), model,
                   entity_id=d.entity_id,
                   journal_path=str(tmp_path / "journal.jsonl"))
    driver = SeatDriver(lp, d.name, tmp_path)

    driver.run(lambda: iter([("hello", {"current_round": 1})]))

    # two cycles for one wake-up: round 1's (mid-turn resolution) + the
    # catch-up round 2
    assert len(model.calls) == 2
    entries = [json.loads(l) for l in
               (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert [e["round"] for e in entries] == [1, 2]
    dlines = [json.loads(l) for l in
              (tmp_path / "driver.jsonl").read_text().splitlines()]
    assert any(l["kind"] == "catchup" and l["played"] == 1 and l["current"] == 2
               for l in dlines)
    # the seat's consent resolved round 2 (round 1 resolved under the
    # operator's hand)
    state = decoded(client, round_state(client))
    assert state["round_number"] == 2
    assert state["current_round"] == 3
    assert driver.last_round == 2


# ===========================================================================
# Elimination: the tombstone
# ===========================================================================

def _kill_seat(client):
    from econengine.models import Entity, EntityStatus
    d = client["dynasties"][0]
    with Session(client["engine"]) as s:
        ent = s.get(Entity, d.entity_id)
        ent.status = EntityStatus.INCAPACITATED
        s.commit()


def test_driver_tombstones_without_a_model_call(client, tmp_path):
    _kill_seat(client)
    driver, model = make_driver(client, [], tmp_path)   # no responses AT ALL
    driver.run(lambda: iter([("hello", {"current_round": 1})]))
    assert model.calls == []                            # the dead get no turn
    assert driver.stopped and driver._extinct
    entries = [json.loads(l) for l in
               (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert entries and entries[-1]["action"] == "extinct"
    dlines = [json.loads(l) for l in
              (tmp_path / "driver.jsonl").read_text().splitlines()]
    assert dlines[-1]["kind"] == "eliminated"


def test_driver_spectates_after_elimination(client, tmp_path):
    _kill_seat(client)
    driver, model = make_driver(client, [], tmp_path, spectate=True)
    driver.run(lambda: iter([
        ("hello", {"current_round": 1}),
        ("round_opened", {"round": 2}),
        ("round_closed", {"round_number": 1, "eliminations": []}),
    ]))
    assert not driver.stopped
    assert model.calls == []
    # the spectating dead still journal the world's rounds
    dlines = [json.loads(l) for l in
              (tmp_path / "driver.jsonl").read_text().splitlines()]
    assert any(l["kind"] == "round_closed" for l in dlines)


# ===========================================================================
# The workspace scaffold
# ===========================================================================

def test_init_workspace_scaffolds_manual_and_config(tmp_path):
    init_workspace(tmp_path, "http://127.0.0.1:8925", "House Mine")
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "response.txt" in agents            # the protocol is the manual
    seat = json.loads((tmp_path / "seat.json").read_text())
    # exactly the public facts: no token VALUE ever lands in the
    # workspace (the manual may NAME the env var — naming is not leaking)
    assert seat == {"seat": "House Mine", "base": "http://127.0.0.1:8925"}


# ===========================================================================
# The catalog fold: the readable world through the public surface
# ===========================================================================

def test_catalog_fold_renders_world_catalog(client, tmp_path):
    from experiments.agent.seat_driver import _catalog_fold
    mcp = McpClient(client["transports"]["u-one"])
    text = _catalog_fold(mcp)
    assert text                        # the fold happened, rendered as text
