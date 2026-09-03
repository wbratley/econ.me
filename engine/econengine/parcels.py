"""
Parcels — land ownership, facilities, and deposits.

Land is a first-class engine primitive, not a holding symbol: a Parcel is
non-fungible (an owner, a region id, a zoning tag), a Facility is a built
improvement standing on one, a Deposit is a natural resource dotted onto
one. The engine stores who controls which parcel and what stands on it;
continuous space (meters, meshes) belongs to the world layer, which
resolves extent_ref to geometry.

Engine invariants (design.md § parcels):
- parcel ownership moves only by the owner's intent (transfer_parcel) or by
  explicitly-declared policy — grant_parcel is the genesis/policy path, and
  land *grants* (who may claim what) are votable policy, not mechanism;
- deposits deplete only through extraction recipes' deposit_inputs
  (draw_deposit is called by start_process, nothing else) and regenerate
  only through the per-tick regen pass, toward a declared capacity;
- a parcel with running processes bound to it cannot change hands — a
  half-built smithy cannot be gifted onto (or stolen from) someone else's
  land, and completion always credits the entity that controlled the parcel
  at start.

Facility capacity is the reservation rule shared with machinery (see
production.py): each running process bound to a parcel occupies one
facility of its recipe's required type, so one SMITHY backs one smelt at a
time — build a second smithy to run two.
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Deposit, Entity, Facility, Parcel, Process, ProcessStatus, Recipe

_QUANTUM = Decimal("0.0001")


class InsufficientDepositError(ValueError):
    pass


def create_parcel(
    session: Session,
    parcel_type: str,
    name: str = "",
    region_id: str = "",
    extent_ref: str = "",
    owner: Entity | None = None,
    place: "Place | str | None" = None,
) -> Parcel:
    # ``place`` (docs/spatial.md S1): the parcel's node on the pack's map —
    # genesis data, set once at creation. A string key resolves through
    # places.get_place and must exist (a parcel standing nowhere named is a
    # content bug, loud at setup).
    from . import places as places_mod

    resolved: "Place | None" = None
    if place is not None:
        if isinstance(place, str):
            resolved = places_mod.get_place(session, place)
            if resolved is None:
                raise ValueError(
                    f"unknown place {place.upper()!r} -- install the map before siting parcels")
        else:
            resolved = place
    parcel = Parcel(
        parcel_type=str(parcel_type).upper(),
        name=name,
        region_id=region_id,
        extent_ref=extent_ref,
        owner_id=owner.id if owner is not None else None,
        place_id=resolved.id if resolved is not None else None,
    )
    session.add(parcel)
    session.flush()
    return parcel


def get_parcel(session: Session, parcel_id: str) -> Parcel | None:
    return session.get(Parcel, parcel_id)


def _bound_running_processes(session: Session, parcel_id: str) -> int:
    return session.execute(
        select(func.count(Process.id)).where(
            Process.parcel_id == parcel_id, Process.status == ProcessStatus.RUNNING
        )
    ).scalar_one()


def transfer_parcel(session: Session, parcel_id: str, from_entity_id: str, to_entity: Entity) -> Parcel:
    """Ownership by owner's intent: from_entity_id must be the current owner."""
    parcel = session.get(Parcel, parcel_id)
    if parcel is None:
        raise ValueError("unknown parcel")
    if parcel.owner_id != from_entity_id:
        raise ValueError("entity does not own parcel")
    return _change_owner(session, parcel, to_entity)


def grant_parcel(session: Session, parcel: Parcel, to_entity: Entity | None) -> Parcel:
    """Ownership by declared policy (genesis grants, admin stand-in for land
    votes); None revokes to unclaimed."""
    return _change_owner(session, parcel, to_entity)


def _change_owner(session: Session, parcel: Parcel, to_entity: Entity | None) -> Parcel:
    if _bound_running_processes(session, parcel.id):
        raise ValueError("parcel has running processes bound to it")
    parcel.owner_id = to_entity.id if to_entity is not None else None
    session.flush()
    return parcel


# ---------------------------------------------------------------------------
# Facilities
# ---------------------------------------------------------------------------

