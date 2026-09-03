"""Travel — the road as work (docs/spatial.md S3, Fork 2).

Travel is a Process per hop, not teleport-with-cost: each hop is an
ordinary start_process against the pack's travel recipe for the edge's
mode (``TRAVEL_WALK``, ``TRAVEL_RAFT``… — packs author them with normal
inputs and requirements, so a vehicle is a requirement, not mechanism),
with two differences the road imposes: the hop's duration is the edge's
cost_ticks (not the recipe's duration), and its completion moves the
entity (arrival) instead of producing. Everything else — presence
gates, daylight, tech requirements, input draws, reservations — applies
to a hop exactly as to any work, which is the point: the road is part
of the economy, priced and gated by the same rows.

The itinerary lives on a TravelRoute row (auditable, frozen at
departure): hops are chained one at a time on arrival, the entity's
location moves only through places.move_entity, and every transition
is an event — travel_departed per hop, travel_arrived per arrival
(with remaining_hops), travel_stranded when a journey stops short
(cancellation, a failed per-tick input mid-road, or a next hop whose
requirements can no longer be met — stranding is a real state, the
pack's business).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import edges as edges_mod
from . import places as places_mod
from . import production
from .models import (
    Entity, Process, ProcessStatus, SpatialEdge, TravelRoute, TravelRouteStatus,
)

#: The reserved recipe namespace (roadmap S3): a recipe whose code is
#: TRAVEL_{MODE} is the travel template for that mode — its inputs and
#: requirements gate every hop on roads of that mode. Its product is
#: arrival, not goods, so it may declare no outputs (create_recipe
#: allows this for the namespace).
TRAVEL_RECIPE_PREFIX = "TRAVEL_"


def travel_recipe(session: Session, mode: str):
    """The pack's travel template for a mode (TRAVEL_WALK for WALK)."""
    return production.get_recipe(session, f"{TRAVEL_RECIPE_PREFIX}{str(mode).upper()}")


def _check_travel_recipe(session: Session, mode: str):
    """Loud, readable validation of a mode's template at planning time —
    a route planned over a mode whose road is uninstallable must fail
    before the first step, naming the missing recipe."""
    recipe = travel_recipe(session, mode)
    if recipe is None:
        raise ValueError(
            f"no way to travel by {mode} here -- recipe "
            f"{TRAVEL_RECIPE_PREFIX}{mode} is not installed")
    if recipe.duration_ticks == 0:
        raise ValueError(
            f"travel recipe {recipe.code} declares duration_ticks 0 -- a "
            f"hop's time comes from the road (the edge's cost_ticks), and a "
            f"zero-duration template would arrive before departing")
    return recipe


def start_route(
    session: Session,
    entity: Entity,
    to: str,
    modes=None,
) -> tuple[TravelRoute, Process, dict]:
    """Plan and begin a journey to ``to`` (place key). Raises ValueError
    with readable reasons for every refusal — the resolver turns each
    into the ordinary rejected result.

    Returns (route, first hop's Process, departure facts) — the facts
    feed the applied event so a watcher reads the whole itinerary from
    one line: from, to, mode, cost_ticks, hops, total_ticks.
    """
    if entity.status.value != "active":
        raise ValueError("entity is incapacitated")
    dest = places_mod.get_place(session, to)
    if dest is None:
        raise ValueError(
            f"unknown place {str(to).upper()!r} -- no such destination on the map")
    origin = entity.place
    if origin is None:
        raise ValueError(
            "you are not on the map -- travel needs somewhere to start from")
    if dest.id == origin.id:
        raise ValueError(f"already at {places_mod.label(dest)}")
    active = session.execute(
        select(TravelRoute).where(
            TravelRoute.entity_id == entity.id,
            TravelRoute.status == TravelRouteStatus.ACTIVE,
        )
    ).scalar_one_or_none()
    if active is not None:
        raise ValueError(
            f"already on the road to {places_mod.label(active.destination)} "
            f"-- finish or cancel that journey first")

    hops = edges_mod.route(session, origin, dest, modes)
    if hops is None:
        allowed = edges_mod.normalize_modes(modes)
        note = (f" by {', '.join(sorted(allowed))}" if allowed else "")
        raise ValueError(
            f"no road from {places_mod.label(origin)} to "
            f"{places_mod.label(dest)}{note}")
    for hop_mode in sorted({h["mode"] for h in hops}):
        _check_travel_recipe(session, hop_mode)

    allowed = edges_mod.normalize_modes(modes)
    route_row = TravelRoute(
        entity_id=entity.id,
        destination_place_id=dest.id,
        modes=sorted(allowed) if allowed else [],
        hops=[h["edge_id"] for h in hops],
        next_index=0,
        status=TravelRouteStatus.ACTIVE,
    )
    session.add(route_row)
    session.flush()

    process, departed = _start_hop(session, route_row, entity)
    total = sum(h["cost_ticks"] for h in hops)
    facts = {
        "route_id": route_row.id,
        "from": origin.key,
        "to": dest.key,
        "mode": departed["mode"],
        "cost_ticks": departed["cost_ticks"],
        "hops": len(hops),
        "total_ticks": total,
        "remaining_hops": len(hops) - 1,
        "process_id": process.id,
    }
    return route_row, process, facts


