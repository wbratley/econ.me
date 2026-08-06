from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import scripting
from .capabilities import LEVY, LEGISLATE, MONETARY_AUTHORITY, SET_FISCAL_POLICY
from .models.entity import Entity, EntityType
from .models.account import Account
from .models.script import Script, ScriptType
from .models.transaction import Transaction, TransactionType
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


def create_entity(session: Session, name: str, entity_type: EntityType) -> Entity:
    entity = Entity(name=name, entity_type=entity_type)
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


def _op(op_type: str, account: Account, amount: Decimal, reference: str) -> dict:
    return {
        "type": op_type,
        "entity_id": account.entity_id,
        "account_id": account.id,
        "amount": str(amount),
        "currency": account.currency,
        "reference": reference,
    }
