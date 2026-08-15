from datetime import datetime, timezone
from decimal import Decimal
import os
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import scripting
from .capabilities import LEVY, LEGISLATE, MONETARY_AUTHORITY, SET_FISCAL_POLICY, AMEND_CONSTITUTION, SEIZE, GRANT_CAPABILITY, SPAWN
from .models.entity import Entity, EntityType, EntityStatus
from .models.account import Account
from .models.script import Script, ScriptType
from .models.transaction import Transaction, TransactionType
from .models.proposal import Proposal, Vote, ProposalStatus, VoteChoice, ProposalType
from .models.parcel import Parcel
from .models import WorldSetting


class InsufficientFundsError(ValueError):
    pass


class CurrencyMismatchError(ValueError):
    pass


class MissingCapabilityError(ValueError):
    """A privileged action was attempted by an entity that lacks the
    required capability.

    Defense in depth: `scripting.resolve_intent` rejects the same cases
    earlier, at the capability gate, so this only fires for direct
    in-process callers of the service layer (tests, the tick engine,
    future policy scripts). Subclasses ``ValueError`` so the intent
    resolver's broad ``except ValueError`` reports it cleanly.
    """

    def __init__(self, entity_id: str, capability: str):
        self.entity_id = entity_id
        self.capability = capability
        super().__init__(f"entity {entity_id!r} lacks capability {capability!r}")


class NotMonetaryAuthorityError(ValueError):
    # Predates the generic MissingCapabilityError; left as its own class so
    # existing callers/tests keep catching it by name. New privileged
    # actions (levy, and later seize/set_fiscal_policy) raise
    # MissingCapabilityError directly.
    pass


class OwnershipError(ValueError):
    """Raised by the autonomy path (``set_entity_behaviour``) when the
    acting user does not own the target entity (``docs/game.md`` §6).

    The autonomy path's guard is *ownership* where ``set_script``'s is the
    ``legislate`` capability: a player rewriting their own entity's
    BEHAVIOUR script needs no vote and no capability, only proof that they
    own the entity. This is its refusal, parallel to
    ``MissingCapabilityError``. Defense in depth: the API checks ownership
    (``_own_entity``) before calling the service; the service re-checks.
    """

    def __init__(self, owner_id: str, entity_id: str):
        self.owner_id = owner_id
        self.entity_id = entity_id
        super().__init__(f"user {owner_id!r} does not own entity {entity_id!r}")


def create_entity(session: Session, name: str, entity_type: EntityType) -> Entity:
    entity = Entity(name=name, entity_type=entity_type)
    # Birth tick = the latest committed tick at creation. An entity spawned
    # at genesis (before tick 1) is born at tick 0; one created between ticks
    # is born at the last-completed tick. Set once, never mutated -- age is
    # derived (ctx.tick - birth_tick) and unforgeable (Step 6,
    # docs/actors.md).
    entity.birth_tick = scripting._latest_tick_number(session)
    session.add(entity)
    session.flush()
    return entity


def create_account(
    session: Session,
    entity: Entity,
    currency: str,
    initial_balance: Decimal = Decimal("0"),
) -> Account:
    account = Account(entity=entity, currency=currency.upper(), balance=initial_balance)
    session.add(account)
    session.flush()
    return account


def deposit(
    session: Session,
    account: Account,
    amount: Decimal,
    reference: str,
    date: datetime | None = None,
) -> Transaction:
    if amount <= 0:
        raise ValueError("amount must be positive")
    op = _op("deposit", account, amount, reference)
    scripting.fire_validators(session, op)
    tx = Transaction(
        account=account,
        date=date or datetime.now(timezone.utc),
        amount=amount,
        tx_type=TransactionType.CREDIT,
        to_account_id=account.id,
        reference=reference,
    )
    account.balance += amount
    session.add(tx)
    session.flush()
    scripting.fire_hooks(session, {**op, "transaction_ids": [tx.id]})
    return tx


def withdraw(
    session: Session,
    account: Account,
    amount: Decimal,
    reference: str,
    date: datetime | None = None,
) -> Transaction:
    if amount <= 0:
        raise ValueError("amount must be positive")
    if account.balance < amount:
        raise InsufficientFundsError(
            f"account {account.id} has {account.balance} {account.currency}, need {amount}"
        )
    op = _op("withdraw", account, amount, reference)
    scripting.fire_validators(session, op)
    tx = Transaction(
        account=account,
        date=date or datetime.now(timezone.utc),
        amount=amount,
        tx_type=TransactionType.DEBIT,
        from_account_id=account.id,
        reference=reference,
    )
    account.balance -= amount
    session.add(tx)
    session.flush()
    scripting.fire_hooks(session, {**op, "transaction_ids": [tx.id]})
    return tx


def transfer(
    session: Session,
    from_account: Account,
    to_account: Account,
    amount: Decimal,
    reference: str,
    date: datetime | None = None,
) -> tuple[Transaction, Transaction]:
    if amount <= 0:
        raise ValueError("amount must be positive")
    if from_account.currency != to_account.currency:
        raise CurrencyMismatchError(
            f"cannot transfer between {from_account.currency} and {to_account.currency}"
        )
    if from_account.balance < amount:
        raise InsufficientFundsError(
            f"account {from_account.id} has {from_account.balance} {from_account.currency}, need {amount}"
        )
    op = {
        "type": "transfer",
        "entity_id": from_account.entity_id,
        "from_account_id": from_account.id,
        "to_account_id": to_account.id,
        "amount": str(amount),
        "currency": from_account.currency,
        "reference": reference,
    }
    scripting.fire_validators(session, op)
    ts = date or datetime.now(timezone.utc)
    debit = Transaction(
        account=from_account,
        date=ts,
        amount=amount,
        tx_type=TransactionType.DEBIT,
        from_account_id=from_account.id,
        to_account_id=to_account.id,
        reference=reference,
    )
    credit = Transaction(
        account=to_account,
        date=ts,
        amount=amount,
        tx_type=TransactionType.CREDIT,
        from_account_id=from_account.id,
        to_account_id=to_account.id,
        reference=reference,
    )
    from_account.balance -= amount
    to_account.balance += amount
    session.add_all([debit, credit])
    session.flush()
    scripting.fire_hooks(session, {**op, "transaction_ids": [debit.id, credit.id]})
    return debit, credit


