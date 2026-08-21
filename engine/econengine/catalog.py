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

from econengine.models import Good, Market, Need, Recipe, Technology


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
    if recipe.requirements:
        for req in recipe.requirements:
            scope = req.technology.scope.value.lower()
            lines.append(f"requires the {req.technology.code} technology ({scope}-scoped)")
    for r in recipe.good_requirements:
        lines.append(f"holds ≥ {_num(r.quantity)} {r.symbol} to run (reserved, not consumed)")
    return lines


def catalog_state(session: Session) -> dict:
    """The whole readable world, grouped by row kind (§15.1: `GET /catalog`).

    Pure read; safe to call any time. Rendered text never enters the hash
    chain, so this shape may evolve without a replay break.
    """
    goods = list(session.execute(select(Good).order_by(Good.symbol)).scalars())
    needs = list(session.execute(select(Need).order_by(Need.priority, Need.code)).scalars())
    recipes = list(session.execute(select(Recipe).order_by(Recipe.code)).scalars())
    techs = list(session.execute(select(Technology).order_by(Technology.code)).scalars())
    markets = list(session.execute(select(Market).order_by(Market.symbol)).scalars())

    needs_by_condition: dict[str, list[Need]] = {}
    for need in needs:
        if need.condition_symbol:
            needs_by_condition.setdefault(need.condition_symbol, []).append(need)

    return {
        "goods": [
            {
                "symbol": g.symbol,
                "name": g.name,
                "description": g.description,
                "effect": good_effect(g, needs_by_condition),
            }
            for g in goods
        ],
        "needs": [
            {
                "code": n.code,
                "name": n.name,
                "description": n.description,
                "draws": (
                    f"{_num(n.quantity_per_tick)} {n.code} per tick from "
                    + ", ".join(s.symbol for s in n.satisfiers)
                    + (" (in draw order)" if len(n.satisfiers) > 1 else "")
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
                "currency": m.currency,
            }
            for m in markets
        ],
    }
