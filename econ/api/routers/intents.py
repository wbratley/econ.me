"""Machine-client intent API (design.md §4.5): a faster, batch-capable
channel for programmatic clients that reuses the exact resolver
(scripting.resolve_intent) tick.py drains script-queued intents through —
no bespoke per-action endpoint, no duplicated business rules. Existing
per-type routers (production.py, markets.py, ...) are unchanged; this is
an additive alternative path.

Under the world clock (#194) submissions QUEUE (econ.api.intents) and
resolve at the next tick's intent pass, where their events land in the
Tick row every observer reads; with the clock off the historical
between-ticks immediate resolution is bit-identical to before."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from econ.api.deps import get_current_user, get_session
from econ.api.intents import submit_intent
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
        entity = session.get(Entity, item.entity_id)
        if entity is None or entity.owner_id != current_user.id:
            results.append({
                "type": item.type,
                "entity_id": item.entity_id,
                "params": item.params,
                "idempotency_key": "",
                "status": "rejected",
                "reason": "entity not found",
            })
            continue
        results.append(submit_intent(
            session, entity, item.type, item.params, item.priority))
    return results
