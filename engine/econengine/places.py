"""Places — the world's map as content rows (docs/spatial.md S1).

The engine's spatial vocabulary grows by one word: a Place is an opaque
ref with a name, a kind tag, and a region id — no coordinate anywhere
(design.md §4.5's join-key doctrine, extended the way the roadmap
Fork 1 chose). Packs install the map at genesis exactly like threats;
the manifest counts and pins it like any other content.

Who stands where is an entity fact: ``entities.location_place_id``,
nullable. NULL = unplaced, and unplaced is the legacy citizen — a
world that never authors places runs identically to today (Fork 6).
The single writer is :func:`move_entity` (genesis, scenario setup,
tests) until travel processes arrive in S3 to write it as arrivals.

S1 ships recording and reading only: presence gates are S2, topology
and travel S3. Nothing existing consults a place, so the whole layer
is dormant until a pack draws a map.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Entity, Place


def create_place(
    session: Session,
    key: str,
    kind: str,
    name: str = "",
    region_id: str = "",
    description: str = "",
    extent_ref: str | None = None,
    pack_id: str | None = None,
) -> Place:
    """Install one place. The conflict rule is the pack-provenance one:
    a second installer claiming an existing key is a clean ValueError,
    never an IntegrityError from the constraint."""
    key = str(key).upper()
    existing = get_place(session, key)
    if existing is not None:
        raise ValueError(
            f"place {key!r} already installed by "
            f"{existing.pack_id or 'the platform'} -- a pack may not claim "
            f"another installer's key")
    place = Place(
        key=key,
        kind=str(kind).upper(),
        name=name,
        region_id=region_id,
        description=description,
        extent_ref=extent_ref,
        pack_id=pack_id,
    )
    session.add(place)
    session.flush()
    return place


def label(place: "Place") -> str:
    """How a place reads in reasons, refusals, and catalog lines: the
    authored name when the pack wrote one, else the key. (S2 presence
    gates refuse with this, so a place must always read as something.)"""
    return place.name or place.key


def get_place(session: Session, key: str) -> Place | None:
    return session.scalar(
        select(Place).where(Place.key == str(key).upper()))


def list_places(session: Session) -> list[Place]:
    return list(session.execute(
        select(Place).order_by(Place.key)).scalars())


def place_facts(place: Place | None) -> dict | None:
    """The readable projection every observation surface shares (ctx,
    MCP entity_state): what a script or player may know about a spot."""
    if place is None:
        return None
    return {
        "key": place.key,
        "name": place.name,
        "kind": place.kind,
        "region_id": place.region_id,
        "description": place.description,
    }


def move_entity(session: Session, entity: Entity, place: Place | str | None) -> Place | None:
    """Set (or clear, with None) an entity's location.

    The single writer of ``location_place_id`` in S1 — genesis, scenario
    setup, tests. A string key resolves via the map; an unknown key is a
    ValueError (a world that names a place that is not installed is a
    content bug, loud at setup, not a silent nil at tick time). Travel
    (S3) writes the same column on arrival; nothing else ever does —
    location is engine-recorded fact, not scribbleable script state.
    """
    resolved: Place | None
    if place is None:
        resolved = None
    elif isinstance(place, str):
        resolved = get_place(session, place)
        if resolved is None:
            raise ValueError(f"unknown place {place.upper()!r} -- install it before standing on it")
    else:
        resolved = place
    # Write the column AND the relationship: a gate that already lazy-
    # loaded ``entity.place`` (an earlier refusal) must not keep seeing
    # the old view after the column moves — SQLAlchemy does not expire
    # loaded relationships on direct column writes.
    entity.location_place_id = resolved.id if resolved is not None else None
    entity.place = resolved
    session.flush()
    return resolved
