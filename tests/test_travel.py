"""Travel — the road as work (docs/spatial.md S3).

Edges are weighted, mode-tagged roads between places; the engine routes
(Dijkstra over mode-allowed edges, deterministic) and travel runs as one
Process per hop against the pack's TRAVEL_{mode} recipe — so every
input, requirement, and gate that applies to work applies to the road.
The itinerary is auditable (TravelRoute row, frozen hops), arrivals move
the entity through the single writer, stranding is a real state, and the
whole layer is opt-in: a world with no edges refuses every journey
readably and otherwise runs exactly as before.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import describe, edges, places, production, services, travel
from econengine import markets as markets_mod
from econengine.lua_engine import Intent, LuaEngine
from econengine.models import (
    Base, EntityType, ProcessStatus, Script, ScriptType, TravelRoute,
    TravelRouteStatus,
)
from econengine.scripting import build_queries, resolve_intent
from econengine.tick import run_tick

_PACK = "demo-world"
lua_engine = LuaEngine()


@pytest.fixture
def session():
    engine_db = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine_db)
    with Session(engine_db) as s:
        yield s


def _map(session):
    """A trail map: hearth --1t-- thicket --2t-- river, plus a ford the
    river crossing needs a raft for."""
    for key, kind in (("HEARTH", "HEARTH"), ("THICKET", "THICKET"),
                      ("RIVER", "RIVER"), ("POST", "POST")):
        places.create_place(session, key, kind=kind, name=key.title(),
                            pack_id=_PACK)
    edges.create_edge(session, "HEARTH", "THICKET", "walk", 1, pack_id=_PACK)
    edges.create_edge(session, "THICKET", "RIVER", "walk", 2, pack_id=_PACK)
    edges.create_edge(session, "THICKET", "RIVER", "raft", 1, pack_id=_PACK)
    edges.create_edge(session, "RIVER", "POST", "raft", 2, pack_id=_PACK)


def _walk_recipe(session, **kwargs):
    kwargs.setdefault("duration_ticks", 1)
    return production.create_recipe(
        session, "TRAVEL_WALK", inputs={}, outputs={}, **kwargs)


def _raft_recipe(session, **kwargs):
    """The free raft template: crossing by RAFT costs nothing to start
    (the vehicle-as-requirement case is its own test below)."""
    kwargs.setdefault("duration_ticks", 1)
    return production.create_recipe(
        session, "TRAVEL_RAFT", inputs={}, outputs={}, **kwargs)


def _entity(session, name="Wanderer", at="HEARTH"):
    entity = services.create_entity(session, name, EntityType.INDIVIDUAL)
    if at is not None:
        places.move_entity(session, entity, at)
    return entity


def _travel(session, entity, to, modes=None):
    params = {"to": to}
    if modes is not None:
        params["modes"] = modes
    return resolve_intent(session, Intent(
        entity_id=entity.id, intent_type="travel", params=params,
        resource_ids=[],
    ))


def make_script(session, name, source, entity):
    script = Script(name=name, source=source,
                    script_type=ScriptType.BEHAVIOUR, entity_id=entity.id,
                    timeout_ms=200)
    session.add(script)
    session.flush()
    return script


# --- edges: content rows ----------------------------------------------------


def test_edge_install_reads_back_uppercased(session):
    _map(session)
    edge = edges.create_edge(session, "POST", "HEARTH", "Ride", 4,
                             region_id="pass", pack_id="other")
    assert edge.mode == "RIDE"
    assert edge.bidirectional is True  # the default
    assert edge.region_id == "pass"
    assert len(edges.list_edges(session)) == 5


def test_edge_wiring_refuses_unknown_places_and_bad_cost(session):
    _map(session)
    with pytest.raises(ValueError, match="unknown from-place 'MOON'"):
        edges.create_edge(session, "MOON", "HEARTH", "walk", 1)
    with pytest.raises(ValueError, match="cost_ticks must be >= 1"):
        edges.create_edge(session, "HEARTH", "POST", "walk", 0)


def test_duplicate_road_is_a_pack_claim_conflict(session):
    _map(session)
    with pytest.raises(ValueError, match="already installed by demo-world"):
        edges.create_edge(session, "HEARTH", "THICKET", "walk", 3,
                          pack_id="other")
    # a different mode or direction is a different road
    edges.create_edge(session, "HEARTH", "THICKET", "ride", 3, pack_id="other")
    edges.create_edge(session, "THICKET", "RIVER", "walk", 2,
                      bidirectional=False, pack_id="other")


# --- routing: Dijkstra over mode-allowed edges ------------------------------


def test_route_picks_the_cheapest_road_deterministically(session):
    _map(session)
    # a longer cheap road beats a short expensive one
    edges.create_edge(session, "HEARTH", "RIVER", "walk", 5, pack_id=_PACK)
    origin = places.get_place(session, "HEARTH")
    dest = places.get_place(session, "RIVER")
    hops = edges.route(session, origin, dest)
    assert [(h["from"], h["to"], h["mode"], h["cost_ticks"]) for h in hops] == [
        ("HEARTH", "THICKET", "WALK", 1),
        ("THICKET", "RIVER", "RAFT", 1),
    ]
    assert edges.distance_ticks(session, origin, dest) == 2
    # same map, same road — replayable
    assert [h["edge_id"] for h in edges.route(session, origin, dest)] == \
        [h["edge_id"] for h in edges.route(session, origin, dest)]


def test_one_way_edges_only_walk_forward(session):
    places.create_place(session, "A", kind="WOOD", pack_id=_PACK)
    places.create_place(session, "B", kind="WOOD", pack_id=_PACK)
    edges.create_edge(session, "A", "B", "walk", 2, bidirectional=False,
                      pack_id=_PACK)
    a, b = places.get_place(session, "A"), places.get_place(session, "B")
    assert edges.route(session, a, b) is not None
    assert edges.route(session, b, a) is None  # the reverse is another road


def test_mode_allow_list_scopes_the_roads_used(session):
    _map(session)
    origin = places.get_place(session, "HEARTH")
    dest = places.get_place(session, "POST")
    # only RAFT edges: HEARTH has none -- unreachable
    assert edges.route(session, origin, dest, modes="RAFT") is None
    # walking to the POST needs the raft crossing
    hops = edges.route(session, origin, dest, modes="WALK,RAFT")
    assert [h["mode"] for h in hops] == ["WALK", "RAFT", "RAFT"]
    assert edges.normalize_modes("walk, raft") == {"WALK", "RAFT"}
    assert edges.normalize_modes(None) is None


# --- the travel intent -------------------------------------------------------


def test_journey_chains_hops_and_arrives(session):
    _map(session)
    _walk_recipe(session)
    _raft_recipe(session)
    entity = _entity(session)

    outcome = _travel(session, entity, "POST")  # 3 hops: walk, raft, raft
    assert outcome["status"] == "applied"
    assert outcome["hops"] == 3
    assert outcome["total_ticks"] == 4
    assert outcome["to"] == "POST"
    assert outcome["params"]["hops"] == 3  # the itinerary rides the params

    seen = []
    for _ in range(6):
        tick = run_tick(session)
        seen.extend(e for e in tick.events if e["type"].startswith("travel"))
    kinds = [(e["type"], e.get("place")) for e in seen]
    assert kinds == [
        ("travel_arrived", "THICKET"), ("travel_departed", None),
        ("travel_arrived", "RIVER"), ("travel_departed", None),
        ("travel_arrived", "POST"),
    ]
    route = session.query(TravelRoute).one()
    assert route.status is TravelRouteStatus.ARRIVED
    assert route.next_index == 3
    assert entity.place.key == "POST"


def test_hops_are_processes_but_not_production(session):
    _map(session)
    _walk_recipe(session)
    _raft_recipe(session)
    entity = _entity(session)
    _travel(session, entity, "RIVER")
    events = []
    for _ in range(4):
        events.extend(run_tick(session).events)
    assert not [e for e in events if e["type"] == "process_completed"]
    travel_events = [e for e in events if e["type"].startswith("travel")]
    assert travel_events  # arrival is the record instead


def test_travel_recipe_may_yield_carried_goods(session):
    _map(session)
    # trail foraging: the road's template declares outputs like any recipe
    production.create_recipe(
        session, "TRAVEL_WALK", inputs={}, outputs={"BERRIES": Decimal("0.5")},
        duration_ticks=1)
    entity = _entity(session)
    _travel(session, entity, "THICKET")
    run_tick(session)
    tick = run_tick(session)
    arrived = next(e for e in tick.events if e["type"] == "travel_arrived")
    assert arrived["carried"] == {"BERRIES": "0.5000"}
    holding = markets_mod.get_holding(session, entity.id, "BERRIES")
    assert holding is not None and holding.quantity == Decimal("0.5")


def test_refusals_are_readable(session):
    _map(session)
    _walk_recipe(session)
    placed = _entity(session)
    unplaced = _entity(session, "Ghost", at=None)

    out = _travel(session, placed, "MOON")
    assert out["status"] == "rejected" and "unknown place 'MOON'" in out["reason"]

    out = _travel(session, unplaced, "RIVER")
    assert out["status"] == "rejected" and "not on the map" in out["reason"]

    out = _travel(session, placed, "HEARTH")
    assert out["status"] == "rejected" and "already at Hearth" in out["reason"]


def test_no_road_refusal_names_both_ends_and_modes(session):
    _map(session)
    _walk_recipe(session)
    entity = _entity(session, at="POST")
    out = _travel(session, entity, "HEARTH", modes="WALK")  # POST needs a raft
    assert out["status"] == "rejected"
    assert "no road from Post to Hearth by WALK" in out["reason"]


def test_missing_travel_recipe_is_loud(session):
    _map(session)
    entity = _entity(session)
    # walk-only roads (the raft crossing excluded): no TRAVEL_WALK installed
    out = _travel(session, entity, "RIVER", modes="WALK")
    assert out["status"] == "rejected"
    assert "recipe TRAVEL_WALK is not installed" in out["reason"]


def test_zero_duration_travel_recipe_is_refused(session):
    _map(session)
    production.create_recipe(session, "TRAVEL_WALK", inputs={}, outputs={},
                             duration_ticks=0)
    entity = _entity(session)
    out = _travel(session, entity, "THICKET")
    assert out["status"] == "rejected"
    assert "zero-duration template would arrive before departing" in out["reason"]


def test_one_journey_at_a_time(session):
    _map(session)
    _walk_recipe(session)
    _raft_recipe(session)
    entity = _entity(session)
    assert _travel(session, entity, "RIVER")["status"] == "applied"
    out = _travel(session, entity, "POST")
    assert out["status"] == "rejected"
    assert "already on the road to River" in out["reason"]


def test_mapless_world_refuses_travel_cleanly(session):
    # zero places, zero edges: the legacy world. The refusal names the gap.
    _walk_recipe(session)
    entity = _entity(session, at=None)
    out = _travel(session, entity, "ANYWHERE")
    assert out["status"] == "rejected"
    assert "no such destination" in out["reason"]


# --- requirements are data: the raft ----------------------------------------


def test_vehicle_is_a_requirement_not_mechanism(session):
    _map(session)
    _walk_recipe(session)
    production.create_recipe(
        session, "TRAVEL_RAFT", inputs={}, outputs={}, duration_ticks=1,
        good_requirements={"RAFT": Decimal("1")})
    entity = _entity(session, at="THICKET")

    out = _travel(session, entity, "POST")
    assert out["status"] == "rejected"
    assert "RAFT" in out["reason"]  # the ordinary shortfall reason

    markets_mod.adjust_holding(session, entity, "RAFT", Decimal("1"))
    out = _travel(session, entity, "POST")
    assert out["status"] == "applied"
    assert out["mode"] == "RAFT"


def test_per_hop_inputs_draw_each_hop(session):
    _map(session)
    # the trail costs food per hop: two hops, two draws
    production.create_recipe(
        session, "TRAVEL_WALK", inputs={"FOOD": Decimal("1")}, outputs={},
        duration_ticks=1)
    entity = _entity(session)
    markets_mod.adjust_holding(session, entity, "FOOD", Decimal("1"))

    out = _travel(session, entity, "RIVER", modes="WALK")  # 2 walk hops
    assert out["status"] == "applied"
    run_tick(session)  # hop 1 completes next tick
    tick = run_tick(session)
    arrived = next(e for e in tick.events if e["type"] == "travel_arrived")
    # hop 2 could not start: the second FOOD draw was short -- stranded
    # mid-journey is a real state, with the reason on the record
    assert arrived["place"] == "THICKET"
    stranded = next(e for e in tick.events if e["type"] == "travel_stranded")
    assert stranded["place"] == "THICKET"
    route = session.query(TravelRoute).one()
    assert route.status is TravelRouteStatus.STRANDED
    assert entity.place.key == "THICKET"


def test_cancel_mid_route_strands_at_the_last_place(session):
    _map(session)
    _walk_recipe(session)
    entity = _entity(session)
    outcome = _travel(session, entity, "RIVER", modes="WALK")
    run_tick(session)  # tick 1: hop 1 on the road
    run_tick(session)  # tick 2: arrives THICKET, hop 2 starts

    route = session.query(TravelRoute).one()
    out = resolve_intent(session, Intent(
        entity_id=entity.id, intent_type="cancel_process",
        params={"process_id": route.current_process_id},
        resource_ids=[route.current_process_id],
    ))
    assert out["status"] == "applied"

    tick = run_tick(session)  # the stranded pass reads the cancellation
    stranded = next(e for e in tick.events if e["type"] == "travel_stranded")
    assert stranded["place"] == "THICKET"
    assert "cancelled" in stranded["reason"]
    route = session.query(TravelRoute).one()
    assert route.status is TravelRouteStatus.STRANDED
    assert entity.place.key == "THICKET"  # never left the last arrival


def test_failed_per_tick_input_strands_mid_road(session):
    _map(session)
    production.create_recipe(
        session, "TRAVEL_WALK", inputs={}, outputs={}, duration_ticks=1,
        per_tick_inputs={"FOOD": Decimal("1")})
    entity = _entity(session)
    # exactly enough for hop 1's single tick -- hop 2 (2 ticks) starves
    markets_mod.adjust_holding(session, entity, "FOOD", Decimal("1"))

    assert _travel(session, entity, "RIVER", modes="WALK")["status"] == "applied"
    run_tick(session)                     # tick 1: hop 1 walks, eats
    tick = run_tick(session)              # tick 2: arrive THICKET, hop 2 starts,
    #                                   and its per-tick FOOD is already short
    assert next(e for e in tick.events if e["type"] == "travel_arrived")
    failed = next(e for e in tick.events if e["type"] == "process_failed")
    assert failed["symbol"] == "FOOD"
    tick = run_tick(session)              # tick 3: the pass reads the failure
    stranded = next(e for e in tick.events if e["type"] == "travel_stranded")
    assert "per-tick input" in stranded["reason"]
    assert entity.place.key == "THICKET"


# --- the script surface ------------------------------------------------------


def test_script_can_travel_and_see_the_road(session):
    _map(session)
    _walk_recipe(session)
    _raft_recipe(session)
    entity = _entity(session)
    make_script(session, "walker", """
