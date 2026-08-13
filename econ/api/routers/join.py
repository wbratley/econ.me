"""Player onboarding -- ``POST /join`` (docs/game.md §6, §12.6; Phase 1).

The entry point that makes this a game a player can actually *enter*. It is
platform orchestration over engine primitives: it creates a founder
INDIVIDUAL owned by the authenticated user, endows it (account + genesis
money from the world's ``join.config``), and applies the world's starter
BEHAVIOUR template if one is configured. No new mechanism -- just
``create_entity`` + ``create_account`` + ``set_entity_behaviour`` + the
server caps, wired together.

Safety is inherited, by construction:

  * **Autonomy tier, not fixed.** The founder is player-owned and ``is_fixed``
    is left False, so the player can immediately rewrite its behaviour via
    ``POST /entities/{id}/behaviour`` (the autonomy path, §6). Onboarding and
    autonomy compose: join seeds the default, the player edits from there.
  * **Capabilities don't breed (§8).** ``create_entity`` grants no capability,
    so a founder starts with none -- no SEIZE/LEVY/MONETARY_AUTHORITY arrive
    by joining. Any privilege is a later act of governance.
  * **Server caps enforced.** Join shares the same fairness gate as the
    engine's in-game spawn path (``spawn_entity``): a saturated world, or a
    player at their per-owner ceiling, is refused (409).
  * **Money-scope invariant untouched.** Onboarding endows *this* entity's
    account; whatever script runs as it can still only spend that account.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from econ.api.deps import get_current_user, get_session
from econ.api.onboarding import get_join_config
from econ.api.schemas import JoinResult
from econengine import services
from econengine.models import EntityType, User
from econengine.services import ServerCapExceededError

router = APIRouter(tags=["join"])


@router.post("/join", response_model=JoinResult, status_code=201)
def join_world(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Bring a new player into the world as a founder INDIVIDUAL.

    Reads the world's founder package (endowment / currency / starter
    behaviour) from ``join.config``, then builds the entity. The starter is
    optional: a world with no starter configured still lets a player join --
    they get a blank founder they must script themselves (the autonomy path).
    """
    cfg = get_join_config(session)

    # The same non-votable fairness gate as spawn_entity: a saturated world
    # or a player at their ceiling is refused before any entity is created.
    try:
        services._enforce_server_caps(session, current_user.id)
    except ServerCapExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    entity = services.create_entity(session, "Founder", EntityType.INDIVIDUAL)
    entity.owner_id = current_user.id
    account = services.create_account(
        session, entity, cfg["currency"], cfg["endowment"],
    )

    behaviour = None
    starter = cfg["starter_behaviour"]
    if starter:
        behaviour = services.set_entity_behaviour(
            session, entity, starter, owner_id=current_user.id,
        )

    session.commit()
    session.refresh(entity)
    session.refresh(account)
    if behaviour is not None:
        session.refresh(behaviour)
    return JoinResult(entity=entity, account=account, behaviour=behaviour)
