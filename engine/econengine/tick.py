"""
Tick engine — advances the simulation one step.

run_tick():
  1. completes every production process due this tick — outputs are
     credited BEFORE scripts run, so a script can sell fresh goods in this
     tick's auction
  2. auto-issues goods (top-up to each Good's quantity) — also before
     scripts, so fresh labor is sellable in this tick's auction
  2b. regenerates deposits (regen_per_tick toward capacity) — also before
     scripts, so an extraction script sees the replenished deposit
  3. runs active POLICY scripts (attached to an entity, e.g. a central bank)
     first — they see ALL of the previous tick's events
  4. then runs active BEHAVIOUR scripts, which see only the previous tick's
     events for their own entity. If entity_tick_compute_budget_ms is set
     (world_settings, votable data — get_/set_compute_budget_ms), an
     entity's POLICY+BEHAVIOUR scripts stop running for the rest of this
     tick once their cumulative elapsed_ms meets the budget; skipped runs
     emit compute_budget_exceeded instead of executing. Unset = unlimited.
  5. persists each successful script's ctx.state mutations
  6. resolves all queued intents in priority order through the service layer
     (policy intents come first on priority ties), inside a savepoint each,
     so one bad intent cannot poison the rest
  7. clears every active commodity market in a uniform-price call auction —
     orders placed this tick (by scripts or via the API since the last tick)
     participate in this tick's auction
 7b. retries any start_process from step 6 that was rejected solely for want
     of inputs — AFTER the auction, so a process can be fed by what its
     entity bought this tick instead of waiting a tick and paying a round of
     decay first. Only that rejection is retried, and the step-6 attempt is
     what fixes ordering against sell orders of the same good, so each intent
     still yields exactly one event
 7c. draws each RUNNING process's per_tick_inputs from its entity's holdings
     (production.consume_per_tick_inputs) — AFTER the retry so a process can
     be fed by labour its entity converted this tick, BEFORE decay so that
     labour is not halved first. A process whose entity cannot meet a
     per-tick input is abandoned (FAILED, forfeit). Lets a flow income fund
     a multi-tick process (research, construction) that the one-shot `inputs`
     model could only demand as a lump sum
  8. runs the consumption pass — AFTER the auction, so goods bought this
     tick are eaten this tick and sell orders settle before anything is
     eaten; draws down need-satisfying holdings and rewrites satisfaction
     scores, emitting per-entity need_satisfied / need_unmet events
  9. decays perishable goods — AFTER consumption, so entities eat fresh
     stock and only unsold, uneaten perishables rot
 9b. runs the incapacity pass — AFTER decay, so this tick's natural
     recovery counts before thresholds are read; entities holding an
     incapacitating condition at threshold are deactivated and the
     world's estate rule is applied (conditions.py)
 10. records every outcome (applied / rejected / script_error /
     compute_budget_exceeded / trade / order_cancelled / auction /
     process_completed / auto_issue / need_satisfied / need_unmet / decay /
     entity_incapacitated) as an event on a new Tick row — those events
     feed ctx.events next tick

Incapacitated entities take no part in any pass: no auto-issue, no
consumption, their scripts do not run, and start_process / place_order
refuse them.

An intent may only move money out of accounts owned by the entity whose
script queued it; the service layer additionally enforces monetary-authority
rules, balance/currency invariants, and VALIDATOR scripts.
"""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import clock, conditions, goods, markets, needs, parcels, production, rng, tech
from . import witness
from .lua_engine import Intent, LuaEngine
from .models import (
    Entity, EntityStatus, Holding, Need, NeedState, Parcel, Process,
    ProcessStatus, Script, ScriptType, Tick, WorldSetting,
)
from .scripting import (
    build_queries, get_world_libraries, resolve_intent, set_executing_tick,
)

# Votable data (world_settings): max total ms of Lua execution an entity's
# tick-scripts (POLICY + BEHAVIOUR) may consume in a single tick. Missing/None
# means unlimited — budgets are opt-in so existing worlds are unaffected.
COMPUTE_BUDGET_KEY = "entity_tick_compute_budget_ms"


def get_compute_budget_ms(session: Session) -> int | None:
    setting = session.get(WorldSetting, COMPUTE_BUDGET_KEY)
    if setting is None or not isinstance(setting.value, (int, float)):
        return None
    return int(setting.value)