def levy(
    session: Session,
    authority: Entity,
    from_account: Account,
    to_account: Account,
    amount: Decimal,
    rule_ref: str,
    reference: str = "",
    date: datetime | None = None,
) -> tuple[Transaction, Transaction]:
    """Compel a money transfer out of an account the authority does not own.

    The privilege layer above ownership (docs/actors.md Fork 1C). Where
    ``transfer`` requires the source account to be the actor's own, a levy
    moves the taxpayer's money *by engine authority* — generalising the
    estate rule (``conditions._apply_estate``), which moves a dead
    entity's assets by the same authority, from death to enacted policy.

    All the safety lives in the gating, not the movement:

    - ``authority`` must hold the ``levy`` capability (checked here as
      defense in depth, and earlier by ``resolve_intent`` at the
      capability gate, the same boundary that enforces ownership).
    - ``to_account`` must belong to the authority — the state collects
      into its own treasury. An authority may levy *from* others but only
      *into* its own accounts; redirecting between two third parties is
      not levy, it is seizure under a different rule.
    - ``rule_ref`` identifies the votable rule under which the levy is
      taken (a ``WorldSetting`` key or policy name). It rides ``ctx.op``
      so a VALIDATOR can veto an illegal levy — e.g. that the amount
      exceeds what the schedule permits. Validators are fail-closed, so a
      broken policy gate never silently seizes.

    Movement is money-conserving (a DEBIT/CREDIT pair, like ``transfer``);
    the levy-ness is carried by op-type and ``rule_ref``, not a new
    transaction flavour. Unlike the estate sweep this records transactions
    and fires validators, because a levy is a discrete act by a capable
    actor under a declared rule, not a bulk deterministic sweep.
    """
    if not authority.has_capability(LEVY):
        raise MissingCapabilityError(authority.id, LEVY)
    if amount <= 0:
        raise ValueError("amount must be positive")
    if to_account.entity_id != authority.id:
        # the authority may only levy INTO its own account
        raise ValueError("authority does not own recipient account")
    if from_account.currency != to_account.currency:
        raise CurrencyMismatchError(
            f"cannot levy between {from_account.currency} and {to_account.currency}"
        )
    if from_account.balance < amount:
        raise InsufficientFundsError(
            f"account {from_account.id} has {from_account.balance} "
            f"{from_account.currency}, levy requires {amount}"
        )
    op = {
        "type": "levy",
        "entity_id": authority.id,
        "from_account_id": from_account.id,
        "to_account_id": to_account.id,
        "amount": str(amount),
        "currency": from_account.currency,
        "reference": reference,
        "rule_ref": rule_ref,
    }
    scripting.fire_validators(session, op)
    ts = date or datetime.now(timezone.utc)
    debit = Transaction(
        account=from_account,
        date=ts,
        amount=amount,
        tx_type=TransactionType.DEBIT,
        from_account_id=from_account.id,
        to_account_id=to_account.id,
        reference=reference,
    )
    credit = Transaction(
        account=to_account,
        date=ts,
        amount=amount,
        tx_type=TransactionType.CREDIT,
        from_account_id=from_account.id,
        to_account_id=to_account.id,
        reference=reference,
    )
    from_account.balance -= amount
    to_account.balance += amount
    session.add_all([debit, credit])
    session.flush()
    scripting.fire_hooks(session, {**op, "transaction_ids": [debit.id, credit.id]})
    return debit, credit


def seize(
    session: Session,
    authority: Entity,
    from_entity: Entity,
    *,
    symbol: str | None = None,
    quantity: Decimal | None = None,
    parcel_ids: list[str] | None = None,
    to_entity: Entity | None = None,
    rule_ref: str,
    reference: str = "",
    date: datetime | None = None,
) -> dict:
    """Compel movement of goods and/or parcels out of an entity the
    authority does not own, into a declared recipient.

    The goods/parcels analogue of :func:`levy` (which is the money half):
    where a levy compels a money transfer, a seizure expropriates the
    things themselves — tax-in-kind, confiscation, eminent domain. It is
    the second half of the enforced-state-action primitive
    (``docs/actors.md`` Fork 1C); the estate sweep
    (``conditions._apply_estate``) already moves goods and parcels by
    engine authority, and seize generalises that from death to policy.

    All the safety lives in the gating, not the movement:

    - ``authority`` must hold the ``seize`` capability (checked here as
      defense in depth, and earlier by ``resolve_intent`` at the
      capability gate, the same boundary that enforces ownership).
    - the recipient defaults to the authority (the state seizes into its
      own); a different ``to_entity`` is the redistribution case — the
      authority is the actor and the recipient is declared, and a
      VALIDATOR may constrain where seized assets may go.
    - goods movement is goods-conserving: the quantity leaves the
      victim's holding (a debit that raises ``InsufficientHoldingsError``
      if the victim is short — fail-closed) and enters the recipient's.
      Like ``transfer_parcel`` it records no ``Transaction`` (transactions
      are money-only); the movement is carried by the holding rows.
    - parcels are reassigned via the same ownership flip as
      ``parcels.grant_parcel`` (which refuses a parcel with running
      processes bound to it); the victim must currently own each parcel.
    - ``rule_ref`` rides ``ctx.op`` so a VALIDATOR can veto an illegal
      seizure — e.g. that the quantity exceeds what the schedule permits
      — exactly as for levy. Validators are fail-closed, so a broken
      policy gate never silently expropriates.

    At least one of (``symbol``+``quantity``) or ``parcel_ids`` must be
    given; both may be given (seize a farm and its standing crop in one
    act). Returns a summary of what moved.
    """
    # markets imports services (circular) — defer, like fiscal/constitution.
    from . import markets, parcels

    if not authority.has_capability(SEIZE):
        raise MissingCapabilityError(authority.id, SEIZE)
    wants_goods = symbol is not None or quantity is not None
    wants_parcels = bool(parcel_ids)
    if not wants_goods and not wants_parcels:
        raise ValueError("seize needs goods (symbol+quantity) or parcels (parcel_ids)")
    if wants_goods and (symbol is None or quantity is None):
        raise ValueError("goods seizure needs both symbol and quantity")
    if wants_goods and quantity <= 0:
        raise ValueError("quantity must be positive")
    recipient = to_entity or authority

    op = {
        "type": "seize",
        "entity_id": authority.id,
        "from_entity_id": from_entity.id,
        "to_entity_id": recipient.id,
        "symbol": symbol,
        "quantity": str(quantity) if quantity is not None else None,
        "parcel_ids": list(parcel_ids) if parcel_ids else [],
        "reference": reference,
        "rule_ref": rule_ref,
    }
    scripting.fire_validators(session, op)
    _ = date or datetime.now(timezone.utc)  # goods/parcels are untimed; parity with levy

    goods_moved = Decimal("0")
    if wants_goods:
        # debit the victim first (raises InsufficientHoldingsError if
        # short), then credit the recipient — goods-conserving, and atomic
        # under the caller's savepoint.
        markets.adjust_holding(session, from_entity, symbol, -quantity)
        markets.adjust_holding(session, recipient, symbol, quantity)
        goods_moved = quantity

    parcels_moved = 0
    for parcel_id in parcel_ids or []:
        parcel = session.get(Parcel, parcel_id)
        if parcel is None:
            raise ValueError("unknown parcel")
        if parcel.owner_id != from_entity.id:
            raise ValueError("entity does not own parcel")
        parcels.grant_parcel(session, parcel, recipient)
        parcels_moved += 1

    summary = {
        "goods_symbol": symbol,
        "goods_quantity": str(goods_moved),
        "parcels": parcels_moved,
        "to_entity_id": recipient.id,
    }
    scripting.fire_hooks(session, {**op, **summary})
    return summary


