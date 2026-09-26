"""The conditions register (P2, ROADMAP P2 — the torch rework).

`conditions.py` owns conditions-as-consequences (a condition IS a Good
there: COND-* impairments accumulating from unmet needs). The register
is the OTHER family, and the read layer over both: named, per-tick,
queryable states of an entity — LOUD, LIT, EMBER today; tired and
broken-arm join later by declaring new derivations, not new readers.

Design: the register is a PROJECTION, not a store. Every condition the
world declares derives, on demand, from state the engine already keeps:

- ``holding`` — {symbol: floor}: active while every floor is met (LIT is
  "a flame above the floor burns"). The stamp pass records when these
  were last true (``entities.last_lit_tick``) — the only write.
- ``lit_within_ticks`` — N (with ``after`` naming a holding condition):
  active within N ticks of the last stamp. The ember: your torch died,
  but for two ticks the dead brand still lights the next.
- ``loud_events_within_ticks`` — N: active for entities with applied
  ``loud``-marked events in the last N persisted ticks. Loudness is an
  ACT, marked at write time by whoever emits the event (speech, a
  torchlit night departure); the register grades it by count.
- ``place_facility_fuel`` — {facility_type, min_fuel}: active while the
  entity's current place hosts a facility of the type with fuel at or
  above the floor (FIRESIDE: the camp's fire is burning where you
  stand — firelight, not the memory of it).

Held condition-goods (the COND-* family) join the register by name
(symbol) automatically — one read shows every active condition, from
either family. Future derivations (need-state for FATIGUE, injury rows)
extend the vocabulary without touching readers.

Readers gate actions with names, not symbols: ``travel.rules`` refuses
night travel without LIT; ``combat.rules`` deterrence accepts condition
names alongside symbol floors; a recipe's ``requires_conditions`` is a
start-gate. ``ctx.entity.conditions`` carries the entity's own register
every tick — your state modifies your behaviour — and
``ctx.query.conditions(entity_id)`` / ``ctx.query.carriers(condition)``
read anyone's (the beacon doctrine: queryable facts, not deliveries).
"""
import json
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Entity, EntityStatus, Good, Holding, Tick, WorldSetting

CONDITIONS_RULES_KEY = "conditions.rules"


def set_rules(session: Session, rules: dict) -> None:
    row = session.get(WorldSetting, CONDITIONS_RULES_KEY)
    if row is None:
        session.add(WorldSetting(key=CONDITIONS_RULES_KEY, value=rules))
    else:
        row.value = rules


def get_rules(session: Session) -> dict:
    row = session.get(WorldSetting, CONDITIONS_RULES_KEY)
    return dict(row.value) if row is not None else {}


def declared_conditions(session: Session) -> list[str]:
    """Register names in declaration order (determinism for every reader)."""
    return list(get_rules(session).keys())


def is_declared(session: Session, name: str) -> bool:
    return name in get_rules(session)


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------

def _meets_floors(holdings: dict[str, Decimal], floors: dict) -> bool:
    try:
        return all(
            holdings.get(sym, Decimal("0")) >= Decimal(str(floor))
            for sym, floor in (floors or {}).items()
        )
    except (ArithmeticError, ValueError):
        return False


def _holdings_map(session: Session, entity_id: str) -> dict[str, Decimal]:
    return {
        h.symbol: h.quantity
        for h in session.execute(
            select(Holding).where(Holding.entity_id == entity_id)
        ).scalars()
    }


def _held_condition_goods(session: Session) -> set[str]:
    return set(session.execute(
        select(Good.symbol).where(
            (Good.modifies_pattern.isnot(None))
            | (Good.incapacitates_at.isnot(None))
        )
    ).scalars())


def _place_facility_lit(session: Session, place_id: str,
                        facility_type: str, min_fuel: Decimal) -> bool:
    """Does this place host a facility of the type with fuel at/above
    the floor? The camp-fire read: whatever parcel owns it, a burning
    fire lights the ground it stands on (a wolf at the door does not
    read deeds -- firelight is firelight)."""
    from .models import Facility, Parcel
    return session.execute(
        select(func.count(Facility.id))
        .join(Parcel, Facility.parcel_id == Parcel.id)
        .where(Parcel.place_id == place_id,
               Facility.facility_type == facility_type,
               Facility.fuel >= min_fuel)
    ).scalar_one() > 0