def set_compute_budget_ms(session: Session, budget_ms: int | None) -> WorldSetting | None:
    setting = session.get(WorldSetting, COMPUTE_BUDGET_KEY)
    if budget_ms is None:
        if setting is not None:
            session.delete(setting)
            session.flush()
        return None
    if setting is None:
        setting = WorldSetting(key=COMPUTE_BUDGET_KEY, value=budget_ms)
        session.add(setting)
    else:
        setting.value = budget_ms
    session.flush()
    return setting


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
    events.extend(parcels.regen_deposits(session, tick_number=number))
    intents: list[Intent] = []

    budget_ms = get_compute_budget_ms(session)
    used_ms: dict[str, float] = {}

    # Library tiers (docs/scripting.md): `std` always; the per-world `world`
    # lib when the world set one. Read once per tick; read-only for scripts.
    libraries = get_world_libraries(session)

    # POLICY scripts run first and see every event from the previous tick;
    # BEHAVIOUR scripts see only their own entity's events.
    for script_type in (ScriptType.POLICY, ScriptType.BEHAVIOUR):
        for script in _tick_scripts(session, script_type):
            entity = session.get(Entity, script.entity_id)
            if entity is None or entity.status != EntityStatus.ACTIVE:
                continue
            if budget_ms is not None and used_ms.get(entity.id, 0.0) >= budget_ms:
                events.append({
                    "type": "compute_budget_exceeded",
                    "entity_id": entity.id,
                    "script_id": script.id,
                })
                continue
            entity_events = (
                prev_events if script_type == ScriptType.POLICY
                # The witness feed (game.md 15.6): a behaviour script sees
                # its own events PLUS what was delivered to it -- speech
                # and loud facts from the tick before. Rival privacy is
                # layered, not repealed: delivery is the observable
                # vocabulary, nothing more.
                else witness.script_feed(session, prev.number, entity.id, prev_events)
                if prev is not None else []
            )
            ctx = _build_script_ctx(session, entity, script, entity_events, number)
            result = lua_engine.run(script.source, ctx, timeout_ms=script.timeout_ms,
                                    libraries=libraries)
            used_ms[entity.id] = used_ms.get(entity.id, 0.0) + result.elapsed_ms
            if result.error:
                events.append({
                    "type": "script_error",
                    "entity_id": entity.id,
                    "script_id": script.id,
                    "error": result.error,
                })
                # A compiling-but-broken submission paralyzes the entity
                # every tick with no fallback — stone-run6 death: 28 straight
                # nil-index crashes while HUNGER crossed the threshold. At
                # CRASH_REVERT_TICKS the engine falls back to the lineage
                # ancestor, so the entity keeps living (and the model gets
                # the error + a translation next round).
                if script_type == ScriptType.BEHAVIOUR:
                    fallback = _record_crash(session, script)
                    if fallback is not None:
                        events.append({
                            "type": "script_reverted",
                            "entity_id": entity.id,
                            "from_script_id": script.id,
                            "to_script_id": fallback.id,
                        })
                continue
            script.consecutive_errors = 0
            script.state = dict(result.state_updates)
            intents.extend(result.intents)

    intents.sort(key=lambda i: i.priority)  # stable: collection order breaks ties

    # Every intent is tried here, before the auction, exactly as priority order
    # says. A start_process that fails ONLY for want of inputs is then retried
    # after clearing, because that is the one rejection buying could have
    # cured: with a single pass, an input bought this tick was unusable until
    # the next one and took a full round of decay on the way -- a flat tax on
    # hiring rather than self-supplying. Measured in the inequality scenario,
    # where LABOR decays 0.5/tick, hired labour arrived at 50% while a
    # smallholder's own auto-issued labour arrived at 100%.
    #
    # Retrying rather than simply moving production after the auction is what
    # keeps the rest of the ordering honest, and all three of these matter:
    #
    #   * a process still has first claim on its own entity's goods. Orders do
    #     not escrow (markets.py) -- holdings are checked live at settlement --
    #     so whichever runs first wins. Deferring production wholesale would
    #     silently let a sell order take inputs out from under it.
    #   * priority keeps meaning what it says between "use this" and "sell
    #     this". Splitting by intent type would have made type outrank
    #     priority, which no script author could see coming.
    #   * a duration-0 recipe still completes inline in time to sell its output
    #     into this tick's auction (production.start_process completes them
    #     immediately). Deferring production would have pushed that output past
    #     the auction and into the decay pass -- the same bug, mirrored onto
    #     producers.
    #
    # A first attempt leaves nothing behind when it fails: resolve_intent runs
    # each one in a savepoint, so a rejection is fully rolled back and the
    # retry starts clean. The held-back rejection is not recorded, so each
    # intent still produces exactly one event -- the outcome that stood.
    # Mark the tick in progress so a mid-tick spawn_entity stamps the
    # executing tick (the one the spawner saw as ctx.tick), not the latest
    # committed one (the current Tick row is only written at the end of
    # run_tick). Cleared in finally so the API/test path -- which resolves
    # intents between ticks -- falls back to the latest committed tick.
    set_executing_tick(number)
    try:
        retry: list[Intent] = []
        said: set[str] = set()   # speech budget: one say per entity per tick
        for intent in intents:
            outcome = resolve_intent(session, intent, said=said)
            if intent.intent_type == "start_process" and outcome.get("short_of_holdings"):
                retry.append(intent)
                continue
            events.append(outcome)

        events.extend(markets.run_auctions(session, tick_number=number))
        for intent in retry:
            events.append(resolve_intent(session, intent, said=said))
    finally:
        set_executing_tick(None)
    events.extend(production.consume_per_tick_inputs(session, tick_number=number))
    events.extend(needs.run_consumption(session, tick_number=number))
    events.extend(goods.apply_decay(session, tick_number=number))
    events.extend(conditions.run_incapacity(session, tick_number=number))

    tick = Tick(
        number=number,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        events=events,
        # commit the event list: this hash seeds next tick's outcome rolls
        events_hash=rng.hash_events(events),
    )
    session.add(tick)
    # Freeze witness delivery for the finalized tick (game.md 15.6):
    # who heard what, as a fact of this tick -- before the world moves on.
    witness.record_delivery(session, number, events)
    session.flush()
    return tick


