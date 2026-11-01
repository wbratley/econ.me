"""The resume ledger (run-69 one-rung trap): ticks a process already paid
before dying — per-tick shortfall or cancellation — are banked per
(entity, recipe), and re-starting the same recipe resumes the work: no
second lump draw, no second currency leg, only the remaining ticks. The
failure event says how much was kept, so it reads as a pause to finish,
not a wall (in run 69 both houses tried the ladder exactly once, failed
it starved, and never tried again)."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.markets import adjust_holding, get_holding
from econengine.models import Base, EntityType, ProcessStatus
from econengine.production import (
    cancel_process, create_recipe, get_progress, start_process,
)
from econengine.services import create_entity
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def scholar(session):
    # SCHOLAR: the study shape — lump FLINT at start, 1 LABOR per running
    # tick, duration 2, one SCROLL at the end. Started between ticks, the
    # first draw lands on the next run_tick.
    ent = create_entity(session, "Scholar", EntityType.INDIVIDUAL)
    create_recipe(session, "SCHOLAR", inputs={"FLINT": Decimal("1")},
                  outputs={"SCROLL": Decimal("1")}, duration_ticks=2,
                  per_tick_inputs={"LABOR": Decimal("1")})
    return ent


def _run_out_of_labor(session, ent):
    held = get_holding(session, ent.id, "LABOR")
    if held is not None and held.quantity > 0:
        adjust_holding(session, ent, "LABOR", -held.quantity)


def test_shortfall_banks_ticks_and_event_teaches(scholar, session):
    """A starved per-tick draw fails the run but banks the paid ticks, and
    the failure event carries kept/of so the sentence can teach the restart."""
    adjust_holding(session, scholar, "FLINT", Decimal("1"))
    adjust_holding(session, scholar, "LABOR", Decimal("1"))
    process = start_process(session, scholar, "SCHOLAR")  # lump FLINT -> 0

    run_tick(session)  # tick 1: draw -> LABOR 0
    _run_out_of_labor(session, scholar)
    tick2 = run_tick(session)  # tick 2: starved -> FAILED, 1 tick banked

    assert process.status == ProcessStatus.FAILED
    failed = next(e for e in tick2.events if e["type"] == "process_failed")
    assert failed["kept"] == 1 and failed["of"] == 2
    assert failed["symbol"] == "LABOR"
    row = get_progress(session, scholar.id, process.recipe_id)
    assert row is not None and row.ticks_paid == 1


def test_restart_resumes_without_second_lump_draw(scholar, session):
    """The restart runs only the remaining ticks and does not re-charge the
    lump inputs — the FLINT holding is empty and the start still succeeds;
    completion clears the ledger."""
    adjust_holding(session, scholar, "FLINT", Decimal("1"))
    adjust_holding(session, scholar, "LABOR", Decimal("1"))
    start_process(session, scholar, "SCHOLAR")

    run_tick(session)  # tick 1: draw -> LABOR 0
    _run_out_of_labor(session, scholar)
    run_tick(session)  # tick 2: FAILED, 1 banked
    assert get_holding(session, scholar.id, "FLINT").quantity == Decimal("0")

    # restart: no FLINT to draw — the resume must not ask for it
    adjust_holding(session, scholar, "LABOR", Decimal("1"))
    resumed = start_process(session, scholar, "SCHOLAR")  # started 3, 1 left
    assert resumed.completes_tick == 4
    assert resumed.status == ProcessStatus.RUNNING

    run_tick(session)  # tick 3: the last draw -> LABOR 0
    tick4 = run_tick(session)  # tick 4: completes
    assert resumed.status == ProcessStatus.COMPLETED
    assert get_holding(session, scholar.id, "SCROLL").quantity == Decimal("1")
    assert get_progress(session, scholar.id, resumed.recipe_id) is None
    completed = next(e for e in tick4.events if e["type"] == "process_completed")
    assert completed["process_id"] == resumed.id


def test_total_labor_paid_equals_duration(scholar, session):
    """Across the failed run and the resume, exactly `duration` per-tick
    draws happened — the ledger credits time, it never mints work."""
    adjust_holding(session, scholar, "FLINT", Decimal("1"))
    adjust_holding(session, scholar, "LABOR", Decimal("5"))
    start_process(session, scholar, "SCHOLAR")

    run_tick(session)  # tick 1: draw (LABOR 4)
    _run_out_of_labor(session, scholar)
    run_tick(session)  # tick 2: FAILED (banked 1)
    adjust_holding(session, scholar, "LABOR", Decimal("5"))  # 4 remaining
    resumed = start_process(session, scholar, "SCHOLAR")
    run_tick(session)  # tick 3: the last draw (LABOR 3)
    run_tick(session)  # tick 4: completes

    assert resumed.status == ProcessStatus.COMPLETED
    # draws across both runs: tick 1 (failed run) and tick 3 (resume) —
    # exactly `duration` of them; the ledger credited time, never minted
    assert get_holding(session, scholar.id, "LABOR").quantity == Decimal("4")


def test_cancel_banks_progress_too(scholar, session):
    """Cancelling is a pause, not a forfeiture: the paid ticks bank, the
    restart picks up the remainder without a second lump draw."""
    adjust_holding(session, scholar, "FLINT", Decimal("1"))
    adjust_holding(session, scholar, "LABOR", Decimal("2"))
    process = start_process(session, scholar, "SCHOLAR")

    run_tick(session)  # tick 1: draw -> LABOR 1
    cancel_process(session, process.id, scholar.id)  # paid 1 banked
    assert process.status == ProcessStatus.CANCELLED
    row = get_progress(session, scholar.id, process.recipe_id)
    assert row.ticks_paid == 1

    resumed = start_process(session, scholar, "SCHOLAR")  # no FLINT needed
    run_tick(session)  # tick 2: the last draw
    run_tick(session)  # tick 3: completes
    assert resumed.status == ProcessStatus.COMPLETED
    assert get_holding(session, scholar.id, "SCROLL").quantity == Decimal("1")


def test_progress_capped_and_fully_paid_finishes_immediately(scholar, session):
    """Two CONCURRENT runs of the same recipe each pay one tick, then both
    starve: the ledger banks 1 + 1 = the whole duration (capped), and the
    next start is fully paid — it completes the moment it starts, drawing
    nothing. Sequential runs alone can never get here (a resume that dies
    on its only remaining draw banks 0; the cancel window closes first),
    so the cap is exactly this corner."""
    adjust_holding(session, scholar, "FLINT", Decimal("2"))
    adjust_holding(session, scholar, "LABOR", Decimal("2"))
    a = start_process(session, scholar, "SCHOLAR")  # lump draw -> FLINT 1
    b = start_process(session, scholar, "SCHOLAR")  # lump draw -> FLINT 0

    run_tick(session)  # tick 1: both draw -> LABOR 0
    _run_out_of_labor(session, scholar)
    run_tick(session)  # tick 2: both starved -> both FAILED
    assert a.status == ProcessStatus.FAILED and b.status == ProcessStatus.FAILED

    row = get_progress(session, scholar.id, a.recipe_id)
    assert row.ticks_paid == 2  # 1 + 1, capped at the duration

    # no FLINT, no LABOR held — and it completes anyway: time was paid
    resumed = start_process(session, scholar, "SCHOLAR")
    assert resumed.status == ProcessStatus.COMPLETED
    assert get_holding(session, scholar.id, "SCROLL").quantity == Decimal("1")
    assert get_progress(session, scholar.id, a.recipe_id) is None


def test_resume_keeps_the_banked_run_effort(session):
    """A restart re-prices outputs at the effort the banked run started at —
    a short-ration start cannot be laundered into a full one by failing."""
    ent = create_entity(session, "Tired", EntityType.INDIVIDUAL)
    create_recipe(session, "GRIND", inputs={"LABOR": Decimal("1")},
                  outputs={"MEAL": Decimal("1")}, duration_ticks=2,
                  per_tick_inputs={"FUEL": Decimal("1")})
    adjust_holding(session, ent, "LABOR", Decimal("0.6"))  # a weak start
    adjust_holding(session, ent, "FUEL", Decimal("5"))

    process = start_process(session, ent, "GRIND")  # effort 0.6, LABOR -> 0
    assert process.effort_factor == Decimal("0.6")
    run_tick(session)  # tick 1: FUEL draw
    cancel_process(session, process.id, ent.id)  # 1 tick banked at effort 0.6

    resumed = start_process(session, ent, "GRIND")  # no LABOR held: skipped
    assert resumed.effort_factor == Decimal("0.6")
    run_tick(session)  # tick 2: the last FUEL draw
    run_tick(session)  # tick 3: completes at the paid effort
    assert resumed.status == ProcessStatus.COMPLETED
    assert get_holding(session, ent.id, "MEAL").quantity == Decimal("0.6")


def test_travel_hops_never_bank(session):
    """The road prices a journey, not the ledger: a starved travel hop must
    not bank ticks a later journey would resume for free."""
    from econengine import edges as edges_mod
    from econengine import places as places_mod
    from econengine.production import get_recipe
    from econengine.travel import start_route

    home = places_mod.create_place(session, "HOME", kind="HEARTH", name="Home")
    away = places_mod.create_place(session, "AWAY", kind="FLINT", name="Away")
    edges_mod.create_edge(session, "HOME", "AWAY", "WALK", 2)
    ent = create_entity(session, "Walker", EntityType.INDIVIDUAL)
    places_mod.move_entity(session, ent, home)
    create_recipe(session, "TRAVEL_WALK",
                  inputs={}, outputs={}, duration_ticks=1,
                  per_tick_inputs={"FOOD": Decimal("1")})
    adjust_holding(session, ent, "FOOD", Decimal("1"))

    start_route(session, ent, "AWAY", modes=["WALK"])
    run_tick(session)  # tick 1: hop's first draw -> FOOD 0
    run_tick(session)  # tick 2: starved -> FAILED

    recipe = get_recipe(session, "TRAVEL_WALK")
    assert get_progress(session, ent.id, recipe.id) is None
