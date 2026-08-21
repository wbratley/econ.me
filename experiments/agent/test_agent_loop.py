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
    extract_script, extract_script_detailed, model_from_env, strip_fences,
    strip_think,
)
from experiments.agent.loop import (
    AgentLoop, McpClient, McpError, system_prompt, user_prompt,
)
from experiments.agent.run import run_cycles

TRAP = "local fills = settle_last_orders()"          # the nil-call zombie
CLEAN = "ctx.state.plan = std.amount_str(1)"         # legal, tiered, clean
STATEDEP = "ctx.state.hunger = ctx.state.hunger + 1"  # warns on synthetic ctx

# The stone-run6 wrong-candidate shape: Nemotron's round-3 script still
# indexed ctx.accounts[0] (Lua lists are 1-indexed — the runtime crash),
# differing from the round-2 crasher by ONE leading space per line: the
# reply quoted the old script in an indented fence before the fix.
CRASHER = ("local account_id = ctx.accounts[0].id\n"
           "ctx.action.place_order('JERKY', 'buy', std.amount_str(2), "
           "std.amount_str(2.21), account_id, 30)\n")
FIXED_IDX = CRASHER.replace("accounts[0]", "accounts[1]")


def _quote(src: str, indent: str = " ") -> str:
    """Re-emit a script one indent deeper — what a model does when it
    quotes the running behaviour inside a fenced block."""
    return "\n".join(indent + ln if ln.strip() else ln
                      for ln in src.splitlines())


QUOTED_REPLY = (
    "The current behaviour for reference:\n"
    "    ```lua\n" + _quote(CRASHER) + "\n    ```\n"
    "And the corrected behaviour:\n"
    "```lua\n" + FIXED_IDX + "```"
)


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
    # a single-candidate accepted round journals the choice but not a
    # reply head — forensics only where there was a choice to get wrong
    assert entry["extractor"]["n"] == 1 and entry["reply_head"] is None
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["source"] == CLEAN


def test_diary_captures_the_minds_reasoning(client):
    # on: one extra call after the decision lands in the entry — and it
    # carries the full round record, so the diary is grounded in what
    # the model actually wrote, not a hallucination of it
    lp, model = loop(client, [CLEAN, "Farming held. Grain is abundant and "
                                 "cheap, so I will keep producing and try "
                                 "selling the surplus."], diary=True)
    entry = lp.cycle()
    assert entry["accepted"]
    assert "Grain is abundant" in entry["thoughts"]
    diary_user = model.calls[-1]["user"]      # the diary call's prompt
    assert "FINAL OUTCOME: action taken: rewrite" in diary_user
    assert CLEAN in diary_user                 # its own code, verbatim
    assert "RULES YOU PLAYED UNDER" in diary_user   # the system, too
    assert "OBSERVATION (exactly what your entity can see)" in diary_user
    assert entry["prompt_bytes"] == len(model.calls[0]["user"])  # decision,
    #                                                               not diary


def test_diary_leak_flag_marks_prompt_restatement_openers(client):
    # run-7's one true leak of 44: the entry OPENED with a restatement of
    # the prompt (context-poisoned by the refused prose round before it)
    lp, _ = loop(client, [CLEAN, "We are given the current behavior and "
                                 "the observation for the entity. The entity "
                                 "is House Nemotron."], diary=True)
    entry = lp.cycle()
    assert entry["diary_leak"] is True


def test_diary_leak_flag_ignores_mid_sentence_phrasing(client):
    # ...adequately in the given scenario" is first-person English, not
    # leakage — only an OPENER restates the prompt
    lp, _ = loop(client, [CLEAN, "I kept the behaviour unchanged as it "
                                 "seemed to be functioning adequately in "
                                 "the given scenario."], diary=True)
    entry = lp.cycle()
    assert entry["diary_leak"] is False


def test_diary_record_shows_the_full_retry_chain(client):
    # a round with a lint refusal: the diary record must carry BOTH
    # attempts — the refused reply, the platform's rejection, the
    # corrected reply — or the diary would narrate a clean round that
    # never happened
    lp, model = loop(client, [TRAP, CLEAN,
                              "I tried to settle orders at the last tick and "
                              "lint rejected it; the fallback holds."],
                     diary=True)
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 2
    diary_user = model.calls[-1]["user"]
    assert TRAP in diary_user                    # the failed attempt, kept
    assert "submission refused by lint" in diary_user
    assert diary_user.count("PROMPT TO YOU") == 2
    assert CLEAN in diary_user


