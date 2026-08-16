"""The agent loop over the real MCP surface (docs/actors.md fork A made
real; the scripting arc's payoff — docs/scripting.md Phases 1-3).

The loop is driven through McpClient over the TestClient exactly as a
remote client drives it over httpx (same JSON-RPC bytes; run.py is the
live twin). The model is ScriptedModel — the same prompts a real model
receives, recorded for assertion. What these tests prove is the LOOP:
the parity observation set, the tier vocabulary in the system prompt,
and the feedback cycle — lint refusals and warnings reaching the model
as plain text it must address, never as a zombie its entity dies of.
"""

import json

import pytest
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econ.api.deps import bearer_scheme, get_current_user, get_session
from econ.api.main import app
from econengine.models import Base, User, WorldSetting

from experiments.agent.llm import (
    AnthropicModel, OpenAIModel, ScriptedModel, ScriptedModelEmpty,
    model_from_env, strip_fences,
)
from experiments.agent.loop import AgentLoop, McpClient, McpError
from experiments.agent.run import run_cycles

TRAP = "local fills = settle_last_orders()"          # the nil-call zombie
CLEAN = "ctx.state.plan = std.amount_str(1)"         # legal, tiered, clean
STATEDEP = "ctx.state.hunger = ctx.state.hunger + 1"  # warns on synthetic ctx


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


def mcp(client, user="u-alice") -> McpClient:
    def transport(method, params):
        r = client.post(
            "/mcp", headers={"Authorization": f"Bearer {user}"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        body = r.json()
        assert "error" not in body, body
        return body["result"]
    return McpClient(transport)


def loop(client, responses, user="u-alice", **kw) -> tuple[AgentLoop, ScriptedModel]:
    model = ScriptedModel(responses)
    return AgentLoop(mcp(client, user), model, **kw), model


# ===========================================================================
# The cycle: accept / retry / keep
# ===========================================================================

def test_first_cycle_joins_and_accepts_clean_rewrite(client):
    lp, _ = loop(client, [CLEAN])
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 1
    assert entry["warnings"] == [] and entry["refusal"] is None
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["source"] == CLEAN


def test_lint_refusal_feeds_back_and_second_attempt_accepts(client):
    lp, model = loop(client, [TRAP, CLEAN])
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 2
    assert "undeclared global 'settle_last_orders'" in entry["refusal"]
    # the refusal reached the model verbatim, as plain text to address
    assert "submission refused by lint" in model.calls[1]["user"]
    assert "settle_last_orders" in model.calls[1]["user"]
    # and the fix is live: the entity now runs the clean source
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["source"] == CLEAN


def test_exhausted_attempts_keep_the_working_behaviour(client):
    lp, _ = loop(client, [TRAP, TRAP, TRAP], max_attempts=3)
    good = lp.mcp.call("set_behaviour",
                       {"entity_id": lp.ensure_entity(), "source": "-- healthy"})
    entry = lp.cycle()
    assert not entry["accepted"] and entry["kept_old"]
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["id"] == good["id"] and got["source"] == "-- healthy"


def test_no_behaviour_yet_is_not_a_crash(client):
    """A world without a join starter: the fresh player's first cycle has
    nothing to keep and nothing to show — the loop still runs, and the
    first accepted submission creates the behaviour."""
    lp, _ = loop(client, [CLEAN])
    with pytest.raises(McpError):
        lp.mcp.call("get_behaviour", {"entity_id": lp.ensure_entity()})
    entry = lp.cycle()
    assert entry["accepted"]
    assert lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})["source"] == CLEAN


# ===========================================================================
# Feed-forward: the model sees the consequences of its last rewrite
# ===========================================================================

def test_lint_warnings_ride_into_the_next_prompt(client):
    lp, model = loop(client, [STATEDEP, CLEAN])
    first = lp.cycle()
    assert first["accepted"] and len(first["warnings"]) == 1
    lp.cycle()
    assert "lint warning: smoke-run:" in model.calls[1]["user"]


def test_observation_is_the_parity_set(client):
    lp, model = loop(client, [CLEAN])
    lp.cycle()
    user = model.calls[0]["user"]
    assert lp.entity_id in user                    # own entity only
    assert '"prices"' in user                      # public facts
    assert '"leaderboard"' in user
    assert '"round"' in user
    assert "u-bob" not in user                     # no other dynasty's affairs


def test_system_prompt_carries_the_tier_vocabulary(client):
    lp, model = loop(client, [CLEAN])
    lp.cycle()
    system = model.calls[0]["system"]
    assert "amount_str" in system                  # std source, verbatim
    assert "(no world lib installed)" in system    # bare world says so
    assert "settle_last_orders" not in system      # it hallucinates, it errs


# ===========================================================================
# The runner mechanics
# ===========================================================================

