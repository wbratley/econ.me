"""Machine-client intent API (design.md §4.5): a faster, batch-capable
channel for programmatic clients that reuses the exact resolver
(scripting.resolve_intent) tick.py drains script-queued intents through —
no bespoke per-action endpoint, no duplicated business rules. Existing
per-type routers (production.py, markets.py, ...) are unchanged; this is
an additive alternative path."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from econengine import scripting
from econengine.lua_engine import Intent
from econ.api.deps import get_current_user, get_session
from econ.api.schemas import IntentRequest, IntentResult
from econengine.models import Entity, User

router = APIRouter(tags=["intents"])


@router.post("/intents", response_model=list[IntentResult])
def submit_intents(
    body: list[IntentRequest],
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    results = []
    for item in body:
        intent = Intent(
            entity_id=item.entity_id,
            intent_type=item.type,
            params=item.params,
            resource_ids=[],
            priority=item.priority,
        )
        entity = session.get(Entity, item.entity_id)
        if entity is None or entity.owner_id != current_user.id:
            results.append({
                "type": intent.intent_type,
                "entity_id": intent.entity_id,
                "params": intent.params,
                "idempotency_key": intent.idempotency_key,
                "status": "rejected",
                "reason": "entity not found",
            })
            continue
        results.append(scripting.resolve_intent(session, intent))
    session.commit()
    return results
