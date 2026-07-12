"""
Tick engine — advances the simulation one step.

run_tick():
  1. runs active POLICY scripts (attached to an entity, e.g. a central bank)
     first — they see ALL of the previous tick's events
  2. then runs active BEHAVIOUR scripts, which see only the previous tick's
     events for their own entity
  3. persists each successful script's ctx.state mutations
  4. resolves all queued intents in priority order through the service layer
     (policy intents come first on priority ties), inside a savepoint each,
     so one bad intent cannot poison the rest
  5. records every outcome (applied / rejected / script_error) as an event
     on a new Tick row — those events feed ctx.events next tick

An intent may only move money out of accounts owned by the entity whose
script queued it; the service layer additionally enforces monetary-authority
rules, balance/currency invariants, and VALIDATOR scripts.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .lua_engine import Intent, LuaEngine
from .models import Entity, Script, ScriptType, Tick
from .scripting import build_queries, resolve_intent


def run_tick(session: Session, lua_engine: LuaEngine | None = None) -> Tick:
    lua_engine = lua_engine or LuaEngine()
    started_at = datetime.now(timezone.utc)

    prev = session.execute(
        select(Tick).order_by(Tick.number.desc()).limit(1)
    ).scalar_one_or_none()
    number = prev.number + 1 if prev else 1
    prev_events = list(prev.events or []) if prev else []

    events: list[dict] = []
    intents: list[Intent] = []

    # POLICY scripts run first and see every event from the previous tick;
    # BEHAVIOUR scripts see only their own entity's events.
    for script_type in (ScriptType.POLICY, ScriptType.BEHAVIOUR):
        for script in _tick_scripts(session, script_type):
            entity = session.get(Entity, script.entity_id)
            if entity is None:
                continue
            entity_events = (
                prev_events if script_type == ScriptType.POLICY
                else [e for e in prev_events if e.get("entity_id") == entity.id]
            )
            ctx = _build_script_ctx(session, entity, script, entity_events)
            result = lua_engine.run(script.source, ctx, timeout_ms=script.timeout_ms)
            if result.error:
                events.append({
                    "type": "script_error",
                    "entity_id": entity.id,
                    "script_id": script.id,
                    "error": result.error,
                })
                continue
            script.state = dict(result.state_updates)
            intents.extend(result.intents)

    intents.sort(key=lambda i: i.priority)  # stable: collection order breaks ties
    for intent in intents:
        events.append(resolve_intent(session, intent))

    tick = Tick(
        number=number,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        events=events,
    )
    session.add(tick)
    session.flush()
    return tick


def _tick_scripts(session: Session, script_type: ScriptType):
    return session.execute(
        select(Script)
        .where(
            Script.script_type == script_type,
            Script.is_active.is_(True),
            Script.entity_id.is_not(None),
        )
        .order_by(Script.created_at, Script.id)
    ).scalars().all()


def _build_script_ctx(session: Session, entity: Entity, script: Script, entity_events: list) -> dict:
    return {
        "entity": {
            "id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type.value,
            "is_monetary_authority": entity.is_monetary_authority,
        },
        "accounts": [
            {"id": a.id, "currency": a.currency, "balance": str(a.balance)}
            for a in entity.accounts
        ],
        "events": entity_events,
        "state": dict(script.state or {}),
        "queries": build_queries(session),
    }
