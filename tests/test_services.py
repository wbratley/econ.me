import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.models import Base, EntityType
from econengine.models.transaction import TransactionType
from econengine.services import (
    create_entity,
    create_account,
    deposit,
    withdraw,
    transfer,
    issue_money,
    retire_money,
    InsufficientFundsError,
    CurrencyMismatchError,
    NotMonetaryAuthorityError,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def alice_bob(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    a = create_account(session, alice, "USD", initial_balance=Decimal("1000"))
    b = create_account(session, bob, "USD")
    return session, a, b


@pytest.fixture
def central_bank(session):
    bank = create_entity(session, "Central Bank", EntityType.BANK)
    bank.is_monetary_authority = True
    session.flush()
    acct = create_account(session, bank, "USD", initial_balance=Decimal("0"))
    return session, acct


# --- create_entity ---

def test_create_entity_stores_name_and_type(session):
    e = create_entity(session, "Acme Corp", EntityType.BUSINESS)
    assert e.name == "Acme Corp"
    assert e.entity_type == EntityType.BUSINESS
    assert e.id is not None


def test_create_entity_is_flushed(session):
    e = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    fetched = session.get(type(e), e.id)
    assert fetched is e


# --- create_account ---

def test_create_account_defaults_to_zero_balance(session):
    entity = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    acct = create_account(session, entity, "usd")
    assert acct.balance == Decimal("0")


def test_create_account_upcases_currency(session):
    entity = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    acct = create_account(session, entity, "eur")
    assert acct.currency == "EUR"


def test_create_account_links_to_entity(session):
    entity = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    acct = create_account(session, entity, "USD")
    assert acct.entity_id == entity.id


def test_create_account_initial_balance(session):
    entity = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    acct = create_account(session, entity, "USD", initial_balance=Decimal("500"))
    assert acct.balance == Decimal("500")


# --- deposit ---

def test_deposit_increases_balance(session):
    entity = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    acct = create_account(session, entity, "USD")
    deposit(session, acct, Decimal("300"), "opening")
    assert acct.balance == Decimal("300")


def test_deposit_creates_credit_transaction(session):
    entity = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    acct = create_account(session, entity, "USD")
    tx = deposit(session, acct, Decimal("100"), "ref")
    assert tx.tx_type == TransactionType.CREDIT
    assert tx.amount == Decimal("100")
    assert tx.account_id == acct.id
    assert tx.to_account_id == acct.id
    assert tx.from_account_id is None
    assert tx.reference == "ref"


def test_deposit_appends_to_account_transactions(session):
    entity = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    acct = create_account(session, entity, "USD")
    tx = deposit(session, acct, Decimal("50"), "ref")
    assert tx in acct.transactions


def test_deposit_rejects_zero_amount(session):
    entity = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    acct = create_account(session, entity, "USD")
    with pytest.raises(ValueError):
        deposit(session, acct, Decimal("0"), "bad")


def test_deposit_rejects_negative_amount(session):
    entity = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    acct = create_account(session, entity, "USD")
    with pytest.raises(ValueError):
        deposit(session, acct, Decimal("-1"), "bad")


# --- withdraw ---

def test_withdraw_decreases_balance(alice_bob):
    session, a, _ = alice_bob
    withdraw(session, a, Decimal("400"), "cash out")
    assert a.balance == Decimal("600")


def test_withdraw_creates_debit_transaction(alice_bob):
    session, a, _ = alice_bob
    tx = withdraw(session, a, Decimal("100"), "ref")
    assert tx.tx_type == TransactionType.DEBIT
    assert tx.amount == Decimal("100")
    assert tx.account_id == a.id
    assert tx.from_account_id == a.id
    assert tx.to_account_id is None
    assert tx.reference == "ref"


def test_withdraw_raises_on_insufficient_funds(alice_bob):
    session, a, _ = alice_bob
    with pytest.raises(InsufficientFundsError):
        withdraw(session, a, Decimal("9999"), "too much")


def test_withdraw_does_not_mutate_balance_on_failure(alice_bob):
    session, a, _ = alice_bob
    original = a.balance
    with pytest.raises(InsufficientFundsError):
        withdraw(session, a, Decimal("9999"), "too much")
    assert a.balance == original


def test_withdraw_rejects_zero_amount(alice_bob):
    session, a, _ = alice_bob
    with pytest.raises(ValueError):
        withdraw(session, a, Decimal("0"), "bad")


def test_withdraw_rejects_negative_amount(alice_bob):
    session, a, _ = alice_bob
    with pytest.raises(ValueError):
        withdraw(session, a, Decimal("-1"), "bad")


# --- transfer ---

def test_transfer_moves_funds(alice_bob):
    session, a, b = alice_bob
    transfer(session, a, b, Decimal("250"), "payment")
    assert a.balance == Decimal("750")
    assert b.balance == Decimal("250")


def test_transfer_creates_paired_transactions(alice_bob):
    session, a, b = alice_bob
    debit, credit = transfer(session, a, b, Decimal("100"), "ref")

    assert debit.tx_type == TransactionType.DEBIT
    assert debit.account_id == a.id
    assert debit.amount == Decimal("100")
    assert debit.from_account_id == a.id
    assert debit.to_account_id == b.id

    assert credit.tx_type == TransactionType.CREDIT
    assert credit.account_id == b.id
    assert credit.amount == Decimal("100")
    assert credit.from_account_id == a.id
    assert credit.to_account_id == b.id


def test_transfer_paired_transactions_share_timestamp(alice_bob):
    session, a, b = alice_bob
    debit, credit = transfer(session, a, b, Decimal("100"), "ref")
    assert debit.date == credit.date


def test_transfer_raises_on_insufficient_funds(alice_bob):
    session, a, b = alice_bob
    with pytest.raises(InsufficientFundsError):
        transfer(session, a, b, Decimal("9999"), "too much")


def test_transfer_does_not_mutate_balances_on_failure(alice_bob):
    session, a, b = alice_bob
    a_bal, b_bal = a.balance, b.balance
    with pytest.raises(InsufficientFundsError):
        transfer(session, a, b, Decimal("9999"), "too much")
    assert a.balance == a_bal
    assert b.balance == b_bal


def test_transfer_raises_on_currency_mismatch(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    a_usd = create_account(session, alice, "USD", initial_balance=Decimal("500"))
    a_eur = create_account(session, alice, "EUR")
    with pytest.raises(CurrencyMismatchError):
        transfer(session, a_usd, a_eur, Decimal("100"), "bad")


def test_transfer_rejects_zero_amount(alice_bob):
    session, a, b = alice_bob
    with pytest.raises(ValueError):
        transfer(session, a, b, Decimal("0"), "bad")


def test_transfer_rejects_negative_amount(alice_bob):
    session, a, b = alice_bob
    with pytest.raises(ValueError):
        transfer(session, a, b, Decimal("-1"), "bad")


# --- issue_money ---

def test_issue_money_increases_balance(central_bank):
    session, acct = central_bank
    issue_money(session, acct, Decimal("1000"), "initial issuance")
    assert acct.balance == Decimal("1000")


def test_issue_money_creates_issuance_transaction(central_bank):
    session, acct = central_bank
    tx = issue_money(session, acct, Decimal("500"), "ref")
    assert tx.tx_type == TransactionType.ISSUANCE
    assert tx.amount == Decimal("500")
    assert tx.account_id == acct.id
    assert tx.to_account_id == acct.id
    assert tx.from_account_id is None


def test_issue_money_has_no_from_account(central_bank):
    session, acct = central_bank
    tx = issue_money(session, acct, Decimal("100"), "ref")
    assert tx.from_account_id is None


def test_issue_money_rejects_non_authority(alice_bob):
    session, a, _ = alice_bob
    with pytest.raises(NotMonetaryAuthorityError):
        issue_money(session, a, Decimal("100"), "ref")


def test_issue_money_rejects_zero_amount(central_bank):
    session, acct = central_bank
    with pytest.raises(ValueError):
        issue_money(session, acct, Decimal("0"), "ref")


def test_issue_money_rejects_negative_amount(central_bank):
    session, acct = central_bank
    with pytest.raises(ValueError):
        issue_money(session, acct, Decimal("-1"), "ref")


# --- retire_money ---

def test_retire_money_decreases_balance(central_bank):
    session, acct = central_bank
    issue_money(session, acct, Decimal("1000"), "issue")
    retire_money(session, acct, Decimal("400"), "retire")
    assert acct.balance == Decimal("600")


def test_retire_money_creates_retirement_transaction(central_bank):
    session, acct = central_bank
    issue_money(session, acct, Decimal("500"), "issue")
    tx = retire_money(session, acct, Decimal("200"), "ref")
    assert tx.tx_type == TransactionType.RETIREMENT
    assert tx.amount == Decimal("200")
    assert tx.account_id == acct.id
    assert tx.from_account_id == acct.id
    assert tx.to_account_id is None


def test_retire_money_has_no_to_account(central_bank):
    session, acct = central_bank
    issue_money(session, acct, Decimal("500"), "issue")
    tx = retire_money(session, acct, Decimal("100"), "ref")
    assert tx.to_account_id is None


def test_retire_money_rejects_non_authority(alice_bob):
    session, a, _ = alice_bob
    with pytest.raises(NotMonetaryAuthorityError):
        retire_money(session, a, Decimal("100"), "ref")


def test_retire_money_raises_on_insufficient_funds(central_bank):
    session, acct = central_bank
    with pytest.raises(InsufficientFundsError):
        retire_money(session, acct, Decimal("1"), "ref")


def test_retire_money_does_not_mutate_balance_on_failure(central_bank):
    session, acct = central_bank
    issue_money(session, acct, Decimal("100"), "issue")
    before = acct.balance
    with pytest.raises(InsufficientFundsError):
        retire_money(session, acct, Decimal("9999"), "ref")
    assert acct.balance == before


def test_retire_money_rejects_zero_amount(central_bank):
    session, acct = central_bank
    with pytest.raises(ValueError):
        retire_money(session, acct, Decimal("0"), "ref")


def test_retire_money_rejects_negative_amount(central_bank):
    session, acct = central_bank
    with pytest.raises(ValueError):
        retire_money(session, acct, Decimal("-1"), "ref")
