"""The MCP player tool surface -- the agent's view of the game.

An AI player joins over MCP (Model Context Protocol) instead of REST: same
bearer auth, same three-tier control model (docs/game.md §4), same
ownership gates. MCP is *only a wire protocol* here -- every tool is a thin
wrapper over the same platform functions the REST API serves, so nothing
reaches the engine through this file that a REST player could not do, and
vice versa.

**The observability decision (game.md §13), resolved:** an agent sees
exactly what its own behaviour script sees -- no more. The event digest is
filtered to ``entity_id == own entity``, the *same* filter the engine
applies when it feeds events to a BEHAVIOUR script each tick (tick.py).
That gives two properties for free:

  * **no omniscience** -- no reading other dynasties' affairs, and
  * **parity** -- the agent reasons over the same world its script will
    observe, so what it decides on is what its script will see.

World-visible facts (the round clock, market prices) are public to all
authenticated players, as they are in-world: a market price is a posted
fact.

Writes go exclusively through the autonomy path: ``set_behaviour`` is the
ownership-gated script swap (§6). Voting, transfers, production -- all of it
is done by *writing the script that will do it at tick time*, never by
acting out-of-band. The engine still owns the tick.
"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from econengine import clock, edges, places as places_mod, scripting, services, tech
from econengine.catalog import catalog_state
from econengine.describe import render_event, symbol_names
from econengine.lua_engine import stdlib_fingerprint, stdlib_source
from econengine.models import (
    Entity, EntityType, Holding, Market, NeedState, Parcel, Process,
    ProcessStatus, Script, ScriptType, Tick, User,
)
from econengine.models.order import Order, OrderSide, OrderStatus
from econengine.services import ServerCapExceededError
from econ.api.activity import activity_rows
from econ.api.epochs import get_epoch_state, player_eliminated_in_running_epoch
from econ.api.governance import governance_state
from econ.api.leaderboard import leaderboard_state
from econengine.catalog import catalog_state
from econ.api.onboarding import get_join_config
from econ.api.rounds import (
    NotEligibleError, current_round_state, set_user_ready, unset_user_ready,
)


class ToolError(Exception):
    """A tool-level failure (bad args, not-your-entity, fixed entity...).

    Reported to the agent as a normal MCP tool result with ``isError``
    (the call itself was well-formed) -- not as a protocol error.
    """


def _own_entity(session: Session, user: User, entity_id: str) -> Entity:
    """Ownership-gated entity lookup: 404-style refusal for anyone else's
    entity (info-hiding: 'not found' and 'not yours' are indistinguishable)."""
    entity = session.get(Entity, str(entity_id))
    if entity is None or entity.owner_id != user.id:
        raise ToolError(f"Entity not found: {entity_id!r} (not yours or does not exist)")
    return entity


def _latest_tick_number(session: Session) -> int:
    row = session.execute(select(Tick.number).order_by(Tick.number.desc()).limit(1)).scalar_one_or_none()
    return row if row is not None else 0


# ===========================================================================
# Read tools
# ===========================================================================

def tool_my_entities(session: Session, user: User, args: dict[str, Any]) -> list[dict]:
    """Your dynasty: every entity you own, with lifecycle state."""
    entities = (
        session.query(Entity)
        .filter_by(owner_id=user.id)
        .order_by(Entity.name, Entity.id)
        .all()
    )
    latest = _latest_tick_number(session)
    return [
        {
            "id": e.id,
            "name": e.name,
            "entity_type": e.entity_type.value,
            "status": e.status.value,
            "age": (latest - e.birth_tick) if e.birth_tick is not None else None,
        }
        for e in entities
    ]


def tool_entity_state(session: Session, user: User, args: dict[str, Any]) -> dict:
    """Full state of one of your entities -- the same picture its behaviour
    script gets each tick: accounts, holdings, needs, processes, parcels,
    unlocks, and the active behaviour's id + state."""
    entity = _own_entity(session, user, args.get("entity_id", ""))
    latest = _latest_tick_number(session)
    behaviour = session.query(Script).filter_by(
        entity_id=entity.id, script_type=ScriptType.BEHAVIOUR, is_active=True,
    ).order_by(Script.created_at.desc()).first()

    return {
        "clock": clock.clock_facts(latest + 1),
        "entity": {
            "id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type.value,
            "status": entity.status.value,
            "age": (latest - entity.birth_tick) if entity.birth_tick is not None else None,
            "capabilities": list(entity.capabilities or []),
            "is_monetary_authority": entity.is_monetary_authority,
            "is_fixed": entity.is_fixed,
        },
        "accounts": [
            {"id": a.id, "currency": a.currency, "balance": str(a.balance)}
            for a in entity.accounts
        ],
        "holdings": [
            {"symbol": h.symbol, "quantity": str(h.quantity)}
            for h in session.execute(
                select(Holding)
                .where(Holding.entity_id == entity.id)
                .order_by(Holding.symbol)
            ).scalars()
        ],
        "needs": [
            {"need": s.need.code, "satisfaction": str(s.satisfaction),
             "updated_tick": s.updated_tick}
            for s in sorted(
                session.query(NeedState).filter_by(entity_id=entity.id).all(),
                key=lambda s: s.need.code,
            )
        ],
        "processes": [
            {"id": p.id, "recipe": p.recipe.code, "parcel_id": p.parcel_id,
             "started_tick": p.started_tick, "completes_tick": p.completes_tick}
            for p in session.execute(
                select(Process)
                .where(Process.entity_id == entity.id, Process.status == ProcessStatus.RUNNING)
                .order_by(Process.created_at)
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
                .order_by(Parcel.created_at)
            ).scalars()
        ],
        # The map (docs/spatial.md S1), parity with the behaviour ctx:
        # where this entity stands, or null when unplaced/no map.
        "place": (places_mod.place_facts(entity.place)
                  if entity.location_place_id else None),
        "unlocks": tech.entity_unlocks(session, entity.id),
        "behaviour": (
            {"id": behaviour.id, "description": behaviour.description,
             "state": dict(behaviour.state or {})}
            if behaviour else None
        ),
    }


