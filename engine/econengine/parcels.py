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

from .models import (Deposit, Entity, Facility, FacilityHolding, Parcel, Place,
                     Process, ProcessStatus, Recipe)

_QUANTUM = Decimal("0.0001")
# The shared-pool owner sentinel (docs/camp-storage.md): a FacilityHolding
# row with owner_id == "*" is the facility's SHARED pool, touchable by any
# entity; a real entity id is that entity's private store.
SHARED = "*"


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
    session: Session, parcel: Parcel, facility_type: str, built_tick: int | None = None,
    access: str = "OWNER", capacity: int = 1,
    fuel: Decimal = Decimal("0"),
    fuel_capacity: Decimal | None = None,
    fuel_burn_per_tick: Decimal = Decimal("0"),
) -> Facility:
    """Genesis placement (built_tick None) or construction-recipe completion.

    The commons knobs (P1): `access` "OWNER"/"PLACE" (who may bind),
    `capacity` (concurrent binder slots), and the burning stock -- `fuel`
    now, `fuel_capacity` the banked ceiling, `fuel_burn_per_tick` the
    per-tick draw. Defaults reproduce the historical one-slot private
    facility exactly."""
    access = str(access).upper()
    if access not in ("OWNER", "PLACE"):
        raise ValueError(f"facility access must be OWNER or PLACE, not {access!r}")
    capacity = int(capacity)
    if capacity < 1:
        raise ValueError("facility capacity must be >= 1")
    fuel = Decimal(fuel).quantize(_QUANTUM)
    fuel_burn_per_tick = Decimal(fuel_burn_per_tick).quantize(_QUANTUM)
    if fuel < 0:
        raise ValueError("fuel must be >= 0")
    if fuel_burn_per_tick < 0:
        raise ValueError("fuel_burn_per_tick must be >= 0")
    if fuel_capacity is not None:
        fuel_capacity = Decimal(fuel_capacity).quantize(_QUANTUM)
        if fuel_capacity < fuel:
            raise ValueError("fuel_capacity must be >= the starting fuel")
    facility = Facility(
        parcel_id=parcel.id, facility_type=str(facility_type).upper(),
        built_tick=built_tick, access=access, capacity=capacity,
        fuel=fuel, fuel_capacity=fuel_capacity,
        fuel_burn_per_tick=fuel_burn_per_tick,
    )
    session.add(facility)
    session.flush()
    return facility


def get_facility(session: Session, facility_id: str) -> Facility | None:
    return session.get(Facility, facility_id)


def facilities_of_type(
    session: Session, parcel_id: str, facility_type: str
) -> list[Facility]:
    """The parcel's facilities of one type, creation-ordered (the binding
    scan's deterministic order)."""
    return list(session.execute(
        select(Facility)
        .where(
            Facility.parcel_id == parcel_id,
            Facility.facility_type == str(facility_type).upper(),
        )
        .order_by(Facility.created_at, Facility.id)
    ).scalars().all())


def facility_count(session: Session, parcel_id: str, facility_type: str) -> int:
    return session.execute(
        select(func.count(Facility.id)).where(
            Facility.parcel_id == parcel_id,
            Facility.facility_type == str(facility_type).upper(),
        )
    ).scalar_one()


def facility_capacity(session: Session, parcel_id: str, facility_type: str) -> int:
    """Sum of the parcel's facility capacities of this type -- how many
    processes may bind at once. The pre-commons rule (one facility backs
    one process) is the capacity=1 case; a four-stone hearth seats four."""
    return session.execute(
        select(func.coalesce(func.sum(Facility.capacity), 0)).where(
            Facility.parcel_id == parcel_id,
            Facility.facility_type == str(facility_type).upper(),
        )
    ).scalar_one()


def public_facility_parcels(
    session: Session, facility_type: str
) -> list[Parcel]:
    """Parcels hosting a PLACE-access facility of this type, id-ordered --
    the fallback scan for an entity that owns none (the village fire)."""
    parcel_ids = session.execute(
        select(Facility.parcel_id).distinct()
        .where(
            Facility.facility_type == str(facility_type).upper(),
            Facility.access == "PLACE",
        )
        .order_by(Facility.parcel_id)
    ).scalars().all()
    return [
        p for pid in parcel_ids
        if (p := session.get(Parcel, pid)) is not None
    ]


