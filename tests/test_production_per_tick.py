"""Per-tick recipe inputs: a duration-N recipe that consumes inputs once per
tick it is RUNNING, rather than only at start. Covers the happy path, the
forfeit-on-shortfall path (FAILED, no partial draw), atomicity of a tick's
draw across multiple inputs, and the lump-plus-flow combination."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.markets import adjust_holding, get_holding
from econengine.models import Base, EntityType, Process, ProcessStatus
from econengine.production import consume_per_tick_inputs, create_recipe, start_process
from econengine.services import create_entity
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def farmer(session):
    # STUDY: 3-tick recipe that burns 1 FUEL/tick and yields 1 DIPLOMA at end.
    # With per_tick_inputs it pays FUEL on ticks 1,2,3 and completes at tick 4.
    ent = create_entity(session, "Farmer", EntityType.INDIVIDUAL)
    create_recipe(session, "STUDY", inputs={}, outputs={"DIPLOMA": Decimal("1")},
                  duration_ticks=3, per_tick_inputs={"FUEL": Decimal("1")})
    return ent


def test_per_tick_inputs_paid_each_tick_then_completes(farmer, session):
    """Duration-3 recipe pays its per-tick input three times (one per running
    tick) and completes at tick 4 without paying a fourth time."""
    adjust_holding(session, farmer, "FUEL", Decimal("5"))
    process = start_process(session, farmer, "STUDY")  # started 1, completes 4

    run_tick(session)  # tick 1: draw -> FUEL 4
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("4")
    run_tick(session)  # tick 2: draw -> FUEL 3
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("3")
    run_tick(session)  # tick 3: draw -> FUEL 2
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("2")
    assert get_holding(session, farmer.id, "DIPLOMA") is None

    tick4 = run_tick(session)  # tick 4: completes (step 1), not drawn again
    assert process.status == ProcessStatus.COMPLETED
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("2")  # 5 - 3
    assert get_holding(session, farmer.id, "DIPLOMA").quantity == Decimal("1")
    assert not any(e["type"] == "process_failed" for e in tick4.events)


def test_per_tick_shortfall_aborts_process_no_outputs(farmer, session):
    """A tick the entity cannot meet the per-tick input forfeits the process:
    status FAILED, no outputs, everything already paid stays paid."""
    adjust_holding(session, farmer, "FUEL", Decimal("2"))  # enough for 2 of 3 ticks
    process = start_process(session, farmer, "STUDY")

    run_tick(session)  # tick 1: draw -> FUEL 1
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("1")
    run_tick(session)  # tick 2: draw -> FUEL 0
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("0")
    assert process.status == ProcessStatus.RUNNING

    tick3 = run_tick(session)  # tick 3: 0 < 1 -> FAILED
    assert process.status == ProcessStatus.FAILED
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("0")
    assert get_holding(session, farmer.id, "DIPLOMA") is None
    failed = [e for e in tick3.events if e["type"] == "process_failed"]
    assert len(failed) == 1 and failed[0]["recipe"] == "STUDY"
    assert failed[0]["symbol"] == "FUEL"


def test_failed_process_not_redrawn(farmer, session):
    """Once FAILED a process is skipped by every later draw: holdings stay put
    even if the entity later acquires more of the input."""
    adjust_holding(session, farmer, "FUEL", Decimal("2"))
    start_process(session, farmer, "STUDY")
    run_tick(session); run_tick(session)  # pays FUEL 1, 2 -> 0
    tick3 = run_tick(session)
    assert tick3.events and any(e["type"] == "process_failed" for e in tick3.events)

    adjust_holding(session, farmer, "FUEL", Decimal("10"))  # now flush
    tick4 = run_tick(session); tick5 = run_tick(session)
    # nothing drew on ticks 4-5: the 10 FUEL we added is still there
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("10")
    assert get_holding(session, farmer.id, "DIPLOMA") is None
    assert not any(e["type"] == "process_failed" for e in tick4.events + tick5.events)


def test_shortfall_does_not_partially_draw(farmer, session):
    """A tick's per-tick draw is all-or-nothing across its inputs: if the
    second input is short, the first is left untouched (no partial draw)."""
    # RESEARCH-style recipe with two per-tick inputs; we starve the second.
    create_recipe(session, "DUAL", inputs={}, outputs={"DIPLOMA": Decimal("1")},
                  duration_ticks=2,
                  per_tick_inputs={"FUEL": Decimal("1"), "WATER": Decimal("1")})
    adjust_holding(session, farmer, "FUEL", Decimal("5"))
    adjust_holding(session, farmer, "WATER", Decimal("0"))
    process = start_process(session, farmer, "DUAL")

    tick1 = run_tick(session)
    assert process.status == ProcessStatus.FAILED
    # FUEL was checked (5 >= 1) but WATER (0 < 1) shorted before any draw:
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("5")
    assert get_holding(session, farmer.id, "WATER").quantity == Decimal("0")
    failed = next(e for e in tick1.events if e["type"] == "process_failed")
    assert failed["symbol"] == "WATER"


def test_lump_inputs_plus_per_tick_inputs(farmer, session):
    """A recipe may take a lump input at start AND a per-tick input: the lump
    is consumed once in start_process, the flow each running tick thereafter."""
    create_recipe(session, "KILN", inputs={"CLAY": Decimal("2")},
                  outputs={"POT": Decimal("1")}, duration_ticks=2,
                  per_tick_inputs={"FUEL": Decimal("1")})
    adjust_holding(session, farmer, "CLAY", Decimal("2"))
    adjust_holding(session, farmer, "FUEL", Decimal("3"))
    process = start_process(session, farmer, "KILN")  # CLAY drawn up front -> 0

    assert get_holding(session, farmer.id, "CLAY").quantity == Decimal("0")
    run_tick(session)  # per-tick FUEL -> 2
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("2")
    run_tick(session)  # per-tick FUEL -> 1
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("1")
    run_tick(session)  # completes at tick 3
    assert process.status == ProcessStatus.COMPLETED
    assert get_holding(session, farmer.id, "POT").quantity == Decimal("1")
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("1")  # not drawn 3rd time


def test_consume_per_tick_skips_recipes_without_per_tick_inputs(farmer, session):
    """A RUNNING process whose recipe has no per_tick_inputs is a no-op for the
    pass (and is not failed). Regression guard for the filter."""
    create_recipe(session, "PLAIN", inputs={}, outputs={"DIPLOMA": Decimal("1")},
                  duration_ticks=2)
    adjust_holding(session, farmer, "FUEL", Decimal("5"))
    start_process(session, farmer, "PLAIN")
    run_tick(session)  # no per-tick input declared -> no draw, no fail
    assert get_holding(session, farmer.id, "FUEL").quantity == Decimal("5")


def test_consume_per_tick_inputs_empty_when_no_running(session):
    """Calling the pass with no RUNNING processes returns no events and does
    not touch anything (the early-out path)."""
    assert consume_per_tick_inputs(session, tick_number=1) == []
