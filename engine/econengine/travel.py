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
(with remaining_hops), travel_halted when the dark road holds a
journey for want of a flame (P2: the relight window), travel_stranded
when a journey stops short (cancellation, a failed per-tick input
mid-road, a flame that never came, or a next hop whose
requirements can no longer be met — stranding is a real state, the
pack's business).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock
from . import edges as edges_mod
from . import places as places_mod
from . import production
from . import statuses
from .models import (
    Entity, Process, ProcessStatus, SpatialEdge, TravelRoute, TravelRouteStatus,
)

#: The reserved recipe namespace (roadmap S3): a recipe whose code is
#: TRAVEL_{MODE} is the travel template for that mode — its inputs and
#: requirements gate every hop on roads of that mode. Its product is
#: arrival, not goods, so it may declare no outputs (create_recipe
#: allows this for the namespace).
TRAVEL_RECIPE_PREFIX = "TRAVEL_"

#: The night gate's rules (P2): {"night_travel_requires": [condition …],
#: "relight_ticks": N}. Darkness is ambient — the clock's, not any
#: recipe's — and the register's conditions relieve it. A hop that
#: departs at night needs every named condition on the traveller; a
#: chained hop that cannot (the torch burned out mid-journey) halts the
#: route WAITING_LIGHT for the relight window, then strands. Dawn needs
#: nothing — day travel is unchanged.
TRAVEL_RULES_KEY = "travel.rules"


class _NightGateError(ValueError):
    """The dark road refused this hop: a named condition is dark. Caught
    by complete_travel to WAIT (ember window) instead of stranding."""


def get_travel_rules(session: Session) -> dict:
    from .models import WorldSetting
    row = session.get(WorldSetting, TRAVEL_RULES_KEY)
    return dict(row.value) if row is not None else {}


def set_travel_rules(session: Session, rules: dict) -> None:
    from .models import WorldSetting
    row = session.get(WorldSetting, TRAVEL_RULES_KEY)
    if row is None:
        session.add(WorldSetting(key=TRAVEL_RULES_KEY, value=rules))
    else:
        row.value = rules


def _night_gate(session: Session, entity: Entity, tick_number: int) -> bool:
    """Returns True (and marks the caller's event loud) when this hop is
    a torchlit night departure. Raises _NightGateError when night has
    come and a required condition is dark. Day, or a world without
    travel rules, passes silently — yesterday's behavior."""
    rules = get_travel_rules(session)
    required = rules.get("night_travel_requires") or []
    if not required or not clock.is_night(tick_number):
        return False
    active = statuses.active_conditions(session, entity, tick_number)
    missing = [name for name in required if name not in active]
    if missing:
        raise _NightGateError(
            f"the dark road refuses you -- night travel needs "
            f"{', '.join(required)} ({', '.join(missing)} is dark)")
    return True


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

    # hop 1 pays the night gate before the itinerary exists -- a refused
    # departure must not leave a route row behind
    _night_gate(session, entity, production.next_tick_number(session))

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
    if departed.get("loud"):
        # a torchlit night departure is loud (P2): the marker rides the
        # applied event too, so the register (and every listening pack)
        # reads it from the tick's record either way
        facts["loud"] = True
    return route_row, process, facts


def _hop_ends(edge: SpatialEdge, origin_id: str | None):
    """A road walked either way: the near end is where the traveller
    stands, the far end is the other side. Bidirectional edges carry
    no inherent direction — the journey's does."""
    if origin_id == edge.from_place_id:
        return edge.from_place, edge.to_place
    if origin_id == edge.to_place_id:
        return edge.to_place, edge.from_place
    return None, None


def _start_hop(
    session: Session, route_row: TravelRoute, entity: Entity
) -> tuple[Process, dict]:
    """Start the route's next hop: an ordinary start_process against the
    mode's travel recipe (inputs drawn, requirements and gates checked —
    refusals propagate to the caller), re-timed to the edge's cost and
    marked is_travel. Emits nothing; returns the departure facts."""
    edge = session.get(SpatialEdge, route_row.hops[route_row.next_index])
    near, far = _hop_ends(edge, entity.location_place_id)
    if near is None:
        raise ValueError(
            f"no road from where you stand -- the itinerary's next hop "
            f"does not touch {places_mod.label(entity.place)}")
    recipe = _check_travel_recipe(session, edge.mode)
    # torchlit night departures are loud (P2): the gate also answers it
    lit_departure = _night_gate(session, entity, production.next_tick_number(session))
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
        "from": near.key,
        "to": far.key,
        "mode": edge.mode,
        "cost_ticks": edge.cost_ticks,
        "remaining_hops": len(route_row.hops) - route_row.next_index - 1,
    }
    if lit_departure:
        # the torch IS the light: a night departure under flame is a loud
        # fact, marked for the register (and every listening pack)
        departed["loud"] = True
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

    # The relight window (P2): a route halted by the dark road retries its
    # next hop every pass -- a struck flame or dawn resumes the journey,
    # a window elapsed strands it where it stands.
    waiting = session.execute(
        select(TravelRoute)
        .where(TravelRoute.status == TravelRouteStatus.WAITING_LIGHT)
        .order_by(TravelRoute.created_at, TravelRoute.id)
    ).scalars().all()
    relight_ticks = int(get_travel_rules(session).get("relight_ticks", 0))
    for route_row in waiting:
        entity = session.get(Entity, route_row.entity_id)
        if entity is None:
            continue
        try:
            _, departed = _start_hop(session, route_row, entity)
            route_row.status = TravelRouteStatus.ACTIVE
            route_row.waiting_since_tick = None
            events.append(departed)
        except _NightGateError:
            halted_at = route_row.waiting_since_tick or tick_number
            if tick_number - halted_at > relight_ticks:
                route_row.status = TravelRouteStatus.STRANDED
                route_row.current_process_id = None
                route_row.waiting_since_tick = None
                events.append(_stranded(
                    route_row, entity,
                    f"no flame came within {relight_ticks} ticks of the halt "
                    f"-- the dark road kept its refusal"))
        except ValueError as exc:
            route_row.status = TravelRouteStatus.STRANDED
            route_row.current_process_id = None
            route_row.waiting_since_tick = None
            events.append(_stranded(route_row, entity, str(exc)))
    if waiting:
        session.flush()
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
            near, far = _hop_ends(edge, entity.location_place_id)
            if far is None:
                # moved off the road's ends mid-hop (the single writer
                # runs elsewhere too): the itinerary no longer knows
                # where this hop ends. A real state, named.
                route_row.status = TravelRouteStatus.STRANDED
                route_row.current_process_id = None
                events.append(_stranded(
                    route_row, entity,
                    "the road moved under the traveller mid-hop"))
                continue
            places_mod.move_entity(session, entity, far)
            route_row.next_index += 1
            remaining = len(route_row.hops) - route_row.next_index
            arrived = {
                "type": "travel_arrived",
                "entity_id": entity.id,
                "route_id": route_row.id,
                "process_id": process.id,
                "place": far.key,
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
            except _NightGateError as exc:
                # the next hop wants a flame the traveller lacks: halt, hold
                # the itinerary open for the relight window (the ember may
                # still strike, dawn may come), strand only if neither does
                route_row.status = TravelRouteStatus.WAITING_LIGHT
                route_row.waiting_since_tick = tick_number
                route_row.current_process_id = None
                events.append({
                    "type": "travel_halted",
                    "entity_id": entity.id,
                    "route_id": route_row.id,
                    "place": (entity.place.key if entity.place is not None else None),
                    "reason": str(exc),
                })
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
