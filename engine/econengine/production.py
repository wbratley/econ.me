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

REQUIREMENTS are the present-but-not-consumed check (design.md § recipes):
one mechanism serving machinery ("hold 1 OVEN" — good_requirements) and
facilities ("a SMITHY you control" — requires_facility) alike. Checked at
start, never consumed, and *reserving*: a symbol's requirements summed
across the entity's running processes may not exceed its holding (one
hammer cannot back unlimited concurrent workshops), inputs may only consume
the unreserved balance, and market settlement treats reserved quantities as
unavailable (markets.reserved_quantity). Facilities reserve per parcel the
same way: each bound running process occupies one facility of the required
type.

Parcels make production LOCATED (see parcels.py). A recipe that requires a
facility, draws a deposit (deposit_inputs, drawn from the bound parcel at
start), or builds a facility (builds_facility, erected on the bound parcel
at completion) must be started bound to a parcel the entity controls —
checked at start like the tech gates; a parcel with bound running processes
cannot change hands, so the binding stays valid through completion.
"""

from decimal import Decimal
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import clock, conditions, parcels, rng, tech
from .markets import InsufficientHoldingsError, adjust_holding, get_holding, reserved_quantity
from .models import (
    Entity, EntityStatus, Parcel, Process, ProcessStatus, Recipe, RecipeBranch,
    RecipeBranchOutput, RecipeDepositInput, RecipeGoodRequirement, RecipeInput,
    RecipeOutput, RecipePerTickInput, RecipeRequirement, RecipeUnlock, Tick,
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
    description: str = "",
    requires: list[str] | None = None,
    unlocks: list[str] | None = None,
    branches: list[dict] | None = None,
    good_requirements: dict[str, Decimal] | None = None,
    deposit_inputs: dict[str, Decimal] | None = None,
    per_tick_inputs: dict[str, Decimal] | None = None,
    requires_facility: str | None = None,
    builds_facility: str | None = None,
    requires_daylight: bool = False,
    requires_place_kind: str | None = None,
    requires_place_key: str | None = None,
) -> Recipe:
    """Branches, if given, are the outcome table: each entry is
    {"weight": Decimal, "outputs": {symbol: qty}, "label": str}, in table
    order. A branch's outputs may be empty (total loss). Mutually exclusive
    with plain outputs."""
    existing = get_recipe(session, code)
    if existing is not None:
        raise ValueError(
            f"recipe {str(code).upper()} already installed by "
            f"{existing.pack_id or 'the platform'} -- a pack may not claim another installer's key")
    if duration_ticks < 0:
        raise ValueError("duration_ticks must be >= 0")
    if branches and outputs:
        raise ValueError("recipe declares either outputs or branches, not both")
    if not outputs and not branches and not unlocks and not builds_facility:
        raise ValueError(
            "recipe must declare at least one output, branch, unlock, or built facility"
        )

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
        description=description,
        duration_ticks=duration_ticks,
        requires_facility=requires_facility.upper() if requires_facility else None,
        builds_facility=builds_facility.upper() if builds_facility else None,
        requires_daylight=requires_daylight,
        requires_place_kind=requires_place_kind.upper() if requires_place_kind else None,
        requires_place_key=requires_place_key.upper() if requires_place_key else None,
        inputs=rows(RecipeInput, inputs),
        outputs=rows(RecipeOutput, outputs),
        branches=branch_rows,
        requirements=tech_rows(RecipeRequirement, requires),
        unlocks=tech_rows(RecipeUnlock, unlocks),
        good_requirements=rows(RecipeGoodRequirement, good_requirements or {}),
        deposit_inputs=rows(RecipeDepositInput, deposit_inputs or {}),
        per_tick_inputs=rows(RecipePerTickInput, per_tick_inputs or {}),
    )
    session.add(recipe)
    session.flush()
    return recipe


def get_recipe(session: Session, code: str) -> Recipe | None:
    return session.execute(
        select(Recipe).where(Recipe.code == str(code).upper())
    ).scalar_one_or_none()


def recipe_needs_parcel(recipe: Recipe) -> bool:
    return bool(recipe.requires_facility or recipe.builds_facility or recipe.deposit_inputs)


def _available_quantity(
    session: Session, entity: Entity, symbol: str, factor: Decimal = Decimal("1")
) -> Decimal:
    """Held (× the caller's condition factor) minus reserved-by-running-
    processes — what requirements, inputs, and market settlement may draw
    on. Only requirement checks pass a factor (an effective-quantity read
    site, docs/design.md § conditions): a fever halves what your SKILL-SMITH
    counts for without drawing the holding down. Inputs are consumed at face
    value — conditions scale capability, not matter."""
    holding = get_holding(session, entity.id, symbol)
    held = holding.quantity if holding else Decimal("0")
    if factor != 1:
        held = conditions.effective_quantity(held, factor)
    return held - reserved_quantity(session, entity.id, symbol)


def start_process(
    session: Session, entity: Entity, recipe_code: str, parcel_id: str | None = None
) -> Process:
    """Consume the recipe's inputs from the entity's holdings (and its
    deposit_inputs from the bound parcel) and create a Process. Raises
    InsufficientHoldingsError if any input or requirement is short (all
    consumption rolls back with the caller's savepoint). Requirements and
    inputs may only draw on the unreserved balance — holdings backing other
    running processes are unavailable."""
    recipe = get_recipe(session, recipe_code)
    if recipe is None:
        # Name the valid codes: an agent that guesses a recipe name
        # (FARMING vs FARM_GRAIN) is blind otherwise — the unlock names
        # technologies, not recipes, and nothing else in its observation
        # ever lists the codes. One error message with the catalog turns
        # 200 ticks of starvation into one corrected submission.
        available = ", ".join(
            session.execute(
                select(Recipe.code).where(Recipe.is_active)
                .order_by(Recipe.code)
            ).scalars())
        raise ValueError(
            f"no recipe {str(recipe_code).upper()!r}; "
            f"available: {available or 'none'}")
    if not recipe.is_active:
        raise ValueError(f"recipe {recipe.code} is inactive")
    if entity.status != EntityStatus.ACTIVE:
        raise ValueError("entity is incapacitated")
    if recipe.requires_daylight:
        tick = next_tick_number(session)
        hour = clock.hour_of(tick)
        if clock.is_night(tick):
            raise ValueError(
                f"too dark for {recipe.code} (hour {hour:02d}, night — "
                f"daylight is hours {clock.DAY_START_HOUR:02d}.."
                f"{clock.DAY_END_HOUR - 1:02d})"
            )
    if recipe.requires_place_kind or recipe.requires_place_key:
        # The S2 presence gate (docs/spatial.md §6): where the entity
        # STANDS gates what it may start. Unplaced satisfies no gate —
        # the legacy NULL location is the unsubjected citizen, but a
        # gated recipe is declared spatial data, and it fires on it.
        from . import places as places_mod

        where = entity.place
        if recipe.requires_place_key is not None:
            target = places_mod.get_place(session, recipe.requires_place_key)
            if target is None:
                raise ValueError(
                    f"recipe {recipe.code} requires presence at "
                    f"{recipe.requires_place_key!r} -- no such place is installed"
                )
            if where is None or where.id != target.id:
                raise ValueError(
                    f"must be at {places_mod.label(target)} for {recipe.code} -- "
                    + (f"you are at {places_mod.label(where)}"
                       if where is not None else "you are not on the map")
                )
        elif where is None or where.kind != recipe.requires_place_kind:
            raise ValueError(
                f"must be at a {recipe.requires_place_kind} for {recipe.code} -- "
                + (f"{places_mod.label(where)} is not one"
                   if where is not None else "you are not on the map")
            )

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

    parcel = None
    if parcel_id is not None:
        parcel = parcels.get_parcel(session, parcel_id)
        if parcel is None:
            raise ValueError("unknown parcel")
        if parcel.owner_id != entity.id:
            raise ValueError("entity does not control parcel")
    elif recipe.requires_facility:
        # Auto-bind (run 15: 20 refusals "recipe TEND_FIRE must be bound
        # to a parcel" while the engine knew exactly where the entity's
        # FIRE stood). A facility recipe started without a parcel binds
        # itself to the first owned parcel with a FREE facility of that
        # type -- where the facility lives is bookkeeping, not strategy.
        owned = session.execute(
            select(Parcel).where(Parcel.owner_id == entity.id).order_by(Parcel.id)
        ).scalars().all()
        has_any = False
        for p in owned:
            count = parcels.facility_count(session, p.id, recipe.requires_facility)
            if not count:
                continue
            has_any = True
            free = count - parcels.reserved_facilities(
                session, p.id, recipe.requires_facility
            )
            if free > 0:
                parcel = p
                break
        if parcel is None:
            if has_any:
                raise ValueError(
                    f"recipe {recipe.code}: your {recipe.requires_facility} "
                    f"facilities are fully reserved by running processes"
                )
            raise ValueError(
                f"recipe {recipe.code} requires a {recipe.requires_facility} "
                f"facility on a parcel you control; you have none"
            )
    elif recipe_needs_parcel(recipe):
        raise ValueError(
            f"recipe {recipe.code} must be bound to a parcel you control "
            f"(pass its parcel_id)"
        )

    if recipe.requires_facility:
        free = (
            parcels.facility_count(session, parcel.id, recipe.requires_facility)
            - parcels.reserved_facilities(session, parcel.id, recipe.requires_facility)
        )
        if free < 1:
            raise ValueError(
                f"parcel has no free {recipe.requires_facility} facility"
            )

    modifiers = (
        conditions.held_modifiers(session, entity.id) if recipe.good_requirements else []
    )
    for req in recipe.good_requirements:
        factor = conditions.effective_factor(session, entity.id, req.symbol, modifiers)
        available = _available_quantity(session, entity, req.symbol, factor)
        if available < req.quantity:
            raise InsufficientHoldingsError(
                f"entity {entity.id} has {available} {req.symbol} effective unreserved, "
                f"recipe {recipe.code} requires {req.quantity}"
            )

    for item in recipe.inputs:
        available = _available_quantity(session, entity, item.symbol)
        if available < item.quantity:
            # Name the balance the check actually drew on, and -- when
            # reservations hold part of it -- WHO holds them (run 15:
            # 144 "insufficient unreserved LABOR" refusals against a
            # holdings read that looked fine, with no way to see the
            # spendable side from the refusal).
            held = get_holding(session, entity.id, item.symbol)
            held_qty = held.quantity if held else Decimal("0")
            reservers = sorted({
                code for code in session.execute(
                    select(Recipe.code)
                    .select_from(Process)
                    .join(Recipe, Process.recipe_id == Recipe.id)
                    .join(RecipeGoodRequirement,
                          RecipeGoodRequirement.recipe_id == Recipe.id)
                    .where(
                        Process.entity_id == entity.id,
                        Process.status == ProcessStatus.RUNNING,
                        RecipeGoodRequirement.symbol == item.symbol,
                    )
                ).scalars()
            })
            note = (
                f", reserved by your running {', '.join(reservers)}"
                if reservers else ""
            )
            raise InsufficientHoldingsError(
                f"entity {entity.id} has {available} unreserved "
                f"{item.symbol} of {held_qty} held{note} "
                f"for recipe {recipe.code} input {item.quantity} "
                f"(std.unreserved('{item.symbol}') is the spendable balance)"
            )
        adjust_holding(session, entity, item.symbol, -item.quantity)
    for item in recipe.deposit_inputs:
        parcels.draw_deposit(session, parcel, item.symbol, item.quantity)

    tick = next_tick_number(session)
    process = Process(
        recipe=recipe,
        entity=entity,
        parcel=parcel,
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


def consume_per_tick_inputs(session: Session, tick_number: int) -> list[dict]:
    """Draw each RUNNING process's per_tick_inputs from its entity's holdings,
    once this tick. The recurring-cost counterpart to the lump-sum draw at
    start_process: a duration-N recipe with per_tick_inputs pays them N
    times over its run.

    Runs AFTER the auction and the intent retry (tick step 7c), so an entity
    can convert this tick's freshly bought labour (WORK_AS_FARMER, a
    duration-0 process retried in 7b) and feed a running process with it
    BEFORE the decay pass (9) takes half. That ordering is what lets a flow
    income fund a multi-tick process where banking the stock could not (with
    0.5/tick decay on the labour goods, no lump ever accumulates).

    A process whose entity cannot meet a per-tick input on a given tick is
    abandoned: status FAILED, no outputs and no unlocks credited, everything
    already paid forfeit -- the same consequence as a cancellation, but
    engine-initiated for want of an input rather than owner-initiated. The
    engine never partially draws a per-tick input: a process is all-or-nothing
    per tick, so a recipe's declared transform is always whole. Deterministic
    by created_at/id, like every process pass."""
    running = session.execute(
        select(Process)
        .where(Process.status == ProcessStatus.RUNNING)
        .order_by(Process.created_at, Process.id)
    ).scalars().all()
    events: list[dict] = []
    for process in running:
        recipe = process.recipe
        if not recipe.per_tick_inputs:
            continue
        entity = process.entity
        # Check every input against the unreserved balance BEFORE drawing
        # any, so a shortfall leaves the holdings untouched (the process is
        # forfeited whole, not partially consumed).
        short: str | None = None
        for item in recipe.per_tick_inputs:
            if _available_quantity(session, entity, item.symbol) < item.quantity:
                short = item.symbol
                break
        if short is not None:
            process.status = ProcessStatus.FAILED
            events.append({
                "type": "process_failed",
                "entity_id": entity.id,
                "process_id": process.id,
                "recipe": recipe.code,
                "reason": f"per-tick input {short} short",
                "symbol": short,
            })
            continue
        for item in recipe.per_tick_inputs:
            adjust_holding(session, entity, item.symbol, -item.quantity)
    if running:
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
    if process.parcel_id is not None:
        event["parcel_id"] = process.parcel_id
    if recipe.builds_facility:
        event["facility"] = recipe.builds_facility
    if branch is not None:
        event["branch"] = branch.position
        event["roll"] = process.outcome_roll
        if branch.label:
            event["branch_label"] = branch.label
    if recipe.unlocks:
        event["unlocks"] = sorted(u.technology.code for u in granted)
    return event


def _credit_output(session: Session, entity: Entity, item, reference: str) -> None:
    """Credit one recipe output. Goods adjust holdings; a symbol the
    world banks in (some Account is denominated in it) is money — the
    output credits the entity's account instead, creating it at zero if
    needed: production-minted currency (the stone age finds shiny stones
    while gathering; a later world might mine gold). The credit rides
    services.deposit, so it lands in the ledger and passes the same
    validators every other money movement does."""
    from . import services
    from .models import Account

    banked = session.execute(
        select(Account.id).where(Account.currency == item.symbol).limit(1)
    ).scalar_one_or_none()
    if banked is None:
        adjust_holding(session, entity, item.symbol, item.quantity)
        return
    account = next(
        (a for a in entity.accounts if a.currency == item.symbol), None)
    if account is None:
        account = services.create_account(session, entity, item.symbol)
    services.deposit(session, account, item.quantity, reference)


def _complete(session: Session, process: Process, seed: str) -> list:
    """Credit outputs — rolling the outcome branch first if the recipe is
    stochastic — erect the built facility, and grant the recipe's unlocks;
    returns the Unlock rows actually created (technology-code order,
    already-held ones skipped)."""
    recipe = process.recipe
    outputs = recipe.outputs
    if recipe.branches:
        process.outcome_roll = rng.outcome_roll(seed, process.id)
        process.outcome_branch = rng.weighted_index(
            process.outcome_roll, [b.weight for b in recipe.branches]
        )
        outputs = recipe.branches[process.outcome_branch].outputs
    for item in outputs:
        _credit_output(session, process.entity, item, f"mint {recipe.code}")
    if recipe.builds_facility:
        parcels.add_facility(
            session, process.parcel, recipe.builds_facility,
            built_tick=process.completes_tick,
        )
    granted = []
    for u in sorted(recipe.unlocks, key=lambda u: u.technology.code):
        unlock = tech.grant_unlock(
            session, process.entity, u.technology, tick_number=process.completes_tick
        )
        if unlock is not None:
            granted.append(unlock)
    process.status = ProcessStatus.COMPLETED
    return granted
