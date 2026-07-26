from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from econengine import services
from econ.api.deps import get_session, require_admin
from econ.api.schemas import AdminEntityCreate, EntityRead, EntityUpdate, UserRead, UserUpdate
from econengine.models import Entity, User

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/entities", response_model=list[EntityRead])
def list_all_entities(session: Session = Depends(get_session), _: User = Depends(require_admin)):
    return session.query(Entity).all()


@router.post("/entities", response_model=EntityRead, status_code=201)
def create_entity(
    body: AdminEntityCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    entity = services.create_entity(session, body.name, body.entity_type)
    entity.owner_id = body.owner_id
    session.commit()
    session.refresh(entity)
    return entity


@router.patch("/entities/{entity_id}", response_model=EntityRead)
def update_entity(
    entity_id: str,
    body: EntityUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    entity = session.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    if body.name is not None:
        entity.name = body.name
    if body.entity_type is not None:
        entity.entity_type = body.entity_type
    if body.is_monetary_authority is not None:
        entity.is_monetary_authority = body.is_monetary_authority
    session.commit()
    session.refresh(entity)
    return entity


@router.delete("/entities/{entity_id}", status_code=204)
def delete_entity(
    entity_id: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    entity = session.get(Entity, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    session.delete(entity)
    session.commit()


@router.get("/users", response_model=list[UserRead])
def list_users(session: Session = Depends(get_session), _: User = Depends(require_admin)):
    return session.query(User).all()


@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: str,
    body: UserUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if body.name is not None:
        user.name = body.name
    if body.is_admin is not None:
        user.is_admin = body.is_admin
    session.commit()
    session.refresh(user)
    return user