def test_diary_off_by_default_and_failure_degrades_to_silence(client):
    lp, model = loop(client, [CLEAN])          # no diary line provisioned
    entry = lp.cycle()
    assert entry["thoughts"] == ""           # off: no extra call at all
    assert len(model.calls) == 1


def test_lint_refusal_feeds_back_and_second_attempt_accepts(client):
    lp, model = loop(client, [TRAP, CLEAN])
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 2
    assert "undeclared global 'settle_last_orders'" in entry["refusal"]
    # the refusal reached the model verbatim, as plain text to address
    assert "submission refused by lint" in model.calls[1]["user"]
    assert "settle_last_orders" in model.calls[1]["user"]
    # strict-globals refusals carry their hint: the one move that fixes them
    assert "undeclared global" in model.calls[1]["user"]
    assert "local" in model.calls[1]["user"]
    # and the fix is live: the entity now runs the clean source
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["source"] == CLEAN


def test_syntax_refusal_hints_against_prose(client):
    """The stone-run2 failure mode: a reply that opens with English dies
    as `syntax error near 'are'`. The retry prompt must say WHAT to do —
    resend code alone — not just parrot the loader's error."""
    # NOTE: extract_script now recovers code from prose (island scan),
    # so the hint fires only when there is NO code to recover: pure
    # deliberation, no fence, no Lua-looking line anywhere.
    pure_prose = ("I would keep gathering berries this round and cook "
                  "the meat before rot sets in, then reconsider the spear.")
    lp, model = loop(client, [pure_prose, CLEAN])
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 2
    retry = model.calls[1]["user"]
    assert "syntax error near" in retry
    assert "prose" in retry
    assert "code alone" in retry


def test_extract_script_recovers_unfenced_code_from_prose(client):
    """The stone-run5 failure: 9 Nemotron rounds died as `syntax error
    near 'are'` because the reply had prose around code and NO fence —
    the one-shot extractor fell back to the raw text. The island scan
    plus compile check now recovers the program."""
    raw = ("We are given the current behaviour. My plan:\n"
           "cook first, then gather.\n\n"
           + CLEAN + "\n\n"
           "This should keep the house fed through the round.")
    lp, _ = loop(client, [raw])
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 1
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["source"] == CLEAN


def test_think_block_is_peeled_before_the_action_is_read(client):
    """A reasoning model that deliberates in <think> tags then decides
    KEEP: the decision is the post-deliberation text, the deliberation
    is not prose-around-KEEP."""
    lp, _ = loop(client, ["<think>\nthe firewood plan holds another round\n</think>\nKEEP"])
    good = lp.mcp.call("set_behaviour",
                       {"entity_id": lp.ensure_entity(), "source": "-- healthy"})
    entry = lp.cycle()
    assert entry["action"] == "keep" and entry["attempts"] == 1
    assert entry["refusal"] is None
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["id"] == good["id"]


def test_exhausted_attempts_keep_the_working_behaviour(client):
    lp, _ = loop(client, [TRAP, TRAP, TRAP], max_attempts=3)
    good = lp.mcp.call("set_behaviour",
                       {"entity_id": lp.ensure_entity(), "source": "-- healthy"})
    entry = lp.cycle()
    assert not entry["accepted"] and entry["kept_old"]
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["id"] == good["id"] and got["source"] == "-- healthy"


# ===========================================================================
# KEEP and edit blocks: three ways to answer
# ===========================================================================

def test_keep_carries_the_behaviour_forward_verbatim(client):
    lp, _ = loop(client, ["KEEP"])
    good = lp.mcp.call("set_behaviour",
                       {"entity_id": lp.ensure_entity(), "source": "-- healthy"})
    entry = lp.cycle()
    assert entry["action"] == "keep" and entry["attempts"] == 1
    assert not entry["accepted"] and entry["kept_old"]
    assert entry["refusal"] is None              # a choice, not a failure
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["id"] == good["id"] and got["source"] == "-- healthy"


def test_keep_without_previous_behaviour_is_refused(client):
    lp, model = loop(client, ["KEEP", CLEAN])
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 2
    assert "no previous behaviour to keep" in model.calls[1]["user"]


