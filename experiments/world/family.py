"""The family genome installer — the aging-repro spec's seam, v1.

The engine's spawn mechanism deliberately does not endow: "how much a
child inherits is policy, not mechanism." This module is that policy
for the pack layer: every child (an ACTIVE entity with parents, no
behaviour of its own yet) receives its first living parent's ACTIVE
BEHAVIOUR — the genome is inherited whole (spec principle 4), the
age-branching inside it (the JUVENILE block) does the rest.

The long-term home is the spawn template (the spec's open mechanics
note); until then the WORLD DRIVER calls this after each tick — the
soak, the trainer's assay, the live harness's watcher. One function,
called once per tick, no context: the seam stays narrow on purpose.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine import services
from econengine.models import Entity, EntityStatus, Script, ScriptType


def install_family_genomes(session: Session) -> int:
    """Install the family genome on every unscripted child. Returns
    the number installed. Idempotent (a child with any active
    BEHAVIOUR is left alone); a parentless orphan is left scriptless
    (the census will read the orphanhood — that is a world fact, not
    an error to paper over)."""
    installed = 0
    children = session.execute(
        select(Entity).where(
            Entity.status == EntityStatus.ACTIVE,
            Entity.parents.is_not(None),
        ).order_by(Entity.created_at if hasattr(Entity, "created_at")
                   else Entity.id)
    ).scalars().all()
    for child in children:
        has_script = session.execute(
            select(Script.id).where(
                Script.entity_id == child.id,
                Script.script_type == ScriptType.BEHAVIOUR,
                Script.is_active.is_(True),
            )
        ).first()
        if has_script:
            continue
        for pid in child.parents or []:
            parent = session.get(Entity, pid)
            if parent is None:
                continue
            src = session.execute(
                select(Script).where(
                    Script.entity_id == parent.id,
                    Script.script_type == ScriptType.BEHAVIOUR,
                    Script.is_active.is_(True),
                ).order_by(Script.created_at.desc())
            ).scalars().first()
            if src is not None:
                services.set_entity_behaviour(
                    session, child, src.source,
                    owner_id=child.owner_id,
                    description=f"family genome from {parent.name}")
                installed += 1
                break
    if installed:
        session.flush()
    return installed
