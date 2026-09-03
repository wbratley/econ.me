"""Edges — the world's roads as content rows (docs/spatial.md S3).

The topology half of the spatial layer. An edge is one road between two
places, tagged with a travel mode and priced in ticks; distance in this
engine is always ticks-through-topology, never meters (Fork 1). The
engine ROUTES (Dijkstra over mode-allowed edges — the shortest road,
deterministically chosen); scripts choose destinations and mode
allow-lists. Per-entity pathfinding policy is explicitly unbuilt
(roadmap §7): a world that wants dumb agents to get lost authors a
validator that refuses good routes.

Edges are content like places: installed at genesis, claimed by a pack,
counted and pinned by the manifest. A world with no edges has no
topology — every journey is refused with a readable reason, and
everything else runs exactly as before.
"""

import heapq

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Place, SpatialEdge
from . import places as places_mod


def create_edge(
    session: Session,
    from_place: "Place | str",
    to_place: "Place | str",
    mode: str,
    cost_ticks: int,
    bidirectional: bool = True,
    region_id: str = "",
    pack_id: str | None = None,
) -> SpatialEdge:
    """Wire one road between two places. Both endpoints must be installed
    (a pack that names an uninstalled place is a content bug, loud at
    setup), the mode is an uppercase tag, and cost is at least one tick —
    a zero-tick road would arrive before departing. The conflict rule is
    the pack-provenance one: an identical road (same endpoints, mode,
    direction) already installed is a clean ValueError, never a
    duplicate row."""
    origin = _resolve(session, from_place, "from")
    dest = _resolve(session, to_place, "to")
    mode = str(mode).upper()
    cost_ticks = int(cost_ticks)
    if cost_ticks < 1:
        raise ValueError("cost_ticks must be >= 1 -- a road takes at least one tick")
    existing = session.execute(
        select(SpatialEdge).where(
            SpatialEdge.from_place_id == origin.id,
            SpatialEdge.to_place_id == dest.id,
            SpatialEdge.mode == mode,
            SpatialEdge.bidirectional == bool(bidirectional),
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(
            f"road {origin.key}->{dest.key} by {mode} already installed by "
            f"{existing.pack_id or 'the platform'} -- a pack may not claim "
            f"another installer's road")
    edge = SpatialEdge(
        from_place_id=origin.id,
        to_place_id=dest.id,
        mode=mode,
        cost_ticks=cost_ticks,
        bidirectional=bool(bidirectional),
        region_id=region_id,
        pack_id=pack_id,
    )
    session.add(edge)
    session.flush()
    return edge


def _resolve(session: Session, place: "Place | str", which: str) -> Place:
    if isinstance(place, str):
        resolved = places_mod.get_place(session, place)
        if resolved is None:
            raise ValueError(
                f"unknown {which}-place {place.upper()!r} -- install the map "
                f"before wiring roads")
        return resolved
    return place


def list_edges(session: Session) -> list[SpatialEdge]:
    return list(session.execute(
        select(SpatialEdge).order_by(SpatialEdge.created_at, SpatialEdge.id)
    ).scalars())


def normalize_modes(modes) -> set[str] | None:
    """The mode allow-list as a set: None/empty = every mode. Accepts a
    comma-separated string (\"WALK, RAFT\" — the stringly intent param and
    the Lua convention) or any iterable of tags."""
    if modes is None:
        return None
    if isinstance(modes, str):
        parts = [p.strip() for p in modes.split(",")]
    else:
        parts = [str(p) for p in modes]
    allowed = {p.upper() for p in parts if p}
    return allowed or None


def route(
    session: Session,
    from_place: Place,
    to_place: Place,
    modes=None,
) -> list[dict] | None:
    """Dijkstra over mode-allowed edges: the cheapest itinerary from
    ``from_place`` to ``to_place``, as hops in walk order —
    ``{"from": key, "to": key, "mode": MODE, "cost_ticks": n, "edge_id": id}``.
    None when no road exists under the allow-list.

    Directionality: a bidirectional edge walks both ways; a one-way edge
    only from → to (its reverse is a different road the pack must wire).
    Deterministic: edges relax in (cost, tie-break, destination key)
    order, so the same map always yields the same road — replayable.
    """
    allowed = normalize_modes(modes)
    # adjacency built in a globally deterministic order (from/to/mode), so
    # relaxation order never depends on row ids — the same map routes the
    # same way across runs, replays included
    neighbours: dict[str, list[tuple[int, int, str, str, SpatialEdge]]] = {}
    tie = 0
    for edge in sorted(
        list_edges(session),
        key=lambda e: (e.from_place.key, e.to_place.key, e.mode),
    ):
        if allowed is not None and edge.mode not in allowed:
            continue
        if edge.bidirectional:
            walks = ((edge.from_place_id, edge.to_place_id),
                     (edge.to_place_id, edge.from_place_id))
        else:
            walks = ((edge.from_place_id, edge.to_place_id),)
        for a, b in walks:
            neighbours.setdefault(a, []).append(
                (edge.cost_ticks, tie, b, edge.mode, edge))
            tie += 1

    key_of = {p.id: p.key for p in places_mod.list_places(session)}
    dist = {from_place.id: 0}
    came_from: dict[str, tuple[str, SpatialEdge]] = {}
    heap = [(0, 0, from_place.id)]
    done: set[str] = set()
    while heap:
        cost, _, node = heapq.heappop(heap)
        if node in done:
            continue
        done.add(node)
        if node == to_place.id:
            break
        for step_cost, step_tie, nxt, mode, edge in neighbours.get(node, []):
            if nxt in done:
                continue
            new_cost = cost + step_cost
            if new_cost < dist.get(nxt, float("inf")):
                dist[nxt] = new_cost
                came_from[nxt] = (node, edge)
                heapq.heappush(heap, (new_cost, step_tie, nxt))

    if to_place.id not in done:
        return None
    hops: list[dict] = []
    node = to_place.id
    while node != from_place.id:
        prev, edge = came_from[node]
        hops.append({
            "from": key_of[prev],
            "to": key_of[node],
            "mode": edge.mode,
            "cost_ticks": edge.cost_ticks,
            "edge_id": edge.id,
        })
        node = prev
    hops.reverse()
    return hops


def distance_ticks(session: Session, from_place: Place, to_place: Place, modes=None) -> int | None:
    """Total cost of the cheapest road, or None when there is none. The
    query-side read (world.distance_ticks): how far, in ticks."""
    hops = route(session, from_place, to_place, modes)
    if hops is None:
        return None
    return sum(h["cost_ticks"] for h in hops)


def edge_facts(edge: SpatialEdge) -> dict:
    """The readable projection of one road (query results, census)."""
    return {
        "from": edge.from_place.key if edge.from_place else None,
        "to": edge.to_place.key if edge.to_place else None,
        "mode": edge.mode,
        "cost_ticks": edge.cost_ticks,
        "bidirectional": edge.bidirectional,
        "region_id": edge.region_id,
    }
