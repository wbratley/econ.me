"""
Needs — per-entity-type demand definitions and the per-tick consumption pass.

A Need row declares what entities of a type must consume each tick: which
holding symbols satisfy it (1:1 — one unit of any satisfier covers one unit
of the need), how much per tick, and a priority. The consumption pass draws
down satisfying holdings and rewrites a per-(entity, need) satisfaction
score: consumed / required, rounded DOWN so 1.0000 means fully met.

Pass ordering: consumption runs AFTER the auction — goods bought this tick
are eaten this tick, and declared sell orders settle against the holdings
that backed them before anything is eaten (the same reason decay is
post-auction) — and BEFORE decay, so entities eat fresh stock and only
unsold, uneaten perishables rot. Buying exactly your per-tick quantity of a
perishable therefore fully satisfies the need.

All ordering is explicit (determinism invariant): needs are consumed in
priority order (lower = more essential, code ascending on ties) so when two
needs share a satisfying good the essential one draws first; within a need,
satisfiers are drawn down symbol-ascending; entities are visited
id-ascending.

Unlike the goods passes, events here are PER ENTITY — exactly one
need_satisfied or need_unmet event per (need, matching entity) each tick —
because they are the signal behaviour scripts react to, and behaviour
scripts see only their own entity's events.

Consequences of unmet needs are the conditions system (docs/design.md
§ conditions; decision rules are votable data, effect mechanisms are
engine). The cause mechanism lives here: a need may declare a condition
symbol, and an unmet tick credits it scaled by the shortfall —
condition_quantity × (1 − satisfaction). NeedState stays instantaneous;
the memory of deprivation lives in the holding, where decay_per_tick is
natural recovery and a healing recipe can consume it.
"""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock
from .models import (
    Entity, EntityStatus, EntityType, Holding, Need, NeedSatisfier, NeedState,
)

_QUANTUM = Decimal("0.0001")


def create_need(
    session: Session,
    code: str,
    quantity_per_tick: Decimal,
    satisfiers: list[str],
    entity_type: EntityType | None = None,
    priority: int = 0,
    name: str = "",
    description: str = "",
    condition_symbol: str | None = None,
    condition_quantity: Decimal = Decimal("0"),
    night_quantity_per_tick: Decimal | None = None,
    season_quantity_per_tick: dict | None = None,
    season_night_quantity_per_tick: dict | None = None,
    holding_rate_modifiers: dict | None = None,
) -> Need:
    existing = get_need(session, code)
    if existing is not None:
        raise ValueError(
            f"need {str(code).upper()} already installed by "
            f"{existing.pack_id or 'the platform'} -- a pack may not claim another installer's key")
    quantity_per_tick = Decimal(quantity_per_tick).quantize(_QUANTUM)
    if quantity_per_tick <= 0:
        raise ValueError("quantity_per_tick must be positive")
    symbols = sorted({str(s).upper() for s in satisfiers})
    if not symbols:
        raise ValueError("need must declare at least one satisfier")
    condition_quantity = Decimal(condition_quantity).quantize(_QUANTUM)
    if (condition_symbol is None) != (condition_quantity <= 0):
        raise ValueError(
            "condition_symbol and a positive condition_quantity go together"
        )
    if night_quantity_per_tick is not None:
        night_quantity_per_tick = Decimal(night_quantity_per_tick).quantize(_QUANTUM)
        if night_quantity_per_tick <= 0:
            raise ValueError("night_quantity_per_tick must be positive when set")

    def season_map(raw: dict | None, arg: str) -> dict[str, str] | None:
        """Normalize a {SEASON: qty} draw-override map: seasons in the
        engine calendar, quantities positive, stored as exact-decimal
        strings in calendar order (JSON dicts are order-stable here)."""
        if raw is None:
            return None
        cleaned = {str(s).upper(): Decimal(q).quantize(_QUANTUM) for s, q in raw.items()}
        unknown = set(cleaned) - set(clock.SEASONS)
        if unknown:
            raise ValueError(
                f"unknown seasons {sorted(unknown)} in {arg}; the year is "
                f"{'/'.join(clock.SEASONS)}")
        bad = {s: q for s, q in cleaned.items() if q <= 0}
        if bad:
            raise ValueError(f"{arg} quantities must be positive: {bad}")
        return {
            s: str(cleaned[s]) for s in clock.SEASONS if s in cleaned
        } or None

    season_quantity_per_tick = season_map(season_quantity_per_tick, "season_quantity_per_tick")
    season_night_quantity_per_tick = season_map(
        season_night_quantity_per_tick, "season_night_quantity_per_tick")
    holding_rate_modifiers = _normalize_modifiers(holding_rate_modifiers)

    need = Need(
        code=code.upper(),
        name=name,
        description=description,
        entity_type=entity_type,
        quantity_per_tick=quantity_per_tick,
        night_quantity_per_tick=night_quantity_per_tick,
        season_quantity_per_tick=season_quantity_per_tick,
        season_night_quantity_per_tick=season_night_quantity_per_tick,
        holding_rate_modifiers=holding_rate_modifiers,
        priority=priority,
        condition_symbol=condition_symbol.upper() if condition_symbol else None,
        condition_quantity=condition_quantity,
        satisfiers=[NeedSatisfier(symbol=s) for s in symbols],
    )
    session.add(need)
    session.flush()
    return need