def test_run_calls_between_only_between_cycles(client):
    lp, _ = loop(client, [CLEAN, CLEAN, CLEAN])
    seen = []
    entries = lp.run(3, between=lambda i: seen.append(i))
    assert [e["accepted"] for e in entries] == [True, True, True]
    assert seen == [0, 1]                          # never after the last

    with pytest.raises(ScriptedModelEmpty):
        lp.cycle()                                 # queue dry: loud, not stale


def test_journal_records_each_cycle(client, tmp_path):
    path = tmp_path / "journal.jsonl"
    lp, _ = loop(client, [TRAP, CLEAN], journal_path=str(path))
    lp.cycle()
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["attempts"] == 2 and lines[0]["accepted"]
    assert lines[0]["model"] == "scripted" and lines[0]["source_sha"]


# ===========================================================================
# Model plumbing (no network)
# ===========================================================================

def test_strip_fences():
    assert strip_fences("```lua\n-- x\n```") == "-- x"
    assert strip_fences("  -- plain  ") == "-- plain"


def test_model_from_env_picks_by_configuration():
    with pytest.raises(RuntimeError, match="no model configured"):
        model_from_env({})
    m = model_from_env({"ECON_AGENT_SCRIPTED_FILE": "/dev/null"})
    assert isinstance(m, ScriptedModel)
    m = model_from_env({"ANTHROPIC_API_KEY": "k", "ECON_AGENT_MODEL": "m-slug"})
    assert isinstance(m, AnthropicModel) and m.name == "anthropic:m-slug"
    m = model_from_env({"OPENAI_API_KEY": "k"})
    assert isinstance(m, OpenAIModel) and m.name == "openai:gpt-4o"


# ===========================================================================
# The clock vote: set_ready (game.md §9.1)
# ===========================================================================

def test_set_ready_records_consent_in_operator_mode(client):
    """Default gate: readiness is recorded but never fires — the operator
    stays the clock. The call is pure MCP, like everything else."""
    lp, _ = loop(client, [CLEAN])
    lp.ensure_entity()                       # alice now owns an ACTIVE entity
    out = lp.set_ready()
    assert out["resolved"] is None
    r = out["readiness"]
    assert r["mode"] == "operator"
    assert r["ready"] == 1 and r["eligible"] == 1


def test_set_ready_closes_the_round_in_readiness_mode(client, monkeypatch):
    """Readiness-gated world: the sole eligible player's ready is final —
    it resolves the round in-request, and the next observation sees a
    world that moved. The players' clock, no admin anywhere."""
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "2")
    with Session(app.state._test_engine) as session:
        session.add(WorldSetting(key="round.gate", value={"mode": "readiness"}))
        session.commit()
    lp, _ = loop(client, [CLEAN])
    lp.ensure_entity()
    out = lp.set_ready()
    assert out["resolved"]["round_number"] == 1
    assert out["resolved"]["ticks"] == [1, 2]
    assert out["readiness"]["round"] == 2      # register reset for round 2
    assert lp.observe()["round"]["round_number"] == 1


def test_run_cycles_ready_moves_the_world_between_cycles(client, monkeypatch):
    """--ready semantics: the ready fires after EACH cycle (the last
    included), so cycle 2 observes a world that moved. Regression for the
    first live run, where all cycles observed one frozen round and both
    readies fired at the end."""
    monkeypatch.setenv("ECON_TICKS_PER_ROUND", "2")
    with Session(app.state._test_engine) as session:
        session.add(WorldSetting(key="round.gate", value={"mode": "readiness"}))
        session.commit()
    lp, _ = loop(client, [CLEAN, CLEAN])
    lp.ensure_entity()                        # sole eligible player
    entries, ready_log = run_cycles(lp, 2, ready=True)
    # each ready closed its round: two resolutions, world moved each time
    assert [r["resolved"]["round_number"] for r in ready_log] == [1, 2]
    # and the cycles saw it: cycle 2 observed the round AFTER round 1
    assert entries[0]["round"] == 1
    assert entries[1]["round"] == 2


def test_run_cycles_operator_mode_ready_records_without_firing(client):
    """Default gate: run_cycles still works -- readies record, nothing
    fires, and every cycle stays inside the open round."""
    lp, _ = loop(client, [CLEAN, CLEAN])
    lp.ensure_entity()
    entries, ready_log = run_cycles(lp, 2, ready=True)
    assert all(r["resolved"] is None for r in ready_log)
    assert [e["round"] for e in entries] == [1, 1]   # world never moved


def test_run_cycles_operator_between_never_after_last(client):
    """The operator clock through run_cycles: between cycles only, never
    after the last -- run.py's --advance path rides this."""
    lp, _ = loop(client, [CLEAN, CLEAN, CLEAN])
    lp.ensure_entity()
    seen = []
    entries, ready_log = run_cycles(lp, 3, between=lambda i: seen.append(i))
    assert seen == [0, 1]
    assert ready_log == []
    assert all(e["accepted"] for e in entries)
