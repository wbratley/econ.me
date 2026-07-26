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
votable parameter, not engine mechanism).
"""

from decimal import Decimal
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .markets import adjust_holding
from .models import Entity, Process, ProcessStatus, Recipe, RecipeInput, RecipeOutput, Tick

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
) -> Recipe:
    if duration_ticks < 0:
        raise ValueError("duration_ticks must be >= 0")
    if not outputs:
        raise ValueError("recipe must declare at least one output")

    def rows(cls, quantities: dict) -> list:
        out = []
        for symbol, quantity in sorted(quantities.items()):
            quantity = Decimal(quantity).quantize(_QUANTUM)
            if quantity <= 0:
                raise ValueError(f"quantity for {symbol.upper()} must be positive")
            out.append(cls(symbol=symbol.upper(), quantity=quantity))
        return out

    recipe = Recipe(
        code=code.upper(),
        name=name,
        duration_ticks=duration_ticks,
        inputs=rows(RecipeInput, inputs),
        outputs=rows(RecipeOutput, outputs),
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
    if recipe.duration_ticks == 0:
        _complete(session, process)
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
    process.status = ProcessStatus.CANCELLED  # inputs are forfeit
    session.flush()
    return process


def complete_processes(session: Session, tick_number: int) -> list[dict]:
    """Complete every due process; returns process_completed tick events."""
    due = session.execute(
        select(Process)
        .where(Process.status == ProcessStatus.RUNNING, Process.completes_tick <= tick_number)
        .order_by(Process.created_at, Process.id)
    ).scalars().all()
    events = []
    for process in due:
        _complete(session, process)
        events.append({
            "type": "process_completed",
            "entity_id": process.entity_id,
            "process_id": process.id,
            "recipe": process.recipe.code,
            "outputs": {o.symbol: str(o.quantity) for o in process.recipe.outputs},
        })
    if due:
        session.flush()
    return events


def _complete(session: Session, process: Process) -> None:
    for item in process.recipe.outputs:
        adjust_holding(session, process.entity, item.symbol, item.quantity)
    process.status = ProcessStatus.COMPLETED
