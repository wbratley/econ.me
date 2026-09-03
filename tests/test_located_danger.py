"""Located danger — threats, bites, and prowls get an address
(docs/spatial.md S4).

A placed threat pressures only the entities its world says are there:
standing at the spot, or mid-hop on a road that started there (a
traveller stands at the hop's origin until arrival moves them — the
road exposes you place by place, "computed from the same rows").
Hunting is up close: when both fighters are placed they must share a
spot, and a placed predator's desperate prowl finds only the speakers
on its own ground. Everything fires on declared data only — a threat
with no place stays the ambient everywhere-dark, an unplaced fighter
keeps the global night, and a world with no map runs exactly as
before (the legacy pins below).
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import combat, edges, markets, places, production, threats
from econengine import goods as goods_mod
from econengine import services
from econengine.models import (
    Base, Entity, EntityType, EntityStatus, Place, Tick,
)
from econengine.tick import run_tick

_PACK = "demo-world"


@pytest.fixture
def session():
    engine_db = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine_db)
    with Session(engine_db) as s:
        yield s


def _map(session):
    """Two grounds an hour apart: the hearth and the wolves' forest."""
    for key in ("HEARTH", "FOREST"):
        places.create_place(session, key, kind=key, name=key.title(),
                            pack_id=_PACK)
    edges.create_edge(session, "HEARTH", "FOREST", "walk", 1, pack_id=_PACK)
    production.create_recipe(session, "TRAVEL_WALK", inputs={}, outputs={},
                             duration_ticks=1)


def _someone(session, name, at=None):
    entity = services.create_entity(session, name, EntityType.INDIVIDUAL)
    if at is not None:
        places.move_entity(session, entity, at)
    return entity


def _night_pressure(session, tick=1):
    """One dark hour of every installed threat (tick 1 = hour 0)."""
    return threats.apply_pressure(session, tick, [])


def _hold(session, entity_id, symbol):
    from sqlalchemy import select
    from econengine.models import Holding
    row = session.execute(select(Holding).where(
        Holding.entity_id == entity_id,
        Holding.symbol == symbol)).scalar_one_or_none()
    return row.quantity if row is not None else Decimal("0")


# --- threats: a home range ----------------------------------------------------


def test_threat_install_with_a_place_and_a_loud_unknown(session):
    _map(session)
    t = threats.create_threat(session, "WOLF_DARK", "EXPOSURE",
                              Decimal("0.5"), place_key="FOREST")
    assert t.place.key == "FOREST"
    with pytest.raises(ValueError, match="ranges over unknown place 'MOON'"):
        threats.create_threat(session, "MOON_DARK", "EXPOSURE",
                              Decimal("0.5"), place_key="MOON")


def test_placed_threat_presses_only_its_ground(session):
    _map(session)
    threats.create_threat(session, "WOLF_DARK", "EXPOSURE",
                          Decimal("0.5"), place_key="FOREST")
    camper = _someone(session, "Camper", at="HEARTH")
    hunter = _someone(session, "Hunter", at="FOREST")
    ghost = _someone(session, "Ghost")            # unplaced: nowhere

    events = _night_pressure(session)
    pressed = {e["entity_id"] for e in events}
    assert pressed == {hunter.id}
    assert _hold(session, hunter.id, "EXPOSURE") == Decimal("0.5")
    assert _hold(session, camper.id, "EXPOSURE") == Decimal("0")
    assert _hold(session, ghost.id, "EXPOSURE") == Decimal("0")


def test_unplaced_threat_stays_the_ambient_dark(session):
    """The legacy pin: a threat with no place finds everyone, placed or
    not -- the pre-spatial night, unchanged."""
    _map(session)
    threats.create_threat(session, "NIGHT", "EXPOSURE", Decimal("0.5"))
    camper = _someone(session, "Camper", at="HEARTH")
    hunter = _someone(session, "Hunter", at="FOREST")
    ghost = _someone(session, "Ghost")

    pressed = {e["entity_id"] for e in _night_pressure(session)}
    assert pressed == {camper.id, hunter.id, ghost.id}