def test_edit_blocks_patch_the_current_behaviour(client):
    lp, _ = loop(client, ["<<<<<<< SEARCH\nctx.state.plan = std.amount_str(1)\n=======\nctx.state.plan = std.amount_str(2)\n>>>>>>> REPLACE"])
    lp.mcp.call("set_behaviour",
                {"entity_id": lp.ensure_entity(), "source": CLEAN})
    entry = lp.cycle()
    assert entry["accepted"] and entry["action"] == "edit"
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["source"] == "ctx.state.plan = std.amount_str(2)"


def test_failed_patch_feeds_back_and_retries(client):
    bad = "<<<<<<< SEARCH\nnot in the source anywhere\n=======\nx\n>>>>>>> REPLACE"
    good = f"<<<<<<< SEARCH\n{CLEAN}\n=======\nctx.state.plan = std.amount_str(3)\n>>>>>>> REPLACE"
    lp, model = loop(client, [bad, good])
    lp.mcp.call("set_behaviour",
                {"entity_id": lp.ensure_entity(), "source": CLEAN})
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 2
    assert "SEARCH not found" in model.calls[1]["user"]
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["source"] == "ctx.state.plan = std.amount_str(3)"


def test_system_prompt_offers_the_three_actions(client):
    lp, _ = loop(client, [CLEAN])
    model_edit = ScriptedModel([CLEAN])
    AgentLoop(mcp(client), model_edit, edit_mode=True)
    # default mode: full rewrite, and KEEP as the no-change answer
    prompt = system_prompt({"std": {"source": "-- std"}}, "e1")
    assert "KEEP" in prompt and "SEARCH" not in prompt
    # edit mode: all three
    prompt_edit = system_prompt({"std": {"source": "-- std"}}, "e1",
                                edit_mode=True)
    assert "KEEP" in prompt_edit and "<<<<<<< SEARCH" in prompt_edit
    assert ">>>>>>> REPLACE" in prompt_edit


class FlakyProvider:
    """A model that fails like a reasoning model whose thinking ate the
    token budget: the call succeeds at the wire, the final channel is
    empty. The loop must count it as an attempt and play on."""

    name = "flaky"

    def __init__(self, failures: int):
        self.failures = failures
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("empty final content (finish_reason=length)")
        return CLEAN


def test_model_failure_is_an_attempt_not_a_crash(client):
    model = FlakyProvider(1)
    lp = AgentLoop(mcp(client), model)
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 2
    assert "model failure" in entry["refusal"] or entry["refusal"] is None
    # the failure reached the model as plain text to react to
    assert "finish_reason=length" in model.calls[1]["user"]


def test_persistent_model_failure_keeps_the_old_behaviour(client):
    model = FlakyProvider(99)
    lp = AgentLoop(mcp(client), model, max_attempts=3)
    good = lp.mcp.call("set_behaviour",
                       {"entity_id": lp.ensure_entity(), "source": "-- healthy"})
    entry = lp.cycle()
    assert not entry["accepted"] and entry["kept_old"]
    assert "model failure" in entry["refusal"]
    assert len(model.calls) == 3                 # bounded, never a crash loop
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


def test_observation_hides_everyones_money(client):
    """Rival privacy, prompt side: the standings survive but the money
    column does not — not the rival's, not the player's own either (the
    entity_state accounts carry own cash; the leaderboard is standings)."""
    lp, model = loop(client, [CLEAN])
    lp.cycle()
    user = model.calls[0]["user"]
    assert '"money"' not in user


def test_manual_rides_in_the_system_prompt(client):
    """A pack that ships a manual gets it into every system prompt,
    verbatim — the world's numbers, stated plainly."""
    lp, model = loop(client, [CLEAN], manual="FIRE FIRST: 2 WOOD. Then a BAG.")
    lp.cycle()
    assert "FIRE FIRST: 2 WOOD. Then a BAG." in model.calls[0]["system"]
    assert "WORLD MANUAL" in model.calls[0]["system"]


def test_no_manual_no_section(client):
    lp, model = loop(client, [CLEAN])
    lp.cycle()
    assert "WORLD MANUAL" not in model.calls[0]["system"]


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


def test_strip_fences_survives_a_prose_prefix():
    """The stone-run2 failure: Nemotron prefixed every answer with
    'Here are the changes:' before the fence, and the whole response
    was submitted as Lua -- syntax error near 'are', three attempts,
    frozen behaviour, and a DISEASE death the round-10 fix (which says
    'prioritizes cooking meat') never got to prevent. The fix: take the
    first fenced block anywhere in the response."""
    raw = ("Here are the changes I would make to the behaviour script:\n"
           "```lua\nctx.state.plan = 'cook'\n```\n"
           "This should avoid disease.")
    assert strip_fences(raw) == "ctx.state.plan = 'cook'"


