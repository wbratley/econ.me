from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from econ import services
from econ.api.deps import get_current_user, get_session
from econ.api.schemas import (
    AccountCreate, AccountRead, EntityCreate, EntityRead, HoldingRead, NeedStateRead, TransactionRead,
)
from econ.models import Account, Entity, Holding, NeedState, User

router = APIRouter(prefix="/entities", tags=["entities"])


def _own_entity(entity_id: str, user: User, session: Session) -> Entity:
    entity = session.get(Entity, entity_id)
    if entity is None or entity.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.get("", response_model=list[EntityRead])
def list_entities(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return session.query(Entity).filter_by(owner_id=current_user.id).all()


@router.post("", response_model=EntityRead, status_code=201)
def create_entity(
    body: EntityCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    entity = services.create_entity(session, body.name, body.entity_type)
    entity.owner_id = current_user.id
    session.commit()
    session.refresh(entity)
    return entity


@router.get("/{entity_id}", response_model=EntityRead)
def get_entity(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _own_entity(entity_id, current_user, session)


@router.post("/{entity_id}/accounts", response_model=AccountRead, status_code=201)
def create_account(
    entity_id: str,
    body: AccountCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    entity = _own_entity(entity_id, current_user, session)
    try:
        initial = Decimal(str(body.initial_balance))
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="Invalid initial_balance")
    account = services.create_account(session, entity, body.currency, initial)
    session.commit()
    session.refresh(account)
    return account


@router.get("/{entity_id}/holdings", response_model=list[HoldingRead])
def list_holdings(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    entity = _own_entity(entity_id, current_user, session)
    return (
        session.query(Holding)
        .filter_by(entity_id=entity.id)
        .order_by(Holding.symbol)
        .all()
    )


@router.get("/{entity_id}/needs", response_model=list[NeedStateRead])
def list_need_states(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    entity = _own_entity(entity_id, current_user, session)
    states = (
        session.query(NeedState)
        .filter_by(entity_id=entity.id)
        .all()
    )
    return sorted(
        (
            NeedStateRead(need=s.need.code, satisfaction=s.satisfaction, updated_tick=s.updated_tick)
            for s in states
        ),
        key=lambda s: s.need,
    )


@router.get("/{entity_id}/accounts/{account_id}/transactions", response_model=list[TransactionRead])
def list_transactions(
    entity_id: str,
    account_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    entity = _own_entity(entity_id, current_user, session)
    account = session.get(Account, account_id)
    if account is None or account.entity_id != entity.id:
        raise HTTPException(status_code=404, detail="Account not found")
    return account.transactions