def _derived(session: Session, entity: Entity, holdings: dict[str, Decimal],
             tick_number: int) -> list[str]:
    """The register's derived half, in declaration order. ``holdings`` is
    passed in so multi-entity reads (carriers) fetch once per entity."""
    out: list[str] = []
    for name, spec in get_rules(session).items():
        spec = spec or {}
        floors = spec.get("holding")
        if floors is not None:
            if _meets_floors(holdings, floors):
                out.append(name)
            continue
        window = spec.get("lit_within_ticks")
        if window is not None:
            last = entity.last_lit_tick
            if (last is not None
                    and 0 <= tick_number - last <= int(window)):
                out.append(name)
                continue
            # the window's other half: a brand burning RIGHT NOW strikes
            # its own successor (the `after` condition's floors, met)
            after = spec.get("after")
            if after is not None:
                afloors = ((get_rules(session).get(after) or {}).get("holding"))
                if afloors and _meets_floors(holdings, afloors):
                    out.append(name)
            continue
        hearth = spec.get("place_facility_fuel")
        if hearth is not None:
            ftype = str(hearth.get("facility_type", "")).upper()
            floor = Decimal(str(hearth.get("min_fuel", "1")))
            if (ftype and entity.location_place_id is not None
                    and _place_facility_lit(
                        session, entity.location_place_id, ftype, floor)):
                out.append(name)
            continue
        # loud_events_within_ticks: answered by the events scan
        # (graded per entity); unknown derivation types: never active
    return out


def _loud_counts(session: Session, window: int, tick_number: int) -> dict[str, int]:
    """Applied loud-marked acts per entity over the last ``window``
    persisted ticks ([tick-window, tick-1]): this tick's walkers are
    next tick's readings — events persist with their tick."""
    rows = session.execute(
        select(Tick).where(
            Tick.number >= tick_number - window,
            Tick.number < tick_number,
        ).order_by(Tick.number)
    ).scalars().all()
    loud: dict[str, int] = {}
    for row in rows:
        raw = row.events or []
        if isinstance(raw, str):
            raw = json.loads(raw)
        for ev in raw:
            if (ev.get("loud") and ev.get("status") != "rejected"
                    and ev.get("entity_id")):
                who = ev["entity_id"]
                loud[who] = loud.get(who, 0) + 1
    return loud


def active_conditions(session: Session, entity: Entity,
                      tick_number: int) -> list[str]:
    """Every active condition on an entity: derived flags (declaration
    order) then held condition-goods (symbol order). Deterministic."""
    rules = get_rules(session)
    holdings = _holdings_map(session, entity.id)
    held = _held_condition_goods(session)
    out = _derived(session, entity, holdings, tick_number)
    for name, spec in rules.items():
        if (spec or {}).get("loud_events_within_ticks") is not None:
            loud = _loud_counts(
                session, int(spec["loud_events_within_ticks"]), tick_number)
            if loud.get(entity.id):
                out.append(name)
    out.extend(sorted(s for s in held if holdings.get(s, Decimal("0")) > 0))
    return out


def stamp_lit(session: Session, tick_number: int) -> None:
    """The register's one write: entities satisfying a holding condition
    that some ``lit_within_ticks`` derivation names (``after``) get
    ``last_lit_tick = tick``. Runs after the decay pass, so the stamp
    records post-decay truth — a torch that burned to the floor this
    tick does not re-stamp, and the ember starts."""
    stampers = {
        (spec or {}).get("after")
        for spec in get_rules(session).values()
        if (spec or {}).get("lit_within_ticks") is not None
    }
    if not stampers:
        return
    floors = {
        name: (spec or {}).get("holding")
        for name, spec in get_rules(session).items()
        if name in stampers
    }
    floors = {n: f for n, f in floors.items() if f}
    if not floors:
        return
    changed = False
    for entity in session.execute(
        select(Entity).where(Entity.status == EntityStatus.ACTIVE)
        .order_by(Entity.id)
    ).scalars():
        holdings = _holdings_map(session, entity.id)
        if any(_meets_floors(holdings, f) for f in floors.values()):
            entity.last_lit_tick = tick_number
            changed = True
    if changed:
        session.flush()


def carriers(session: Session, condition: str, tick_number: int) -> list[dict]:
    """Who bears a condition, graded: [{entity_id, strength}]. Loud-family
    conditions carry act counts from the events scan; every other
    condition carries strength 1 for each active bearer. Deterministic
    by entity id."""
    spec = (get_rules(session).get(condition) or {})
    if spec.get("loud_events_within_ticks") is not None:
        loud = _loud_counts(
            session, int(spec["loud_events_within_ticks"]), tick_number)
        return [
            {"entity_id": who, "strength": n}
            for who, n in sorted(loud.items())
        ]
    entities = session.execute(
        select(Entity).where(Entity.status == EntityStatus.ACTIVE)
        .order_by(Entity.id)
    ).scalars().all()
    held = _held_condition_goods(session)
    out: list[dict] = []
    for entity in entities:
        if condition in _derived(
                session, entity, _holdings_map(session, entity.id), tick_number) \
                or (condition in held and _holds(session, entity.id, condition)):
            out.append({"entity_id": entity.id, "strength": 1})
    return out


def _holds(session: Session, entity_id: str, symbol: str) -> bool:
    row = session.execute(
        select(Holding).where(
            Holding.entity_id == entity_id, Holding.symbol == symbol
        )
    ).scalar_one_or_none()
    return row is not None and row.quantity > 0
