"""The conditions register (P2, statuses.py): named, per-tick, queryable
conditions — LOUD, LIT, EMBER today. Derived from state the engine
already keeps (holdings, the light memory, loud-marked events), joined
by held condition-goods so one read shows every active condition from
either family. Readers gate actions by NAME: travel.rules refuses the
dark road without LIT, combat deterrence accepts condition keys, and a
recipe's requires_conditions is a start-gate. All of it is opt-in
world data — a world that declares nothing registers nothing and runs
exactly as before.
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import goods, production, statuses, travel
from econengine.lua_engine import Intent
from econengine.models import Base, Tick
from econengine.scripting import resolve_intent
from econengine.tick import run_tick

_PACK = "demo-world"


@pytest.fixture
def session():
    engine_db = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine_db)
    with Session(engine_db) as s:
        yield s


def _rules(session):
    statuses.set_rules(session, {
        "LIT": {"holding": {"LIT_TORCH": "0.5"}},
        "EMBER": {"lit_within_ticks": 2, "after": "LIT"},
        "LOUD": {"loud_events_within_ticks": 1},
    })


def _entity(session, name="Bearer"):
    from econengine import services
    from econengine.models import EntityType
    return services.create_entity(session, name, EntityType.INDIVIDUAL)


def _hold(session, entity, symbol, qty):
    from econengine import markets
    markets.adjust_holding(session, entity, symbol, Decimal(str(qty)))


def _persis_events(session, number, events):
    session.add(Tick(number=number, events=events,
                     events_hash=json.dumps(events, default=str)))
    session.flush()


# --- derivations ------------------------------------------------------------


def test_no_rules_no_conditions(session):
    who = _entity(session)
    _hold(session, who, "LIT_TORCH", 2)
    assert statuses.active_conditions(session, who, 10) == []


def test_lit_reads_the_holding_floor(session):
    _rules(session)
    who = _entity(session)
    _hold(session, who, "LIT_TORCH", "0.4")
    assert "LIT" not in statuses.active_conditions(session, who, 10)
    _hold(session, who, "LIT_TORCH", "0.6")
    assert "LIT" in statuses.active_conditions(session, who, 10)


def test_ember_stamps_while_lit_and_expires(session):
    _rules(session)
    who = _entity(session)
    _hold(session, who, "LIT_TORCH", 1)
    statuses.stamp_lit(session, 10)                      # lit at 10
    assert statuses.active_conditions(session, who, 11) == ["LIT", "EMBER"]
    _hold(session, who, "LIT_TORCH", -1)                 # the brand dies
    assert statuses.active_conditions(session, who, 11) == ["EMBER"]
    assert statuses.active_conditions(session, who, 12) == ["EMBER"]
    assert statuses.active_conditions(session, who, 13) == []   # dark


def test_loud_counts_applied_acts_last_tick_only(session):
    _rules(session)
    who, other = _entity(session, "Walker"), _entity(session, "Hearer")
    _persis_events(session, 9, [{"type": "say", "entity_id": who.id, "loud": True}])
    _persis_events(session, 10, [
        {"type": "say", "entity_id": who.id, "loud": True},
        {"type": "say", "entity_id": who.id, "loud": True, "status": "rejected"},
        {"type": "say", "entity_id": other.id},
        {"type": "travel_departed", "entity_id": other.id, "loud": True},
    ])
    assert statuses.active_conditions(session, who, 11) == ["LOUD"]
    assert statuses.active_conditions(session, who, 12) == []  # window shut
    rows = statuses.carriers(session, "LOUD", 11)
    assert {(r["entity_id"], r["strength"]) for r in rows} == {
        (other.id, 1), (who.id, 1)}  # rejected says never happened


def test_held_condition_goods_join_the_register(session):
    goods.create_good(session, "COND-WEAK", name="Weakness",
                      modifies_pattern="SKILL-*", modifies_factor=Decimal("0.5"))
    who = _entity(session)
    _hold(session, who, "COND-WEAK", 1)
    assert "COND-WEAK" in statuses.active_conditions(session, who, 5)
    assert statuses.carriers(session, "COND-WEAK", 5) == [
        {"entity_id": who.id, "strength": 1}]


def test_carriers_of_a_holding_condition(session):
    _rules(session)
    who = _entity(session)
    _hold(session, who, "LIT_TORCH", 1)
    assert statuses.carriers(session, "LIT", 10) == [
        {"entity_id": who.id, "strength": 1}]


# --- the night gate on travel -------------------------------------------------


def _road(session):
    from econengine import edges, places
    for key in ("HEARTH", "THICKET"):
        places.create_place(session, key, kind="THICKET", name=key.title(),
                            pack_id=_PACK)
    edges.create_edge(session, "HEARTH", "THICKET", "walk", 1, pack_id=_PACK)
    travel._walk = production.create_recipe(
        session, "TRAVEL_WALK", inputs={}, outputs={}, duration_ticks=1)


def _at_night(session, tick_number):
    """Fast-forward the tick counter into the night of day N."""
    for n in range(1, tick_number + 1):
        _persis_events(session, n, [])


def _travel(session, entity, to):
    return resolve_intent(session, Intent(
        entity_id=entity.id, intent_type="travel", params={"to": to},
        resource_ids=[],
    ))


def test_night_gate_refuses_then_passes_lit(session):
    _rules(session)
    travel.set_travel_rules(session, {
        "night_travel_requires": ["LIT"], "relight_ticks": 2})
    _road(session)
    from econengine import places
    who = _entity(session)
    places.move_entity(session, who, "HEARTH")
    _at_night(session, 22)                     # hour 22: night

    ev = _travel(session, who, "THICKET")
    assert ev["status"] == "rejected"
    assert "dark road refuses" in ev["reason"]

    _hold(session, who, "LIT_TORCH", 1)
    ev = _travel(session, who, "THICKET")
    assert ev.get("status") != "rejected"
    assert ev.get("loud") is True              # torchlit night departure


def test_day_travel_needs_no_condition_and_is_quiet(session):
    _rules(session)
    travel.set_travel_rules(session, {
        "night_travel_requires": ["LIT"], "relight_ticks": 2})
    _road(session)
    from econengine import places
    who = _entity(session)
    places.move_entity(session, who, "HEARTH")
    _at_night(session, 7)                      # tick 7 = hour 07: daylight
    ev = _travel(session, who, "THICKET")
    assert ev.get("status") != "rejected"
    assert ev.get("loud") is None


def _trail(session):
    """HEARTH --1t-- THICKET --1t-- RIVER: a two-hop night walk."""
    from econengine import edges, places
    for key in ("HEARTH", "THICKET", "RIVER"):
        places.create_place(session, key, kind="RIVER", name=key.title(),
                            pack_id=_PACK)
    edges.create_edge(session, "HEARTH", "THICKET", "walk", 1, pack_id=_PACK)
    edges.create_edge(session, "THICKET", "RIVER", "walk", 1, pack_id=_PACK)
    production.create_recipe(
        session, "TRAVEL_WALK", inputs={}, outputs={}, duration_ticks=1)


def test_burnout_mid_route_halts_then_resumes_on_relight(session):
    _rules(session)
    travel.set_travel_rules(session, {
        "night_travel_requires": ["LIT"], "relight_ticks": 2})
    _trail(session)
    from econengine import places
    who = _entity(session)
    places.move_entity(session, who, "HEARTH")
    _hold(session, who, "LIT_TORCH", 1)
    _at_night(session, 22)                     # hour 22: night
    ev = _travel(session, who, "RIVER")
    assert ev.get("status") != "rejected"
    _hold(session, who, "LIT_TORCH", -1)       # the brand dies mid-journey
    t23 = run_tick(session)                    # hop 1 runs (started lit)
    assert not [e for e in t23.events if "travel_" in e["type"]]
    t24 = run_tick(session)                    # arrival chains hop 2: dark
    assert [e["type"] for e in t24.events if "travel_" in e["type"]] == [
        "travel_arrived", "travel_halted"]
    _hold(session, who, "LIT_TORCH", 1)        # strike a fresh brand
    t25 = run_tick(session)                    # the waiting route resumes
    kinds = [e["type"] for e in t25.events if "travel_" in e["type"]]
    assert kinds == ["travel_departed"]
    assert t25.events[0].get("loud") is True   # torchlit night departure


def test_burnout_mid_route_strands_when_no_flame_comes(session):
    _rules(session)
    travel.set_travel_rules(session, {
        "night_travel_requires": ["LIT"], "relight_ticks": 2})
    _trail(session)
    from econengine import places
    who = _entity(session)
    places.move_entity(session, who, "HEARTH")
    _hold(session, who, "LIT_TORCH", 1)
    _at_night(session, 22)
    _travel(session, who, "RIVER")
    _hold(session, who, "LIT_TORCH", -1)
    run_tick(session)                          # hop 1 runs
    t24 = run_tick(session)                    # halt at THICKET, window opens
    assert [e["type"] for e in t24.events if "travel_" in e["type"]] == [
        "travel_arrived", "travel_halted"]
    run_tick(session)                          # 25: still dark, waiting
    run_tick(session)                          # 26: still dark, waiting
    t27 = run_tick(session)                    # 27: window shut -- stranded
    assert [e["type"] for e in t27.events if "travel_" in e["type"]] == [
        "travel_stranded"]


# --- recipe start-gate --------------------------------------------------------


def test_requires_conditions_gates_the_start(session):
    _rules(session)
    who = _entity(session)
    _hold(session, who, "TORCH", 1)
    _persis_events(session, 1, [])
    _persis_events(session, 2, [])
    _persis_events(session, 3, [])
    _persis_events(session, 4, [])
    _persis_events(session, 5, [])
    production.create_recipe(
        session, "CHAIN_TORCH", inputs={"TORCH": 1},
        outputs={"LIT_TORCH": 1}, duration_ticks=0,
        requires_conditions=["EMBER"])
    with pytest.raises(ValueError, match="EMBER"):
        production.start_process(session, who, "CHAIN_TORCH")   # next tick 6: dark
    who.last_lit_tick = 4                      # ember open (6-4 <= 2)
    production.start_process(session, who, "CHAIN_TORCH")   # passes


# --- deterrence reads condition keys ------------------------------------------


def test_deterrence_accepts_condition_names(session):
    _rules(session)
    from econengine import combat
    combat.set_rules(session, {"deterrence": {"LIT": 1}})
    who = _entity(session)
    assert combat._deterred(session, combat.get_rules(session), who.id, 10) is False
    _hold(session, who, "LIT_TORCH", 1)
    assert combat._deterred(session, combat.get_rules(session), who.id, 10) is True


def test_ember_counts_a_brand_burning_right_now(session):
    """The window's other half: a freshly lit brand -- never yet stamped
    by a tick -- still strikes its own successor (`after` floors met).
    The road does not wait for a tick boundary to let you re-light."""
    _rules(session)
    who = _entity(session)
    _hold(session, who, "LIT_TORCH", 1)
    who.last_lit_tick = None                      # no stamp has run yet
    assert statuses.active_conditions(session, who, 1) == ["LIT", "EMBER"]
    _hold(session, who, "LIT_TORCH", -1)
    assert statuses.active_conditions(session, who, 1) == []