# How many consecutive runtime crashes before the engine gives up on a
# behaviour and falls back to its ancestor. 3 = one full round of the
# agent loop (stone worlds run 20 ticks/round) is enough signal the
# submission is broken, not unlucky.
CRASH_REVERT_TICKS = 3


def _record_crash(session: Session, script: Script) -> Script | None:
    """Count the crash streak; at CRASH_REVERT_TICKS deactivate the
    crasher and re-activate the entity's most recent other behaviour
    script (lineage ancestor), fresh-streaked. None = no revert (streak
    under threshold, or nothing to fall back to — keep limping along)."""
    script.consecutive_errors = (script.consecutive_errors or 0) + 1
    if script.consecutive_errors < CRASH_REVERT_TICKS:
        return None
    fallback = session.execute(
        select(Script)
        .where(
            Script.entity_id == script.entity_id,
            Script.id != script.id,
            Script.script_type == ScriptType.BEHAVIOUR,
        )
        .order_by(Script.created_at.desc(), Script.id.desc())
    ).scalars().first()
    if fallback is None:
        return None
    script.is_active = False
    script.consecutive_errors = 0          # fresh streak if re-activated
    fallback.is_active = True
    fallback.consecutive_errors = 0         # fresh window on re-activation
    return fallback


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


def _build_script_ctx(session: Session, entity: Entity, script: Script, entity_events: list, tick_number: int) -> dict:
    return {
        "tick": tick_number,
        "clock": clock.clock_facts(tick_number),
        "entity": {
            "id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type.value,
            "age": (tick_number - entity.birth_tick) if entity.birth_tick is not None else None,
            "is_monetary_authority": entity.is_monetary_authority,
            "capabilities": list(entity.capabilities or []),
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
                "parcel_id": p.parcel_id,
                "started_tick": p.started_tick,
                "completes_tick": p.completes_tick,
            }
            for p in session.execute(
                select(Process)
                .where(Process.entity_id == entity.id, Process.status == ProcessStatus.RUNNING)
                .order_by(Process.created_at, Process.id)
            ).scalars()
        ],
        "parcels": [
            {
                "id": p.id,
                "parcel_type": p.parcel_type,
                "region_id": p.region_id,
                "facilities": [f.facility_type for f in p.facilities],
                "deposits": {d.symbol: str(d.quantity) for d in p.deposits},
            }
            for p in session.execute(
                select(Parcel)
                .where(Parcel.owner_id == entity.id)
                .order_by(Parcel.created_at, Parcel.id)
            ).scalars()
        ],
        "needs": _entity_needs(session, entity),
        "unlocks": tech.entity_unlocks(session, entity.id),
        "events": entity_events,
        "state": dict(script.state or {}),
        # owner-scoped: in a private-holdings world the script's query
        # surface shrinks to its own entity (build_queries)
        "queries": build_queries(session, tick_number, owner_id=entity.id),
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
            "condition": n.condition_symbol,
        }
        for n in applicable
    ]