def _start_hop(
    session: Session, route_row: TravelRoute, entity: Entity
) -> tuple[Process, dict]:
    """Start the route's next hop: an ordinary start_process against the
    mode's travel recipe (inputs drawn, requirements and gates checked —
    refusals propagate to the caller), re-timed to the edge's cost and
    marked is_travel. Emits nothing; returns the departure facts."""
    edge = session.get(SpatialEdge, route_row.hops[route_row.next_index])
    recipe = _check_travel_recipe(session, edge.mode)
    process = production.start_process(session, entity, recipe.code)
    # the road, not the template, sets the hop's duration
    process.completes_tick = process.started_tick + edge.cost_ticks
    process.is_travel = True
    process.edge_id = edge.id
    process.route_id = route_row.id
    route_row.current_process_id = process.id
    session.flush()
    departed = {
        "type": "travel_departed",
        "entity_id": entity.id,
        "route_id": route_row.id,
        "process_id": process.id,
        "from": edge.from_place.key,
        "to": edge.to_place.key,
        "mode": edge.mode,
        "cost_ticks": edge.cost_ticks,
        "remaining_hops": len(route_row.hops) - route_row.next_index - 1,
    }
    return process, departed


def complete_travel(session: Session, tick_number: int) -> list[dict]:
    """The arrival pass, right after production's completion pass: move
    travellers to their hops' far ends, chain the next hop, and record
    every transition as an event. Deterministic by route creation order.

    A hop's completion moves the entity through places.move_entity (the
    single writer) and carries anything the template's outputs produced
    as ``carried`` — production statistics skip travel hops entirely
    (no process_completed event; travel_arrived is the record), but the
    goods-conservation audit stays whole: what a road yields is visible.
    """
    active_routes = session.execute(
        select(TravelRoute)
        .where(TravelRoute.status == TravelRouteStatus.ACTIVE)
        .order_by(TravelRoute.created_at, TravelRoute.id)
    ).scalars().all()
    events: list[dict] = []
    for route_row in active_routes:
        process = (
            session.get(Process, route_row.current_process_id)
            if route_row.current_process_id else None
        )
        if process is None or process.status == ProcessStatus.RUNNING:
            continue  # in flight — nothing due this pass
        entity = process.entity
        if process.status == ProcessStatus.COMPLETED:
            edge = session.get(SpatialEdge, process.edge_id)
            places_mod.move_entity(session, entity, edge.to_place)
            route_row.next_index += 1
            remaining = len(route_row.hops) - route_row.next_index
            arrived = {
                "type": "travel_arrived",
                "entity_id": entity.id,
                "route_id": route_row.id,
                "process_id": process.id,
                "place": edge.to_place.key,
                "remaining_hops": remaining,
            }
            carried = production.credited_outputs(process)
            if carried:
                arrived["carried"] = carried
            events.append(arrived)
            if remaining == 0:
                route_row.status = TravelRouteStatus.ARRIVED
                route_row.current_process_id = None
                continue
            try:
                _, departed = _start_hop(session, route_row, entity)
                events.append(departed)
            except ValueError as exc:
                # the itinerary stopped short: requirements, inputs, or
                # capability no longer meet the next hop. A real state.
                route_row.status = TravelRouteStatus.STRANDED
                route_row.current_process_id = None
                events.append(_stranded(route_row, entity, str(exc)))
        else:
            # CANCELLED by the traveller, or FAILED for want of a
            # per-tick input mid-road: the entity never left its last
            # arrival — stranded where it stands.
            reason = (
                "journey cancelled mid-road"
                if process.status == ProcessStatus.CANCELLED
                else "mid-road per-tick input short -- the hop failed"
            )
            route_row.status = TravelRouteStatus.STRANDED
            route_row.current_process_id = None
            events.append(_stranded(route_row, entity, reason))
    if active_routes:
        session.flush()
    return events


def _stranded(route_row: TravelRoute, entity: Entity, reason: str) -> dict:
    where = entity.place
    return {
        "type": "travel_stranded",
        "entity_id": entity.id,
        "route_id": route_row.id,
        "place": where.key if where is not None else None,
        "to": route_row.destination.key if route_row.destination else None,
        "reason": reason,
    }
