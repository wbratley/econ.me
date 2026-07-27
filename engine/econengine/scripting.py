"""
Script dispatch — wires HOOK and VALIDATOR scripts into the service layer,
and holds the intent resolver shared with the tick engine.

VALIDATOR  runs before every money operation with the operation as ctx.op;
           the chunk's return value is the verdict ({allow=false, reason=...}
           or a bare `false` denies; nil or {allow=true} allows). Fail-closed:
           an erroring or timed-out validator vetoes the operation. Validators
           are pure — any intents or state mutations they produce are ignored.

HOOK       runs after a successful operation with ctx.op (including the
           resulting transaction ids). Hooks persist ctx.state and may queue
           intents, which are resolved immediately with dispatch suppressed —
           a hook-triggered operation never re-fires hooks or validators, so
           recursion is impossible. A failing hook never fails the operation.

Scoping: a script with entity_id NULL applies to every operation; with
entity_id set it only fires for operations acted by that entity.
"""

import threading
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .lua_engine import Intent, LuaEngine
from .models import Account, Entity, Holding, Script, ScriptType


class OperationVetoedError(ValueError):
    pass


_engine = LuaEngine()
_local = threading.local()


def _depth() -> int:
    return getattr(_local, "depth", 0)


@contextmanager
def _suppressed():
    _local.depth = _depth() + 1
    try:
        yield
    finally:
        _local.depth -= 1


# ---------------------------------------------------------------------------
# Dispatch — called by the service layer
# ---------------------------------------------------------------------------

def fire_validators(session: Session, op: dict) -> None:
    """Run every applicable VALIDATOR; raise OperationVetoedError on deny."""
    if _depth():
        return
    for script in _applicable_scripts(session, ScriptType.VALIDATOR, op):
        result = _engine.run(script.source, _op_ctx(session, script, op), timeout_ms=script.timeout_ms)
        if result.error:
            raise OperationVetoedError(f"validator {script.name!r} failed: {result.error}")
        verdict = result.return_value
        if verdict is False:
            raise OperationVetoedError(f"vetoed by validator {script.name!r}")
        if isinstance(verdict, dict) and not verdict.get("allow", True):
            reason = verdict.get("reason") or "denied"
            raise OperationVetoedError(f"vetoed by validator {script.name!r}: {reason}")


def fire_hooks(session: Session, op: dict) -> None:
    """Run every applicable HOOK after a successful operation."""
    if _depth():
        return
    for script in _applicable_scripts(session, ScriptType.HOOK, op):
        result = _engine.run(script.source, _op_ctx(session, script, op), timeout_ms=script.timeout_ms)
        if result.error:
            continue  # a broken hook must not fail the operation
        script.state = dict(result.state_updates)
        with _suppressed():
            for intent in sorted(result.intents, key=lambda i: i.priority):
                resolve_intent(session, intent)


def _applicable_scripts(session: Session, script_type: ScriptType, op: dict):
    scripts = session.execute(
        select(Script)
        .where(Script.script_type == script_type, Script.is_active.is_(True))
        .order_by(Script.created_at, Script.id)
    ).scalars().all()
    return [s for s in scripts if s.entity_id is None or s.entity_id == op.get("entity_id")]


def _op_ctx(session: Session, script: Script, op: dict) -> dict:
    entity = session.get(Entity, op.get("entity_id")) if op.get("entity_id") else None
    return {
        "entity": {
            "id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type.value,
            "is_monetary_authority": entity.is_monetary_authority,
        } if entity else {},
        "accounts": [
            {"id": a.id, "currency": a.currency, "balance": str(a.balance)}
            for a in entity.accounts
        ] if entity else [],
        "events": [],
        "state": dict(script.state or {}),
        "op": op,
        "queries": build_queries(session),
    }


# ---------------------------------------------------------------------------
# Shared with the tick engine
# ---------------------------------------------------------------------------