class ServerCapExceededError(ValueError):
    """A server-tier (non-votable) entity cap was reached.

    Distinct from a validator veto: this is the operator's physical ceiling
    (capacity/fairness), enforced in the engine spawn path the way the
    balance check enforces solvency -- no vote reaches it. Subclasses
    ValueError so resolve_intent's broad ``except ValueError`` reports it
    as a clean rejection.
    """
    pass


def _server_cap(name: str) -> int | None:
    """Read an integer server-cap env var, or None if unset/invalid."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _enforce_server_caps(session: Session, owner_id: str | None) -> None:
    """Tier B of the three spawn gates: the operator's non-votable ceiling.

    Three independent caps, each optional (default unbounded):
      * ``ECON_MAX_ACTIVE_ENTITIES`` -- the binding capacity bound, since
        every ACTIVE entity runs its BEHAVIOUR script each tick;
      * ``ECON_MAX_ENTITIES`` -- total rows (the dead still take storage);
      * ``ECON_MAX_ENTITIES_PER_OWNER`` -- fairness (no one player hogs
        entity slots).
    Checked before a VALIDATOR runs (tier C): a physical limit is a hard
    floor, so failing fast keeps a saturated world from even consulting
    policy. None of these is a validator (a world could vote its own
    cap-checker out) or a WorldSetting (governance could amend it) -- that
    is exactly why the check lives here, in the mechanism.
    """
    active_cap = _server_cap("ECON_MAX_ACTIVE_ENTITIES")
    if active_cap is not None:
        active = session.execute(
            select(func.count()).select_from(Entity)
            .where(Entity.status == EntityStatus.ACTIVE)
        ).scalar_one()
        if active >= active_cap:
            raise ServerCapExceededError(
                f"server cap reached: {active} active entities (limit {active_cap})")
    total_cap = _server_cap("ECON_MAX_ENTITIES")
    if total_cap is not None:
        total = session.execute(
            select(func.count()).select_from(Entity)
        ).scalar_one()
        if total >= total_cap:
            raise ServerCapExceededError(
                f"server cap reached: {total} entities (limit {total_cap})")
    per_owner_cap = _server_cap("ECON_MAX_ENTITIES_PER_OWNER")
    if per_owner_cap is not None and owner_id is not None:
        owned = session.execute(
            select(func.count()).select_from(Entity)
            .where(Entity.owner_id == owner_id)
        ).scalar_one()
        if owned >= per_owner_cap:
            raise ServerCapExceededError(
                f"server cap reached: owner holds {owned} entities (limit {per_owner_cap})")


def spawn_entity(
    session: Session,
    caller: Entity,
    *,
    parents: list[str],
    owner_id: str | None = None,
    currency: str = "USD",
    name: str = "entity",
    entity_type: EntityType = EntityType.INDIVIDUAL,
    lifespan: int | None = None,
    reference: str = "",
) -> dict:
    """Bring a new entity into being during a tick -- the one genuinely new
    mechanism of Step 6c (``docs/actors.md``).

    Where :func:`create_entity` is the platform/setup path (entities minted
    between ticks at world genesis), this is the mid-tick path: a caller
    that holds the ``spawn`` capability (validated by the caller; checked
    here as defense in depth) brings a child into the world. The child is
    stamped with immutable provenance (``parents``), an owner (defaulting
    to the caller's owner), a birth tick (the executing tick, so age never
    disagrees with ctx.tick), and an always-created empty account.

    All the safety lives in the gating, not the creation:
      * ``caller`` must hold ``spawn`` (checked here, and earlier by
        ``resolve_intent`` at the capability gate) -- tier A;
      * the server hard caps (active / total / per-owner) are enforced
        before any VALIDATOR runs -- tier B, a non-votable engine invariant;
      * a VALIDATOR may veto the spawn (population cap, wrong parents,
        missing permit) under ``ctx.op`` -- tier C, the world's votable
        rules. Validators are fail-closed, so a broken policy gate never
        silently births.

    The mechanism does NOT endow. Starting wealth/goods are a transfer the
    spawning script or a HOOK makes *after* spawn -- how much a child
    inherits is policy, not mechanism, the way the levy rate is data.
    Returns the new entity and account ids.
    """
    if not caller.has_capability(SPAWN):
        raise MissingCapabilityError(caller.id, SPAWN)

    owner_id = owner_id if owner_id is not None else caller.owner_id
    _enforce_server_caps(session, owner_id)

    op = {
        "type": "spawn_entity",
        "entity_id": caller.id,
        "parents": list(parents),
        "owner_id": owner_id,
        "currency": str(currency).upper(),
        "name": name,
        "entity_type": entity_type.value,
        "lifespan": lifespan,
        "reference": reference,
    }
    scripting.fire_validators(session, op)

    child = Entity(name=name, entity_type=entity_type)
    child.birth_tick = scripting._executing_tick(session)
    child.parents = list(parents) if parents else None
    child.owner_id = owner_id
    child.lifespan = lifespan  # None = immortal (the default); Step 6d
    session.add(child)
    session.flush()

    account = Account(entity=child, currency=str(currency).upper(), balance=Decimal("0"))
    session.add(account)
    session.flush()

    summary = {
        "child_id": child.id,
        "account_id": account.id,
        "parents": list(parents),
        "owner_id": owner_id,
    }
    scripting.fire_hooks(session, {**op, **summary})
    return summary


def set_fiscal_policy(
    session: Session,
    authority: Entity,
    policy: dict,
    reference: str = "",
) -> WorldSetting:
    """Replace the government's fiscal-policy dict (docs/actors.md step 3,
    Fork 4B). The *data* half of the mechanism/data/policy split: this is
    the votable surface citizens (or a legislature) change without touching
    code; a government POLICY script reads the result via
    ``ctx.query.fiscal_policy()`` and turns it into ``levy`` calls.

    All the safety is in the gating, not the storage:

    - ``authority`` must hold the ``set_fiscal_policy`` capability, checked
      here as defense in depth (and earlier by ``resolve_intent`` at the
      capability gate). This replaces admin god-mode for fiscal policy —
      the power is held by the role, not by a superuser.
    - a VALIDATOR may veto the change (fail-closed). Because the op carries
      the proposed ``policy`` dict, a validator becomes a *constitutional
      constraint*: it can cap a rate, forbid a schedule, or block any
      change at all. A broken policy gate never silently changes policy.
    - ``policy`` is replaced wholesale (not merged) so a change is atomic
      and auditable — readers never see a half-updated schedule.

    The engine stays deliberately dumb about *what* the dict contains —
    rates, bands, UBI schedules are the POLICY script's concern. Storing
    arbitrary votable data is the point.
    """
    from . import fiscal

    if not authority.has_capability(SET_FISCAL_POLICY):
        raise MissingCapabilityError(authority.id, SET_FISCAL_POLICY)
    if not isinstance(policy, dict):
        raise ValueError("fiscal policy must be a JSON object")
    op = {
        "type": "set_fiscal_policy",
        "entity_id": authority.id,
        "policy": policy,
        "reference": reference,
    }
    scripting.fire_validators(session, op)
    setting = fiscal.set_fiscal_policy(session, policy)
    scripting.fire_hooks(session, {**op, "setting_key": setting.key})
    return setting


def issue_money(
    session: Session,
    account: Account,
    amount: Decimal,
    reference: str,
    date: datetime | None = None,
) -> Transaction:
    if not account.entity.has_capability(MONETARY_AUTHORITY):
        raise NotMonetaryAuthorityError(
            f"entity {account.entity_id} is not a monetary authority"
        )
    if amount <= 0:
        raise ValueError("amount must be positive")
    op = _op("issue_money", account, amount, reference)
    scripting.fire_validators(session, op)
    tx = Transaction(
        account=account,
        date=date or datetime.now(timezone.utc),
        amount=amount,
        tx_type=TransactionType.ISSUANCE,
        to_account_id=account.id,
        reference=reference,
    )
    account.balance += amount
    session.add(tx)
    session.flush()
    scripting.fire_hooks(session, {**op, "transaction_ids": [tx.id]})
    return tx


def retire_money(
    session: Session,
    account: Account,
    amount: Decimal,
    reference: str,
    date: datetime | None = None,
) -> Transaction:
    if not account.entity.has_capability(MONETARY_AUTHORITY):
        raise NotMonetaryAuthorityError(
            f"entity {account.entity_id} is not a monetary authority"
        )
    if amount <= 0:
        raise ValueError("amount must be positive")
    if account.balance < amount:
        raise InsufficientFundsError(
            f"account {account.id} has {account.balance} {account.currency}, need {amount}"
        )
    op = _op("retire_money", account, amount, reference)
    scripting.fire_validators(session, op)
    tx = Transaction(
        account=account,
        date=date or datetime.now(timezone.utc),
        amount=amount,
        tx_type=TransactionType.RETIREMENT,
        from_account_id=account.id,
        reference=reference,
    )
    account.balance -= amount
    session.add(tx)
    session.flush()
    scripting.fire_hooks(session, {**op, "transaction_ids": [tx.id]})
    return tx


def set_script(
    session: Session,
    authority: Entity,
    script_type: ScriptType,
    lineage_id: str,
    source: str,
    entity_id: str | None = None,
    description: str = "",
    timeout_ms: int = 100,
    reference: str = "",
) -> Script:
    """Governed script lifecycle: enact a new version of a law.

    The privileged write surface for POLICY / BEHAVIOUR / HOOK scripts
    (docs/actors.md step 4a-1). Where the admin API creates scripts by
    operator fiat, ``set_script`` is the *enactable* path — the one a
    proposal->vote->enact cycle (4a-ii) and an electorate will drive. It
    is to scripts what ``set_fiscal_policy`` is to parameters: the
    governed write surface, capability-gated rather than admin-gated.

    Semantics are retire-old + activate-new, never in-place edit, so every
    enacted law leaves a lineage of retired predecessors — auditable,
    revertible, sandbox-triable. ``lineage_id`` is the stable identity of
    the law across versions; ``name`` is auto-versioned per row
    (``{lineage_id}#{n}``) to keep the existing per-row uniqueness while
    ``lineage_id`` carries the human meaning. "The current law" is the one
    row with ``lineage_id=X AND is_active=True``.

    Safety: ``authority`` must hold the ``legislate`` capability (checked
    here as defense in depth, and at the capability gate in
    ``resolve_intent``). VALIDATOR scripts are *excluded* — they are the
    constitution, amendable only through the constitutional process (4b),
    never by ordinary legislation. No validator gates ``set_script``
    itself, because validators are precisely the thing this op is kept
    away from; the capability, not a validator, is the gate. A HOOK fires
    after enactment for audit.
    """
    if not authority.has_capability(LEGISLATE):
        raise MissingCapabilityError(authority.id, LEGISLATE)
    if script_type == ScriptType.VALIDATOR:
        raise ValueError(
            "cannot set_script on a validator; amend the constitution instead (step 4b)"
        )
    if entity_id is not None:
        # The immutable tier: both governed paths -- legislation here, and
        # autonomy in ``set_entity_behaviour`` -- refuse a fixed entity
        # (docs/game.md §4). Its behaviour is operator-set world-physics.
        bound = session.get(Entity, entity_id)
        if bound is not None and bound.is_fixed:
            raise ValueError(
                f"entity {entity_id} is fixed (immutable tier); "
                "legislation may not change its behaviour"
            )
    if not lineage_id:
        raise ValueError("lineage_id is required")

    # retire the currently-active version of this lineage (if any)
    current = session.execute(
        select(Script).where(
            Script.lineage_id == lineage_id,
            Script.is_active.is_(True),
        )
    ).scalars().first()
    if current is not None:
        current.is_active = False

    # auto-version the per-row name; lineage_id carries the stable identity
    version_count = session.execute(
        select(func.count()).select_from(Script).where(Script.lineage_id == lineage_id)
    ).scalar_one()

    new_script = Script(
        name=f"{lineage_id}#{version_count + 1}",
        description=description,
        script_type=script_type,
        source=source,
        is_active=True,
        timeout_ms=timeout_ms,
        entity_id=entity_id,
        lineage_id=lineage_id,
    )
    session.add(new_script)
    session.flush()

    op = {
        "type": "set_script",
        "entity_id": authority.id,
        "script_type": script_type.value,
        "lineage_id": lineage_id,
        "script_id": new_script.id,
        "retired_script_id": current.id if current is not None else None,
        "reference": reference,
    }
    scripting.fire_hooks(session, op)
    return new_script


def set_entity_behaviour(
    session: Session,
    entity: Entity,
    source: str,
    *,
    owner_id: str,
    description: str = "",
    timeout_ms: int = 100,
    reference: str = "",
) -> tuple[Script, list[str]]:
    """Ownership-gated autonomy path -- a player rewrites the BEHAVIOUR
    script of an entity they own (docs/game.md §6).

    This is *autonomy, not legislation*. It needs no vote and no
    capability, only proof of ownership. It is the distinct counterpart to
    ``set_script`` (the LEGISLATE-gated governed surface for polity-owned
    entities): where legislation asks "may the polity impose this?",
    autonomy asks "does the owner of this entity want it?".

      * Scope -- BEHAVIOUR only. The signature fixes ``script_type``; a
        player may not touch POLICY / VALIDATOR / HOOK scripts, which are
        legislation or constitution.
      * Authorisation -- ``entity.owner_id`` must equal ``owner_id``. A
        server-owned entity (no owner) fails this by construction.
      * Refusal -- entities marked ``is_fixed`` are refused: they are the
        immutable tier (NPC labourers, world-physics), writable only by the
        operator at content time.
      * Safety -- unchanged. The money-scope invariant still binds, so an
        autonomy script can spend only its own entity's money; capabilities
        (issue/seize/levy) still gate privileged action.
      * Lint -- submit-time strictness (docs/scripting.md section 4,
        Phase 3): the source is checked against the SAME injected tiers
        and the SAME strict standard the install gate applies to operator
        content, BEFORE anything is retired or stored. A script that
        cannot run (syntax error, or vocabulary that is not injected --
        the nil-call trap that zombied the first live demo's founder) is
        refused with ``ScriptRejected`` and the entity keeps its current
        behaviour. Warnings (synthetic-ctx errors a healthy script can
        still produce) ride back to the caller with the new script.

    Returns ``(script, lint_warnings)`` -- the warnings are informational;
    the script is active either way.

    Semantics match ``set_script``: retire-old + activate-new within an
    entity-scoped lineage (``behaviour:{entity.id}``). Because "my entity's
    behaviour" is singular, every currently-active BEHAVIOUR on the entity
    is retired and the new source becomes the one active behaviour -- the
    entity ends each edit with exactly the script the owner set, whatever
    path set the previous one.
    """
    if entity.owner_id != owner_id:
        raise OwnershipError(owner_id, entity.id)
    if entity.is_fixed:
        raise ValueError(
            f"entity {entity.id} is fixed (immutable tier); "
            "behaviour is operator-set world-physics, not player-editable"
        )
    if not source.strip():
        raise ValueError("source is required")

    # Submit-time lint, BEFORE any mutation: a refusal must leave the
    # entity's current behaviour untouched. One vocabulary source with
    # the tick loop -- the lint cannot drift from what runs.
    problems, warnings = scripting.check_player_script(
        source, scripting.get_world_libraries(session))
    if problems:
        raise scripting.ScriptRejected(problems)

    lineage_id = f"behaviour:{entity.id}"

    # Retire every currently-active BEHAVIOUR on this entity. Autonomy owns
    # the whole behaviour surface of an owned entity: "my entity's
    # behaviour" is one script, the one the owner last set, so a prior
    # behaviour -- whether set by autonomy or by legislation -- is replaced.
    retired: list[Script] = list(
        session.execute(
            select(Script).where(
                Script.entity_id == entity.id,
                Script.script_type == ScriptType.BEHAVIOUR,
                Script.is_active.is_(True),
            )
        ).scalars()
    )
    for prior in retired:
        prior.is_active = False

    version_count = session.execute(
        select(func.count()).select_from(Script).where(Script.lineage_id == lineage_id)
    ).scalar_one()

    new_script = Script(
        name=f"{lineage_id}#{version_count + 1}",
        description=description,
        script_type=ScriptType.BEHAVIOUR,
        source=source,
        is_active=True,
        timeout_ms=timeout_ms,
        entity_id=entity.id,
        lineage_id=lineage_id,
    )
    session.add(new_script)
    session.flush()

    op = {
        "type": "set_behaviour",
        "entity_id": entity.id,
        "owner_id": owner_id,
        "lineage_id": lineage_id,
        "script_id": new_script.id,
        "retired_script_ids": [s.id for s in retired] or None,
        "reference": reference,
    }
    scripting.fire_hooks(session, op)
    return new_script, warnings


#: Mutation types allowed in each proposal tier. The split is ASYMMETRIC
#: by design: ordinary law may never reach the constitution (a
#: simple-majority vote must not touch validators or the voting floor),
#: but a constitutional amendment — which clears a harder bar — may also
#: carry ordinary law. That is the standard hierarchy: the constitution
#: may say anything ordinary law can, and more. So an ORDINARY proposal is
#: limited to ordinary mutations, while a CONSTITUTIONAL proposal may mix
#: constitutional and ordinary changes in one enactment.
ORDINARY_MUTATIONS = frozenset({"set_fiscal_policy", "set_script"})
CONSTITUTIONAL_MUTATIONS = frozenset({
    "set_validator", "set_constitution",
    "grant_capability", "revoke_capability",  # power transfer is meta
})
ALL_MUTATIONS = ORDINARY_MUTATIONS | CONSTITUTIONAL_MUTATIONS


def set_validator(
    session: Session,
    authority: Entity,
    lineage_id: str,
    source: str,
    description: str = "",
    timeout_ms: int = 100,
    entity_id: str | None = None,
    reference: str = "",
) -> Script:
    """Amend the constitution — enact a new version of a VALIDATOR.

    The constitutional twin of ``set_script`` (docs/actors.md step 4b):
    the same retire-old + activate-new governed lifecycle, but for the
    scripts that *are* the constitution. Where ``set_script`` is kept away
    from validators (ordinary legislation may not touch them), this is the
    one path that writes them — gated by the ``amend_constitution``
    capability, reachable only by a passed constitutional proposal.

    A VALIDATOR added here binds the very next operation, including the
    rest of this enactment's mutations if they are money/script ops — so a
    constitutional amendment that installs a cap takes effect immediately.
    No validator gates ``set_validator`` itself, by the same logic as
    ``set_script``: a validator cannot be allowed to lock itself in or out
    of the constitution; the capability + the supermajority are the gate.
    A HOOK fires after enactment for audit.
    """
    if not authority.has_capability(AMEND_CONSTITUTION):
        raise MissingCapabilityError(authority.id, AMEND_CONSTITUTION)
    if not lineage_id:
        raise ValueError("lineage_id is required")

    current = session.execute(
        select(Script).where(
            Script.lineage_id == lineage_id,
            Script.is_active.is_(True),
        )
    ).scalars().first()
    if current is not None:
        current.is_active = False

    version_count = session.execute(
        select(func.count()).select_from(Script).where(Script.lineage_id == lineage_id)
    ).scalar_one()

    new_script = Script(
        name=f"{lineage_id}#{version_count + 1}",
        description=description,
        script_type=ScriptType.VALIDATOR,
        source=source,
        is_active=True,
        timeout_ms=timeout_ms,
        entity_id=entity_id,
        lineage_id=lineage_id,
    )
    session.add(new_script)
    session.flush()

    op = {
        "type": "set_validator",
        "entity_id": authority.id,
        "lineage_id": lineage_id,
        "script_id": new_script.id,
        "retired_script_id": current.id if current is not None else None,
        "reference": reference,
    }
    scripting.fire_hooks(session, op)
    return new_script


def set_constitution(
    session: Session,
    authority: Entity,
    params: dict,
    reference: str = "",
) -> WorldSetting:
    """Amend the constitution — replace the voting-system floor.

    The data twin of ``set_validator`` (docs/actors.md step 4b): where
    ``set_validator`` writes the VALIDATOR scripts (the *constraints* on
    ordinary law), this writes the voting-system params (the *threshold
    and quorum a constitutional amendment itself must clear*). Both are
    the constitution; both need the ``amend_constitution`` capability and
    a passed constitutional proposal.

    A VALIDATOR may veto the change (fail-closed), so the constitution can
    constitutionally constrain its own amendment — e.g. forbid lowering
    the supermajority below two-thirds. The params replace wholesale
    (merged over defaults in ``constitution.set_constitution``); a HOOK
    fires after for audit.
    """
    from . import constitution

    if not authority.has_capability(AMEND_CONSTITUTION):
        raise MissingCapabilityError(authority.id, AMEND_CONSTITUTION)
    if not isinstance(params, dict):
        raise ValueError("constitution must be a JSON object")
    op = {
        "type": "set_constitution",
        "entity_id": authority.id,
        "constitution": params,
        "reference": reference,
    }
    scripting.fire_validators(session, op)
    setting = constitution.set_constitution(session, params)
    scripting.fire_hooks(session, {**op, "setting_key": setting.key})
    return setting


# ---------------------------------------------------------------------------
# Governance — capability transfer (the meta-privilege)
# ---------------------------------------------------------------------------


def grant_capability(
    session: Session,
    authority: Entity,
    target: Entity,
    capability: str,
    reference: str = "",
) -> dict:
    """Confer ``capability`` on ``target`` by engine authority.

    The meta-privilege above every other capability: changing *who can
    exercise power*. ``authority`` must hold ``GRANT_CAPABILITY`` (checked
    here AND at the intent boundary, like levy/seize). The free-grant model:
    a holder may confer any *declared* capability (``capabilities.ALL``) on
    any entity — a legislature constitutes agencies with powers it does not
    itself exercise. The safety floor is this gate + a VALIDATOR veto + (for
    a voted grant) the constitutional supermajority, not "you may only
    delegate what you hold."

    A VALIDATOR may veto the grant (fail-closed): the constitution can
    forbid conferring a dangerous capability (e.g. ``seize``) regardless of
    who authorises it. Idempotent — granting a capability already held is a
    no-op success. Returns the target's resulting capability list.
    """
    from . import capabilities as _caps

    if not authority.has_capability(GRANT_CAPABILITY):
        raise MissingCapabilityError(authority.id, GRANT_CAPABILITY)
    if capability not in _caps.ALL:
        raise ValueError(f"unknown capability {capability!r}")
    op = {
        "type": "grant_capability",
        "entity_id": authority.id,
        "to_entity_id": target.id,
        "capability": capability,
        "reference": reference,
    }
    scripting.fire_validators(session, op)
    caps = list(target.capabilities or [])
    if capability not in caps:
        caps.append(capability)
        target.capabilities = caps
        session.flush()
    scripting.fire_hooks(session, op)
    return {"entity_id": target.id, "capabilities": list(target.capabilities or [])}


def revoke_capability(
    session: Session,
    authority: Entity,
    target: Entity,
    capability: str,
    reference: str = "",
) -> dict:
    """Remove ``capability`` from ``target`` by engine authority.

    The symmetric partner of ``grant_capability`` — withdrawing power is
    the same meta-privilege as conferring it, so gated by the same
    ``GRANT_CAPABILITY`` capability (and vetoable by a VALIDATOR). A
    revoke of a capability not held is a no-op success (the postcondition —
    target lacks it — already holds). Removing a capability an entity
    exercises via the legacy ``is_monetary_authority`` flag does *not*
    clear that flag (the flag is its own backward-compatible grant); the
    capability list and the flag are independent grants.
    """
    if not authority.has_capability(GRANT_CAPABILITY):
        raise MissingCapabilityError(authority.id, GRANT_CAPABILITY)
    op = {
        "type": "revoke_capability",
        "entity_id": authority.id,
        "to_entity_id": target.id,
        "capability": capability,
        "reference": reference,
    }
    scripting.fire_validators(session, op)
    caps = [c for c in (target.capabilities or []) if c != capability]
    if len(caps) != len(target.capabilities or []):
        target.capabilities = caps
        session.flush()
    scripting.fire_hooks(session, op)
    return {"entity_id": target.id, "capabilities": list(target.capabilities or [])}


# ---------------------------------------------------------------------------
# Governance — proposal / vote / enact (actors step 4a-ii)
# ---------------------------------------------------------------------------

def create_proposal(
    session: Session,
    proposer_id: str,
    target_id: str,
    title: str,
    weight_model: str,
    threshold: Decimal,
    quorum: Decimal,
    mutations: list,
    proposal_type: ProposalType = ProposalType.ORDINARY,
    reference: str = "",
) -> Proposal:
    """Open a proposal for vote.

    A proposal bundles a batch of mutations with a weight model, a
    threshold, and a quorum. It is inert until enacted: creating it changes
    nothing but the row. The ``target`` is the government that will enact —
    the entity the mutations run as, whose capabilities they exercise.
    ``weight_model`` names a resolver in ``weights.WEIGHT_MODELS`` (the
    electorate definition — the form of government as data).

    ``proposal_type`` is the tier: ``ORDINARY`` (set_fiscal_policy /
    set_script — ordinary law) or ``CONSTITUTIONAL`` (set_validator /
    set_constitution — the constitution). The tier gates which mutations
    are allowed, asymmetrically: an ordinary proposal is confined to
    ordinary law (it must never reach a validator), while a constitutional
    amendment may also carry ordinary law (a harder bar may say more). The
    tier also decides the enactment's capability (``legislate`` vs
    ``amend_constitution``) and floor (the proposal's own threshold vs the
    supermajority) — checked at enactment, not here.

    Validation here is structural (known model, well-formed mutations,
    tier-consistent mutation types). The proposer's electorate membership
    is checked in ``resolve_intent``; capability sufficiency for the
    mutations is checked at *enactment*.
    """
    from . import weights
    if weights.get_model(weight_model) is None:
        raise ValueError(f"unknown weight model {weight_model!r}")
    if not mutations or not isinstance(mutations, list):
        raise ValueError("mutations must be a non-empty list")
    allowed = (ALL_MUTATIONS
               if proposal_type == ProposalType.CONSTITUTIONAL
               else ORDINARY_MUTATIONS)
    for i, m in enumerate(mutations):
        if not isinstance(m, dict) or "type" not in m or "params" not in m:
            raise ValueError(f"mutation {i} must have 'type' and 'params'")
        if not isinstance(m["params"], dict):
            raise ValueError(f"mutation {i} params must be an object")
        if m["type"] not in allowed:
            raise ValueError(
                f"mutation {i} type {m['type']!r} not allowed for "
                f"{proposal_type.value} proposals"
            )
    proposal = Proposal(
        title=title,
        proposer_id=proposer_id,
        target_id=target_id,
        weight_model=weight_model,
        threshold=str(threshold),
        quorum=str(quorum),
        mutations=mutations,
        proposal_type=proposal_type,
        status=ProposalStatus.OPEN,
    )
    session.add(proposal)
    session.flush()
    scripting.fire_hooks(session, {
        "type": "create_proposal",
        "entity_id": proposer_id,
        "proposal_id": proposal.id,
        "reference": reference,
    })
    return proposal


def cast_vote(
    session: Session,
    proposal: Proposal,
    voter_id: str,
    choice: VoteChoice,
    weight: Decimal,
    reference: str = "",
) -> Vote:
    """Record a vote — idempotent per voter per proposal.

    One vote per voter per proposal (a unique constraint backs this).
    Re-submitting the *same* choice is idempotent (returns the existing
    vote); re-submitting a *different* choice is rejected. The weight is
    computed by the resolver at cast time and snapshotted, so the record
    shows exactly what was counted (for the citizen model, always "1").
    """
    existing = session.execute(
        select(Vote).where(
            Vote.proposal_id == proposal.id,
            Vote.voter_id == voter_id,
        )
    ).scalars().first()
    if existing is not None:
        if existing.choice == choice:
            return existing  # idempotent re-submit
        raise ValueError("already voted; cannot change choice")
    vote = Vote(
        proposal_id=proposal.id,
        voter_id=voter_id,
        choice=choice,
        weight=str(weight),
    )
    session.add(vote)
    session.flush()
    scripting.fire_hooks(session, {
        "type": "vote",
        "entity_id": voter_id,
        "proposal_id": proposal.id,
        "choice": choice.value,
        "weight": str(weight),
        "reference": reference,
    })
    return vote


def enact_proposal(
    session: Session,
    proposal: Proposal,
    reference: str = "",
) -> dict:
    """Tally a proposal and, if it passes, apply its mutations atomically.

    Enactment is the only op that changes the world as a result of a
    proposal; everything before it is bookkeeping. The tally is:

      passed  iff  yes >= threshold * (yes + no)   (fraction of cast weight)
              and  (yes + no) / electorate >= quorum   (turnout)

    For a CONSTITUTIONAL proposal the threshold/quorum are raised to the
    supermajority floor in the ``constitution`` world setting (a proposer
    cannot lower the bar by writing a smaller number). The capability an
    enactment needs is also tier-dependent (``legislate`` vs
    ``amend_constitution``); that is checked in ``resolve_intent``'s enact
    branch, which calls this only after the gate passes.

    On pass, every mutation is resolved through ``scripting.resolve_intent``
    *as the target government*, so capability gates and VALIDATORs fire
    exactly as for a live intent — a citizen-enacted 100% levy is still
    vetoed by the constitutional cap. Mutations apply atomically
    (all-or-nothing, one savepoint): if any is rejected or vetoed the whole
    enactment rolls back and the proposal is marked FAILED with the reason.

    Returns the tally (yes/no/electorate/turnout/passed) and the resulting
    status. ``resolve_intent`` rejects enacting a non-OPEN proposal or one
    whose target is not the enactor, so this need not.
    """
    from . import weights, scripting
    from .lua_engine import Intent

    electorate_weights = weights.electorate(session, proposal.weight_model)
    electorate_total = sum(electorate_weights.values(), Decimal(0))
    yes = sum(
        (Decimal(v.weight) for v in proposal.votes if v.choice == VoteChoice.FOR),
        Decimal(0),
    )
    no = sum(
        (Decimal(v.weight) for v in proposal.votes if v.choice == VoteChoice.AGAINST),
        Decimal(0),
    )
    cast = yes + no
    turnout = (cast / electorate_total) if electorate_total > 0 else Decimal(0)
    threshold = Decimal(proposal.threshold)
    quorum = Decimal(proposal.quorum)
    # A constitutional amendment must clear the supermajority floor held in
    # the `constitution` world setting, on top of the proposal's own bar —
    # a proposer cannot lower the amendment threshold below two-thirds by
    # writing a smaller number on the proposal. The floor is read from the
    # constitution IN FORCE at enactment; a set_constitution mutation in
    # this proposal amends it only after the (old) floor is cleared.
    if proposal.proposal_type == ProposalType.CONSTITUTIONAL:
        from . import constitution
        floor_threshold, floor_quorum = constitution.supermajority_floor(session)
        threshold = max(threshold, floor_threshold)
        quorum = max(quorum, floor_quorum)
    passed = (
        electorate_total > 0
        and cast > 0
        and yes >= (threshold * cast)
        and turnout >= quorum
    )
    now = datetime.now(timezone.utc)
    tally = {
        "yes": yes, "no": no, "electorate": electorate_total,
        "turnout": turnout, "passed": passed,
    }
    # stamp the tally on the row regardless of outcome (audit)
    proposal.tally_yes = str(yes)
    proposal.tally_no = str(no)
    proposal.tally_electorate = str(electorate_total)
    proposal.tally_turnout = str(turnout)
    proposal.enacted_at = now

    if not passed:
        proposal.status = ProposalStatus.FAILED
        proposal.failure_reason = "did not meet threshold or quorum"
        return {**tally, "status": "failed", "reason": proposal.failure_reason}

    # passed — apply every mutation atomically as the target government.
    # resolve_intent re-checks capabilities and fires VALIDATORs/HOOKs, so
    # a veto here fails the whole enactment (the savepoint rolls back).
    try:
        with session.begin_nested():
            for m in proposal.mutations:
                result = scripting.resolve_intent(session, Intent(
                    entity_id=proposal.target_id,
                    intent_type=m["type"],
                    params=dict(m["params"]),
                    resource_ids=[],
                ))
                if result.get("status") != "applied":
                    raise ValueError(
                        f"mutation {m['type']!r} rejected: {result.get('reason')}"
                    )
    except ValueError as exc:
        # a mutation was rejected/vetoed -> savepoint rolled back; fail
        proposal.status = ProposalStatus.FAILED
        proposal.failure_reason = str(exc)
        return {**tally, "status": "failed", "reason": str(exc)}

    proposal.status = ProposalStatus.ENACTED
    scripting.fire_hooks(session, {
        "type": "enact",
        "entity_id": proposal.target_id,
        "proposal_id": proposal.id,
        "status": "enacted",
        "tally_yes": str(yes),
        "tally_no": str(no),
        "reference": reference,
    })
    return {**tally, "status": "enacted", "reason": None}


def _op(op_type: str, account: Account, amount: Decimal, reference: str) -> dict:
    return {
        "type": op_type,
        "entity_id": account.entity_id,
        "account_id": account.id,
        "amount": str(amount),
        "currency": account.currency,
        "reference": reference,
    }
