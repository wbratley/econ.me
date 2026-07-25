from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from econ import production
from econ.api.deps import get_current_user, get_session, require_admin
from econ.api.schemas import ProcessCreate, ProcessRead, RecipeCreate, RecipeRead, RecipeUpdate
from econ.models import Entity, Process, Recipe, User

router = APIRouter(tags=["production"])


def _recipe_or_404(session: Session, code: str) -> Recipe:
    recipe = production.get_recipe(session, code)
    if recipe is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return recipe


def _parse_quantities(raw: dict[str, str], field: str) -> dict[str, Decimal]:
    try:
        return {symbol: Decimal(q) for symbol, q in raw.items()}
    except InvalidOperation:
        raise HTTPException(status_code=422, detail=f"Invalid quantity in {field}")


# ---------------------------------------------------------------------------
# Recipes — public data, admin-managed
# ---------------------------------------------------------------------------

@router.get("/recipes", response_model=list[RecipeRead])
def list_recipes(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return session.execute(select(Recipe).order_by(Recipe.code)).scalars().all()


@router.get("/recipes/{code}", response_model=RecipeRead)
def get_recipe(
    code: str,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _recipe_or_404(session, code)


@router.post("/admin/recipes", response_model=RecipeRead, status_code=201, tags=["admin"])
def create_recipe(
    body: RecipeCreate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    if production.get_recipe(session, body.code) is not None:
        raise HTTPException(status_code=409, detail="Recipe already exists")
    try:
        recipe = production.create_recipe(
            session,
            body.code,
            inputs=_parse_quantities(body.inputs, "inputs"),
            outputs=_parse_quantities(body.outputs, "outputs"),
            duration_ticks=body.duration_ticks,
            name=body.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(recipe)
    return recipe


@router.patch("/admin/recipes/{code}", response_model=RecipeRead, tags=["admin"])
def update_recipe(
    code: str,
    body: RecipeUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    recipe = _recipe_or_404(session, code)
    if body.name is not None:
        recipe.name = body.name
    if body.is_active is not None:
        recipe.is_active = body.is_active
    session.commit()
    session.refresh(recipe)
    return recipe


# ---------------------------------------------------------------------------
# Processes — start production on an entity the user owns
# ---------------------------------------------------------------------------

@router.post("/processes", response_model=ProcessRead, status_code=201)
def start_process(
    body: ProcessCreate,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    entity = session.get(Entity, body.entity_id)
    if entity is None or entity.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Entity not found")
    try:
        process = production.start_process(session, entity, body.recipe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(process)
    return process


@router.get("/processes", response_model=list[ProcessRead])
def list_processes(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return session.execute(
        select(Process)
        .join(Entity, Process.entity_id == Entity.id)
        .where(Entity.owner_id == current_user.id)
        .order_by(Process.created_at.desc(), Process.id.desc())
    ).scalars().all()


@router.post("/processes/{process_id}/cancel", response_model=ProcessRead)
def cancel_process(
    process_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    process = session.get(Process, process_id)
    if process is None or process.entity.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Process not found")
    try:
        production.cancel_process(session, process_id, process.entity_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    session.refresh(process)
    return process
