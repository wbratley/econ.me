from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine import tech
from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.schemas import TechnologyCreate, TechnologyRead, TechnologyUpdate, UnlockGrant, UnlockRead
from econengine.models import Entity, Technology, Unlock, User
from econengine.production import next_tick_number

router = APIRouter(tags=["tech"])


def _technology_or_404(session: Session, code: str) -> Technology:
    technology = tech.get_technology(session, code)
    if technology is None:
        raise HTTPException(status_code=404, detail="Technology not found")
    return technology


# ---------------------------------------------------------------------------
# Technologies — public data, admin-managed
# ---------------------------------------------------------------------------

@router.get("/technologies", response_model=list[TechnologyRead])
def list_technologies(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return session.execute(select(Technology).order_by(Technology.code)).scalars().all()


@router.get("/technologies/{code}", response_model=TechnologyRead)
def get_technology(
    code: str,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _technology_or_404(session, code)


@router.post("/admin/technologies", response_model=TechnologyRead, status_code=201, tags=["admin"])
def create_technology(
    body: TechnologyCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    if tech.get_technology(session, body.code) is not None:
        raise HTTPException(status_code=409, detail="Technology already exists")
    try:
        technology = tech.create_technology(
            session,
            body.code,
            prerequisites=body.prerequisites,
            scope=body.scope,
            name=body.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(technology)
    return technology


@router.patch("/admin/technologies/{code}", response_model=TechnologyRead, tags=["admin"])
def update_technology(
    code: str,
    body: TechnologyUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    technology = _technology_or_404(session, code)
    if body.name is not None:
        technology.name = body.name
    if body.is_active is not None:
        technology.is_active = body.is_active
    session.commit()
    session.refresh(technology)
    return technology


# ---------------------------------------------------------------------------
# Unlocks — admin grant (research grants happen in the tick engine)
# ---------------------------------------------------------------------------

@router.post("/admin/technologies/{code}/grant", response_model=UnlockRead, tags=["admin"])
def grant_unlock(
    code: str,
    body: UnlockGrant,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    technology = _technology_or_404(session, code)
    entity = session.get(Entity, body.entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    try:
        unlock = tech.grant_unlock(
            session, entity, technology, tick_number=next_tick_number(session)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if unlock is None:
        raise HTTPException(status_code=409, detail="Already unlocked")
    session.commit()
    session.refresh(unlock)
    return unlock


@router.get("/entities/{entity_id}/unlocks", response_model=list[UnlockRead])
def list_entity_unlocks(
    entity_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    entity = session.get(Entity, entity_id)
    if entity is None or (entity.owner_id != current_user.id and not current_user.is_admin):
        raise HTTPException(status_code=404, detail="Entity not found")
    return session.execute(
        select(Unlock)
        .join(Technology, Unlock.technology_id == Technology.id)
        .where((Unlock.entity_id.is_(None)) | (Unlock.entity_id == entity_id))
        .order_by(Technology.code)
    ).scalars().all()