def _normalize_modifiers(raw: dict | None) -> dict[str, str] | None:
    """Normalize a {holding_symbol: per-point multiplier delta} map to
    exact-decimal strings, symbol-ascending. Deltas may be negative (a
    hardy constitution draws less) but the resolved product is floored
    at zero by the pass, never here -- a body cannot draw negative, but
    the authored map may overshoot (a 20-CON monk simply eats nothing)."""
    if raw is None:
        return None
    cleaned = {str(s).upper(): Decimal(str(d)).quantize(_QUANTUM)
               for s, d in raw.items()}
    if not cleaned:
        return None
    bad = {s: d for s, d in cleaned.items() if d == 0}
    if bad:
        raise ValueError(f"holding_rate_modifiers deltas must be nonzero: {bad}")
    return {s: str(cleaned[s]) for s in sorted(cleaned)}


def normalize_need_profile(raw: dict | None) -> dict | None:
    """Normalize an entity need profile -- {"omit": [CODE...],
    "rates": {CODE: multiplier}} -- for the entities.need_profile column.
    Codes uppercase and sorted; multipliers exact-decimal strings in
    (0, 10] (a profile may ease or tax a draw, not abolish it -- abolition
    is what "omit" is for). Empty shapes normalize to None (the default
    body) so a template that mentions no needs stays a plain creature."""
    if raw is None:
        return None
    omit = sorted({str(c).upper() for c in raw.get("omit") or []})
    rates = {str(c).upper(): Decimal(str(v)).quantize(_QUANTUM)
             for c, v in (raw.get("rates") or {}).items()}
    bad = {c: v for c, v in rates.items() if v <= 0 or v > Decimal("10")}
    if bad:
        raise ValueError(
            f"need profile rates must be in (0, 10]: {bad} -- omit the need "
            "instead of scaling it to nothing")
    rates = {c: str(rates[c]) for c in sorted(rates)}
    profile: dict = {}
    if omit:
        profile["omit"] = omit
    if rates:
        profile["rates"] = rates
    return profile or None


def get_need(session: Session, code: str) -> Need | None:
    return session.execute(
        select(Need).where(Need.code == str(code).upper())
    ).scalar_one_or_none()


def required_quantity(
    need: Need, tick_number: int, days_per_season: int | None = None
) -> Decimal:
    """The draw THIS tick: the most specific applicable rate wins --
    season-night beats season beats global-night beats base (run 54 E1:
    a winter night is its own intensity; a winter day overrides nothing
    about the night, and a seasonless world draws its plain rates
    forever). This is the WORLD'S rate -- before the creature's profile
    and body modulate it (``effective_quantity``)."""
    days = days_per_season or clock.SEASON_DAYS
    season = clock.season_of(tick_number, days)
    if clock.is_night(tick_number):
        season_night = need.season_night_quantity_per_tick or {}
        if season in season_night:
            return Decimal(season_night[season])
        if need.night_quantity_per_tick is not None:
            return need.night_quantity_per_tick
    season_day = need.season_quantity_per_tick or {}
    if season in season_day:
        return Decimal(season_day[season])
    return need.quantity_per_tick


