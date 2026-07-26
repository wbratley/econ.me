"""
Goods — optional per-symbol properties and the tick passes they drive.

A Good row attaches behaviour to a holding symbol: *auto-issue* (entities of
a given type are topped up to N units each tick) and *decay* (a fraction of
every holding is lost at end of tick). Symbols without a row keep working;
rows only add properties.

Auto-issue is a TOP-UP — holding = max(holding, N) — not an addition. That
prevents issuer-side hoarding without age-tracked inventory lots and never
destroys stock an entity bought; buyer-side banking is the accepted v1
trade-off (worlds can vote decay onto the symbol instead). It runs BEFORE
scripts, so fresh labor is sellable in the same tick's auction. Decay runs
AFTER the auction, so unsold perishables rot. The amount lost is quantized
(4dp, half-up), so dust holdings die instead of shrinking forever.

Both passes emit one summary event per good (entity_id None), never
per-entity — policies see the aggregate without the event feed drowning.

A good may also be a CONDITION (docs/design.md § conditions, see
conditions.py): modifies_pattern/modifies_factor scale holders' effective
quantities, incapacitates_at deactivates entities. Auto-issue is one of the
exactly two effective-quantity read sites: the top-up target is scaled by
the entity's held condition factors, which is how productivity loss reaches
the labor market — auto-issue runs at the top of the tick, so tick N's
labor is throttled by tick N−1's conditions. decay_per_tick doubles as a
condition's natural recovery rate.
"""

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import conditions
from .models import Entity, EntityStatus, EntityType, Good, Holding

_QUANTUM = Decimal("0.0001")


def create_good(
    session: Session,
    symbol: str,
    name: str = "",
    decay_per_tick: Decimal = Decimal("0"),
    auto_issue_quantity: Decimal = Decimal("0"),
    auto_issue_entity_type: EntityType | None = None,
    modifies_pattern: str | None = None,
    modifies_factor: Decimal | None = None,
    incapacitates_at: Decimal | None = None,
) -> Good:
    decay_per_tick = Decimal(decay_per_tick).quantize(_QUANTUM)
    auto_issue_quantity = Decimal(auto_issue_quantity).quantize(_QUANTUM)
    if not Decimal("0") <= decay_per_tick <= Decimal("1"):
        raise ValueError("decay_per_tick must be between 0 and 1")
    if auto_issue_quantity < 0:
        raise ValueError("auto_issue_quantity must be >= 0")
    if (modifies_pattern is None) != (modifies_factor is None):
        raise ValueError("modifies_pattern and modifies_factor go together")
    if modifies_factor is not None:
        modifies_factor = Decimal(modifies_factor).quantize(_QUANTUM)
        if modifies_factor < 0:
            raise ValueError("modifies_factor must be >= 0")
    if incapacitates_at is not None:
        incapacitates_at = Decimal(incapacitates_at).quantize(_QUANTUM)
        if incapacitates_at <= 0:
            raise ValueError("incapacitates_at must be positive")

    good = Good(
        symbol=symbol.upper(),
        name=name,
        decay_per_tick=decay_per_tick,
        auto_issue_quantity=auto_issue_quantity,
        auto_issue_entity_type=auto_issue_entity_type,
        modifies_pattern=modifies_pattern.upper() if modifies_pattern else None,
        modifies_factor=modifies_factor,
        incapacitates_at=incapacitates_at,
    )
    session.add(good)
    session.flush()
    return good


def get_good(session: Session, symbol: str) -> Good | None:
    return session.execute(
        select(Good).where(Good.symbol == str(symbol).upper())
    ).scalar_one_or_none()


def auto_issue(session: Session, tick_number: int) -> list[dict]:
    """Top up matching ACTIVE entities' holdings to each good's
    auto_issue_quantity, scaled by the entity's held condition factors
    (effective-quantity read site — a feverish smith is issued less
    LABOR-SMITH; the top-up never destroys stock, so an over-target holding
    is simply not topped up). Returns one auto_issue event per good that
    actually issued anything."""
    events: list[dict] = []
    goods = session.execute(
        select(Good).where(Good.auto_issue_quantity > 0).order_by(Good.symbol)
    ).scalars().all()
    modifiers_by_entity: dict[str, list] = {}
    for good in goods:
        query = select(Entity).where(Entity.status == EntityStatus.ACTIVE).order_by(Entity.id)
        if good.auto_issue_entity_type is not None:
            query = query.where(Entity.entity_type == good.auto_issue_entity_type)
        issued = Decimal("0")
        recipients = 0
        for entity in session.execute(query).scalars():
            if entity.id not in modifiers_by_entity:
                modifiers_by_entity[entity.id] = conditions.held_modifiers(session, entity.id)
            target = conditions.effective_quantity(
                good.auto_issue_quantity,
                conditions.effective_factor(
                    session, entity.id, good.symbol, modifiers_by_entity[entity.id]
                ),
            )
            holding = session.execute(
                select(Holding).where(
                    Holding.entity_id == entity.id, Holding.symbol == good.symbol
                )
            ).scalar_one_or_none()
            if holding is None:
                holding = Holding(entity_id=entity.id, symbol=good.symbol, quantity=Decimal("0"))
                session.add(holding)
            if holding.quantity < target:
                issued += target - holding.quantity
                holding.quantity = target
                recipients += 1
        if issued > 0:
            events.append({
                "type": "auto_issue",
                "entity_id": None,
                "symbol": good.symbol,
                "issued": str(issued),
                "recipients": recipients,
            })
    if events:
        session.flush()
    return events


def apply_decay(session: Session, tick_number: int) -> list[dict]:
    """Rot every holding of each perishable good. Returns one decay event per
    good that actually lost anything."""
    events: list[dict] = []
    goods = session.execute(
        select(Good).where(Good.decay_per_tick > 0).order_by(Good.symbol)
    ).scalars().all()
    for good in goods:
        holdings = session.execute(
            select(Holding)
            .where(Holding.symbol == good.symbol, Holding.quantity > 0)
            .order_by(Holding.entity_id)
        ).scalars().all()
        decayed = Decimal("0")
        holders = 0
        for holding in holdings:
            lost = (holding.quantity * good.decay_per_tick).quantize(
                _QUANTUM, rounding=ROUND_HALF_UP
            )
            if lost > 0:
                holding.quantity -= lost
                decayed += lost
                holders += 1
        if decayed > 0:
            events.append({
                "type": "decay",
                "entity_id": None,
                "symbol": good.symbol,
                "decayed": str(decayed),
                "holders": holders,
            })
    if events:
        session.flush()
    return events