def has_public_facility(
    session: Session, parcel_id: str, facility_type: str
) -> bool:
    """Does this parcel host a PLACE-access facility of the type? The
    explicit-binding rule: an entity may bind a parcel it does not control
    only where the world made the facility public."""
    return session.execute(
        select(func.count(Facility.id)).where(
            Facility.parcel_id == parcel_id,
            Facility.facility_type == str(facility_type).upper(),
            Facility.access == "PLACE",
        )
    ).scalar_one() > 0


def credit_fuel(
    session: Session, facility: Facility, quantity: Decimal,
    entity_id: str | None = None,
) -> dict:
    """Stoke: add fuel to one facility, refusing past its fuel_capacity
    (the caller checks capacity BEFORE drawing inputs; this belt does the
    same job for the direct/genesis path). Returns the event fact --
    `facility_lit` when the fire was dark and lights (the beacon); a plain
    `facility_fuel` top-up otherwise. Excess past capacity is an error,
    never a silent spill: banking burnwood is capped, like every input."""
    quantity = Decimal(quantity).quantize(_QUANTUM)
    if quantity <= 0:
        raise ValueError("fuel credit must be positive")
    if facility.fuel_capacity is not None:
        if facility.fuel + quantity > facility.fuel_capacity:
            raise ValueError(
                f"facility is fully banked ({facility.fuel}/{facility.fuel_capacity} "
                f"fuel) -- wait for it to burn down"
            )
    was_dark = facility.fuel <= 0
    facility.fuel += quantity
    session.flush()
    return {
        "type": "facility_lit" if was_dark else "facility_fuel",
        "entity_id": entity_id,
        "parcel_id": facility.parcel_id,
        "facility_type": facility.facility_type,
        "fuel": str(facility.fuel),
    }


def burn_facility_fuel(session: Session, tick_number: int) -> list[dict]:
    """Per-tick pass: every burning facility (fuel > 0, a burn rate set)
    draws its fuel down -- the fire consumes whether anyone sits by it or
    not. One `facility_dark` event per facility that ran out this tick
    (the beacon going out is the fact worth hearing; steady burning is
    telemetry, not news, and the level is queryable). Deterministic by
    created_at/id, like every engine pass."""
    events: list[dict] = []
    burning = session.execute(
        select(Facility)
        .where(Facility.fuel > 0, Facility.fuel_burn_per_tick > 0)
        .order_by(Facility.created_at, Facility.id)
    ).scalars().all()
    for facility in burning:
        facility.fuel = max(Decimal("0"), facility.fuel - facility.fuel_burn_per_tick)
        if facility.fuel <= 0:
            facility.fuel = Decimal("0")
            events.append({
                "type": "facility_dark",
                "entity_id": None,
                "parcel_id": facility.parcel_id,
                "facility_type": facility.facility_type,
                "fuel": "0",
            })
    if events:
        session.flush()
    return events


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
# Storage — goods physically at a facility's shelf (docs/camp-storage.md).
#
# One table (FacilityHolding) covers all three surfaces: the hearth's
# private STORAGE (owner = the entity, capped), the shared WOODPILE
# (owner = "*", wood only), and the shared DROP at every other place
# (owner = "*", any symbol). store/take move between an entity's
# holdings and its private rows; drop/pickup move between holdings and
# the shared pool. Goods move between holdings and pools (and rot);
# nothing is created or destroyed here — conservation holds, and
# "theft" is just a pickup of goods someone else dropped.
# ---------------------------------------------------------------------------

def _facility_at(session: Session, entity: Entity, facility_type: str) -> Facility | None:
    """The first facility of `facility_type` on a parcel at the entity's
    standing place, creation-ordered (deterministic). None when the entity
    stands nowhere or the place carries no such facility — the presence
    gate: storage is where you are."""
    if entity.location_place_id is None:
        return None
    return session.execute(
        select(Facility)
        .join(Parcel, Facility.parcel_id == Parcel.id)
        .where(
            Parcel.place_id == entity.location_place_id,
            Facility.facility_type == str(facility_type).upper(),
        )
        .order_by(Facility.created_at, Facility.id)
    ).scalars().first()