def add_facility(
    session: Session, parcel: Parcel, facility_type: str, built_tick: int | None = None
) -> Facility:
    """Genesis placement (built_tick None) or construction-recipe completion."""
    facility = Facility(
        parcel_id=parcel.id, facility_type=str(facility_type).upper(), built_tick=built_tick
    )
    session.add(facility)
    session.flush()
    return facility


def facility_count(session: Session, parcel_id: str, facility_type: str) -> int:
    return session.execute(
        select(func.count(Facility.id)).where(
            Facility.parcel_id == parcel_id,
            Facility.facility_type == str(facility_type).upper(),
        )
    ).scalar_one()


def reserved_facilities(session: Session, parcel_id: str, facility_type: str) -> int:
    """Facilities of this type on the parcel occupied by running processes —
    a query against running processes, not an escrow (nothing is locked)."""
    return session.execute(
        select(func.count(Process.id))
        .join(Recipe, Process.recipe_id == Recipe.id)
        .where(
            Process.parcel_id == parcel_id,
            Process.status == ProcessStatus.RUNNING,
            Recipe.requires_facility == str(facility_type).upper(),
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------

def add_deposit(
    session: Session,
    parcel: Parcel,
    symbol: str,
    quantity: Decimal,
    capacity: Decimal | None = None,
    regen_per_tick: Decimal = Decimal("0"),
) -> Deposit:
    quantity = Decimal(quantity).quantize(_QUANTUM)
    regen_per_tick = Decimal(regen_per_tick).quantize(_QUANTUM)
    if quantity < 0:
        raise ValueError("deposit quantity must be >= 0")
    if regen_per_tick < 0:
        raise ValueError("regen_per_tick must be >= 0")
    if capacity is not None:
        capacity = Decimal(capacity).quantize(_QUANTUM)
        if capacity < quantity:
            raise ValueError("capacity must be >= quantity")
    elif regen_per_tick > 0:
        raise ValueError("a regenerating deposit needs a capacity")
    deposit = Deposit(
        parcel_id=parcel.id,
        symbol=str(symbol).upper(),
        quantity=quantity,
        capacity=capacity,
        regen_per_tick=regen_per_tick,
    )
    session.add(deposit)
    session.flush()
    return deposit


def get_deposit(session: Session, parcel_id: str, symbol: str) -> Deposit | None:
    return session.execute(
        select(Deposit).where(
            Deposit.parcel_id == parcel_id, Deposit.symbol == str(symbol).upper()
        )
    ).scalar_one_or_none()


def draw_deposit(session: Session, parcel: Parcel, symbol: str, quantity: Decimal) -> Deposit:
    """Extraction only — called by start_process for a recipe's deposit_inputs."""
    deposit = get_deposit(session, parcel.id, symbol)
    if deposit is None or deposit.quantity < quantity:
        held = deposit.quantity if deposit else Decimal("0")
        raise InsufficientDepositError(
            f"parcel {parcel.id} holds {held} {str(symbol).upper()} deposit, cannot draw {quantity}"
        )
    deposit.quantity -= quantity
    session.flush()
    return deposit


def regen_deposits(session: Session, tick_number: int) -> list[dict]:
    """Per-tick pass: top each regenerating deposit up by regen_per_tick,
    capped at capacity. Returns one deposit_regen event per deposit that
    actually grew, addressed to the parcel's owner (None while unclaimed)."""
    events: list[dict] = []
    deposits = session.execute(
        select(Deposit)
        .where(Deposit.regen_per_tick > 0, Deposit.quantity < Deposit.capacity)
        .order_by(Deposit.created_at, Deposit.id)
    ).scalars().all()
    for deposit in deposits:
        new_quantity = min(deposit.capacity, deposit.quantity + deposit.regen_per_tick)
        regenerated = new_quantity - deposit.quantity
        deposit.quantity = new_quantity
        events.append({
            "type": "deposit_regen",
            "entity_id": deposit.parcel.owner_id,
            "parcel_id": deposit.parcel_id,
            "symbol": deposit.symbol,
            "regenerated": str(regenerated),
            "quantity": str(new_quantity),
        })
    if events:
        session.flush()
    return events
