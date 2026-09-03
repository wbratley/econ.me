"""Places — the map as content rows (docs/spatial.md S1).

S1 is deliberately dormant: it records and reads, gates nothing. These
tests pin exactly that: install/claim conflicts (pack provenance),
move_entity as the single writer, parcels sited on the map, spawns with
a den, the observation surfaces (behaviour ctx, MCP entity_state
shape), and the world-lib vocabulary — plus the legacy invariant that
a world with no places behaves exactly as before (nil place, empty
map, no errors).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import places, services, spawns
from econengine.lua_engine import LuaEngine
from econengine.models import Base, EntityType, Parcel, Script, ScriptType
from econengine.tick import run_tick

_PACK = "demo-world"
engine = LuaEngine()


@pytest.fixture
def session():
    engine_db = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine_db)
    with Session(engine_db) as s:
        yield s


def _map(session):
    """A tiny three-place map: hearth clearing, river, trading post."""
    places.create_place(session, "HEARTH", kind="HEARTH", name="Hearth clearing",
                        region_id="valley", pack_id=_PACK)
    places.create_place(session, "RIVER", kind="RIVER", name="The river",
                        region_id="valley", pack_id=_PACK)
    places.create_place(session, "POST", kind="POST", name="Trading post",
                        region_id="pass", pack_id=_PACK)


def _entity(session, name="Wanderer"):
    return services.create_entity(session, name, EntityType.INDIVIDUAL)


def make_script(session, name, source, entity):
    script = Script(
        name=name,
        source=source,
        script_type=ScriptType.BEHAVIOUR,
        entity_id=entity.id,
        timeout_ms=200,
    )
    session.add(script)
    session.flush()
    return script


# --- installing the map ---------------------------------------------------


def test_create_place_uppercases_and_reads_back(session):
    place = places.create_place(session, "river", kind="river",
                                name="The river", pack_id=_PACK)
    assert place.key == "RIVER"
    assert place.kind == "RIVER"
    got = places.get_place(session, "River")  # lookups uppercase too
    assert got is not None and got.id == place.id
    keys = [p.key for p in places.list_places(session)]
    assert keys == ["RIVER"]


def test_second_pack_cannot_claim_a_key(session):
    places.create_place(session, "RIVER", kind="RIVER", pack_id=_PACK)
    with pytest.raises(ValueError, match="already installed by demo-world"):
        places.create_place(session, "RIVER", kind="RIVER", pack_id="other-pack")
    # the platform's keys are equally claimed
    places.create_place(session, "POST", kind="POST")
    with pytest.raises(ValueError, match="already installed by the platform"):
        places.create_place(session, "POST", kind="POST", pack_id=_PACK)


def test_place_facts_projection(session):
    place = places.create_place(session, "RIVER", kind="RIVER", name="The river",
                                region_id="valley", description="Cold and quick.",
                                pack_id=_PACK)
    assert places.place_facts(place) == {
        "key": "RIVER", "name": "The river", "kind": "RIVER",
        "region_id": "valley", "description": "Cold and quick.",
    }
    assert places.place_facts(None) is None
    # extent_ref is a world-layer hint: recorded, never projected
    assert "extent_ref" not in places.place_facts(place)


# --- standing on it --------------------------------------------------------


def test_move_entity_places_and_clears(session):
    _map(session)
    entity = _entity(session)
    assert entity.location_place_id is None  # born unplaced

    places.move_entity(session, entity, "RIVER")
    assert entity.place.key == "RIVER"

    river = places.get_place(session, "RIVER")
    places.move_entity(session, entity, river)  # object form
    assert entity.place.key == "RIVER"

    places.move_entity(session, entity, None)  # clear
    assert entity.location_place_id is None


def test_move_entity_refuses_unknown_place(session):
    entity = _entity(session)
    with pytest.raises(ValueError, match="unknown place 'NOWHERE'"):
        places.move_entity(session, entity, "nowhere")
    assert entity.location_place_id is None


def test_parcel_sited_on_the_map(session):
    _map(session)
    from econengine import parcels as parcels_mod

    parcel = parcels_mod.create_parcel(session, "FIELD", place="RIVER")
    assert parcel.place_id == places.get_place(session, "RIVER").id

    legacy = parcels_mod.create_parcel(session, "FIELD")  # no map, no problem
    assert legacy.place_id is None

    with pytest.raises(ValueError, match="unknown place"):
        parcels_mod.create_parcel(session, "FIELD", place="NOWHERE")


# --- the observation surfaces ----------------------------------------------


def test_behaviour_ctx_sees_place_and_map(session):
    _map(session)
    entity = _entity(session, "Sitter")
    places.move_entity(session, entity, "HEARTH")
    make_script(session, "sitter-behaviour", """