def _holding_row(
    session: Session, facility_id: str, owner_id: str, symbol: str
) -> FacilityHolding | None:
    return session.execute(
        select(FacilityHolding).where(
            FacilityHolding.facility_id == facility_id,
            FacilityHolding.owner_id == owner_id,
            FacilityHolding.symbol == str(symbol).upper(),
        )
    ).scalar_one_or_none()


def _credit_row(session: Session, facility_id: str, owner_id: str,
                 symbol: str, quantity: Decimal) -> FacilityHolding:
    """Credit a (facility, owner, symbol) shelf row, creating it at 0 if the
    shelf is bare. The pool's writer — the mirror of adjust_holding for the
    pool side of a move."""
    row = _holding_row(session, facility_id, owner_id, symbol)
    if row is None:
        row = FacilityHolding(
            facility_id=facility_id, owner_id=owner_id,
            symbol=str(symbol).upper(), quantity=Decimal("0"),
        )
        session.add(row)
    row.quantity = (row.quantity + quantity).quantize(_QUANTUM)
    session.flush()
    return row


def _draw_row(session: Session, facility_id: str, owner_id: str,
              symbol: str, quantity: Decimal) -> FacilityHolding:
    """Draw from a shelf row, refusing to overdraw (the pool cannot go
    negative — a pickup of goods nobody dropped is impossible)."""
    row = _holding_row(session, facility_id, owner_id, symbol)
    have = row.quantity if row is not None else Decimal("0")
    if have < quantity:
        from . import markets as _markets
        raise _markets.InsufficientHoldingsError(
            f"the {str(owner_id)} shelf holds {have.quantize(_QUANTUM)} "
            f"{str(symbol).upper()}, cannot draw {quantity.quantize(_QUANTUM)}"
        )
    if row is None:
        row = FacilityHolding(
            facility_id=facility_id, owner_id=owner_id,
            symbol=str(symbol).upper(), quantity=Decimal("0"),
        )
        session.add(row)
    row.quantity = (row.quantity - quantity).quantize(_QUANTUM)
    session.flush()
    return row


def _store_total(session: Session, facility_id: str, owner_id: str) -> Decimal:
    """A private store's total: every symbol the owner has shelved there.
    The capacity is a per-owner policy (one total-unit cap), not a
    per-symbol one."""
    return Decimal(session.execute(
        select(func.coalesce(func.sum(FacilityHolding.quantity), 0))
        .where(
            FacilityHolding.facility_id == facility_id,
            FacilityHolding.owner_id == owner_id,
        )
    ).scalar_one())


def _storage_place_key(session: Session, facility: Facility) -> str:
    parcel = session.get(Parcel, facility.parcel_id)
    place = parcel.place if parcel is not None else None
    return place.key if place is not None else ""


def _where(entity: Entity) -> str:
    """The presence-gate refusal's place: the standing place's label, or
    "nowhere" for an entity off the map (the legacy unplaced citizen)."""
    from . import places as places_mod
    return places_mod.label(entity.place) if entity.place is not None else "nowhere"


def _clean(x: Decimal) -> str:
    """A quantity for a human message: 4dp trimmed to the last real digit
    (40 -> '40', 2.5 -> '2.5', 3.0000 -> '3'). The cap's message reads
    'store is full (40/40)' -- whole units, not the ledger's 4dp."""
    s = f"{x.quantize(_QUANTUM):f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _storage_summary(session: Session, facility: Facility,
                    symbol: str, quantity: Decimal) -> dict:
    """The applied event's fact: who dropped/stored what, where — the audit
    line the theft record reads (the applied row IS the record)."""
    return {
        "facility_type": facility.facility_type,
        "symbol": str(symbol).upper(),
        "quantity": str(quantity.quantize(_QUANTUM)),
        "place": _storage_place_key(session, facility),
    }