ctx.state.at = ctx.entity.place
ctx.state.hops = ctx.query.distance_ticks("HEARTH", "RIVER")
local it = ctx.query.route("HEARTH", "RIVER")
ctx.state.first_mode = it and it.hops[1].mode or "none"
ctx.action.travel("RIVER")
""", entity)

    run_tick(session)   # queues and resolves the travel intent
    assert entity.place.key == "HEARTH"  # location moves on arrival only
    script = session.query(Script).filter_by(name="walker").one()
    assert script.state["at"] == "HEARTH"
    assert script.state["hops"] == 2  # walk, then the cheap raft crossing
    assert script.state["first_mode"] == "WALK"

    run_tick(session)
    run_tick(session)
    assert entity.place.key == "RIVER"
    script = session.query(Script).filter_by(name="walker").one()
    assert script.state["at"] == "RIVER"


def test_ctx_marks_running_hops_as_travel(session):
    _map(session)
    _walk_recipe(session)
    entity = _entity(session)
    make_script(session, "watcher", """
ctx.state.n = #ctx.processes
ctx.state.recipes = {}
for i, p in ipairs(ctx.processes) do
  ctx.state.recipes[i] = p.recipe .. ":" .. tostring(p.is_travel)
end
""", entity)
    _travel(session, entity, "THICKET")
    run_tick(session)
    script = session.query(Script).filter_by(name="watcher").one()
    assert script.state["n"] == 1
    assert script.state["recipes"] == ["TRAVEL_WALK:true"]


def test_world_lib_route_vocabulary(session):
    from experiments.world.manifest import read_manifest

    _map(session)
    _walk_recipe(session)
    world_lib = (Path(__file__).parent.parent / "experiments" / "world"
                 / "lua" / "world_lib.lua").read_text()
    ctx = {
        "entity": {"id": "e1", "name": "E", "entity_type": "individual"},
        "places": [], "state": {},
        "queries": build_queries(session),
    }
    result = lua_engine.run("""
