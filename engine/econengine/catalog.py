"""The readable world: the catalog render (Phase 3a, game.md §15.1).

Legibility is a product surface. The players are LLM agents and, soon,
modders-by-pack; a world whose vocabulary is machine-shaped -- GRAIN,
MINE_ORE, ``{"type": "trade", ...}`` -- is unplayable from a prompt and
unauthorable by a stranger. This module renders the whole world
vocabulary as data: every good (with its condition effect line), every
recipe (inputs → outputs, duration, gates, branch odds), the tech tree,
needs, and markets.

The doctrine is **derived where derivable, authored where meaningful**
(§15.1). A condition's effect line is generated from its row -- HUNGER:
"granted 1 per fully-unmet FOOD tick; decays 5%/tick (equilibrium ≈ 20
held); incapacitates at 15" -- and likewise `modifies`, auto-issue,
decay, branch tables with odds and labels, and recipe requirement gates.
Prose cannot drift from physics because the prose is a function of the
physics; the authored `name`/`description` columns carry only what the
row cannot say (flavor, advice).

Everything here is a pure function of (rows, event-free catalog tables),
computed at read time -- it never enters ``events_hash``, so determinism,
replay, and the RNG commit-reveal chain are untouched, and a replayed
world renders identically. Served by ``GET /catalog`` and MCP
``world_catalog``; the agent loop folds it into the system prompt where
the hand-written stone_age manual sits today (the §13 parity doctrine:
the prompt and the script read the same catalog).
"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from econengine.conditions import is_condition
from econengine.models import (
    Good, Market, Need, Place, Recipe, SpatialEdge, Technology, Threat,
)


def _num(value: Decimal | int | float | str) -> str:
    """A quantity as compact prose: 15.0000 -> '15', 0.5000 -> '0.5'."""
    d = Decimal(str(value))
    text = format(d.normalize(), "f")
    return text


def _pct(fraction: Decimal) -> str:
    """0.2500 -> '25%'."""
    return f"{_num(Decimal(str(fraction)) * 100)}%"


def good_effect(good: Good, needs_by_condition: dict[str, list[Need]]) -> str | None:
    """The derived physics line for a Good row: what holding/issuing it does.

    ``None`` for a plain good with no behaviour -- the catalog lists it
    under its name and description alone.
    """
    parts: list[str] = []
    if good.modifies_pattern is not None:
        parts.append(
            f"while held, effective {good.modifies_pattern} × {_num(good.modifies_factor)}"
        )
    grants = needs_by_condition.get(good.symbol, [])
    if grants:
        for need in grants:
            parts.append(
                f"granted {_num(need.condition_quantity)} per fully-unmet "
                f"{need.code} tick (scaled by shortfall)"
            )
        parts.append(f"decays {_pct(good.decay_per_tick)}/tick")
        if good.decay_per_tick:
            grant = max(need.condition_quantity for need in grants)
            parts.append(
                f"equilibrium ≈ {_num(Decimal(grant) / good.decay_per_tick)} held"
            )
    elif good.decay_per_tick:
        parts.append(f"decays {_pct(good.decay_per_tick)}/tick")
    if good.auto_issue_quantity:
        who = (
            "every entity"
            if good.auto_issue_entity_type is None
            else f"every {good.auto_issue_entity_type.value.lower()}"
        )
        parts.append(
            f"auto-issued up to {_num(good.auto_issue_quantity)}/tick to {who}"
        )
        if good.auto_issue_daylight_only:
            parts.append("daylight only (hours 06..19; night issues nothing)")
    if good.incapacitates_at is not None:
        parts.append(f"incapacitates at {_num(good.incapacitates_at)}")
    return "; ".join(parts) or None


def _qty_list(rows) -> str:
    return " + ".join(f"{_num(r.quantity)} {r.symbol}" for r in rows)


def recipe_line(recipe: Recipe) -> str:
    """The one-line summary: what goes in, what comes out."""
    ins = []
    if recipe.inputs:
        ins.append(_qty_list(recipe.inputs))
    if recipe.per_tick_inputs:
        ins.append(
            " + ".join(
                f"{_num(r.quantity)} {r.symbol}/tick while running"
                for r in recipe.per_tick_inputs
            )
        )
    if recipe.deposit_inputs:
        ins.append(
            " + ".join(
                f"{_num(r.quantity)} {r.symbol} drawn from the parcel's seam"
                for r in recipe.deposit_inputs
            )
        )
    if recipe.builds_facility:
        outs = f"erects a {recipe.builds_facility} facility"
    elif recipe.unlocks:
        outs = "unlocks " + ", ".join(u.technology.code for u in recipe.unlocks)
    elif recipe.branches:
        total = sum((b.weight for b in recipe.branches), Decimal(0))
        outs = "; ".join(
            f"{_pct(b.weight / total)}: "
            + (_qty_list(b.outputs) if b.outputs else "nothing")
            + (f" ({b.label})" if b.label else "")
            for b in recipe.branches
        )
    else:
        outs = _qty_list(recipe.outputs)
    return f"{' + '.join(ins) or 'nothing'} → {outs}"


def recipe_effects(recipe: Recipe) -> list[str]:
    """The derived gate/cost lines beyond the summary (§15.1)."""
    lines = []
    if recipe.duration_ticks:
        lines.append(f"takes {recipe.duration_ticks} tick"
                     f"{'s' if recipe.duration_ticks != 1 else ''}")
    if recipe.requires_facility:
        lines.append(f"must run at a {recipe.requires_facility} facility")
    if recipe.requires_daylight:
        lines.append("needs daylight (refused at night, hours 20..05)")
    if recipe.requires_place_key is not None:
        # the exact spot, by pack key — the name join lives in the places
        # surface (world.places() / ctx.places); the key is the handle
        lines.append(f"requires presence at {recipe.requires_place_key}")
    elif recipe.requires_place_kind is not None:
        lines.append(f"requires presence at a {recipe.requires_place_kind}")
    if recipe.requirements:
        for req in recipe.requirements:
            scope = req.technology.scope.value.lower()
            lines.append(f"requires the {req.technology.code} technology ({scope}-scoped)")
    for r in recipe.good_requirements:
        lines.append(f"holds ≥ {_num(r.quantity)} {r.symbol} to run (reserved, not consumed)")
    return lines


def _satisfier_chain(need: Need, good_by_symbol: dict[str, Good]) -> str:
    """The need's satisfiers as a draw-order sentence.

    The consumption pass eats holdings of these goods each tick, tried
    in relationship order (symbol-ascending), each unit covering one
    tick's draw. A satisfier that fully decays within a tick is flagged
    inline: it is a flow (spend it the tick it lands or it is gone),
    not a store -- the difference run 15's readers kept missing when
    the list read like a menu of equivalents.
    """
    names = []
    for s in need.satisfiers:
        g = good_by_symbol.get(s.symbol)
        if g is not None and g.decay_per_tick and g.decay_per_tick >= 1:
            names.append(f"{s.symbol} (fades the same tick)")
        else:
            names.append(s.symbol)
    return ", then ".join(names)


def catalog_state(session: Session) -> dict:
    """The whole readable world, grouped by row kind (§15.1: `GET /catalog`).

    Pure read; safe to call any time. Rendered text never enters the hash
    chain, so this shape may evolve without a replay break.
    """
    goods = list(session.execute(select(Good).order_by(Good.symbol)).scalars())
    needs = list(session.execute(select(Need).order_by(Need.priority, Need.code)).scalars())
    threats = list(session.execute(select(Threat).order_by(Threat.code)).scalars())
    recipes = list(session.execute(select(Recipe).order_by(Recipe.code)).scalars())
    techs = list(session.execute(select(Technology).order_by(Technology.code)).scalars())
    markets = list(session.execute(select(Market).order_by(Market.symbol)).scalars())
    map_places = list(session.execute(
        select(Place).order_by(Place.key)).scalars())
    roads = list(session.execute(
        select(SpatialEdge).order_by(SpatialEdge.id)).scalars())

    needs_by_condition: dict[str, list[Need]] = {}
    for need in needs:
        if need.condition_symbol:
            needs_by_condition.setdefault(need.condition_symbol, []).append(need)

    good_by_symbol = {g.symbol: g for g in goods}

    return {
        "places": [
            {
                "key": p.key,
                "name": p.name or p.key,
                "kind": p.kind,
                "region_id": p.region_id,
                "description": p.description,
                "pack": p.pack_id,
            }
            for p in map_places
        ],
        # The roads, read as authored: from -> to, the mode, and the
        # hours on the road (cost_ticks; distance in this engine is
        # always ticks-through-topology). Bidirectional roads read both
        # ways. A world with no roads has no topology; travel is
        # refused with a readable reason.
        "roads": [
            {
                "from": e.from_place.key,
                "to": e.to_place.key,
                "mode": e.mode,
                "cost_ticks": e.cost_ticks,
                "bidirectional": e.bidirectional,
                "region_id": e.region_id,
                "pack": e.pack_id,
            }
            for e in roads
        ],
        "goods": [
            {
                "symbol": g.symbol,
                "name": g.name,
                "description": g.description,
                "pack": g.pack_id,      # 15.4 provenance; None = platform
                "condition": is_condition(g),   # machine-readable: a
                # condition good sheds quantity as recovery/relapse and
                # carries held modifiers -- consumers (snapshots,
                # dashboards) split it out of commodity holdings.
                "effect": good_effect(g, needs_by_condition),
            }
            for g in goods
        ],
        "needs": [
            {
                "code": n.code,
                "name": n.name,
                "description": n.description,
                "pack": n.pack_id,      # 15.4 provenance; None = platform
                # Draw-order wording: the need is PAID from holdings,
                # satisfier by satisfier in the order the consumption
                # pass tries them -- not a menu of equivalent sources.
                "draws": (
                    f"draws {_num(n.quantity_per_tick)}/tick from holdings, "
                    f"eating {_satisfier_chain(n, good_by_symbol)}"
                    + (f" but {_num(n.night_quantity_per_tick)}/tick at night"
                       if n.night_quantity_per_tick is not None else "")
                    + ("; tried in that order, each unit covers one tick"
                       if len(n.satisfiers) > 1 else " (1:1)")
                ),
                "entity_type": n.entity_type.value if n.entity_type else None,
                "priority": n.priority,
                "condition": (
                    {"symbol": n.condition_symbol,
                     "quantity": _num(n.condition_quantity)}
                    if n.condition_symbol
                    else None
                ),
            }
            for n in needs
        ],
        "recipes": [
            {
                "code": r.code,
                "name": r.name,
                "description": r.description,
                "pack": r.pack_id,      # 15.4 provenance; None = platform
                "line": recipe_line(r),
                "effects": recipe_effects(r),
            }
            for r in recipes
        ],
        "technologies": [
            {
                "code": t.code,
                "name": t.name,
                "description": t.description,
                "pack": t.pack_id,      # 15.4 provenance; None = platform
                "scope": t.scope.value,
                "requires": [p.prerequisite.code for p in t.prerequisites],
            }
            for t in techs
        ],
        "markets": [
            {
                "symbol": m.symbol,
                "name": m.name,
                "description": m.description,
                "pack": m.pack_id,      # 15.4 provenance; None = platform
                "currency": m.currency,
                # S2: the seat this market trades at, by pack key —
                # None = the global market, reachable from anywhere.
                "place": m.place.key if m.place is not None else None,
            }
            for m in markets
        ],
        "threats": [
            {
                "code": t.code,
                "name": t.name,
                "description": t.description,
                "pack": t.pack_id,
                "condition": t.condition_symbol,
                "entity_type": t.entity_type.value if t.entity_type else None,
                "place": t.place.key if t.place is not None else None,
                "line": threat_line(t),
            }
            for t in threats
        ],
    }


def threat_line(t: Threat) -> str:
    """The derived physics line for a Threat row: when it presses, what
    it hears, what keeps it shy, and — S4 — where it lives."""
    bits = []
    if t.place is not None:
        from . import places as places_mod

        bits.append(f"lives at {places_mod.label(t.place)}")
    bits.append(f"at night, +{_num(t.ambient_night_per_tick)}/hour")
    if t.per_say_night > 0:
        bits.append(f"+{_num(t.per_say_night)} per say you make (noise "
                    f"carries after dark)")
    if t.deterred_by_symbol is not None:
        bits.append(f"held {t.deterred_by_symbol} >= "
                    f"{_num(t.deterred_by_quantity)} quarters all of it "
                    f"(x{_num(t.deterrence_factor)})")
    return ", ".join(bits)


def catalog_text(state: dict) -> str:
    """The catalog as compact prose: the prompt fold (3a tail, §15.5).

    A pure render of catalog_state — the same derived numbers the REST
    catalog and the MCP surface serve, as plain text for system prompts
    (and any human reading a transcript). Derived where derivable: the
    tables of the old hand-written manual are now generated from the
    physics; the authored manual keeps only what the numbers cannot
    spell (strategy, seams, flavor).

    Stable section order — goods, needs, the action space, tech tree,
    markets — so diffs between world versions read top to bottom.
    """
    out: list[str] = []

    out.append("== GOODS (what exists; what holding or issuing it does) ==")
    for g in state["goods"]:
        bits = [g["symbol"]]
        if g["name"] and g["name"] != g["symbol"]:
            bits[0] += f" ({g['name']})"
        if g["description"]:
            bits.append(g["description"])
        if g["effect"]:
            bits.append(g["effect"])
        out.append("- " + "; ".join(bits))

    if state["needs"]:
        out.append("")
        out.append("== NEEDS (drawn every tick; shortfalls bite) ==")
        for n in state["needs"]:
            line = f"- {n['code']}: {n['draws']}"
            if n["condition"]:
                line += (f" -- while short, accumulates "
                         f"{n['condition']['symbol']} "
                         f"{n['condition']['quantity']}/tick")
            out.append(line)

    if state["threats"]:
        out.append("")
        out.append("== THREATS (what presses at night) ==")
        for t in state["threats"]:
            line = f"- {t['code']} -> {t['condition']}: {t['line']}"
            if t["description"]:
                line += f" -- {t['description']}"
            out.append(line)

    out.append("")
    out.append("== THE ACTION SPACE (recipes: inputs -> outputs) ==")
    for r in state["recipes"]:
        head = f"- {r['code']}: {r['line']}"
        if r["effects"]:
            head += "  [" + "; ".join(r["effects"]) + "]"
        if r["description"]:
            out.append(f"{head} -- {r['description']}")
        else:
            out.append(head)

    if state["technologies"]:
        out.append("")
        out.append("== TECHNOLOGIES ==")
        for t in state["technologies"]:
            reqs = (", ".join(t["requires"]) if t["requires"]
                    else "no prerequisites")
            out.append(f"- {t['code']} ({t['scope']}): {reqs}")

    if state["markets"]:
        out.append("")
        out.append("== MARKETS (order books; quote currencies) ==")
        out.append("- " + ", ".join(
            f"{m['symbol']}/{m['currency']}" for m in state["markets"]))

    return "\n".join(out)