def _held_quantity(session: Session, entity: Entity, symbol: str) -> Decimal:
    from . import markets as _markets
    h = _markets.get_holding(session, entity.id, str(symbol).upper())
    return h.quantity if h is not None else Decimal("0")


def facility_store(session: Session, entity: Entity,
                   symbol: str, quantity: Decimal) -> dict:
    """store(symbol, qty) — holdings -> the private STORAGE at the hearth.
    Capped: the owner's total shelved <= the facility's capacity. The
    load-release valve at home (run-57's Lagertha)."""
    from . import markets as _markets
    symbol = str(symbol).upper()
    quantity = Decimal(quantity).quantize(_QUANTUM)
    facility = _facility_at(session, entity, "STORAGE")
    if facility is None:
        raise ValueError(f"no storage at {_where(entity)}")
    held = _held_quantity(session, entity, symbol)
    if held < quantity:
        raise ValueError(
            f"holds {held.quantize(_QUANTUM)} {symbol}, "
            f"cannot store {quantity}")
    total = _store_total(session, facility.id, entity.id)
    if total + quantity > facility.capacity:
        raise ValueError(
            f"store is full ({_clean(total)}/{facility.capacity})")
    _markets.adjust_holding(session, entity, symbol, -quantity)
    _credit_row(session, facility.id, entity.id, symbol, quantity)
    return _storage_summary(session, facility, symbol, quantity)


def facility_take(session: Session, entity: Entity,
                  symbol: str, quantity: Decimal) -> dict:
    """take(symbol, qty) — the private STORAGE -> holdings. The inverse of
    store: reclaim what you set down at home."""
    from . import markets as _markets
    symbol = str(symbol).upper()
    quantity = Decimal(quantity).quantize(_QUANTUM)
    facility = _facility_at(session, entity, "STORAGE")
    if facility is None:
        raise ValueError(f"no storage at {_where(entity)}")
    row = _holding_row(session, facility.id, entity.id, symbol)
    have = row.quantity if row is not None else Decimal("0")
    if have < quantity:
        raise ValueError(
            f"the store has {have.quantize(_QUANTUM)} {symbol}, "
            f"cannot take {quantity}")
    _draw_row(session, facility.id, entity.id, symbol, quantity)
    _markets.adjust_holding(session, entity, symbol, quantity)
    return _storage_summary(session, facility, symbol, quantity)


def _drop_target(session: Session, entity: Entity, symbol: str):
    """Route a drop/pickup to the shared pool at the entity's place:
    WOOD goes to the WOODPILE where one stands (the camp's common wood,
    pulled from first), anything else (and wood where no pile stands) to
    the DROP. None when the place has no pool of the symbol's kind —
    the hearth takes no shared non-wood (home is private)."""
    symbol = str(symbol).upper()
    if symbol == "WOOD":
        facility = _facility_at(session, entity, "WOODPILE")
        if facility is not None:
            return facility
    return _facility_at(session, entity, "DROP")


def facility_drop(session: Session, entity: Entity,
                  symbol: str, quantity: Decimal) -> dict:
    """drop(symbol, qty) — holdings -> the shared pool at the caller's
    place (WOODPILE for wood at the hearth, DROP elsewhere). The
    away-from-home valve (run-57's Harald) and the theft source: once
    dropped, the goods are in a pool nobody owns."""
    from . import markets as _markets
    symbol = str(symbol).upper()
    quantity = Decimal(quantity).quantize(_QUANTUM)
    facility = _drop_target(session, entity, symbol)
    if facility is None:
        raise ValueError(f"no drop at {_where(entity)}")
    if facility.facility_type == "WOODPILE" and symbol != "WOOD":
        raise ValueError("the woodpile takes WOOD only")
    held = _held_quantity(session, entity, symbol)
    if held < quantity:
        raise ValueError(
            f"holds {held.quantize(_QUANTUM)} {symbol}, "
            f"cannot drop {quantity}")
    _markets.adjust_holding(session, entity, symbol, -quantity)
    _credit_row(session, facility.id, SHARED, symbol, quantity)
    return _storage_summary(session, facility, symbol, quantity)


