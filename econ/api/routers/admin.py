from fastapi import APIRouter, Depends, HTTPException
import re
from sqlalchemy.orm import Session

from econengine import conditions, councils, delegations, scripting, services, tick
from econengine.capabilities import ALL as ALL_CAPABILITIES
from econ.api.deps import get_session, require_admin
from econ.api.onboarding import get_join_config, set_join_config
from econ.api.schemas import (
    AdminEntityCreate, ComputeBudgetRead, ComputeBudgetUpdate, CouncilRead,
    CouncilWrite, DelegationRead, DelegationWrite, EntityRead, EntityUpdate,
    EstateRuleRead, EstateRuleUpdate, JoinConfigRead, JoinConfigWrite,
    ScriptingTiersRead, UserRead, UserUpdate, WorldLibRead, WorldLibUpdate,
)
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
    if body.capabilities is not None:
        unknown = [c for c in body.capabilities if c not in ALL_CAPABILITIES]
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown capability: {unknown}")
        entity.capabilities = list(body.capabilities)
    if "heir_id" in body.model_fields_set:
        if body.heir_id is not None:
            if body.heir_id == entity.id:
                raise HTTPException(status_code=422, detail="Entity cannot be its own heir")
            if session.get(Entity, body.heir_id) is None:
                raise HTTPException(status_code=422, detail="Unknown heir entity")
        entity.heir_id = body.heir_id
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


@router.get("/estate-rule", response_model=EstateRuleRead)
def get_estate_rule(session: Session = Depends(get_session), _: User = Depends(require_admin)):
    rule = conditions.get_estate_rule(session)
    return EstateRuleRead(
        policy=rule.get("policy", "burn"),
        treasury_entity_id=rule.get("treasury_entity_id"),
    )


