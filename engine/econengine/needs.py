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
scripts see only their own entity's events. Consequences of unmet needs
are the conditions system (docs/design.md § conditions, not yet built):
decision rules are votable data, effect mechanisms are engine.
"""

from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Entity, EntityType, Holding, Need, NeedSatisfier, NeedState

_QUANTUM = Decimal("0.0001")


def create_need(
    session: Session,
    code: str,
    quantity_per_tick: Decimal,
    satisfiers: list[str],
    entity_type: EntityType | None = None,
    priority: int = 0,
    name: str = "",
) -> Need:
    quantity_per_tick = Decimal(quantity_per_tick).quantize(_QUANTUM)
    if quantity_per_tick <= 0:
        raise ValueError("quantity_per_tick must be positive")
    symbols = sorted({str(s).upper() for s in satisfiers})
    if not symbols:
        raise ValueError("need must declare at least one satisfier")

    need = Need(
        code=code.upper(),
        name=name,
        entity_type=entity_type,
        quantity_per_tick=quantity_per_tick,
        priority=priority,
        satisfiers=[NeedSatisfier(symbol=s) for s in symbols],
    )
    session.add(need)
    session.flush()
    return need


def get_need(session: Session, code: str) -> Need | None:
    return session.execute(
        select(Need).where(Need.code == str(code).upper())
    ).scalar_one_or_none()


def run_consumption(session: Session, tick_number: int) -> list[dict]:
    """Draw down satisfying holdings for every active need and rewrite each
    matching entity's satisfaction score. Returns one need_satisfied or
    need_unmet event per (need, entity)."""
    events: list[dict] = []
    needs = session.execute(
        select(Need).where(Need.is_active.is_(True)).order_by(Need.priority, Need.code)
    ).scalars().all()
    for need in needs:
        query = select(Entity).order_by(Entity.id)
        if need.entity_type is not None:
            query = query.where(Entity.entity_type == need.entity_type)
        required = need.quantity_per_tick
        for entity in session.execute(query).scalars():
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
            events.append({
                "type": "need_satisfied" if consumed == required else "need_unmet",
                "entity_id": entity.id,
                "need": need.code,
                "consumed": str(consumed.quantize(_QUANTUM)),
                "required": str(required),
                "satisfaction": str(satisfaction),
            })
    if events:
        session.flush()
    return events


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