def facility_pickup(session: Session, entity: Entity,
                    symbol: str, quantity: Decimal) -> dict:
    """pickup(symbol, qty) — the shared pool at the caller's place ->
    holdings, drawn regardless of who dropped it. THE theft/scavenging
    action: taking from a pool nobody owns is a normal goods move, not a
    transfer between entities (the ownership invariant holds)."""
    from . import markets as _markets
    symbol = str(symbol).upper()
    quantity = Decimal(quantity).quantize(_QUANTUM)
    facility = _drop_target(session, entity, symbol)
    if facility is None:
        raise ValueError(f"no drop at {_where(entity)}")
    row = _holding_row(session, facility.id, SHARED, symbol)
    have = row.quantity if row is not None else Decimal("0")
    if have < quantity:
        raise ValueError(
            f"the shared pile has {have.quantize(_QUANTUM)} {symbol}, "
            f"cannot pickup {quantity}")
    _draw_row(session, facility.id, SHARED, symbol, quantity)
    _markets.adjust_holding(session, entity, symbol, quantity)
    return _storage_summary(session, facility, symbol, quantity)


def facility_at_place(session: Session, place_key: str,
                      facility_type: str) -> Facility | None:
    """The first facility of `facility_type` on any parcel at `place_key`
    (creation-ordered), or None. The presence check for a place's
    facilities (the pack's genesis guard and the catalog both want it)."""
    place = session.execute(
        select(Place).where(Place.key == str(place_key).upper())
    ).scalar_one_or_none()
    if place is None:
        return None
    return session.execute(
        select(Facility)
        .join(Parcel, Facility.parcel_id == Parcel.id)
        .where(
            Parcel.place_id == place.id,
            Facility.facility_type == str(facility_type).upper(),
        )
        .order_by(Facility.created_at, Facility.id)
    ).scalars().first()


def woodpile_pool(session: Session, entity: Entity, symbol: str = "WOOD") -> Decimal:
    """The WOODPILE's shared quantity of `symbol` at the entity's place —
    0 when the place has no woodpile (the hearth is the only one in the
    stone-age world). The pull-first source: production draws this BEFORE
    the entity's own holdings, so a house never starves its fire for lack
    of a carrying capacity it could have dropped."""
    facility = _facility_at(session, entity, "WOODPILE")
    if facility is None:
        return Decimal("0")
    row = _holding_row(session, facility.id, SHARED, str(symbol).upper())
    return row.quantity if row is not None else Decimal("0")


def draw_woodpile(session: Session, entity: Entity, symbol: str,
                  quantity: Decimal) -> None:
    """Draw from the WOODPILE at the entity's place (the pull-first source).
    The caller has already checked the pool is deep enough; this is the
    writer, refused (never silently short) if it is not."""
    facility = _facility_at(session, entity, "WOODPILE")
    if facility is None:
        raise ValueError(f"no woodpile at {_where(entity)}")
    _draw_row(session, facility.id, SHARED, str(symbol).upper(),
              Decimal(quantity).quantize(_QUANTUM))


def shared_pool_quantity(session: Session, entity: Entity, symbol: str) -> Decimal | None:
    """ctx.query.dropped — the shared pool's quantity of `symbol` at the
    caller's place (WOODPILE for wood at the hearth, DROP elsewhere). Nil
    (None) when the place has no pool of the symbol's kind — "nil when
    none"; a present-but-empty pool reads 0."""
    facility = _drop_target(session, entity, symbol)
    if facility is None:
        return None
    row = _holding_row(session, facility.id, SHARED, str(symbol).upper())
    qty = row.quantity if row is not None else Decimal("0")
    return qty.quantize(_QUANTUM)


def stored_quantity(session: Session, entity: Entity, symbol: str) -> Decimal | None:
    """ctx.query.stored — the caller's PRIVATE store's quantity of
    `symbol`. Nil (None) when the caller is not at a place carrying a
    STORAGE ("nil ... not at home"); a present-but-empty store reads 0."""
    facility = _facility_at(session, entity, "STORAGE")
    if facility is None:
        return None
    row = _holding_row(session, facility.id, entity.id, str(symbol).upper())
    qty = row.quantity if row is not None else Decimal("0")
    return qty.quantize(_QUANTUM)


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