@router.put("/estate-rule", response_model=EstateRuleRead)
def set_estate_rule(
    body: EstateRuleUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    try:
        setting = conditions.set_estate_rule(
            session, body.policy, treasury_entity_id=body.treasury_entity_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    return EstateRuleRead(
        policy=setting.value["policy"],
        treasury_entity_id=setting.value.get("treasury_entity_id"),
    )


@router.get("/compute-budget", response_model=ComputeBudgetRead)
def get_compute_budget(session: Session = Depends(get_session), _: User = Depends(require_admin)):
    return ComputeBudgetRead(budget_ms=tick.get_compute_budget_ms(session))


@router.put("/compute-budget", response_model=ComputeBudgetRead)
def set_compute_budget(
    body: ComputeBudgetUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    tick.set_compute_budget_ms(session, body.budget_ms)
    session.commit()
    return ComputeBudgetRead(budget_ms=body.budget_ms)


# --- player onboarding config (the founder package; docs/game.md §12.6) ---

@router.get("/join-config", response_model=JoinConfigRead)
def get_join_config_endpoint(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Read the join-time founder package (endowment / currency / starter
    behaviour). Defaults if unset."""
    return JoinConfigRead(**get_join_config(session))


@router.put("/join-config", response_model=JoinConfigRead)
def set_join_config_endpoint(
    body: JoinConfigWrite,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Configure what a new player starts with on ``POST /join``.

    Merge semantics: only the fields you send change; the rest are left
    alone, so you can rotate the starter without touching the endowment."""
    fields = {k: v for k, v in body.model_dump().items() if k in body.model_fields_set}
    cfg = set_join_config(session, **fields)
    session.commit()
    return JoinConfigRead(**cfg)


# --- scripting: the per-world script library (docs/scripting.md) ---------

@router.get("/world-lib", response_model=WorldLibRead)
def get_world_lib_endpoint(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Read the world's script library (the `world` namespace injected into
    every script alongside the engine `std`)."""
    return WorldLibRead(source=scripting.get_world_lib(session))


@router.put("/world-lib", response_model=WorldLibRead)
def set_world_lib_endpoint(
    body: WorldLibUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Set the world lib -- operator fiat at world creation (docs/
    scripting.md settled decision #2; whether this becomes votable is
    deliberately open). The source must be a Lua chunk returning its
    namespace table and passes the install-time gate first: syntax,
    strict smoke-run, purity (a broken source is refused with 400, never
    silently tolerated as per-script errors)."""
    try:
        scripting.set_world_lib(session, body.source)
    except scripting.LibraryRejected as exc:
        raise HTTPException(status_code=400, detail=exc.problems)
    session.commit()
    return WorldLibRead(source=scripting.get_world_lib(session))


@router.delete("/world-lib", response_model=WorldLibRead)
def clear_world_lib_endpoint(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Clear the world lib: scripts keep the engine `std`, lose `world`."""
    scripting.set_world_lib(session, None)
    session.commit()
    return WorldLibRead(source=None)


# --- scripting: the content-pack lib (docs/scripting.md, tier three) ----

@router.get("/pack-lib", response_model=WorldLibRead)
def get_pack_lib_endpoint(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Read the content-pack lib (the `pack` namespace: the play opinions
    this world's starter inherits -- pricing adaptation, pantry policy)."""
    return WorldLibRead(source=scripting.get_pack_lib(session))


@router.put("/pack-lib", response_model=WorldLibRead)
def set_pack_lib_endpoint(
    body: WorldLibUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Set the pack lib. Same gate as the world lib: syntax, strict
    smoke-run, purity -- and remember the content-pack manifest pins the
    sha it was authored against (drift shows in /admin/scripting-tiers)."""
    try:
        scripting.set_pack_lib(session, body.source)
    except scripting.LibraryRejected as exc:
        raise HTTPException(status_code=400, detail=exc.problems)
    session.commit()
    return WorldLibRead(source=scripting.get_pack_lib(session))


@router.delete("/pack-lib", response_model=WorldLibRead)
def clear_pack_lib_endpoint(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Clear the pack lib: scripts lose `pack` (starter behaviours written
    against it will nil-call -- the tier is part of the pack contract)."""
    scripting.set_pack_lib(session, None)
    session.commit()
    return WorldLibRead(source=None)


# --- scripting: tier identity + gate status (determinism pinning) --------

@router.get("/scripting-tiers", response_model=ScriptingTiersRead)
def scripting_tiers_endpoint(
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """The identity of every script tier in this world (settled decision
    #1: determinism pinning): the engine-stdlib fingerprint and whether it
    matches the pinned baseline, lib shas, and the current gate verdicts.
    `matches_pinned: false` means the engine's stdlib changed under a
    running world -- replay inputs are suspect until the world re-pins."""
    return ScriptingTiersRead(**scripting.scripting_report(session))


# --- council registers (seeding membership for the council/weighted models) ---

_COUNCIL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,55}$")


def _validate_council_name(name: str) -> str:
    """A council name is a short, path-safe label (it becomes the
    weight-model scope, e.g. ``council:senate``). Reject colons and other
    characters that would collide with the spec grammar."""
    if not _COUNCIL_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail="council name must be 1-56 chars of [A-Za-z0-9_-]",
        )
    return name


@router.get("/councils/{name}", response_model=CouncilRead)
def get_council(
    name: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    _validate_council_name(name)
    return CouncilRead(name=name, members=councils.get_register(session, name))


@router.put("/councils/{name}", response_model=CouncilRead)
def set_council(
    name: str,
    body: CouncilWrite,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    _validate_council_name(name)
    try:
        councils.set_register(session, name, body.members)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    return CouncilRead(name=name, members=councils.get_register(session, name))


@router.delete("/councils/{name}", status_code=204)
def delete_council(
    name: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    _validate_council_name(name)
    councils.delete_register(session, name)
    session.commit()


# --- delegation registers (seeding the liquid-democracy graph) ---

#: A polity name obeys the same label rule as a council name (it becomes
#: the weight-model scope, e.g. ``liquid:senate``).
_validate_delegation_name = _validate_council_name


@router.get("/delegations/{name}", response_model=DelegationRead)
def get_delegations(
    name: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    _validate_delegation_name(name)
    return DelegationRead(
        name=name, delegations=delegations.get_delegations(session, name))


@router.put("/delegations/{name}", response_model=DelegationRead)
def set_delegations(
    name: str,
    body: DelegationWrite,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    _validate_delegation_name(name)
    try:
        delegations.set_delegations(session, name, body.delegations)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    session.commit()
    return DelegationRead(
        name=name, delegations=delegations.get_delegations(session, name))


@router.delete("/delegations/{name}", status_code=204)
def delete_delegations(
    name: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    _validate_delegation_name(name)
    delegations.delete_delegations(session, name)
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
