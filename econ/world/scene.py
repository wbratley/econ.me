"""Read-only public scene projection for the living spectator (W1).

The cut is deliberately physical and generic: an observer can see that an
actor is working or travelling, not its recipe, holdings, owner, prompt, or
script. Parcel/facility/deposit objects come from public engine rows; terrain
never manufactures them.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from econengine import clock as clock_mod
from econengine import witness
from econengine.models import (
    Deposit, Entity, EntityStatus, EntityType, Facility, Parcel, Place, Process,
    ProcessStatus, SpatialEdge, Tick, User,
)
from .presence import runtime
from .tile2d import (
    GEOMETRY_ID, Tile2DConfig, asset_manifest, geometry_descriptor,
    load_config, place_positions, ring_position,
)


def _latest_tick(session: Session) -> int:
    value = session.execute(select(func.max(Tick.number))).scalar_one_or_none()
    return int(value) if value is not None else 0


def _clock(session: Session, tick: int) -> dict | None:
    if tick < 1:
        return None
    return clock_mod.clock_facts(tick, clock_mod.season_days_of(session))


def _speech(session: Session, names: dict[str, str], last_ticks: int = 12) -> list[dict]:
    """Recent applied speech, through the same public filter as SSE/witness."""
    ticks = session.execute(
        select(Tick).order_by(Tick.number.desc()).limit(last_ticks)
    ).scalars().all()
    rows: list[dict] = []
    for tick in sorted(ticks, key=lambda row: row.number):
        for event in witness.observable_events(tick.events or []):
            if event.get("type") != "say":
                continue
            text = (event.get("params") or {}).get("text")
            entity_id = event.get("entity_id")
            if not isinstance(text, str) or entity_id not in names:
                continue
            rows.append({
                "tick": tick.number,
                "entity_id": entity_id,
                "name": names[entity_id],
                "text": text,
            })
    return rows[-50:]


def _running_processes(session: Session) -> dict[str, list[Process]]:
    processes = session.execute(
        select(Process)
        .where(Process.status == ProcessStatus.RUNNING)
        .order_by(Process.created_at, Process.id)
    ).scalars().all()
    grouped: dict[str, list[Process]] = defaultdict(list)
    for process in processes:
        grouped[process.entity_id].append(process)
    return grouped


def _objects(
    session: Session, places_by_id: dict[str, Place],
    anchors: dict[str, dict[str, float]], config: Tile2DConfig,
) -> list[dict]:
    parcels = session.execute(
        select(Parcel)
        .where(Parcel.place_id.isnot(None))
        .options(
            selectinload(Parcel.facilities),
            selectinload(Parcel.deposits),
        )
        .order_by(Parcel.created_at, Parcel.id)
    ).scalars().all()
    grouped: dict[str, list[Parcel]] = defaultdict(list)
    for parcel in parcels:
        if parcel.place_id in places_by_id:
            grouped[places_by_id[parcel.place_id].key].append(parcel)

    objects: list[dict] = []
    for place_key in sorted(grouped):
        rows = sorted(grouped[place_key], key=lambda parcel: parcel.id)
        anchor = anchors[place_key]
        for index, parcel in enumerate(rows):
            parcel_position = ring_position(
                anchor, config, f"parcels:{place_key}", index, len(rows), 4.0)
            objects.append({
                "id": parcel.id,
                "kind": "parcel",
                "appearance": parcel.parcel_type.lower(),
                "name": parcel.name or parcel.parcel_type,
                "place": place_key,
                "position": parcel_position,
                "parcel_id": parcel.id,
            })
            components: list[Facility | Deposit] = [
                *sorted(parcel.facilities, key=lambda row: row.id),
                *sorted(parcel.deposits, key=lambda row: row.id),
            ]
            for component_index, component in enumerate(components):
                position = ring_position(
                    parcel_position, config, f"components:{parcel.id}",
                    component_index, len(components), 1.25)
                common = {
                    "id": component.id,
                    "place": place_key,
                    "position": position,
                    "parcel_id": parcel.id,
                }
                if isinstance(component, Facility):
                    objects.append({
                        **common,
                        "kind": "facility",
                        "appearance": component.facility_type.lower(),
                        "name": component.facility_type.replace("_", " ").title(),
                        "lit": component.fuel > 0,
                    })
                else:
                    objects.append({
                        **common,
                        "kind": "deposit",
                        "appearance": "deposit",
                        "name": component.symbol.replace("_", " ").title(),
                        "symbol": component.symbol,
                        "depleted": component.quantity <= 0,
                    })
    objects.sort(key=lambda obj: (obj["kind"], obj["id"]))
    return objects


def _actors(
    entities: list[Entity], places_by_id: dict[str, Place],
    anchors: dict[str, dict[str, float]], config: Tile2DConfig,
    processes: dict[str, list[Process]], edges: dict[str, SpatialEdge], tick: int,
) -> tuple[list[dict], int]:
    grouped: dict[str, list[Entity]] = defaultdict(list)
    unplaced = 0
    for entity in entities:
        if entity.location_place_id not in places_by_id:
            unplaced += 1
            continue
        grouped[places_by_id[entity.location_place_id].key].append(entity)

    actors: list[dict] = []
    for place_key in sorted(grouped):
        rows = sorted(grouped[place_key], key=lambda entity: entity.id)
        for index, entity in enumerate(rows):
            position = ring_position(
                anchors[place_key], config, f"actors:{place_key}",
                index, len(rows), 2.25)
            action = "idle"
            action_progress: float | None = None
            if entity.status.value == "incapacitated":
                action = "incapacitated"
            else:
                own_processes = processes.get(entity.id, [])
                travel = next((row for row in own_processes if row.is_travel), None)
                if travel is not None:
                    action = "travel"
                    edge = edges.get(travel.edge_id or "")
                    if edge is not None:
                        origin = places_by_id.get(edge.from_place_id)
                        destination = places_by_id.get(edge.to_place_id)
                        if entity.location_place_id == edge.to_place_id and edge.bidirectional:
                            origin, destination = destination, origin
                        elif entity.location_place_id != edge.from_place_id:
                            origin = None
                        if origin is not None and destination is not None:
                            start = anchors[origin.key]
                            end = anchors[destination.key]
                            duration = max(1, travel.completes_tick - travel.started_tick)
                            progress = max(0.0, min(1.0,
                                (tick - travel.started_tick) / duration))
                            action_progress = round(progress, 4)
                            position = {
                                "x": round(start["x"] + (end["x"] - start["x"]) * progress, 3),
                                "y": round(start["y"] + (end["y"] - start["y"]) * progress, 3),
                            }
                elif own_processes:
                    action = "work"
            actors.append({
                "id": entity.id,
                "name": entity.name,
                "entity_type": entity.entity_type.value,
                "status": entity.status.value,
                "place": place_key,
                "appearance": entity.entity_type.value,
                "action": action,
                "action_progress": action_progress,
                "position": position,
            })
    actors.sort(key=lambda actor: (actor["name"], actor["id"]))
    return actors, unplaced


def project_scene(session: Session, config: Tile2DConfig | None = None) -> dict:
    config = config or load_config(session)
    tick = _latest_tick(session)
    places = list(session.execute(select(Place).order_by(Place.key)).scalars())
    places_by_id = {place.id: place for place in places}
    anchors = place_positions(places, config)
    edge_rows = list(session.execute(
        select(SpatialEdge).order_by(SpatialEdge.created_at, SpatialEdge.id)
    ).scalars())
    edges = {edge.id: edge for edge in edge_rows}
    entities = list(session.execute(
        select(Entity).order_by(Entity.name, Entity.id)
    ).scalars())
    processes = _running_processes(session)

    actors, unplaced = _actors(
        entities, places_by_id, anchors, config, processes, edges, tick)
    # W2 presence is a world-layer overlay, never an engine location: a
    # possessed actor keeps its engine place/process state but renders at
    # its server-owned local coordinate. Engine conflict reconciliation
    # happens on the live channel before scene deltas are emitted.
    presence = runtime.snapshot()
    for actor in actors:
        overlay = presence.get(actor["id"])
        if overlay is not None:
            actor.update({
                "position": overlay["position"],
                "velocity": overlay["velocity"],
                # Engine work/travel/incapacity outranks cosmetic walking.
                "action": overlay["action"] if actor["action"] == "idle" else actor["action"],
                "control": overlay["control"],
            })
    roads = []
    for edge in edge_rows:
        origin = places_by_id.get(edge.from_place_id)
        destination = places_by_id.get(edge.to_place_id)
        if origin is None or destination is None:
            continue
        roads.append({
            "id": edge.id,
            "from": origin.key,
            "to": destination.key,
            "mode": edge.mode,
            "cost_ticks": edge.cost_ticks,
            "bidirectional": bool(edge.bidirectional),
            "path": [anchors[origin.key], anchors[destination.key]],
        })

    core = {
        "geometry": GEOMETRY_ID,
        "geometry_revision": config.cache_key,
        "tick": tick,
        "clock": _clock(session, tick),
        "world": geometry_descriptor(config)["world"],
        "places": [{
            "key": place.key,
            "name": place.name or place.key,
            "kind": place.kind,
            "region_id": place.region_id,
            "description": place.description,
            "position": anchors[place.key],
        } for place in places],
        "roads": roads,
        "actors": actors,
        "objects": _objects(session, places_by_id, anchors, config),
        "unplaced_actor_count": unplaced,
        "speech": _speech(session, {entity.id: entity.name for entity in entities}),
    }
    encoded = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    revision = f"{tick}:{hashlib.sha256(encoded.encode()).hexdigest()[:16]}"
    return {"changed": True, "revision": revision, **core}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def scene_delta(previous: dict, current: dict) -> dict:
    """Field-level scene delta for a connected spectator.

    Collections are replaced only when their canonical content changed. This
    keeps the first protocol easy for independent clients while avoiding a
    per-tick resend of static places, roads, and objects.
    """
    if (previous["geometry_revision"] != current["geometry_revision"]
            or previous["world"] != current["world"]):
        return {"type": "scene_snapshot", "scene": current}
    delta = {
        "type": "scene_delta",
        "base_revision": previous["revision"],
        "revision": current["revision"],
        "tick": current["tick"],
        "clock": current["clock"],
        "unplaced_actor_count": current["unplaced_actor_count"],
    }
    for key in ("places", "roads", "actors", "objects", "speech"):
        if _canonical(previous[key]) != _canonical(current[key]):
            delta[key] = current[key]
    return delta


def scene_response(
    session: Session, since: str | None = None,
) -> dict:
    scene = project_scene(session)
    if since is not None and since == scene["revision"]:
        return {
            "changed": False,
            "revision": scene["revision"],
            "tick": scene["tick"],
        }
    return scene


def _player_entities(session: Session, user: User) -> list[dict]:
    """Entities this account may possess in the W2 world session."""
    entities = list(session.execute(
        select(Entity)
        .where(
            Entity.owner_id == user.id,
            Entity.entity_type == EntityType.INDIVIDUAL,
            Entity.is_fixed.is_(False),
            Entity.status == EntityStatus.ACTIVE,
            Entity.location_place_id.isnot(None),
        )
        .order_by(Entity.name, Entity.id)
    ).scalars())
    places_by_id = {
        place.id: place for place in session.execute(select(Place)).scalars()
    }
    return [{
        "id": entity.id,
        "name": entity.name,
        "place": places_by_id[entity.location_place_id].key,
    } for entity in entities if entity.location_place_id in places_by_id]


def bootstrap(session: Session, user: User) -> dict:
    config = load_config(session)
    descriptor = geometry_descriptor(config)
    return {
        "protocol": "econ-world.v1",
        "session": {
            "role": "spectator",
            "user_id": user.id,
            "entity_id": None,
            "capabilities": [],
        },
        "geometry": descriptor,
        "assets": asset_manifest(),
        "player_entities": _player_entities(session, user),
        "endpoints": {
            "scene": "/world/scene",
            "chunks": "/world/chunks/{cx}/{cy}?v=" + config.cache_key,
            "events": "/rounds/events",
            "stream": "/world/stream",
        },
        "scene": project_scene(session, config),
    }
