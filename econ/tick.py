"""
Tick engine — advances the simulation one step.

run_tick():
  1. loads active BEHAVIOUR scripts that are attached to an entity
  2. runs each in the sandboxed LuaEngine with a ctx built from live data
     (entity, accounts, the previous tick's events for that entity, and the
     script's persistent state); ctx.query.* reads the DB through the session
  3. persists each successful script's ctx.state mutations
  4. resolves all queued intents in priority order through the service layer,
     inside a savepoint each, so one bad intent cannot poison the rest
  5. records every outcome (applied / rejected / script_error) as an event
     on a new Tick row — those events feed ctx.events next tick

An intent may only move money out of accounts owned by the entity whose
script queued it; the service layer additionally enforces monetary-authority
rules and balance/currency invariants.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import services
from .lua_engine import Intent, LuaEngine
from .models import Account, Entity, Script, ScriptType, Tick


def run_tick(session: Session, lua_engine: LuaEngine | None = None) -> Tick:
    lua_engine = lua_engine or LuaEngine()
    started_at = datetime.now(timezone.utc)

    prev = session.execute(
        select(Tick).order_by(Tick.number.desc()).limit(1)
    ).scalar_one_or_none()
    number = prev.number + 1 if prev else 1
    prev_events = list(prev.events or []) if prev else []

    scripts = session.execute(
        select(Script)
        .where(
            Script.script_type == ScriptType.BEHAVIOUR,
            Script.is_active.is_(True),
            Script.entity_id.is_not(None),
        )
        .order_by(Script.created_at, Script.id)
    ).scalars().all()

    events: list[dict] = []
    intents: list[Intent] = []

    for script in scripts:
        entity = session.get(Entity, script.entity_id)
        if entity is None:
            continue
        ctx = _build_script_ctx(session, entity, script, prev_events)
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

    intents.sort(key=lambda i: i.priority)  # stable: script order breaks ties
    for intent in intents:
        events.append(_resolve_intent(session, intent))

    tick = Tick(
        number=number,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        events=events,
    )
    session.add(tick)
    session.flush()
    return tick


def _build_script_ctx(session: Session, entity: Entity, script: Script, prev_events: list) -> dict:
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
        "events": [e for e in prev_events if e.get("entity_id") == entity.id],
        "state": dict(script.state or {}),
        "queries": _build_queries(session),
    }


def _build_queries(session: Session) -> dict:
    """ctx.query.* — read-only, string results so Lua sees exact decimals."""

    def balance(account_id):
        acct = session.get(Account, str(account_id))
        return str(acct.balance) if acct else None

    def total_supply(currency):
        total = session.execute(
            select(func.coalesce(func.sum(Account.balance), 0))
            .where(Account.currency == str(currency).upper())
        ).scalar_one()
        return str(total)

    def market_price(symbol):
        return None  # no markets yet

    return {"balance": balance, "total_supply": total_supply, "market_price": market_price}


def _resolve_intent(session: Session, intent: Intent) -> dict:
    event = {
        "type": intent.intent_type,
        "entity_id": intent.entity_id,
        "params": intent.params,
        "idempotency_key": intent.idempotency_key,
    }

    def rejected(reason: str) -> dict:
        return {**event, "status": "rejected", "reason": reason}

    try:
        amount = Decimal(intent.params["amount"])
    except (InvalidOperation, KeyError, TypeError):
        return rejected("invalid amount")

    reference = intent.params.get("reference", "")

    try:
        if intent.intent_type == "transfer":
            from_account = session.get(Account, intent.params.get("from_account_id"))
            to_account = session.get(Account, intent.params.get("to_account_id"))
            if from_account is None or to_account is None:
                return rejected("unknown account")
            if from_account.entity_id != intent.entity_id:
                return rejected("entity does not own source account")
            with session.begin_nested():
                services.transfer(session, from_account, to_account, amount, reference)

        elif intent.intent_type in ("issue_money", "retire_money"):
            account = session.get(Account, intent.params.get("account_id"))
            if account is None:
                return rejected("unknown account")
            if account.entity_id != intent.entity_id:
                return rejected("entity does not own account")
            op = services.issue_money if intent.intent_type == "issue_money" else services.retire_money
            with session.begin_nested():
                op(session, account, amount, reference)

        else:
            return rejected(f"unknown intent type {intent.intent_type!r}")

    except ValueError as exc:
        # InsufficientFunds / CurrencyMismatch / NotMonetaryAuthority / bad amount
        return rejected(str(exc))

    return {**event, "status": "applied"}
