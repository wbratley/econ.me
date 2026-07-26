"""
Production — recipes, processes, and the per-tick completion pass.

A Recipe declares a transformation (inputs → outputs over duration_ticks);
starting one consumes the inputs from the entity's holdings immediately and
creates a Process. The tick engine calls complete_processes() at the START
of each tick, so outputs credited this tick are already visible to scripts
and sellable in this tick's auction. Conservation is the invariant: a recipe
transforms exactly what it declares — goods enter the world only through
recipe outputs and admin grants.

Durations are measured in ticks, never wall-clock time. A process started
during tick N (by script intent, or via the API before tick N runs)
completes at tick N + duration; duration 0 completes immediately at start.

Cancellation forfeits the consumed inputs (refund policy is a future
votable parameter, not engine mechanism). The window closes one tick early:
a process due at tick N can last be cancelled during tick N-1 — once tick
N-1 has run, its events hash (the seed of every tick-N outcome roll, see
rng.py) is committed, and allowing cancellation after the seed is knowable
would let a roller cherry-pick outcomes. The rule applies to every recipe,
stochastic or not, so the window never depends on recipe shape.

A recipe may instead declare OUTCOME BRANCHES — alternative output sets
with fixed weights, sampled once at completion (a loot table, not a
formula: odds are constant and readable). Equipment the dice can eat is a
catalyst input — consumed at start, re-emitted by the branches that spare
it. Branches carry goods only; a stochastic research breakthrough is a
lucky branch yielding a marker good that a duration-0 research recipe
converts into the unlock — composition, not mechanism.

Recipes are where the tech tree touches the economy (see tech.py). A recipe
may REQUIRE technologies — start_process refuses unless the entity's unlock
set (own + world) contains them all — and may GRANT technologies on
completion, which is all research is: a recipe whose output is an unlock
rather than goods. Both gates are checked at start: requirements, and the
prerequisites of every technology the recipe grants (unlocks are never
revoked, so what is satisfiable at start is still satisfiable at
completion).
"""

from decimal import Decimal
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import rng, tech
from .markets import adjust_holding
from .models import (
    Entity, Process, ProcessStatus, Recipe, RecipeBranch, RecipeBranchOutput,
    RecipeInput, RecipeOutput, RecipeRequirement, RecipeUnlock, Tick,
)

_QUANTUM = Decimal("0.0001")


def next_tick_number(session: Session) -> int:
    """The tick currently running, or the upcoming one between ticks —
    the Tick row only flushes at the end of run_tick."""
    last = session.execute(select(func.max(Tick.number))).scalar_one()
    return (last or 0) + 1


def create_recipe(
    session: Session,
    code: str,
    inputs: dict[str, Decimal],
    outputs: dict[str, Decimal],
    duration_ticks: int,
    name: str = "",
    requires: list[str] | None = None,
    unlocks: list[str] | None = None,
    branches: list[dict] | None = None,
) -> Recipe:
    """Branches, if given, are the outcome table: each entry is
    {"weight": Decimal, "outputs": {symbol: qty}, "label": str}, in table
    order. A branch's outputs may be empty (total loss). Mutually exclusive
    with plain outputs."""
    if duration_ticks < 0:
        raise ValueError("duration_ticks must be >= 0")
    if branches and outputs:
        raise ValueError("recipe declares either outputs or branches, not both")
    if not outputs and not branches and not unlocks:
        raise ValueError("recipe must declare at least one output, branch, or unlock")

    def rows(cls, quantities: dict) -> list:
        out = []
        for symbol, quantity in sorted(quantities.items()):
            quantity = Decimal(quantity).quantize(_QUANTUM)
            if quantity <= 0:
                raise ValueError(f"quantity for {symbol.upper()} must be positive")
            out.append(cls(symbol=symbol.upper(), quantity=quantity))
        return out

    def tech_rows(cls, codes: list[str] | None) -> list:
        out = []
        for tech_code in sorted({str(c).upper() for c in (codes or [])}):
            technology = tech.get_technology(session, tech_code)
            if technology is None:
                raise ValueError(f"no technology {tech_code!r}")
            out.append(cls(technology=technology))
        return out

    branch_rows = []
    for position, branch in enumerate(branches or []):
        weight = Decimal(branch.get("weight", 0)).quantize(_QUANTUM)
        if weight <= 0:
            raise ValueError(f"branch {position} weight must be positive")
        branch_rows.append(RecipeBranch(
            position=position,
            weight=weight,
            label=str(branch.get("label", "")),
            outputs=rows(RecipeBranchOutput, branch.get("outputs") or {}),
        ))

    recipe = Recipe(
        code=code.upper(),
        name=name,
        duration_ticks=duration_ticks,
        inputs=rows(RecipeInput, inputs),
        outputs=rows(RecipeOutput, outputs),
        branches=branch_rows,
        requirements=tech_rows(RecipeRequirement, requires),
        unlocks=tech_rows(RecipeUnlock, unlocks),
    )
    session.add(recipe)
    session.flush()
    return recipe


def get_recipe(session: Session, code: str) -> Recipe | None:
    return session.execute(
        select(Recipe).where(Recipe.code == str(code).upper())
    ).scalar_one_or_none()


