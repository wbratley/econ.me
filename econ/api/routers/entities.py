from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from econengine import scripting, services
from econ.api.deps import get_current_user, get_session
from econ.api.schemas import (
    AccountCreate, AccountRead, BehaviourScriptWrite, EntityCreate, EntityRead,
    BehaviourScriptRead, HoldingRead, NeedStateRead, ScriptRead, TransactionRead,
)
from econengine.models import Account, Entity, Holding, NeedState, Script, ScriptType, User

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


@router.get("/{entity_id}/behaviour", response_model=ScriptRead)
def get_behaviour(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The entity's currently-active BEHAVIOUR script (what runs as it each
    tick), or 404 if it has none. Ownership-gated: a player may read only
    their own entities' behaviour."""
    entity = _own_entity(entity_id, current_user, session)
    script = session.query(Script).filter_by(
        entity_id=entity.id, script_type=ScriptType.BEHAVIOUR, is_active=True
    ).order_by(Script.created_at.desc()).first()
    if script is None:
        raise HTTPException(status_code=404, detail="No active behaviour script")
    return script


@router.post("/{entity_id}/behaviour", response_model=BehaviourScriptRead, status_code=201)
def set_behaviour(
    entity_id: str,
    body: BehaviourScriptWrite,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Ownership-gated autonomy path (docs/game.md §6).

    The authenticated owner replaces their entity's BEHAVIOUR script. This
    is autonomy, not legislation: no vote, no capability, only ownership.
    Server-owned / fixed (immutable-tier) entities are refused.

    Submit-time lint (docs/scripting.md §4, Phase 3): the source is
    checked against the injected tiers with the same strict standard the
    install gate applies. A script referencing vocabulary that is not
    injected (the nil-call trap) is refused with 400 and the entity keeps
    its current behaviour; synthetic-ctx findings a healthy script can
    still produce come back as `warnings` on an accepted script.
    """
    entity = _own_entity(entity_id, current_user, session)
    if entity.is_fixed:
        raise HTTPException(
            status_code=409,
            detail="Entity behaviour is fixed (immutable tier; not player-editable)",
        )
    try:
        script, warnings = services.set_entity_behaviour(
            session, entity, body.source,
            owner_id=current_user.id,
            description=body.description,
            timeout_ms=body.timeout_ms,
        )
    except scripting.ScriptRejected as exc:
        raise HTTPException(status_code=400, detail=exc.problems)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    session.commit()
    session.refresh(script)
    read = BehaviourScriptRead.model_validate(script, from_attributes=True)
    read.warnings = warnings
    return read