def test_strip_fences_prose_and_no_fence_is_raw():
    # no fence anywhere: treat the text as the code (lint will judge)
    assert strip_fences("-- still lua, just unpunctuated") == \
        "-- still lua, just unpunctuated"


def test_strip_fences_drops_prose_after_the_closing_fence():
    assert strip_fences("```lua\ncode()\n```\nThat was the script.") == "code()"


def test_strip_fences_keep_is_bare_only():
    # the KEEP handshake reads the raw reply now (loop.py), not the
    # extract; sanity pins the extractor's half: prose KEEP is prose
    assert strip_fences("I would KEEP the current script.") != "KEEP"
    assert strip_fences("KEEP") == "KEEP"


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


# ===========================================================================
# Reasoning/code separation (the stone-run5 lesson): extraction candidates,
# think-block peeling, eof hints, reply forensics
# ===========================================================================

def test_strip_think_drops_deliberation_blocks():
    raw = ("<think>\nconsider fire, then food\n</think>\n"
           "-- the decision\nctx.state.plan = 'fire'")
    assert strip_think(raw) == "-- the decision\nctx.state.plan = 'fire'"
    # no tags: unchanged
    assert strip_think("-- plain") == "-- plain"


def test_extract_script_takes_last_fence_when_first_is_prose():
    """A model that fences its REASONING first and its ANSWER second: the
    first fence does not compile, the last one does — the Lua parser
    arbitrates, not the position."""
    raw = ("Reasoning about the round:\n"
           "```python\nthis is not lua at all\n```\n"
           "The behaviour:\n"
           "```lua\nctx.state.plan = std.amount_str(1)\n```")
    assert extract_script(raw) == "ctx.state.plan = std.amount_str(1)"


def test_extract_script_falls_back_verbatim_when_nothing_compiles():
    # nothing recoverable: today's behavior, verbatim (lint judges)
    raw = "Just words about the round, no code anywhere."
    assert extract_script(raw) == raw


def test_extract_script_bypasses_the_quoted_current_script():
    """The stone-run6 wrong-candidate: the reply quotes the CURRENT
    (crashing) script in an indented fence before the corrected code.
    Both compile, so first-that-compiles picked the quote — the
    "fix" differed from the crasher by one leading space per line
    while the diary described the real fix. With `current` in hand the
    quote is recognized and bypassed; without it, yesterday's pick
    stands (no behavior change for callers that pass nothing)."""
    quoted_pick = extract_script(QUOTED_REPLY)          # the quote wins
    assert "accounts[0]" in quoted_pick
    assert "accounts[1]" not in quoted_pick
    fixed = extract_script(QUOTED_REPLY, CRASHER)       # the intent wins
    assert fixed.strip() == FIXED_IDX.strip()


def test_extract_script_resubmits_verbatim_when_no_alternative():
    """A genuine verbatim resubmission — one candidate, nothing
    different on offer — must stand: the guard bypasses a quote only
    when a differing candidate exists in the same reply."""
    raw = "```lua\n" + CRASHER + "```"
    source, info = extract_script_detailed(raw, CRASHER)
    assert source.strip() == CRASHER.strip()
    assert info["n"] == 1 and info["winner"] == 0
    assert info["ws_skip"] is None


def test_extract_script_detailed_reports_the_choice():
    """Forensics: the extractor names its candidates, the winner, and
    any quote it bypassed — the run-6 blind spot was an ACCEPTED round
    whose submission nobody could explain after the fact."""
    source, info = extract_script_detailed(QUOTED_REPLY, CRASHER)
    assert source.strip() == FIXED_IDX.strip()
    assert info["n"] >= 2
    assert info["winner"] > 0                    # not the first candidate
    assert 0 in info["ws_skip"]                 # the quote, named
    assert len(info["shas"]) == info["n"]
    assert all(len(s) == 8 for s in info["shas"])