def build_queries(session: Session) -> dict:
    """ctx.query.* — read-only, string results so Lua sees exact decimals."""
    from . import markets, tech  # deferred: markets imports this module

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
        market = markets.get_market(session, str(symbol))
        if market is None or market.last_price is None:
            return None
        return str(market.last_price)

    def holding(entity_id, symbol):
        h = markets.get_holding(session, str(entity_id), str(symbol).upper())
        return str(h.quantity) if h else "0"

    def has_unlock(entity_id, code):
        technology = tech.get_technology(session, str(code))
        if technology is None:
            return False
        return tech.has_unlock(session, str(entity_id), technology)

    def holders(symbol):
        """Every entity holding a positive quantity of the symbol, with the
        settlement account to pay them through.

        The register a share needs: an issuer cannot pay a dividend without
        knowing who its holders are, and once shares trade, a cap table
        cached in Script.state goes stale the first time one changes hands.

        Note this is a GLOBAL read — any script can enumerate holders of any
        symbol, which is right for a share register (real ones are public)
        and considerably more than that for, say, FOOD. If per-symbol
        visibility should be votable data rather than always-on, this is the
        place it would be gated; it is deliberately not gated yet.

        Ordered by entity id so a script iterating holders is deterministic.
        The account is the entity's first in `currency`, matching how
        ctx.accounts[1] is used everywhere else in this codebase.
        """
        rows = session.execute(
            select(Holding.entity_id, Holding.quantity)
            .where(Holding.symbol == str(symbol).upper(), Holding.quantity > 0)
            .order_by(Holding.entity_id)
        ).all()
        if not rows:
            return []

        # Accounts for the whole register in one query rather than one per
        # holder: a dividend reads this every payout period, and an N+1 here
        # would scale with the shareholder count on a hot path.
        holder_ids = [entity_id for entity_id, _ in rows]
        first_account: dict[str, str] = {}
        for account in session.execute(
            select(Account)
            .where(Account.entity_id.in_(holder_ids))
            .order_by(Account.entity_id, Account.currency, Account.id)
        ).scalars():
            first_account.setdefault(account.entity_id, account.id)

        return [
            {
                "entity_id": entity_id,
                "quantity": str(quantity),
                "account_id": first_account.get(entity_id),
            }
            for entity_id, quantity in rows
        ]

    return {
        "balance": balance,
        "total_supply": total_supply,
        "market_price": market_price,
        "holding": holding,
        "has_unlock": has_unlock,
        "holders": holders,
    }


def resolve_intent(session: Session, intent: Intent) -> dict:
    from . import markets, production, services  # deferred: all import this module

    event = {
        "type": intent.intent_type,
        "entity_id": intent.entity_id,
        "params": intent.params,
        "idempotency_key": intent.idempotency_key,
    }

    def rejected(reason: str) -> dict:
        return {**event, "status": "rejected", "reason": reason}

    def amount_of(key: str) -> Decimal:
        try:
            return Decimal(intent.params[key])
        except (InvalidOperation, KeyError, TypeError):
            raise ValueError(f"invalid {key}")

    reference = intent.params.get("reference", "")
    extra: dict = {}

    try:
        if intent.intent_type == "transfer":
            from_account = session.get(Account, intent.params.get("from_account_id"))
            to_account = session.get(Account, intent.params.get("to_account_id"))
            if from_account is None or to_account is None:
                return rejected("unknown account")
            if from_account.entity_id != intent.entity_id:
                return rejected("entity does not own source account")
            with session.begin_nested():
                services.transfer(session, from_account, to_account, amount_of("amount"), reference)

        elif intent.intent_type in ("issue_money", "retire_money"):
            account = session.get(Account, intent.params.get("account_id"))
            if account is None:
                return rejected("unknown account")
            if account.entity_id != intent.entity_id:
                return rejected("entity does not own account")
            op = services.issue_money if intent.intent_type == "issue_money" else services.retire_money
            with session.begin_nested():
                op(session, account, amount_of("amount"), reference)

        elif intent.intent_type == "place_order":
            with session.begin_nested():
                order = markets.place_order(
                    session,
                    intent.entity_id,
                    symbol=intent.params.get("symbol", ""),
                    side=intent.params.get("side", ""),
                    quantity=amount_of("quantity"),
                    limit_price=amount_of("limit_price"),
                    account_id=intent.params.get("account_id", ""),
                    reference=reference,
                )
            extra["order_id"] = order.id  # scripts need this to cancel later

        elif intent.intent_type == "cancel_order":
            with session.begin_nested():
                markets.cancel_order(session, intent.params.get("order_id", ""), intent.entity_id)

        elif intent.intent_type == "start_process":
            entity = session.get(Entity, intent.entity_id)
            if entity is None:
                return rejected("unknown entity")
            with session.begin_nested():
                process = production.start_process(
                    session, entity, intent.params.get("recipe", ""),
                    parcel_id=intent.params.get("parcel_id"),
                )
            extra["process_id"] = process.id  # scripts need this to cancel later

        elif intent.intent_type == "cancel_process":
            with session.begin_nested():
                production.cancel_process(
                    session, intent.params.get("process_id", ""), intent.entity_id
                )

        elif intent.intent_type == "transfer_parcel":
            from . import parcels
            to_entity = session.get(Entity, intent.params.get("to_entity_id"))
            if to_entity is None:
                return rejected("unknown recipient entity")
            with session.begin_nested():
                parcels.transfer_parcel(
                    session, intent.params.get("parcel_id", ""),
                    intent.entity_id, to_entity,
                )

        else:
            return rejected(f"unknown intent type {intent.intent_type!r}")

    except ValueError as exc:
        # InsufficientFunds / CurrencyMismatch / NotMonetaryAuthority /
        # OperationVetoed / InsufficientHoldings / MarketInactive / bad amount
        return rejected(str(exc))

    return {**event, "status": "applied", **extra}