def test_the_road_exposes_the_traveller_place_by_place(session):
    """A traveller stands at the hop's origin until arrival moves them:
    the forest's dark presses the walker while the hop runs, and the
    hearth's dark takes over the hour they arrive. Same rows, no
    special cases."""
    _map(session)
    threats.create_threat(session, "WOLF_DARK", "EXPOSURE",
                          Decimal("0.5"), place_key="FOREST")
    threats.create_threat(session, "COLD_VALLEY", "EXPOSURE",
                          Decimal("0.25"), place_key="HEARTH")
    walker = _someone(session, "Walker", at="HEARTH")

    # tick 1 (hour 0, night): set out for the forest -- the hop runs
    # through the tick, and mid-hop the walker still stands at the
    # hearth, so the valley's cold presses, not the wolves.
    from econengine.scripting import Intent, resolve_intent
    resolve_intent(session, Intent(entity_id=walker.id, intent_type="travel",
                                   params={"to": "FOREST"}, resource_ids=[]))
    tick = run_tick(session)                      # hour 0: night
    pressed = {(e["threat"], e["entity_id"]) for e in tick.events
               if e["type"] == "threat_pressure"}
    assert pressed == {("COLD_VALLEY", walker.id)}

    # tick 2: arrived at the forest (arrival pass runs before pressure)
    # -- now it is the wolves' dark that presses.
    tick = run_tick(session)                      # hour 1: night
    pressed = {(e["threat"], e["entity_id"]) for e in tick.events
               if e["type"] == "threat_pressure"}
    assert pressed == {("WOLF_DARK", walker.id)}


# --- combat: up close ------------------------------------------------------------


def _fighters(session):
    """A wolf-stat attacker and a fightable defender, both creatures."""
    wolf = _someone(session, "Wolf")
    combat.create_stat(session, wolf.id, "ATTACK", Decimal("4"))
    combat.create_stat(session, wolf.id, "DEFENSE", Decimal("1"))
    combat.create_stat(session, wolf.id, "HITS", Decimal("12"))
    markets.adjust_holding(session, wolf, "HITS", Decimal("12"))
    house = _someone(session, "House")
    combat.create_stat(session, house.id, "ATTACK", Decimal("1"))
    combat.create_stat(session, house.id, "DEFENSE", Decimal("1"))
    combat.create_stat(session, house.id, "HITS", Decimal("20"))
    markets.adjust_holding(session, house, "HITS", Decimal("20"))
    return wolf, house


def test_attack_needs_co_location_on_the_map(session):
    _map(session)
    combat.set_rules(session, {"night_only": True,
                               "deterrence": {"WARMTH": 1}})
    wolf, house = _fighters(session)
    places.move_entity(session, wolf, "FOREST")
    places.move_entity(session, house, "HEARTH")

    ev = combat.resolve_attack(session, wolf.id, house.id, 1)   # night hour
    assert ev["status"] == "rejected"
    assert "House is at Hearth" in ev["reason"]
    assert "you are at Forest" in ev["reason"]
    assert "up close" in ev["reason"]
    assert _hold(session, house.id, "HITS") == Decimal("20")

    # same ground: the attempt resolves (the hearth's firelight speaks)
    markets.adjust_holding(session, house, "WARMTH", Decimal("1"))
    places.move_entity(session, wolf, "HEARTH")
    ev = combat.resolve_attack(session, wolf.id, house.id, 1)
    assert ev.get("deterred") is True and not ev.get("hit")