def test_cycle_journals_extractor_forensics_on_accepted_rounds(client):
    """The loop-level contract: an accepted multi-candidate rewrite
    journals the extractor's decision AND a 200-char head of the raw
    reply, and the FIXED program — not the quote — goes live."""
    lp, _ = loop(client, [QUOTED_REPLY])
    lp.mcp.call("set_behaviour",
                {"entity_id": lp.ensure_entity(), "source": CRASHER})
    entry = lp.cycle()
    assert entry["accepted"] and entry["action"] == "rewrite"
    assert entry["attempts"] == 1
    got = lp.mcp.call("get_behaviour", {"entity_id": lp.entity_id})
    assert got["source"].strip() == FIXED_IDX.strip()   # the fix, not the quote
    assert entry["extractor"]["winner"] > 0
    assert entry["extractor"]["ws_skip"]
    assert entry["reply_head"] == QUOTED_REPLY[:200]


def test_eof_refusal_hints_at_unbalanced_blocks(client):
    """The other run-5 mask: `<eof> expected near 'end'` matched NO hint
    class (not 'syntax error near', not 'unexpected symbol') — the model
    got the bare loader error with no translation."""
    unbalanced = "local function plan()\n  ctx.state.x = 1\nend\nend"
    lp, model = loop(client, [unbalanced, CLEAN])
    entry = lp.cycle()
    assert entry["accepted"] and entry["attempts"] == 2
    retry = model.calls[1]["user"]
    assert "expected near 'end'" in retry
    assert "matching `end`" in retry


def test_journal_records_the_refused_reply_head(client):
    """Failed attempts stopped evaporating: the journal keeps the head of
    the last raw reply when a round exhausts its attempts, so a
    postmortem can read what the model actually sent (run 5 had to guess)."""
    lp, _ = loop(client, [TRAP, TRAP, TRAP], max_attempts=3)
    entry = lp.cycle()
    assert not entry["accepted"]
    assert entry["reply_head"].startswith("local fills")
    ok = lp.cycle.__self__  # sanity: accepted rounds journal None instead
    lp2, _ = loop(client, [CLEAN])
    assert lp2.cycle()["reply_head"] is None


# ===========================================================================
# Crash feedback: deduped, translated runtime errors (the stone-run6
# lesson — a raw Lua error string is a riddle; the "fix" was identical)
# ===========================================================================

def test_crash_feedback_dedups_and_translates():
    from experiments.agent.loop import _crash_feedback
    err = ("[string \"<python>\"]:38: attempt to index a nil value "
           "(field 'integer index')")
    ticks = [{"tick": t, "events": [{"type": "script_error",
                                     "error": err}]}
             for t in range(21, 41)]          # 20 identical crashes
    lines = _crash_feedback(ticks)
    assert len(lines) == 1                     # one line, not twenty
    assert "x20 ticks" in lines[0]
    assert "1-indexed" in lines[0]             # the [0] -> [1] translation
    assert "ctx.accounts[1]" in lines[0]


def test_crash_feedback_general_nil_guard_hint():
    from experiments.agent.loop import _crash_feedback
    ticks = [{"tick": 1, "events": [
        {"type": "script_error",
         "error": "[string \"<python>\"]:5: attempt to index a nil value "
                  "(field 'outputs')"}]}]
    line = _crash_feedback(ticks)[0]
    assert "nil at that moment" in line
    assert "if t and t.field" in line


def test_crash_feedback_string_compare_hint():
    """Llama's run-6 death: `hunger.satisfaction < hunger.required` --
    need fields are strings, required was nil. The translation must
    name tonumber, not just nil-guarding."""
    from experiments.agent.loop import _crash_feedback
    ticks = [{"tick": 81, "events": [
        {"type": "script_error",
         "error": "[string \"<python>\"]:76: attempt to compare string with nil"}]}]
    line = _crash_feedback(ticks)[0]
    assert "tonumber" in line
    assert "strings or nil" in line


def test_crash_feedback_reports_the_revert():
    from experiments.agent.loop import _crash_feedback
    ticks = [{"tick": 30, "events": [
        {"type": "script_error", "error": "boom"},
        {"type": "script_reverted", "from_script_id": "x", "to_script_id": "y"},
    ]}]
    lines = _crash_feedback(ticks)
    assert any("reverted" in l for l in lines)
    assert any(l.startswith("script_error x1") for l in lines)


def test_crash_feedback_no_hint_for_unknown_error():
    from experiments.agent.loop import _crash_feedback
    lines = _crash_feedback([{"tick": 1, "events": [
        {"type": "script_error", "error": "something exotic"}]}])
    assert lines == ["script_error x1 ticks: something exotic"]
