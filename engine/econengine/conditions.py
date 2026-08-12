"""
Conditions — consequences of unmet needs (docs/design.md § conditions).

A condition IS a Good: the third member of the entity-attached family after
skills and attributes. The indirection decouples causes from effects —
starvation and plague both credit COND-WEAK, and the labor throttle only
reads COND-WEAK. The engine owns the EFFECT MECHANISMS here; every
threshold, rate, and curve stays votable data (Good/Need rows, the estate
rule in world_settings).

A Good is a condition when it declares either condition property:

- modifies (pattern + factor): while any of the condition is held, the
  holder's EFFECTIVE quantity of matching symbols is held × factor. A fever
  halving smithing must not draw down SKILL-SMITH (that is atrophy's job,
  and it would be permanent) — it is a computed overlay. Factors multiply
  across held conditions (commutative, so determinism is free) and are read
  at exactly two sites: recipe requirement checks and auto-issue targets
  (which is how productivity loss reaches the labor market). Markets and
  consumption never see effective quantities.
- incapacitates_at: hold >= the threshold at end of tick and the engine
  deactivates the entity and applies the world's estate rule. Incapacity is
  the engine state; permanent death is world policy layered on top.

Cause mechanisms live elsewhere and mostly already existed: accumulation in
the consumption pass (needs.py), injury as a stochastic outcome branch,
recovery as decay_per_tick, healing as a recipe consuming the condition.
Note the skill-plateau math in reverse: proportional decay against a
constant grant converges to grant ÷ decay, so incapacitates_at must sit
BELOW that equilibrium or it never fires.

The estate rule (burn / heir / world treasury) is votable data; the
TRANSFER is engine mechanism, because no script may move a dead entity's
assets (the ownership invariant). Condition holdings are always burned —
an heir does not inherit the deceased's fever. Heir falls back to burn
when no active heir is designated; treasury falls back to burn when the
configured treasury entity is missing or inactive.
"""

from decimal import Decimal, ROUND_DOWN
from fnmatch import fnmatchcase

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Account, Entity, EntityStatus, Good, Holding, Order, OrderStatus, Parcel,
    Process, ProcessStatus, WorldSetting,
)

_QUANTUM = Decimal("0.0001")

ESTATE_RULE_KEY = "estate_rule"
ESTATE_POLICIES = ("burn", "heir", "treasury")


def is_condition(good: Good) -> bool:
    return good.modifies_pattern is not None or good.incapacitates_at is not None


# ---------------------------------------------------------------------------
# Effective-quantity overlay


def held_modifiers(session: Session, entity_id: str) -> list[tuple[str, Decimal]]:
    """(pattern, factor) for every modifying condition the entity holds any
    positive quantity of, symbol-ascending."""
    rows = session.execute(
        select(Good.modifies_pattern, Good.modifies_factor)
        .join(Holding, Holding.symbol == Good.symbol)
        .where(
            Good.modifies_pattern.is_not(None),
            Holding.entity_id == entity_id,
            Holding.quantity > 0,
        )
        .order_by(Good.symbol)
    ).all()
    return [(pattern, Decimal(factor)) for pattern, factor in rows]


def effective_factor(
    session: Session, entity_id: str, symbol: str,
    modifiers: list[tuple[str, Decimal]] | None = None,
) -> Decimal:
    """Product of the factors of every held condition whose pattern matches
    the symbol; 1 when none apply. Pass precomputed held_modifiers() when
    checking many symbols for one entity."""
    if modifiers is None:
        modifiers = held_modifiers(session, entity_id)
    factor = Decimal("1")
    for pattern, each in modifiers:
        if fnmatchcase(str(symbol).upper(), pattern):
            factor *= each
    return factor


