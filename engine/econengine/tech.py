"""
Tech tree — technologies, prerequisites, and unlocks.

A Technology is a data row with a set of prerequisite technologies (a DAG).
Research is NOT a separate mechanism: a research project is a recipe whose
output is an unlock rather than goods (see production.py), and the whole
engine mechanism is one question — "does the entity's (or world's) unlock
set contain the recipe's requirements?".

Scope is a per-Technology column, not a single world default (design.md §7):
ENTITY-scoped technologies are researched by and unlocked for one entity at
a time (a smithing rank is per-person); WORLD-scoped technologies unlock for
everyone the moment anyone first completes them (shared physics knowledge).
Which scope each technology uses is genesis data / votable, like the DAG
itself and the research recipes' costs.

The DAG is acyclic by construction: prerequisites are fixed at creation and
must already exist, so a technology can never reference itself or a later
one. Prerequisites are enforced at grant time — an unlock (research OR admin
grant) requires the holder to already have every prerequisite — while recipe
gating is enforced at process start. Unlocks are never revoked.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Entity, Technology, TechnologyPrerequisite, TechScope, Unlock


def create_technology(
    session: Session,
    code: str,
    prerequisites: list[str] | None = None,
    scope: TechScope = TechScope.ENTITY,
    name: str = "",
    description: str = "",
) -> Technology:
    prereq_rows = []
    for prereq_code in sorted({str(c).upper() for c in (prerequisites or [])}):
        prereq = get_technology(session, prereq_code)
        if prereq is None:
            raise ValueError(f"no technology {prereq_code!r}")
        prereq_rows.append(TechnologyPrerequisite(prerequisite=prereq))

    technology = Technology(
        code=code.upper(),
        name=name,
        description=description,
        scope=scope,
        prerequisites=prereq_rows,
    )
    session.add(technology)
    session.flush()
    return technology


def get_technology(session: Session, code: str) -> Technology | None:
    return session.execute(
        select(Technology).where(Technology.code == str(code).upper())
    ).scalar_one_or_none()


def has_unlock(session: Session, entity_id: str, technology: Technology) -> bool:
    """True if the entity can use the technology: it holds an entity-scoped
    unlock, or a world unlock (entity_id NULL) exists. Checked against rows,
    not the technology's current scope, so a scope change never strips
    unlocks already granted."""
    return session.execute(
        select(Unlock.id).where(
            Unlock.technology_id == technology.id,
            (Unlock.entity_id.is_(None)) | (Unlock.entity_id == entity_id),
        ).limit(1)
    ).scalar_one_or_none() is not None


def check_prerequisites(session: Session, entity_id: str, technology: Technology) -> list[str]:
    """Codes of the technology's prerequisites the entity does not yet have."""
    return sorted(
        p.prerequisite.code
        for p in technology.prerequisites
        if not has_unlock(session, entity_id, p.prerequisite)
    )


def grant_unlock(
    session: Session, entity: Entity, technology: Technology, tick_number: int
) -> Unlock | None:
    """Grant the technology to the entity (or to the world, if WORLD-scoped).
    Idempotent — returns None if already held. Raises if a prerequisite is
    missing: the DAG binds admin grants exactly as it binds research."""
    if has_unlock(session, entity.id, technology):
        return None
    missing = check_prerequisites(session, entity.id, technology)
    if missing:
        raise ValueError(
            f"technology {technology.code} requires {', '.join(missing)}"
        )
    unlock = Unlock(
        technology=technology,
        entity_id=None if technology.scope == TechScope.WORLD else entity.id,
        unlocked_tick=tick_number,
    )
    session.add(unlock)
    session.flush()
    return unlock


def entity_unlocks(session: Session, entity_id: str) -> list[str]:
    """Sorted codes of every technology the entity can use (own + world)."""
    rows = session.execute(
        select(Technology.code)
        .join(Unlock, Unlock.technology_id == Technology.id)
        .where((Unlock.entity_id.is_(None)) | (Unlock.entity_id == entity_id))
        .distinct()
        .order_by(Technology.code)
    ).scalars().all()
    return list(rows)
