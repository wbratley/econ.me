"""The direct-action channel, one submission path for every authoring
surface: POST /intents (machine clients), the MCP perform_action tool
(seats), and the future human client. Policy lives here, mechanics in
the engine:

- World clock OFF: resolve immediately between ticks (design.md §4.5,
  unchanged — the batch API's contract since the beginning).
- World clock ARMED: park the intent in the engine's pending_intents
  outbox. run_tick drains it into the same resolution pass scripts
  feed, so the action lands as a tick event every observer sees —
  a direct ``say`` is HEARD, and it shares the one-say-per-tick budget
  with the entity's own script. Latency is one clock interval at most.
"""

from sqlalchemy.orm import Session

from econ.api.rounds import world_tick_seconds
from econengine import scripting
from econengine.lua_engine import Intent
from econengine.models import Entity


def submit_intent(session: Session, entity: Entity, intent_type: str,
                  params: dict, priority: int = 100) -> dict:
    """Queue (clock armed) or resolve now (clock off) as the entity.
    Caller has already proven ownership; the resolver re-proves every
    gate either way."""
    if world_tick_seconds() > 0:
        row = scripting.queue_intent(
            session, entity.id, intent_type, params, priority)
        session.commit()
        return {
            "type": row.intent_type,
            "entity_id": row.entity_id,
            "params": row.params,
            "idempotency_key": row.idempotency_key,
            "status": "queued",
            "reason": None,
        }
    intent = Intent(entity_id=entity.id, intent_type=str(intent_type),
                    params=dict(params), resource_ids=[],
                    priority=int(priority))
    result = scripting.resolve_intent(session, intent)
    session.commit()
    return result
