"""
Tick engine — advances the simulation one step.

run_tick():
  1. completes every production process due this tick — outputs are
     credited BEFORE scripts run, so a script can sell fresh goods in this
     tick's auction
  2. auto-issues goods (top-up to each Good's quantity) — also before
     scripts, so fresh labor is sellable in this tick's auction
  3. runs active POLICY scripts (attached to an entity, e.g. a central bank)
     first — they see ALL of the previous tick's events
  4. then runs active BEHAVIOUR scripts, which see only the previous tick's
     events for their own entity
  5. persists each successful script's ctx.state mutations
  6. resolves all queued intents in priority order through the service layer
     (policy intents come first on priority ties), inside a savepoint each,
     so one bad intent cannot poison the rest
  7. clears every active commodity market in a uniform-price call auction —
     orders placed this tick (by scripts or via the API since the last tick)
     participate in this tick's auction
  8. runs the consumption pass — AFTER the auction, so goods bought this
     tick are eaten this tick and sell orders settle before anything is
     eaten; draws down need-satisfying holdings and rewrites satisfaction
     scores, emitting per-entity need_satisfied / need_unmet events
  9. decays perishable goods — AFTER consumption, so entities eat fresh
     stock and only unsold, uneaten perishables rot
 10. records every outcome (applied / rejected / script_error / trade /
     order_cancelled / auction / process_completed / auto_issue /
     need_satisfied / need_unmet / decay) as an event on a new Tick row —
     those events feed ctx.events next tick

An intent may only move money out of accounts owned by the entity whose
script queued it; the service layer additionally enforces monetary-authority
rules, balance/currency invariants, and VALIDATOR scripts.
"""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import goods, markets, needs, production
from .lua_engine import Intent, LuaEngine
from .models import (
    Entity, Holding, Need, NeedState, Process, ProcessStatus, Script, ScriptType, Tick,
)
from .scripting import build_queries, resolve_intent


def run_tick(session: Session, lua_engine: LuaEngine | None = None) -> Tick:
    lua_engine = lua_engine or LuaEngine()
    started_at = datetime.now(timezone.utc)

    prev = session.execute(
        select(Tick).order_by(Tick.number.desc()).limit(1)
    ).scalar_one_or_none()
    number = prev.number + 1 if prev else 1
    prev_events = list(prev.events or []) if prev else []

    events: list[dict] = list(production.complete_processes(session, tick_number=number))
    events.extend(goods.auto_issue(session, tick_number=number))
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

    events.extend(markets.run_auctions(session, tick_number=number))
    events.extend(needs.run_consumption(session, tick_number=number))
    events.extend(goods.apply_decay(session, tick_number=number))

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
        "holdings": [
            {"symbol": h.symbol, "quantity": str(h.quantity)}
            for h in session.execute(
                select(Holding).where(Holding.entity_id == entity.id).order_by(Holding.symbol)
            ).scalars()
        ],
        "processes": [
            {
                "id": p.id,
                "recipe": p.recipe.code,
                "started_tick": p.started_tick,
                "completes_tick": p.completes_tick,
            }
            for p in session.execute(
                select(Process)
                .where(Process.entity_id == entity.id, Process.status == ProcessStatus.RUNNING)
                .order_by(Process.created_at, Process.id)
            ).scalars()
        ],
        "needs": _entity_needs(session, entity),
        "events": entity_events,
        "state": dict(script.state or {}),
        "queries": build_queries(session),
    }


def _entity_needs(session: Session, entity: Entity) -> list[dict]:
    """Every active need that applies to the entity, with its current
    satisfaction ("0" before the first consumption pass)."""
    applicable = session.execute(
        select(Need)
        .where(
            Need.is_active.is_(True),
            (Need.entity_type.is_(None)) | (Need.entity_type == entity.entity_type),
        )
        .order_by(Need.priority, Need.code)
    ).scalars().all()
    states = {
        s.need_id: s.satisfaction
        for s in session.execute(
            select(NeedState).where(NeedState.entity_id == entity.id)
        ).scalars()
    }
    return [
        {
            "code": n.code,
            "priority": n.priority,
            "quantity_per_tick": str(n.quantity_per_tick),
            "satisfiers": [s.symbol for s in n.satisfiers],
            "satisfaction": str(states.get(n.id, Decimal("0"))),
        }
        for n in applicable
    ]