def effective_quantity(quantity: Decimal, factor: Decimal) -> Decimal:
    # Rounded DOWN so a throttle never rounds back up over a threshold.
    return (quantity * factor).quantize(_QUANTUM, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------------
# Estate rule (votable data in world_settings)


def get_estate_rule(session: Session) -> dict:
    setting = session.get(WorldSetting, ESTATE_RULE_KEY)
    if setting is None or not isinstance(setting.value, dict):
        return {"policy": "burn"}
    return dict(setting.value)


def set_estate_rule(
    session: Session, policy: str, treasury_entity_id: str | None = None
) -> WorldSetting:
    if policy not in ESTATE_POLICIES:
        raise ValueError(f"estate policy must be one of {', '.join(ESTATE_POLICIES)}")
    if policy == "treasury":
        if treasury_entity_id is None:
            raise ValueError("treasury policy requires treasury_entity_id")
        if session.get(Entity, treasury_entity_id) is None:
            raise ValueError("unknown treasury entity")
    value: dict = {"policy": policy}
    if treasury_entity_id is not None:
        value["treasury_entity_id"] = treasury_entity_id
    setting = session.get(WorldSetting, ESTATE_RULE_KEY)
    if setting is None:
        setting = WorldSetting(key=ESTATE_RULE_KEY, value=value)
        session.add(setting)
    else:
        setting.value = value
    session.flush()
    return setting


def _estate_recipient(session: Session, entity: Entity) -> tuple[str, Entity | None]:
    """Resolve the configured rule against this entity: (applied policy,
    recipient). Falls back to burn when the designated recipient is missing,
    inactive, or the entity itself."""
    rule = get_estate_rule(session)
    policy = rule.get("policy", "burn")
    recipient_id = (
        entity.heir_id if policy == "heir" else rule.get("treasury_entity_id")
    )
    if policy in ("heir", "treasury") and recipient_id and recipient_id != entity.id:
        recipient = session.get(Entity, recipient_id)
        if recipient is not None and recipient.status == EntityStatus.ACTIVE:
            return policy, recipient
    return "burn", None


# ---------------------------------------------------------------------------
# Incapacity pass


def run_incapacity(session: Session, tick_number: int) -> list[dict]:
    """Deactivate every active entity that has crossed a death threshold and
    apply the estate rule. Runs AFTER decay, so this tick's natural recovery
    counts before the threshold is read.

    Two thresholds are checked, in order:
      1. CONDITIONS (``incapacitates_at``) -- a held Good at/over its
         declared level (symbol-ascending, then entity id-ascending);
      2. AGE (Step 6d) -- a non-NULL ``lifespan`` reached by
         ``age = tick_number - birth_tick`` (entity id-ascending).

    An entity crossing a condition threshold and its lifespan in one tick
    is incapacitated by whichever fires FIRST (the condition), so the
    recorded cause is deterministic. Both paths share ``_incapacitate``:
    an old-age death is, to the engine, indistinguishable from a
    starvation death (same event, same estate, same insurance trigger) --
    only the ``condition`` cause label differs (``"age"`` vs the Good
    symbol). Entities with a NULL ``lifespan`` or a NULL ``birth_tick``
    are immortal by age."""
    events: list[dict] = []
    goods = session.execute(
        select(Good).where(Good.incapacitates_at.is_not(None)).order_by(Good.symbol)
    ).scalars().all()
    for good in goods:
        holdings = session.execute(
            select(Holding)
            .join(Entity, Entity.id == Holding.entity_id)
            .where(
                Holding.symbol == good.symbol,
                Holding.quantity >= good.incapacitates_at,
                Entity.status == EntityStatus.ACTIVE,
            )
            .order_by(Holding.entity_id)
        ).scalars().all()
        for holding in holdings:
            entity = holding.entity
            if entity.status != EntityStatus.ACTIVE:  # crossed an earlier threshold
                continue
            events.append(_incapacitate(
                session, entity, tick_number,
                condition=good.symbol,
                quantity=holding.quantity,
                threshold=good.incapacitates_at,
            ))
    # Age-based mortality (Step 6d). Only entities with BOTH a lifespan
    # and a birth_tick can die of old age; the rest are immortal by age.
    rows = session.execute(
        select(Entity)
        .where(
            Entity.status == EntityStatus.ACTIVE,
            Entity.lifespan.is_not(None),
            Entity.birth_tick.is_not(None),
            Entity.lifespan <= (tick_number - Entity.birth_tick),
        )
        .order_by(Entity.id)
    ).scalars().all()
    for entity in rows:
        if entity.status != EntityStatus.ACTIVE:  # killed by a condition above
            continue
        events.append(_incapacitate(
            session, entity, tick_number,
            condition="age",
            quantity=tick_number - entity.birth_tick,
            threshold=entity.lifespan,
        ))
    if events:
        session.flush()
    return events


def _incapacitate(
    session: Session, entity: Entity, tick_number: int,
    *, condition: str, quantity, threshold,
) -> dict:
    entity.status = EntityStatus.INCAPACITATED
    entity.incapacitated_tick = tick_number
    quantity_at_threshold = quantity  # the estate zeroes holdings/balances below

    for order in session.execute(
        select(Order)
        .where(Order.entity_id == entity.id, Order.status == OrderStatus.OPEN)
        .order_by(Order.created_at, Order.id)
    ).scalars():
        order.status = OrderStatus.CANCELLED
        order.cancel_reason = "entity incapacitated"
    for process in session.execute(
        select(Process)
        .where(Process.entity_id == entity.id, Process.status == ProcessStatus.RUNNING)
        .order_by(Process.created_at, Process.id)
    ).scalars():
        process.status = ProcessStatus.CANCELLED  # inputs are forfeit, as ever

    policy, recipient = _estate_recipient(session, entity)
    estate = _apply_estate(session, entity, recipient)

    return {
        "type": "entity_incapacitated",
        "entity_id": entity.id,
        "condition": condition,
        "quantity": str(quantity_at_threshold),
        "threshold": str(threshold),
        "estate_policy": policy,
        "recipient_id": recipient.id if recipient else None,
        **estate,
    }


def _apply_estate(session: Session, entity: Entity, recipient: Entity | None) -> dict:
    """Move (or burn, when recipient is None) everything the entity has.
    Condition holdings always burn. Returns event fields summarising what
    moved."""
    condition_symbols = {
        symbol for (symbol,) in session.execute(
            select(Good.symbol).where(
                (Good.modifies_pattern.is_not(None)) | (Good.incapacitates_at.is_not(None))
            )
        ).all()
    }

    goods_moved = Decimal("0")
    goods_burned = Decimal("0")
    for holding in session.execute(
        select(Holding)
        .where(Holding.entity_id == entity.id, Holding.quantity > 0)
        .order_by(Holding.symbol)
    ).scalars():
        if recipient is not None and holding.symbol not in condition_symbols:
            _credit_holding(session, recipient, holding.symbol, holding.quantity)
            goods_moved += holding.quantity
        else:
            goods_burned += holding.quantity
        holding.quantity = Decimal("0")

    money_moved = Decimal("0")
    money_burned = Decimal("0")
    for account in session.execute(
        select(Account)
        .where(Account.entity_id == entity.id, Account.balance != 0)
        .order_by(Account.currency, Account.id)
    ).scalars():
        if recipient is not None:
            _credit_account(session, recipient, account.currency, account.balance)
            money_moved += account.balance
        else:
            money_burned += account.balance  # buried with the dead: supply shrinks
        account.balance = Decimal("0")

    parcels_moved = 0
    for parcel in session.execute(
        select(Parcel)
        .where(Parcel.owner_id == entity.id)
        .order_by(Parcel.created_at, Parcel.id)
    ).scalars():
        # the entity's bound processes were just cancelled, and only the
        # controller can bind a process, so no running process pins these
        parcel.owner_id = recipient.id if recipient else None  # None = unclaimed
        parcels_moved += 1

    return {
        "goods_transferred": str(goods_moved.quantize(_QUANTUM)),
        "goods_burned": str(goods_burned.quantize(_QUANTUM)),
        "money_transferred": str(money_moved.quantize(_QUANTUM)),
        "money_burned": str(money_burned.quantize(_QUANTUM)),
        "parcels": parcels_moved,
    }


def _credit_holding(session: Session, entity: Entity, symbol: str, quantity: Decimal) -> None:
    holding = session.execute(
        select(Holding).where(Holding.entity_id == entity.id, Holding.symbol == symbol)
    ).scalar_one_or_none()
    if holding is None:
        holding = Holding(entity_id=entity.id, symbol=symbol, quantity=Decimal("0"))
        session.add(holding)
    holding.quantity += quantity


def _credit_account(session: Session, entity: Entity, currency: str, amount: Decimal) -> None:
    account = session.execute(
        select(Account)
        .where(Account.entity_id == entity.id, Account.currency == currency)
        .order_by(Account.id)
    ).scalars().first()
    if account is None:
        account = Account(entity_id=entity.id, currency=currency, balance=Decimal("0"))
        session.add(account)
    account.balance += amount
