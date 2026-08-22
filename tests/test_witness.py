"""Speech and witness v1 (game.md 15.6): entities that talk, entities
that hear. The say intent (free, bounded), emission-time delivery into
the witness table, and the three ears: script feed, activity read,
world-log mark."""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from econengine import witness
from econengine.describe import render_event
from econengine.lua_engine import Intent
from econengine.models import (
    Base, EntityType, EventObserver, Script, ScriptType,
)
from econengine.scripting import resolve_intent
from econengine.services import create_account, create_entity
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def pair(session):
    """Two talking entities; their behaviour scripts come per test."""
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    create_account(session, alice, "USD", Decimal("100"))
    create_account(session, bob, "USD", Decimal("100"))
    session.flush()
    return alice, bob


def _behaviour(session, entity, source):
    session.add(Script(name=f"b-{entity.name}", source=source,
                       script_type=ScriptType.BEHAVIOUR,
                       entity_id=entity.id))
    session.flush()


def _says(tick, entity_id):
    return [e for e in tick.events
            if e.get("type") == "say" and e.get("entity_id") == entity_id]


# --- the intent: free but bounded -----------------------------------------

def test_say_applies_and_renders(pair, session):
    alice, _ = pair
    _behaviour(session, alice, 'ctx.action.say("Berries at 3")')
    tick = run_tick(session)
    says = _says(tick, alice.id)
    assert len(says) == 1 and says[0]["status"] == "applied"
    assert render_event(says[0]) == 'says: "Berries at 3"'


def test_say_rejects_empty_and_overlong(pair, session):
    alice, _ = pair
    said: set[str] = set()
    quiet = resolve_intent(session, Intent(
        entity_id=alice.id, intent_type="say",
        params={"text": "   "}, resource_ids=[]), said=said)
    assert quiet["status"] == "rejected" and quiet["reason"] == "say text is empty"

    loud = resolve_intent(session, Intent(
        entity_id=alice.id, intent_type="say",
        params={"text": "a" * 257}, resource_ids=[]), said=said)
    assert loud["status"] == "rejected"
    assert "256" in loud["reason"]

    non_string = resolve_intent(session, Intent(
        entity_id=alice.id, intent_type="say",
        params={"text": 42}, resource_ids=[]), said=said)
    assert non_string["status"] == "rejected"


def test_one_say_per_entity_per_tick(pair, session):
    alice, _ = pair
    _behaviour(session, alice,
               'ctx.action.say("first")\nctx.action.say("second")')
    tick = run_tick(session)
    says = _says(tick, alice.id)
    assert len(says) == 2          # an attempt is an action: both render
    assert [s["status"] for s in says] == ["applied", "rejected"]
    assert says[1]["reason"] == "one say per tick"
    # Next tick the budget resets: Alice may speak again (her script
    # still says twice, so the second is refused again).
    tick2 = run_tick(session)
    assert [s["status"] for s in _says(tick2, alice.id)][0] == "applied"


def test_say_text_is_normalized(pair, session):
    alice, _ = pair
    _behaviour(session, alice, 'ctx.action.say("  padded  ")')
    tick = run_tick(session)
    assert _says(tick, alice.id)[0]["params"]["text"] == "padded"


# --- delivery: the witness table ------------------------------------------

def test_delivery_broadcasts_observables_only(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    session.flush()
    events = [
        {"type": "say", "entity_id": alice.id, "status": "applied"},
        {"type": "say", "entity_id": alice.id, "status": "rejected",
         "reason": "one say per tick"},   # a refusal was never spoken
        {"type": "script_error", "entity_id": bob.id},   # private
        {"type": "entity_incapacitated", "entity_id": bob.id,
         "condition": "HUNGER"},                          # loud fact
    ]
    written = witness.record_delivery(session, 7, events)
    rows = session.execute(select(EventObserver)).scalars().all()
    # say(applied) + incapacitated, each to both entities: 4 rows.
    assert written == 4 and len(rows) == 4
    delivered = {(r.event_index, r.observer_id) for r in rows}
    assert (0, alice.id) in delivered and (0, bob.id) in delivered
    assert (3, alice.id) in delivered and (3, bob.id) in delivered
    assert all(r.tick_number == 7 for r in rows)


def test_run_tick_freezes_delivery(pair, session):
    alice, bob = pair
    _behaviour(session, alice, 'ctx.action.say("hello")')
    _behaviour(session, bob, "return")   # silent, but listening
    tick = run_tick(session)
    rows = session.execute(select(EventObserver)).scalars().all()
    assert {(r.tick_number, r.event_index) for r in rows} == {(tick.number, 0)}
    assert {r.observer_id for r in rows} == {alice.id, bob.id}


# --- ear 1: the behaviour script's ctx.events ------------------------------

def test_scripts_hear_speech_next_tick(pair, session):
    alice, bob = pair
    _behaviour(session, alice, 'ctx.action.say("buying BERRIES at 2")')
    _behaviour(session, bob,
               'local heard = false\n'
               'for _, e in ipairs(ctx.events) do\n'
               '  if e.type == "say" then heard = true end\n'
               'end\n'
               'if heard then ctx.action.say("deal") end')
    run_tick(session)   # Alice speaks; delivery frozen
    tick2 = run_tick(session)
    assert [s["status"] for s in _says(tick2, bob.id)] == ["applied"]


def test_scripts_do_not_hear_private_events(pair, session):
    alice, bob = pair
    # Alice's script crashes (private); Bob listens for ANY rival event.
    _behaviour(session, alice, "error('boom')")
    _behaviour(session, bob,
               'local rival = false\n'
               'for _, e in ipairs(ctx.events) do\n'
               '  if e.entity_id ~= ctx.entity.id then rival = true end\n'
               'end\n'
               'if rival then ctx.action.say("spying") end')
    run_tick(session)
    tick2 = run_tick(session)
    assert not _says(tick2, bob.id)   # the crash never reached him


# --- ear 2: the activity read ----------------------------------------------

def test_witnessed_read_flags_delivered_rows(pair, session):
    from econ.api.activity import activity_rows
    alice, bob = pair
    _behaviour(session, alice, 'ctx.action.say("hello")')
    run_tick(session)

    default_rows = activity_rows(session, bob.id, 5)
    assert default_rows == []          # privacy default unchanged

    rows = activity_rows(session, bob.id, 5, witnessed=True)
    heard = [r for r in rows if r["witnessed"]]
    assert len(heard) == 1
    assert heard[0]["text"] == 'says: "hello"'
    assert heard[0]["entity_id"] == alice.id
    # Alice's own read is unchanged shape (no flags without the param).
    own = activity_rows(session, alice.id, 5)
    assert own and "witnessed" not in own[0]


def test_world_log_unaffected_by_speech(pair, session):
    from econ.api.activity import activity_rows
    alice, _ = pair
    _behaviour(session, alice, 'ctx.action.say("hello")')
    run_tick(session)
    # The world log keeps the unattributed public facts; speech is
    # attributed, so it rides entity reads only.
    assert activity_rows(session, None, 5) == []


def test_render_say_refusal(pair, session):
    alice, _ = pair
    _behaviour(session, alice,
               'ctx.action.say("first")\nctx.action.say("second")')
    tick = run_tick(session)
    rejected = [e for e in _says(tick, alice.id) if e["status"] == "rejected"][0]
    assert "refused: one say per tick" in render_event(rejected)