local w = world
ctx.state.far = w.distance_ticks("HEARTH", "POST")
ctx.state.none = (w.distance_ticks("HEARTH", "POST", "SWIM") == nil) and "nil" or "some"
local it = w.route("HEARTH", "POST")
ctx.state.modes = it and (it.hops[1].mode .. ">" .. it.hops[2].mode)
ctx.state.total = it and it.total_ticks
ctx.state.missing = (w.route("HEARTH", "MOON") == nil) and "nil" or "table"
""", ctx, libraries={"world": world_lib})
    assert result.error is None, result.error
    assert result.state_updates == {
        "far": 4, "none": "nil", "modes": "WALK>RAFT", "total": 4,
        "missing": "nil",
    }
    assert "world_lib.lua" in read_manifest()["lua"]


# --- the readable registry ----------------------------------------------------


def test_describe_renders_the_road(session):
    applied = describe.render_event({
        "type": "travel", "status": "applied",
        "params": {"to": "RIVER", "hops": 2, "total_ticks": 3},
    })
    assert applied == "set out for RIVER (2 hop(s), 3 ticks)"

    departed = describe.render_event({
        "type": "travel_departed", "from": "HEARTH", "to": "THICKET",
        "mode": "WALK", "cost_ticks": 1,
    })
    assert departed == "set out from HEARTH for THICKET by walk — 1 ticks on the road"

    arrived = describe.render_event({
        "type": "travel_arrived", "place": "THICKET", "remaining_hops": 1,
    })
    assert arrived == "arrived at THICKET — 1 hop(s) to go"

    home = describe.render_event({
        "type": "travel_arrived", "place": "RIVER", "remaining_hops": 0,
        "carried": {"BERRIES": "0.5000"},
    })
    assert home == "arrived at RIVER (carried +0.5 BERRIES)"

    stranded = describe.render_event({
        "type": "travel_stranded", "place": "THICKET",
        "reason": "journey cancelled mid-road",
    })
    assert stranded == "stranded at THICKET — journey cancelled mid-road"

    refused = describe.render_event({
        "type": "travel", "status": "rejected",
        "params": {"to": "MOON"}, "reason": "unknown place 'MOON'",
    })
    assert "set out for MOON" in refused and "refused" in refused