ctx.state.at = ctx.place and ctx.place.key or "none"
ctx.state.map = {}
for _, p in ipairs(ctx.places) do table.insert(ctx.state.map, p.key) end
""", entity)
    run_tick(session)

    script = session.query(Script).filter_by(name="sitter-behaviour").one()
    assert script.state["at"] == "HEARTH"
    assert script.state["map"] == ["HEARTH", "POST", "RIVER"]  # key order


def test_unplaced_entity_sees_nil_place(session):
    _map(session)  # a map exists; the entity simply is not on it
    entity = _entity(session, "Ghost")
    make_script(session, "ghost-behaviour", """
ctx.state.at = ctx.place and ctx.place.key or "none"
ctx.state.map_size = #ctx.places
""", entity)

    run_tick(session)

    script = session.query(Script).filter_by(name="ghost-behaviour").one()
    assert script.state["at"] == "none"
    assert script.state["map_size"] == 3


def test_world_without_places_is_the_legacy_citizen(session):
    entity = _entity(session)
    make_script(session, "legacy-behaviour", """
ctx.state.at = ctx.place and ctx.place.key or "none"
ctx.state.map_size = #ctx.places
""", entity)

    run_tick(session)  # no places at all: no error, clean nils

    script = session.query(Script).filter_by(name="legacy-behaviour").one()
    assert script.state["at"] == "none"
    assert script.state["map_size"] == 0


def test_world_lib_place_vocabulary(session):
    from experiments.world.manifest import read_manifest
    from pathlib import Path

    lib = Path(__file__).parent.parent / "experiments" / "world" / "lua" / "world_lib.lua"
    world_lib = lib.read_text()
    ctx = {
        "entity": {"id": "e1", "name": "E", "entity_type": "individual"},
        "places": [
            {"key": "HEARTH", "name": "Hearth clearing", "kind": "HEARTH",
             "region_id": "valley", "description": ""},
            {"key": "RIVER", "name": "The river", "kind": "RIVER",
             "region_id": "valley", "description": ""},
        ],
        "place": {"key": "RIVER", "name": "The river", "kind": "RIVER",
                  "region_id": "valley", "description": ""},
        "state": {},
    }
    result = engine.run("""
local w = world
ctx.state.n = #w.places()
ctx.state.here = w.place() and w.place().key
ctx.state.river = w.place("RIVER") and w.place("RIVER").name
ctx.state.missing = (w.place("MOON") == nil) and "absent" or "present"
""", ctx, libraries={"world": world_lib})
    assert result.error is None, result.error
    assert result.state_updates == {
        "n": 2, "here": "RIVER", "river": "The river", "missing": "absent",
    }
    # the manifest pins this file; a stale pin fails loudly elsewhere --
    # here we only assert the shipped manifest still knows the file
    assert "world_lib.lua" in read_manifest()["lua"]


# --- spawns ----------------------------------------------------------------


def test_spawn_template_places_the_creature(session):
    _map(session)
    spawns.set_script_source(session, "wolf", "state.awake = true")
    spawns.set_rules(session, {
        "from_round": 1, "every_rounds": 1, "up_to": 1, "max_alive": 2,
        "name_prefix": "Wolf",
        "template": {"entity_type": "individual",
                     "stats": {"ATTACK": 4, "DEFENSE": 1, "HITS": 12},
                     "holdings": {"MEAT": 1},
                     "script_setting": "wolf.pack_source",
                     "place": "RIVER"},
    })

    born = spawns.apply_on_round(session, 1)

    assert len(born) == 1
    assert born[0]["place"] == "RIVER"
    from econengine.models import Entity
    wolf = session.get(Entity, born[0]["entity_id"])
    assert wolf.place.key == "RIVER"


def test_spawn_without_place_stays_unplaced(session):
    spawns.set_script_source(session, "wolf", "state.awake = true")
    spawns.set_rules(session, {
        "from_round": 1, "every_rounds": 1, "up_to": 1, "max_alive": 2,
        "name_prefix": "Wolf",
        "template": {"entity_type": "individual",
                     "stats": {"ATTACK": 4, "DEFENSE": 1, "HITS": 12},
                     "holdings": {"MEAT": 1},
                     "script_setting": "wolf.pack_source"},
    })

    born = spawns.apply_on_round(session, 1)

    assert born[0]["place"] is None
    from econengine.models import Entity
    wolf = session.get(Entity, born[0]["entity_id"])
    assert wolf.location_place_id is None
