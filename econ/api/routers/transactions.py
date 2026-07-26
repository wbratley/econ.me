from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from econengine import services
from econ.api.deps import get_current_user, get_session
from econ.api.schemas import DepositRequest, IssueRequest, RetireRequest, TransactionRead, TransferRequest, WithdrawRequest
from econengine.models import Account, User

router = APIRouter(prefix="/transactions", tags=["transactions"])


def _parse_amount(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail=f"Invalid amount: {raw!r}")
    return value


def _ma_account(account_id: str, user: User, session: Session) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.entity.is_monetary_authority:
        raise HTTPException(status_code=403, detail="Account does not belong to a monetary authority")
    if not user.is_admin and account.entity.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return account


def _owned_account(account_id: str, user: User, session: Session) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.entity.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Not your account")
    return account


@router.post("/deposit", response_model=TransactionRead, status_code=201)
def deposit(
    body: DepositRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    account = _owned_account(body.account_id, current_user, session)
    try:
        tx = services.deposit(session, account, _parse_amount(body.amount), body.reference)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(tx)
    return tx


@router.post("/withdraw", response_model=TransactionRead, status_code=201)
def withdraw(
    body: WithdrawRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    account = _owned_account(body.account_id, current_user, session)
    try:
        tx = services.withdraw(session, account, _parse_amount(body.amount), body.reference)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(tx)
    return tx


@router.post("/transfer", response_model=list[TransactionRead], status_code=201)
def transfer(
    body: TransferRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    from_account = _owned_account(body.from_account_id, current_user, session)
    to_account = session.get(Account, body.to_account_id)
    if to_account is None:
        raise HTTPException(status_code=404, detail="Destination account not found")
    try:
        debit, credit = services.transfer(
            session, from_account, to_account, _parse_amount(body.amount), body.reference
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(debit)
    session.refresh(credit)
    return [debit, credit]


@router.post("/issue", response_model=TransactionRead, status_code=201)
def issue(
    body: IssueRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    account = _ma_account(body.account_id, current_user, session)
    try:
        tx = services.issue_money(session, account, _parse_amount(body.amount), body.reference)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(tx)
    return tx


@router.post("/retire", response_model=TransactionRead, status_code=201)
def retire(
    body: RetireRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    account = _ma_account(body.account_id, current_user, session)
    try:
        tx = services.retire_money(session, account, _parse_amount(body.amount), body.reference)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(tx)
    return tx
