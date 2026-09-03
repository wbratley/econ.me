"""Threats — declared per-tick danger, the demand side of the night.

A Threat row declares what circles entities of a type during dark hours:
ambient pressure per night-hour, extra pressure per say the entity made
that tick (noise carries at night — witness delivery's dual), and one
holding read that keeps the threat shy (the lit hearth). Pressure
credits a *condition* holding, so everything the conditions system
already does works on it unchanged:

- ``decay_per_tick`` on the condition good is the threat losing
  interest — including during daylight, when nothing accrues
- ``incapacitates_at`` is the threshold (the pack gets in)
- a recipe that consumes the condition is fighting back
- held modifiers (fear, wounds) bite the entity's effective quantities

Threats pressurize; they never act. Whether to keep the hearth lit,
stay quiet, or answer the dark with a weapon stays the entity's
decision, exactly as eating did (run 19).

Pass ordering: pressure runs AFTER needs consumption and BEFORE decay
(tick.py) — this tick's says are already in the event list, and the
decay pass fades the pressure in the same tick it lands, so a threat's
net drift is (pressure − decay) per dark hour and pure −decay by day.
Events here are per entity (the behaviour script's own signal), like
need events; a threshold hit becomes an entity_incapacitated event,
which the witness carries to every rival (a loud fact: the dark ate
someone).
"""

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock
from .models import Entity, EntityStatus, Holding, Threat

_QUANTUM = Decimal("0.0001")


def create_threat(
    session: Session,
    code: str,
    condition_symbol: str,
    ambient_night_per_tick: Decimal,
    entity_type=None,
    per_say_night: Decimal = Decimal("0"),
    deterred_by_symbol: str | None = None,
    deterred_by_quantity: Decimal = Decimal("0"),
    deterrence_factor: Decimal = Decimal("1"),
    place_key: str | None = None,
    name: str = "",
    description: str = "",
) -> Threat:
    existing = get_threat(session, code)
    if existing is not None:
        raise ValueError(
            f"threat {str(code).upper()!r} already installed by "
            f"{existing.pack_id or 'the platform'} -- a pack may not claim "
            f"another installer's key")
    ambient_night_per_tick = Decimal(ambient_night_per_tick).quantize(_QUANTUM)
    per_say_night = Decimal(per_say_night).quantize(_QUANTUM)
    deterred_by_quantity = Decimal(deterred_by_quantity).quantize(_QUANTUM)
    deterrence_factor = Decimal(deterrence_factor).quantize(
        Decimal("0.0001"))
    if ambient_night_per_tick <= 0:
        raise ValueError("ambient_night_per_tick must be positive")
    if per_say_night < 0:
        raise ValueError("per_say_night must be >= 0")
    if deterrence_factor <= 0 or deterrence_factor > 1:
        raise ValueError("deterrence_factor is a multiplier in (0, 1]")
    if deterred_by_symbol is not None and (
            deterred_by_quantity <= 0 or deterrence_factor >= 1):
        # A deterrent symbol pairs with a threshold and a real discount;
        # the default (no symbol) means nothing deters this threat.
        raise ValueError(
            "a deterrent symbol needs a positive quantity and a factor "
            "below 1 to mean anything")

    # docs/spatial.md S4: a home range, resolved at install. Unlike a
    # recipe's requires_place_key (data kept as a key so catalog rows
    # stay installable on mapless worlds), a placed threat is BOUND to
    # its spot at creation -- a threat ranged over a place that is not
    # installed is a content bug, loud at setup.
    place = None
    if place_key is not None:
        from . import places as places_mod

        place = places_mod.get_place(session, place_key)
        if place is None:
            raise ValueError(
                f"threat {str(code).upper()!r} ranges over unknown place "
                f"{str(place_key).upper()!r} -- install the map before "
                "ranging threats")

    threat = Threat(
        code=str(code).upper(),
        name=name,
        description=description,
        entity_type=entity_type,
        condition_symbol=str(condition_symbol).upper(),
        ambient_night_per_tick=ambient_night_per_tick,
        per_say_night=per_say_night,
        deterred_by_symbol=(deterred_by_symbol.upper()
                            if deterred_by_symbol else None),
        deterred_by_quantity=deterred_by_quantity,
        deterrence_factor=deterrence_factor,
        place_id=place.id if place is not None else None,
    )
    session.add(threat)
    session.flush()
    return threat


def get_threat(session: Session, code: str) -> Threat | None:
    return session.execute(
        select(Threat).where(Threat.code == str(code).upper())
    ).scalar_one_or_none()


def apply_pressure(session: Session, tick_number: int,
                   events: list[dict]) -> list[dict]:
    """Credit each threat's condition for every matching ACTIVE entity,
    during dark hours only, scaled by this tick's delivered says. One
    threat_pressure event per (threat, entity) that felt anything.

    Scoping (docs/spatial.md S4): a threat with no place is the ambient
    dark -- it finds everyone. A threat with a place finds only the
    entities whose location says they are there: standing there, or
    mid-hop on a road that started there (a traveller stands at the
    hop's origin until arrival moves them -- the road exposes you place
    by place, which is exactly "the road at night is a risk profile,
    computed from the same rows"). Unplaced entities are nowhere: a
    located threat cannot find them."""
    threats = session.execute(
        select(Threat).where(Threat.is_active.is_(True)).order_by(Threat.code)
    ).scalars().all()
    if not threats or not clock.is_night(tick_number):
        return []
    # Noise is what the world heard THIS tick: delivered says, indexed by
    # speaker (an intent that was refused never happened).
    says: dict[str, int] = {}
    for event in events:
        if (event.get("type") == "say"
                and event.get("status") != "rejected"
                and event.get("entity_id")):
            says[event["entity_id"]] = says.get(event["entity_id"], 0) + 1
    out: list[dict] = []
    for threat in threats:
        query = select(Entity).where(
            Entity.status == EntityStatus.ACTIVE).order_by(Entity.id)
        if threat.entity_type is not None:
            query = query.where(Entity.entity_type == threat.entity_type)
        if threat.place_id is not None:
            query = query.where(
                Entity.location_place_id == threat.place_id)
        for entity in session.execute(query).scalars():
            rate = threat.ambient_night_per_tick \
                + threat.per_say_night * says.get(entity.id, 0)
            deterred = False
            if threat.deterred_by_symbol is not None:
                hearth = session.execute(
                    select(Holding).where(
                        Holding.entity_id == entity.id,
                        Holding.symbol == threat.deterred_by_symbol,
                    )
                ).scalar_one_or_none()
                deterred = (hearth is not None
                            and hearth.quantity >= threat.deterred_by_quantity)
            if deterred:
                rate = (rate * threat.deterrence_factor).quantize(
                    _QUANTUM, rounding=ROUND_HALF_UP)
            pressure = rate.quantize(_QUANTUM, rounding=ROUND_HALF_UP)
            if pressure <= 0:
                continue
            holding = session.execute(
                select(Holding).where(
                    Holding.entity_id == entity.id,
                    Holding.symbol == threat.condition_symbol,
                )
            ).scalar_one_or_none()
            if holding is None:
                holding = Holding(
                    entity_id=entity.id,
                    symbol=threat.condition_symbol,
                    quantity=Decimal("0"),
                )
                session.add(holding)
            holding.quantity += pressure
            out.append({
                "type": "threat_pressure",
                "entity_id": entity.id,
                "threat": threat.code,
                "pressure": str(pressure),
                "says": says.get(entity.id, 0),
                "deterred": deterred,
            })
    if out:
        session.flush()
    return out