def test_unplaced_fighters_keep_the_global_night(session):
    """The legacy pin: no map binding, no gate -- the pre-spatial hunt.
    Either side unplaced is enough: the night stays global for those
    who are nowhere."""
    goods_mod.create_good(session, "WARMTH")
    combat.set_rules(session, {"night_only": True,
                               "deterrence": {"WARMTH": 1}})
    wolf, house = _fighters(session)      # both unplaced
    markets.adjust_holding(session, house, "WARMTH", Decimal("1"))
    ev = combat.resolve_attack(session, wolf.id, house.id, 1)
    assert ev.get("deterred") is True

    # a placed defender against an unplaced attacker (and vice versa):
    # the gate fires only when both stand on the map
    _map(session)
    places.move_entity(session, house, "HEARTH")
    ev = combat.resolve_attack(session, wolf.id, house.id, 1)
    assert ev.get("deterred") is True and not ev.get("status") == "rejected"
    places.move_entity(session, wolf, "FOREST")
    places.move_entity(session, house, None)
    ev = combat.resolve_attack(session, wolf.id, house.id, 1)
    assert ev.get("deterred") is True


def test_placed_prowl_finds_only_its_own_ground(session):
    _map(session)
    wolf, house = _fighters(session)
    places.move_entity(session, wolf, "FOREST")
    far = _someone(session, "Far")
    combat.create_stat(session, far.id, "HITS", Decimal("20"))
    markets.adjust_holding(session, far, "HITS", Decimal("20"))
    places.move_entity(session, far, "HEARTH")
    places.move_entity(session, house, "FOREST")   # a camper in the range

    # the far house is the loudest speaker of the night -- but it is an
    # hour away, and a prowl is not: the wolf hears the camper instead
    session.add(Tick(number=1, events=[
        {"type": "say", "entity_id": far.id},
        {"type": "say", "entity_id": far.id},
        {"type": "say", "entity_id": house.id},
    ]))
    session.commit()
    assert combat.pick_prey(session, 2, exclude_id=wolf.id) == house.id

    # nothing on its ground: no prey at all (the refusal is honest)
    places.move_entity(session, house, "HEARTH")
    assert combat.pick_prey(session, 2, exclude_id=wolf.id) is None

    # an unplaced predator hears the whole world: the loudest, anywhere
    places.move_entity(session, wolf, None)
    assert combat.pick_prey(session, 2, exclude_id=wolf.id) == far.id


# --- the readable map ------------------------------------------------------------


def test_catalog_renders_places_and_roads(session):
    from econengine.catalog import catalog_state
    _map(session)
    threats.create_threat(session, "WOLF_DARK", "EXPOSURE",
                          Decimal("0.5"), place_key="FOREST")

    state = catalog_state(session)
    assert [p["key"] for p in state["places"]] == ["FOREST", "HEARTH"]
    assert state["places"][0]["kind"] == "FOREST"
    road = state["roads"][0]
    assert (road["from"], road["to"], road["mode"], road["cost_ticks"]) == \
        ("HEARTH", "FOREST", "WALK", 1)
    assert road["bidirectional"] is True
    threat = state["threats"][0]
    assert threat["place"] == "FOREST"
    assert threat["line"].startswith("lives at Forest")


def test_spawn_template_places_the_creature(session):
    """Dens as data (S1 planted the column; S4's wolves use it): a
    template with a place wakes its creature standing there."""
    from econengine import spawns
    goods_mod.create_good(session, "HITS")
    spawns.set_script_source(session, "beast", "-- prowl")
    _map(session)
    spawns.set_rules(session, {
        "from_round": 1, "every_rounds": 1, "up_to": 1, "max_alive": 2,
        "name_prefix": "Beast",
        "template": {"entity_type": "individual",
                     "stats": {"ATTACK": 2, "HITS": 5},
                     "holdings": {"HITS": 5},
                     "script_setting": "beast",
                     "account": {"COIN": 0},
                     "place": "FOREST"},
    })
    born = spawns.apply_on_round(session, 1)
    assert len(born) == 1
    assert born[0]["place"] == "FOREST"
    creature = session.get(Entity, born[0]["entity_id"])
    assert creature.location_place_id == places.get_place(
        session, "FOREST").id