def tool_entity_events(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The per-entity event digest: events your entity took part in, for the
    last N ticks -- the same filtered feed its behaviour script receives
    (no omniscience; game.md §13)."""
    entity = _own_entity(session, user, args.get("entity_id", ""))
    try:
        last_ticks = int(args.get("last_ticks", 3))
    except (TypeError, ValueError):
        raise ToolError("last_ticks must be an integer")
    if last_ticks < 1 or last_ticks > 50:
        raise ToolError("last_ticks must be between 1 and 50")

    ticks = session.execute(
        select(Tick).order_by(Tick.number.desc()).limit(last_ticks)
    ).scalars().all()
    return {
        "entity_id": entity.id,
        "ticks": [
            {"tick": t.number,
             "events": [e for e in (t.events or []) if e.get("entity_id") == entity.id]}
            for t in sorted(ticks, key=lambda t: t.number)
        ],
    }


def tool_get_behaviour(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The full source (and state) of your entity's active behaviour script."""
    entity = _own_entity(session, user, args.get("entity_id", ""))
    script = session.query(Script).filter_by(
        entity_id=entity.id, script_type=ScriptType.BEHAVIOUR, is_active=True,
    ).order_by(Script.created_at.desc()).first()
    if script is None:
        raise ToolError(f"Entity {entity.id} has no active behaviour script")
    return {
        "id": script.id,
        "description": script.description,
        "source": script.source,
        "state": dict(script.state or {}),
        "timeout_ms": script.timeout_ms,
    }


def tool_round_state(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The round clock: which round is open for submission, how many ticks
    have run, and how many ticks resolve per round (K). In readiness mode
    the ``readiness`` block also shows the gate: how many eligible players
    have readied, and who (public facts, like prices)."""
    return current_round_state(session)


def tool_set_ready(session: Session, user: User, args: dict[str, Any]) -> dict:
    """Signal (or withdraw) readiness for the round open now -- the agent's
    vote to close the round (game.md §9.1). The final ready resolves the
    round in-request; the response carries the gate state and, when it
    fired, the round summary."""
    if bool(args.get("ready", True)):
        try:
            out = set_user_ready(session, user.id)
        except NotEligibleError as exc:
            raise ToolError(str(exc)) from exc
    else:
        out = unset_user_ready(session, user.id)
    session.commit()
    return out


def tool_epoch_state(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The epoch: the declared victory condition, whether it has been won
    (and by whom), and whether *you* were eliminated this epoch. Absent =
    no epoch is running; the world plays without a victory condition."""
    state = get_epoch_state(session)
    if state is None:
        return {"running": False, "number": 0, "note": "no epoch has been declared"}
    return {
        "running": state.get("ended_tick") is None,
        "number": state["number"],
        "condition": state.get("condition"),
        "started_tick": state.get("started_tick"),
        "ended_tick": state.get("ended_tick"),
        "winner_user_ids": list(state.get("winner_user_ids", [])),
        "you_are_eliminated": player_eliminated_in_running_epoch(session, user.id),
    }


def tool_governance_current(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The governance calendar: is the round open for submission a window
    round (does resolving it close a window and trigger enactment)? When is
    the next window? Which proposals sit dormant on the docket, with live
    tallies?"""
    return governance_state(session)


def tool_leaderboard(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The standings: one row per dynasty (money, entity counts, oldest
    lineage age, tech unlocks, epoch wins, status), ranked by epoch wins
    then money. Public facts only -- no dynasty's private affairs (§13)."""
    return leaderboard_state(session)


def tool_world_catalog(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The readable world (Phase 3a, game.md §15.1): names, descriptions,
    and derived effect lines for every good, recipe, technology, need, and
    market. The §13 parity doctrine extended from script vocabulary to
    world vocabulary: the prompt and the script read the same catalog."""
    return catalog_state(session)


def tool_world_map(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The world's map as one public view (docs/spatial.md): every
    installed place (the same facts ctx.places and entity_state's place
    carry), every road with its cost and mode (the topology the MANUAL
    describes and travel routes over), and every entity's current
    location — public facts, the same cut as the world log's public
    arrivals and departures. No private affair rides along. A world
    without a map ships empty lists."""
    return {
        "places": [
            {"key": p.key, "name": p.name, "kind": p.kind,
             "region_id": p.region_id, "description": p.description}
            for p in places_mod.list_places(session)
        ],
        "roads": [
            {"from": e.from_place.key, "to": e.to_place.key,
             "mode": e.mode, "cost_ticks": e.cost_ticks,
             "bidirectional": bool(e.bidirectional)}
            for e in edges.list_edges(session)
        ],
        "entities": [
            {"id": ent.id, "name": ent.name,
             "entity_type": ent.entity_type.value, "status": ent.status.value,
             "place": (ent.place.key if ent.location_place_id else None)}
            for ent in session.execute(
                select(Entity).order_by(Entity.name)).scalars()
        ],
    }


def tool_entity_activity(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The entity's own audit trail (Phase 3b, game.md §15.3): its recent
    actions rendered as readable prose — trades, orders, processes, need
    outcomes, crashes, refusals. An attempt is an action: rejections are
    included. Own entity only (§13)."""
    entity = _own_entity(session, user, args.get("entity_id", ""))
    try:
        last_ticks = int(args.get("last_ticks", 20))
    except (TypeError, ValueError):
        raise ToolError("last_ticks must be an integer")
    if last_ticks < 1 or last_ticks > 50:
        raise ToolError("last_ticks must be between 1 and 50")
    try:
        last_ticks = int(args.get("last_ticks", 20))
    except (TypeError, ValueError):
        raise ToolError("last_ticks must be an integer")
    if last_ticks < 1 or last_ticks > 50:
        raise ToolError("last_ticks must be between 1 and 50")
    witnessed = bool(args.get("witnessed", False))
    rows = activity_rows(session, entity.id, last_ticks, witnessed=witnessed)
    return {"entity_id": entity.id, "activity": rows}


def tool_world_activity(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The world's log: the unattributed public facts (auction summaries,
    decay, auto-issue) rendered as readable prose — the same §13 cut as
    GET /activity. No dynasty's private affairs ride along."""
    try:
        last_ticks = int(args.get("last_ticks", 20))
    except (TypeError, ValueError):
        raise ToolError("last_ticks must be an integer")
    if last_ticks < 1 or last_ticks > 50:
        raise ToolError("last_ticks must be between 1 and 50")
    return {"activity": activity_rows(session, None, last_ticks)}


def tool_market_prices(session: Session, user: User, args: dict[str, Any]) -> list[dict]:
    """The public book for every active market: last trade (history) plus
    the touch (present) -- best_bid/best_ask are the best OPEN limits
    resting right now, the prices an order must cross to trade. None on a
    bare side; depth beyond the touch is not public."""
    markets = session.execute(
        select(Market).where(Market.is_active.is_(True)).order_by(Market.symbol)
    ).scalars().all()
    best = {}  # (market_id, side) -> best OPEN limit price
    for side, agg in ((OrderSide.BUY, func.max), (OrderSide.SELL, func.min)):
        rows = session.execute(
            select(Order.market_id, agg(Order.limit_price))
            .where(
                Order.status == OrderStatus.OPEN,
                Order.remaining > 0,
                Order.side == side,
            )
            .group_by(Order.market_id)
        ).all()
        best.update({(mid, side): price for mid, price in rows})
    return [
        {"symbol": m.symbol, "currency": m.currency,
         "last_price": str(m.last_price) if m.last_price is not None else None,
         "best_bid": str(p) if (p := best.get((m.id, OrderSide.BUY))) is not None else None,
         "best_ask": str(p) if (p := best.get((m.id, OrderSide.SELL))) is not None else None}
        for m in markets
    ]


def tool_get_script_libraries(session: Session, user: User, args: dict[str, Any]) -> dict:
    """The script vocabulary tiers injected under every behaviour
    (docs/scripting.md): `std` (engine stdlib -- pure vocabulary, source
    included), `world` (this world's library, when installed) and `pack`
    (the content pack's play opinions, when installed). Authoring a
    behaviour from scratch means reading these -- guessing at a helper
    that is not injected is the classic nil-call trap."""
    libs = scripting.get_world_libraries(session) or {}
    return {
        "std": {"fingerprint": stdlib_fingerprint(), "source": stdlib_source()},
        "world": libs.get("world"),
        "pack": libs.get("pack"),
    }


# ===========================================================================
# Write tools (both are the same platform paths the REST API serves)
# ===========================================================================

def tool_set_behaviour(session: Session, user: User, args: dict[str, Any]) -> dict:
    """Replace your entity's behaviour script (the autonomy path, §6).

    The new source runs as your entity from the next resolved tick. Refused
    for fixed (immutable-tier) entities."""
    entity = _own_entity(session, user, args.get("entity_id", ""))
    source = args.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ToolError("source is required (the Lua behaviour script)")
    if entity.is_fixed:
        raise ToolError("Entity behaviour is fixed (immutable tier; not player-editable)")
    try:
        script, warnings = services.set_entity_behaviour(
            session, entity, source, owner_id=user.id,
            description=str(args.get("description", "")),
        )
    except scripting.ScriptRejected as exc:
        raise ToolError("; ".join(exc.problems))
    except ValueError as exc:
        raise ToolError(str(exc))
    session.commit()
    session.refresh(script)
    return {
        "id": script.id,
        "entity_id": entity.id,
        "description": script.description,
        "status": "active",
        "lint_warnings": warnings,
        "note": "Runs as this entity from the next resolved tick",
    }


def tool_join(session: Session, user: User, args: dict[str, Any]) -> dict:
    """Join the game: found a new INDIVIDUAL entity, endowed per the world's
    join config (account + optional starter behaviour)."""
    cfg = get_join_config(session)
    if player_eliminated_in_running_epoch(session, user.id):
        raise ToolError(
            "Eliminated in the running epoch; wait for the next epoch to rejoin"
        )
    try:
        services._enforce_server_caps(session, user.id)
    except ServerCapExceededError as exc:
        raise ToolError(str(exc))

    entity = services.create_entity(session, "Founder", EntityType.INDIVIDUAL)
    entity.owner_id = user.id
    account = services.create_account(session, entity, cfg["currency"], cfg["endowment"])

    starter = cfg["starter_behaviour"]
    behaviour = None
    if starter:
        # Lint applies here too: a broken starter must fail join loudly
        # (operator content is pre-gated at pack build, so this never
        # fires in practice -- but if it ever does, handing every new
        # player a zombie is the wrong default).
        behaviour, _ = services.set_entity_behaviour(
            session, entity, starter, owner_id=user.id,
        )

    session.commit()
    session.refresh(entity)
    session.refresh(account)
    return {
        "entity": {"id": entity.id, "name": entity.name,
                   "entity_type": entity.entity_type.value},
        "account": {"id": account.id, "currency": account.currency,
                    "balance": str(account.balance)},
        "behaviour_applied": behaviour is not None,
        "note": "Edit its behaviour with set_behaviour; it runs on the next round",
    }


# ===========================================================================
# Registry
# ===========================================================================

Tool = dict[str, Any]  # {"name", "description", "inputSchema", "handler"}

TOOLS: list[Tool] = [
    {
        "name": "join",
        "description": "Join the game: found a new entity (INDIVIDUAL) endowed "
                       "per the world's join config (money + optional starter behaviour).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_join,
    },
    {
        "name": "my_entities",
        "description": "Your dynasty: every entity you own, with status and age.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_my_entities,
    },
    {
        "name": "entity_state",
        "description": "Full state of one of your entities: accounts, holdings, "
                       "needs, running processes, parcels, place (where it "
                       "stands, null on mapless worlds), unlocks, and the active "
                       "behaviour (id + state).",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
        "handler": tool_entity_state,
    },
    {
        "name": "entity_events",
        "description": "The per-entity event digest: events your entity took part "
                       "in over the last N ticks (default 3) -- the same filtered "
                       "feed its behaviour script receives. No other dynasty's "
                       "affairs are visible.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "last_ticks": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["entity_id"],
        },
        "handler": tool_entity_events,
    },
    {
        "name": "get_behaviour",
        "description": "The full Lua source (and state) of your entity's active "
                       "behaviour script.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
        "handler": tool_get_behaviour,
    },
    {
        "name": "get_script_libraries",
        "description": "The script vocabulary tiers injected under every behaviour: "
                       "std (engine stdlib, source included), world (this world's "
                       "library), pack (the content pack's play opinions). Read "
                       "these before authoring a behaviour from scratch.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_get_script_libraries,
    },
    {
        "name": "set_behaviour",
        "description": "Replace your entity's behaviour script (Lua). It runs as "
                       "your entity from the next resolved tick. This is the "
                       "autonomy path: no vote, no capability -- only ownership.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "source": {"type": "string", "description": "Lua behaviour script"},
                "description": {"type": "string"},
            },
            "required": ["entity_id", "source"],
        },
        "handler": tool_set_behaviour,
    },
    {
        "name": "round_state",
        "description": "The round clock: which round is open for submission, how "
                       "many ticks have run, ticks per round (K), and the readiness "
                       "gate (mode, who of the eligible players has readied).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_round_state,
    },
    {
        "name": "set_ready",
        "description": "Signal (or withdraw) readiness for the round open now -- "
                       "your vote to close this round. The final ready resolves the "
                       "round immediately (K ticks run in a batch); withdraw with "
                       "ready=false before it fires if you reconsidered.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ready": {
                    "type": "boolean",
                    "description": "true (default) = ready; false = withdraw",
                },
            },
            "required": [],
        },
        "handler": tool_set_ready,
    },
    {
        "name": "epoch_state",
        "description": "The epoch: the declared victory condition, whether it "
                       "has been won (and by whom), and whether you were "
                       "eliminated this epoch. No epoch running = the world plays "
                       "without a victory condition.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_epoch_state,
    },
    {
        "name": "governance_current",
        "description": "The governance calendar: is the current round a window "
                       "round (does resolving it close a window and trigger the "
                       "clerk's enactment sweep)? When is the next window? Which "
                       "proposals sit dormant on the docket, with live tallies?",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_governance_current,
    },
    {
        "name": "leaderboard",
        "description": "The standings: one row per dynasty (money, entity "
                       "counts, oldest lineage age, tech unlocks, epoch wins, "
                       "status), ranked by epoch wins then money. Public facts "
                       "only -- no other dynasty's private affairs.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_leaderboard,
    },
    {
        "name": "market_prices",
        "description": "Last-trade price for every active market (public facts).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_market_prices,
    },
    {
        "name": "world_catalog",
        "description": "The readable world: every good (name, description, "
                       "derived effect line — decay, conditions, auto-issue), "
                       "every recipe (inputs → outputs, duration, gates, branch "
                       "odds), the tech tree, needs (satisfiers and hourly "
                       "draws), threats (night pressure), and markets. "
                       "The world's vocabulary, public facts only.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_world_catalog,
    },
    {
        "name": "entity_activity",
        "description": "Your entity's audit trail: its recent actions as "
                       "readable prose — trades (\"sold 2 ORE for 10\"), orders, "
                       "processes, need outcomes, crashes, refusals. An attempt "
                       "is an action: rejections are included. Own entity only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "last_ticks": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["entity_id"],
        },
        "handler": tool_entity_activity,
    },
    {
        "name": "world_map",
        "description": "The world's map as one public view: every place, "
                       "every road (mode and cost in ticks), and every "
                       "entity's current location — the same public facts "
                       "as ctx.places and the world log's arrivals. A world "
                       "without a map ships empty lists.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": tool_world_map,
    },
    {
        "name": "world_activity",
        "description": "The world's log: unattributed public facts (auction "
                       "summaries, decay, auto-issue) as readable prose — the "
                       "same public/private cut as GET /activity. No dynasty's "
                       "private affairs ride along.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "last_ticks": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": [],
        },
        "handler": tool_world_activity,
    },
]

TOOL_HANDLERS: dict[str, Callable] = {t["name"]: t["handler"] for t in TOOLS}