def effective_quantity(
    session: Session, need: Need, entity: Entity, tick_number: int,
    days_per_season: int | None = None,
) -> Decimal | None:
    """What THIS entity draws this tick: the world's rate
    (``required_quantity``) scaled by the creature's profile and body
    (run 56, the run-55 lesson: needs are the world's declarations, but
    a wolf is not a house -- the profile omits what the creature does
    not want, and holding-rate modifiers scale the draw by what the
    body holds, D&D-style).

    Returns None when the need does not apply to this entity at all
    (omitted from its profile); zero when the body has moderated the
    draw to nothing (the pass skips -- nothing to consume, nothing to
    report, nothing to condition)."""
    profile = entity.need_profile or {}
    if need.code in (profile.get("omit") or []):
        return None
    rate = Decimal(str((profile.get("rates") or {}).get(need.code, "1")))
    draw = required_quantity(need, tick_number, days_per_season) * rate
    modifiers = need.holding_rate_modifiers or {}
    if modifiers:
        scale = Decimal("1")
        for symbol in sorted(modifiers):
            delta = Decimal(modifiers[symbol])
            holding = session.execute(
                select(Holding).where(
                    Holding.entity_id == entity.id, Holding.symbol == symbol
                )
            ).scalar_one_or_none()
            qty = holding.quantity if holding is not None else Decimal("0")
            scale *= (Decimal("1") + delta * qty)
        if scale <= 0:
            return Decimal("0")
        draw *= scale
    return draw.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def run_consumption(
    session: Session, tick_number: int, days_per_season: int | None = None
) -> list[dict]:
    """Draw down satisfying holdings for every active need and rewrite each
    matching entity's satisfaction score; on an unmet tick, credit the
    need's declared condition scaled by the shortfall. Returns one
    need_satisfied or need_unmet event per (need, active entity) —
    incapacitated entities neither eat nor accumulate."""
    events: list[dict] = []
    needs = session.execute(
        select(Need).where(Need.is_active.is_(True)).order_by(Need.priority, Need.code)
    ).scalars().all()
    for need in needs:
        query = select(Entity).where(Entity.status == EntityStatus.ACTIVE).order_by(Entity.id)
        if need.entity_type is not None:
            query = query.where(Entity.entity_type == need.entity_type)
        # The clock (run 18): a need with a night draw pulls the heavier
        # rate during dark hours — the same cold, a colder intensity.
        # Seasons (run 54 E1) resolve through the same specificity
        # ladder — one definition of "what this tick draws". The
        # creature's slice (run 56): the profile and body modulate the
        # world's rate per entity — a need that does not apply draws
        # nothing and reports nothing (no event, no condition credit).
        for entity in session.execute(query).scalars():
            required = effective_quantity(session, need, entity, tick_number,
                                          days_per_season)
            if required is None or required <= 0:
                continue
            consumed = Decimal("0")
            for satisfier in need.satisfiers:  # relationship is symbol-ordered
                if consumed >= required:
                    break
                holding = session.execute(
                    select(Holding).where(
                        Holding.entity_id == entity.id, Holding.symbol == satisfier.symbol
                    )
                ).scalar_one_or_none()
                if holding is None or holding.quantity <= 0:
                    continue
                take = min(holding.quantity, required - consumed)
                holding.quantity -= take
                consumed += take
            satisfaction = _satisfaction(consumed, required)
            _upsert_state(session, entity.id, need, satisfaction, tick_number)
            event = {
                "type": "need_satisfied" if consumed == required else "need_unmet",
                "entity_id": entity.id,
                "need": need.code,
                "consumed": str(consumed.quantize(_QUANTUM)),
                "required": str(required),
                "satisfaction": str(satisfaction),
            }
            if consumed < required and need.condition_symbol is not None:
                granted = _grant_condition(session, entity, need, satisfaction)
                if granted > 0:
                    event["condition"] = need.condition_symbol
                    event["condition_granted"] = str(granted)
            events.append(event)
    if events:
        session.flush()
    return events


def _grant_condition(
    session: Session, entity: Entity, need: Need, satisfaction: Decimal
) -> Decimal:
    """Credit the need's condition symbol by condition_quantity scaled by
    the shortfall (1 − satisfaction)."""
    granted = (need.condition_quantity * (Decimal("1") - satisfaction)).quantize(
        _QUANTUM, rounding=ROUND_HALF_UP
    )
    if granted <= 0:
        return granted
    holding = session.execute(
        select(Holding).where(
            Holding.entity_id == entity.id, Holding.symbol == need.condition_symbol
        )
    ).scalar_one_or_none()
    if holding is None:
        holding = Holding(
            entity_id=entity.id, symbol=need.condition_symbol, quantity=Decimal("0")
        )
        session.add(holding)
    holding.quantity += granted
    return granted


def _satisfaction(consumed: Decimal, required: Decimal) -> Decimal:
    # Rounded DOWN so 1.0000 is reachable only by full consumption.
    if consumed >= required:
        return Decimal("1").quantize(_QUANTUM)
    return (consumed / required).quantize(_QUANTUM, rounding=ROUND_DOWN)


def _upsert_state(
    session: Session, entity_id: str, need: Need, satisfaction: Decimal, tick_number: int
) -> None:
    state = session.execute(
        select(NeedState).where(
            NeedState.entity_id == entity_id, NeedState.need_id == need.id
        )
    ).scalar_one_or_none()
    if state is None:
        state = NeedState(entity_id=entity_id, need_id=need.id, satisfaction=satisfaction,
                          updated_tick=tick_number)
        session.add(state)
    else:
        state.satisfaction = satisfaction
        state.updated_tick = tick_number
