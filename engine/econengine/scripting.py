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

import json
import threading
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .lua_engine import Intent, LuaEngine
from . import capabilities as _capabilities
from .models import Account, Entity, Holding, Script, ScriptType, Proposal, ProposalStatus, VoteChoice


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
            "capabilities": list(entity.capabilities or []),
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

    def fiscal_policy():
        """The government's votable fiscal-policy dict (or {} if unset).

        This is the read side a government POLICY script uses to turn
        enacted rates into levy calls: citizens vote on the *numbers*
        (set_fiscal_policy), the script reads them here, and the engine
        mechanism (services.levy) does the moving. Global read — any
        script may see the published policy, the way real tax schedules
        are public.
        """
        from . import fiscal
        return fiscal.get_fiscal_policy(session)

    def active_script(lineage_id):
        """The currently-active version of a law (lineage), or None.

        Returns the source so a POLICY script can read another law's text,
        and so the platform can render the live law. Resolves by
        lineage_id + is_active — the retire-old/activate-new identity.
        """
        s = session.execute(
            select(Script).where(
                Script.lineage_id == str(lineage_id),
                Script.is_active.is_(True),
            )
        ).scalars().first()
        if s is None:
            return None
        return {
            "id": s.id,
            "name": s.name,
            "script_type": s.script_type.value,
            "source": s.source,
            "entity_id": s.entity_id,
            "lineage_id": s.lineage_id,
        }

    def script_history(lineage_id):
        """Every version of a law (lineage), oldest first — the legislative
        record that retire-old/activate-new preserves. Metadata only (no
        source) to keep the audit view cheap; read a version's source by id
        where needed.
        """
        rows = session.execute(
            select(Script)
            .where(Script.lineage_id == str(lineage_id))
            .order_by(Script.created_at, Script.id)
        ).scalars().all()
        return [
            {
                "id": s.id,
                "name": s.name,
                "script_type": s.script_type.value,
                "is_active": s.is_active,
                "entity_id": s.entity_id,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in rows
        ]

    def _proposal_view(p):
        return {
            "id": p.id,
            "title": p.title,
            "proposer_id": p.proposer_id,
            "target_id": p.target_id,
            "weight_model": p.weight_model,
            "threshold": p.threshold,
            "quorum": p.quorum,
            "mutations": list(p.mutations or []),
            "status": p.status.value,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "enacted_at": p.enacted_at.isoformat() if p.enacted_at else None,
            "tally_yes": p.tally_yes,
            "tally_no": p.tally_no,
            "tally_turnout": p.tally_turnout,
            "failure_reason": p.failure_reason,
        }

    def proposal(proposal_id):
        """One proposal by id — the record a voter or the platform reads to
        see status, threshold/quorum, and the snapshotted tally."""
        p = session.get(Proposal, str(proposal_id))
        return _proposal_view(p) if p is not None else None

    def proposals(status=None):
        """Every proposal (optionally filtered by status), newest last."""
        stmt = select(Proposal)
        if status is not None:
            try:
                wanted = ProposalStatus(str(status))
            except ValueError:
                return []
            stmt = stmt.where(Proposal.status == wanted)
        stmt = stmt.order_by(Proposal.created_at, Proposal.id)
        return [_proposal_view(p) for p in session.execute(stmt).scalars()]

    def tally(proposal_id):
        """Live tally — yes/no/turnout computed now from recorded votes and
        the current electorate. For a closed proposal this recomputes; the
        authoritative record is the proposal's snapshotted tally_* columns."""
        from . import weights
        p = session.get(Proposal, str(proposal_id))
        if p is None:
            return None
        yes = sum((Decimal(v.weight) for v in p.votes if v.choice == VoteChoice.FOR), Decimal(0))
        no = sum((Decimal(v.weight) for v in p.votes if v.choice == VoteChoice.AGAINST), Decimal(0))
        electorate_total = sum(weights.electorate(session, p.weight_model).values(), Decimal(0))
        cast = yes + no
        turnout = str(cast / electorate_total) if electorate_total > 0 else "0"
        return {
            "proposal_id": p.id,
            "yes": str(yes),
            "no": str(no),
            "cast": str(cast),
            "electorate": str(electorate_total),
            "turnout": turnout,
        }

    return {
        "balance": balance,
        "total_supply": total_supply,
        "market_price": market_price,
        "holding": holding,
        "has_unlock": has_unlock,
        "holders": holders,
        "fiscal_policy": fiscal_policy,
        "active_script": active_script,
        "script_history": script_history,
        "proposal": proposal,
        "proposals": proposals,
        "tally": tally,
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

    # Capability gate — the same boundary that enforces ownership also
    # enforces privilege. An intent type listed in INTENT_CAPABILITIES may
    # only be queued by an entity holding that capability; without it the
    # intent is rejected before any service is touched. Ordinary
    # self-directed action (trade, produce, move your own money) is listed
    # nowhere and requires only ownership, which each branch checks below.
    required_cap = _capabilities.required_for(intent.intent_type)
    if required_cap is not None:
        entity = session.get(Entity, intent.entity_id)
        if entity is None or not entity.has_capability(required_cap):
            return rejected(f"missing capability {required_cap!r}")

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

        elif intent.intent_type == "levy":
            # Privileged transfer: the authority compels money out of an
            # account it does NOT own, into its own. The capability gate
            # above already proved `entity` holds LEVY; here we only check
            # the recipient side — the authority must own `to_account` —
            # and let services.levy bypass ownership on `from_account`.
            from_account = session.get(Account, intent.params.get("from_account_id"))
            to_account = session.get(Account, intent.params.get("to_account_id"))
            if from_account is None or to_account is None:
                return rejected("unknown account")
            if to_account.entity_id != intent.entity_id:
                return rejected("entity does not own recipient account")
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            with session.begin_nested():
                services.levy(
                    session, authority, from_account, to_account,
                    amount_of("amount"),
                    intent.params.get("rule_ref", ""),
                    reference,
                )

        elif intent.intent_type == "set_fiscal_policy":
            # Replace the votable fiscal-policy dict. The capability gate
            # above already proved `entity` holds SET_FISCAL_POLICY; the
            # policy rides as a JSON string (intent params are stringly
            # typed) and is parsed here so the service stays
            # transport-agnostic (it takes a dict, like levy takes a
            # Decimal). services.set_fiscal_policy re-checks the capability
            # and fires a VALIDATOR — a constitutional veto on the rate.
            try:
                policy = json.loads(intent.params.get("policy", "") or "{}")
            except ValueError:
                return rejected("invalid fiscal policy JSON")
            if not isinstance(policy, dict):
                return rejected("fiscal policy must be a JSON object")
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            with session.begin_nested():
                services.set_fiscal_policy(session, authority, policy, reference)

        elif intent.intent_type == "set_script":
            # Governed lawmaking (step 4a-1): enact a new version of a law.
            # The capability gate above already proved `entity` holds
            # LEGISLATE; services.set_script enforces it again and keeps
            # validators out of reach (they are the constitution). A law is
            # identified by lineage_id; the service retires the active
            # version and activates this source as a new one.
            authority = session.get(Entity, intent.entity_id)
            if authority is None:
                return rejected("unknown entity")
            raw_type = intent.params.get("script_type", "")
            try:
                script_type = ScriptType(raw_type)
            except ValueError:
                return rejected(f"unknown script_type {raw_type!r}")
            lineage_id = intent.params.get("lineage_id", "")
            if not lineage_id:
                return rejected("lineage_id required")
            bound_entity_id = intent.params.get("entity_id") or None
            with session.begin_nested():
                script = services.set_script(
                    session, authority, script_type, lineage_id,
                    intent.params.get("source", ""),
                    entity_id=bound_entity_id,
                    description=intent.params.get("description", ""),
                    timeout_ms=int(intent.params.get("timeout_ms", "100")),
                    reference=reference,
                )
            extra["script_id"] = script.id
            extra["lineage_id"] = lineage_id

        elif intent.intent_type == "create_proposal":
            # Open a proposal for vote (step 4a-ii). No capability gates
            # this — participation *is* the electorate, defined by the
            # weight model (form of government as data), so the proposer
            # must be a member. The proposal is inert until enacted; the
            # target is the government whose capabilities the mutations
            # will exercise.
            from . import weights
            target = session.get(Entity, intent.params.get("target_id", ""))
            if target is None:
                return rejected("unknown target entity")
            weight_model = intent.params.get("weight_model", "")
            try:
                proposer_weight = weights.weight_of(session, weight_model, intent.entity_id)
            except ValueError as exc:
                return rejected(str(exc))
            if proposer_weight <= 0:
                return rejected("proposer is not in the electorate")
            try:
                mutations = json.loads(intent.params.get("mutations", "[]"))
            except ValueError:
                return rejected("invalid mutations JSON")
            try:
                with session.begin_nested():
                    proposal = services.create_proposal(
                        session, intent.entity_id, target.id,
                        intent.params.get("title", ""),
                        weight_model,
                        Decimal(intent.params.get("threshold", "0.5")),
                        Decimal(intent.params.get("quorum", "0")),
                        mutations,
                        reference,
                    )
            except ValueError as exc:
                return rejected(str(exc))
            extra["proposal_id"] = proposal.id

        elif intent.intent_type == "vote":
            # Cast a for/against. Gated by electorate membership (the
            # resolver): a non-member gets weight 0 and is rejected. The
            # weight is snapshotted at cast time. Idempotent per voter.
            from . import weights
            proposal = session.get(Proposal, intent.params.get("proposal_id", ""))
            if proposal is None:
                return rejected("unknown proposal")
            if proposal.status != ProposalStatus.OPEN:
                return rejected(f"proposal is {proposal.status.value}, not open")
            choice_raw = intent.params.get("choice", "")
            try:
                choice = VoteChoice(choice_raw)
            except ValueError:
                return rejected("choice must be 'for' or 'against'")
            try:
                voter_weight = weights.weight_of(session, proposal.weight_model, intent.entity_id)
            except ValueError as exc:
                return rejected(str(exc))
            if voter_weight <= 0:
                return rejected("voter is not in the electorate")
            try:
                with session.begin_nested():
                    vote = services.cast_vote(
                        session, proposal, intent.entity_id, choice, voter_weight, reference,
                    )
            except ValueError as exc:
                return rejected(str(exc))
            extra["vote_id"] = vote.id
            extra["choice"] = choice.value
            extra["weight"] = str(voter_weight)

        elif intent.intent_type == "enact":
            # Tally and apply. The capability gate above already proved the
            # enactor (the target government) holds LEGISLATE; here we also
            # confirm the enactor IS this proposal's target. enact_proposal
            # tallies and, on pass, applies the mutations atomically as the
            # target — re-running each through resolve_intent, so caps and
            # validators fire. A failed tally is still an "applied" intent
            # (it did its job); the outcome rides in extra.
            proposal = session.get(Proposal, intent.params.get("proposal_id", ""))
            if proposal is None:
                return rejected("unknown proposal")
            if proposal.status != ProposalStatus.OPEN:
                return rejected(f"proposal is {proposal.status.value}, not open")
            if proposal.target_id != intent.entity_id:
                return rejected("only the target government may enact this proposal")
            with session.begin_nested():
                outcome = services.enact_proposal(session, proposal, reference)
            extra["proposal_id"] = proposal.id
            extra["proposal_status"] = outcome["status"]
            extra["tally_yes"] = str(outcome["yes"])
            extra["tally_no"] = str(outcome["no"])
            extra["tally_turnout"] = str(outcome["turnout"])
            if outcome.get("reason"):
                extra["reason"] = outcome["reason"]

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
        outcome = rejected(str(exc))
        if isinstance(exc, markets.InsufficientHoldingsError):
            # The one rejection this tick's auction could still cure. Flagged by
            # exception type rather than left for the caller to pattern-match
            # out of `reason`, which is a human-readable string and not a
            # contract. run_tick uses this to retry the intent after clearing.
            outcome["short_of_holdings"] = True
        return outcome

    return {**event, "status": "applied", **extra}