def start_process(session: Session, entity: Entity, recipe_code: str) -> Process:
    """Consume the recipe's inputs from the entity's holdings and create a
    Process. Raises InsufficientHoldingsError if any input is short (all
    consumption rolls back with the caller's savepoint)."""
    recipe = get_recipe(session, recipe_code)
    if recipe is None:
        raise ValueError(f"no recipe {str(recipe_code).upper()!r}")
    if not recipe.is_active:
        raise ValueError(f"recipe {recipe.code} is inactive")

    missing = sorted(
        r.technology.code for r in recipe.requirements
        if not tech.has_unlock(session, entity.id, r.technology)
    )
    if missing:
        raise ValueError(f"recipe {recipe.code} requires {', '.join(missing)}")
    for u in recipe.unlocks:
        unmet = tech.check_prerequisites(session, entity.id, u.technology)
        if unmet:
            raise ValueError(
                f"technology {u.technology.code} requires {', '.join(unmet)}"
            )

    for item in recipe.inputs:
        adjust_holding(session, entity, item.symbol, -item.quantity)

    tick = next_tick_number(session)
    process = Process(
        recipe=recipe,
        entity=entity,
        started_tick=tick,
        completes_tick=tick + recipe.duration_ticks,
    )
    session.add(process)
    session.flush()  # assigns process.id, which seeds a duration-0 outcome roll
    if recipe.duration_ticks == 0:
        _complete(session, process, prev_events_hash(session))
        session.flush()
    return process


def cancel_process(session: Session, process_id: str, entity_id: str) -> Process:
    process = session.get(Process, process_id)
    if process is None:
        raise ValueError("unknown process")
    if process.entity_id != entity_id:
        raise ValueError("entity does not own process")
    if process.status != ProcessStatus.RUNNING:
        raise ValueError(f"process is {process.status.value}, only running processes can be cancelled")
    if process.completes_tick <= next_tick_number(session):
        # the seed of the completing tick's outcome rolls is already
        # committed (commit-reveal, see rng.py) — no post-seed cancellation
        raise ValueError("process completes next tick; the cancellation window has closed")
    process.status = ProcessStatus.CANCELLED  # inputs are forfeit
    session.flush()
    return process


def prev_events_hash(session: Session) -> str:
    """The events hash of the latest persisted tick — the entropy every
    outcome roll of the NEXT tick is seeded from. Falls back to hashing the
    stored events for rows persisted before the hash column existed, and to
    the genesis constant before the first tick ever."""
    prev = session.execute(
        select(Tick).order_by(Tick.number.desc()).limit(1)
    ).scalar_one_or_none()
    if prev is None:
        return rng.GENESIS_HASH
    return prev.events_hash or rng.hash_events(list(prev.events or []))


def complete_processes(session: Session, tick_number: int) -> list[dict]:
    """Complete every due process; returns process_completed tick events,
    plus one unlocked event per technology actually granted (a research
    completion that duplicates an existing unlock grants — and emits —
    nothing). Stochastic completions record roll and branch in the event."""
    due = session.execute(
        select(Process)
        .where(Process.status == ProcessStatus.RUNNING, Process.completes_tick <= tick_number)
        .order_by(Process.created_at, Process.id)
    ).scalars().all()
    seed = prev_events_hash(session) if due else None
    events = []
    for process in due:
        granted = _complete(session, process, seed)
        events.append(_completed_event(process, granted))
        for unlock in granted:
            events.append({
                "type": "unlocked",
                "entity_id": process.entity_id,  # the discoverer, even for world scope
                "technology": unlock.technology.code,
                "scope": unlock.technology.scope.value,
            })
    if due:
        session.flush()
    return events


def _completed_event(process: Process, granted: list) -> dict:
    recipe = process.recipe
    if recipe.branches:
        branch = recipe.branches[process.outcome_branch]
        outputs = branch.outputs
    else:
        branch, outputs = None, recipe.outputs
    event = {
        "type": "process_completed",
        "entity_id": process.entity_id,
        "process_id": process.id,
        "recipe": recipe.code,
        "outputs": {o.symbol: str(o.quantity) for o in outputs},
    }
    if branch is not None:
        event["branch"] = branch.position
        event["roll"] = process.outcome_roll
        if branch.label:
            event["branch_label"] = branch.label
    if recipe.unlocks:
        event["unlocks"] = sorted(u.technology.code for u in granted)
    return event


def _complete(session: Session, process: Process, seed: str) -> list:
    """Credit outputs — rolling the outcome branch first if the recipe is
    stochastic — and grant the recipe's unlocks; returns the Unlock rows
    actually created (technology-code order, already-held ones skipped)."""
    recipe = process.recipe
    outputs = recipe.outputs
    if recipe.branches:
        process.outcome_roll = rng.outcome_roll(seed, process.id)
        process.outcome_branch = rng.weighted_index(
            process.outcome_roll, [b.weight for b in recipe.branches]
        )
        outputs = recipe.branches[process.outcome_branch].outputs
    for item in outputs:
        adjust_holding(session, process.entity, item.symbol, item.quantity)
    granted = []
    for u in sorted(recipe.unlocks, key=lambda u: u.technology.code):
        unlock = tech.grant_unlock(
            session, process.entity, u.technology, tick_number=process.completes_tick
        )
        if unlock is not None:
            granted.append(unlock)
    process.status = ProcessStatus.COMPLETED
    return granted
