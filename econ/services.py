from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy.orm import Session

from . import scripting
from .models.entity import Entity, EntityType
from .models.account import Account
from .models.transaction import Transaction, TransactionType


class InsufficientFundsError(ValueError):
    pass


class CurrencyMismatchError(ValueError):
    pass


class NotMonetaryAuthorityError(ValueError):
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


def issue_money(
    session: Session,
    account: Account,
    amount: Decimal,
    reference: str,
    date: datetime | None = None,
) -> Transaction:
    if not account.entity.is_monetary_authority:
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
    if not account.entity.is_monetary_authority:
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


def _op(op_type: str, account: Account, amount: Decimal, reference: str) -> dict:
    return {
        "type": op_type,
        "entity_id": account.entity_id,
        "account_id": account.id,
        "amount": str(amount),
        "currency": account.currency,
        "reference": reference,
    }
