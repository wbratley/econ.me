import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from econ.markets import InsufficientHoldingsError, adjust_holding, get_holding
from econ.models import Base, EntityType, ProcessStatus
from econ.production import (
    cancel_process,
    complete_processes,
    create_recipe,
    get_recipe,
    next_tick_number,
    start_process,
)
from econ.services import create_entity


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """A baker with flour, and a BAKE_BREAD recipe (2 FLOUR -> 3 BREAD, 2 ticks)."""
    baker = create_entity(session, "Baker", EntityType.INDIVIDUAL)
    adjust_holding(session, baker, "FLOUR", Decimal("10"))
    recipe = create_recipe(
        session, "bake_bread",
        inputs={"flour": Decimal("2")},
        outputs={"bread": Decimal("3")},
        duration_ticks=2,
        name="Bake bread",
    )
    return session, baker, recipe


# --- create_recipe ---

def test_create_recipe_normalizes(world):
    session, baker, recipe = world
    assert recipe.code == "BAKE_BREAD"
    assert [(i.symbol, i.quantity) for i in recipe.inputs] == [("FLOUR", Decimal("2"))]
    assert [(o.symbol, o.quantity) for o in recipe.outputs] == [("BREAD", Decimal("3"))]
    assert get_recipe(session, "bake_bread") is recipe


def test_recipe_validations(session):
    with pytest.raises(ValueError, match="output"):
        create_recipe(session, "X", inputs={}, outputs={}, duration_ticks=1)
    with pytest.raises(ValueError, match="duration"):
        create_recipe(session, "X", inputs={}, outputs={"Y": Decimal("1")}, duration_ticks=-1)
    with pytest.raises(ValueError, match="positive"):
        create_recipe(session, "X", inputs={"A": Decimal("0")}, outputs={"Y": Decimal("1")}, duration_ticks=1)


def test_duplicate_code_rejected(world):
    session, baker, recipe = world
    with pytest.raises(IntegrityError):
        create_recipe(session, "BAKE_BREAD", inputs={}, outputs={"X": Decimal("1")}, duration_ticks=1)


def test_gathering_recipe_needs_no_inputs(session):
    """Extraction-style recipes (until deposits exist) may have empty inputs."""
    forager = create_entity(session, "Forager", EntityType.INDIVIDUAL)
    create_recipe(session, "FORAGE", inputs={}, outputs={"BERRIES": Decimal("1")}, duration_ticks=0)
    start_process(session, forager, "FORAGE")
    assert get_holding(session, forager.id, "BERRIES").quantity == Decimal("1")


# --- start_process ---

def test_start_consumes_inputs_and_schedules(world):
    session, baker, recipe = world
    process = start_process(session, baker, "BAKE_BREAD")
    assert get_holding(session, baker.id, "FLOUR").quantity == Decimal("8")
    assert get_holding(session, baker.id, "BREAD") is None
    assert process.status == ProcessStatus.RUNNING
    assert process.started_tick == 1  # no ticks yet -> upcoming tick 1
    assert process.completes_tick == 3


def test_start_insufficient_inputs(world):
    session, baker, recipe = world
    poor = create_entity(session, "Poor", EntityType.INDIVIDUAL)
    with pytest.raises(InsufficientHoldingsError):
        start_process(session, poor, "BAKE_BREAD")


def test_start_unknown_or_inactive_recipe(world):
    session, baker, recipe = world
    with pytest.raises(ValueError, match="no recipe"):
        start_process(session, baker, "NOPE")
    recipe.is_active = False
    with pytest.raises(ValueError, match="inactive"):
        start_process(session, baker, "BAKE_BREAD")


def test_zero_duration_completes_immediately(world):
    session, baker, recipe = world
    create_recipe(session, "QUICK", inputs={"FLOUR": Decimal("1")},
                  outputs={"CRUMBS": Decimal("5")}, duration_ticks=0)
    process = start_process(session, baker, "QUICK")
    assert process.status == ProcessStatus.COMPLETED
    assert get_holding(session, baker.id, "CRUMBS").quantity == Decimal("5")


# --- complete_processes ---

def test_completion_waits_for_due_tick(world):
    session, baker, recipe = world
    process = start_process(session, baker, "BAKE_BREAD")  # completes tick 3

    assert complete_processes(session, tick_number=2) == []
    assert process.status == ProcessStatus.RUNNING

    events = complete_processes(session, tick_number=3)
    assert process.status == ProcessStatus.COMPLETED
    assert get_holding(session, baker.id, "BREAD").quantity == Decimal("3")
    assert events == [{
        "type": "process_completed",
        "entity_id": baker.id,
        "process_id": process.id,
        "recipe": "BAKE_BREAD",
        "outputs": {"BREAD": "3.0000"},
    }]

    # idempotent: a completed process never completes twice
    assert complete_processes(session, tick_number=4) == []
    assert get_holding(session, baker.id, "BREAD").quantity == Decimal("3")


# --- cancel_process ---

def test_cancel_forfeits_inputs(world):
    session, baker, recipe = world
    process = start_process(session, baker, "BAKE_BREAD")
    cancel_process(session, process.id, baker.id)
    assert process.status == ProcessStatus.CANCELLED
    assert get_holding(session, baker.id, "FLOUR").quantity == Decimal("8")  # no refund
    assert complete_processes(session, tick_number=99) == []  # never completes


def test_cancel_ownership_and_state(world):
    session, baker, recipe = world
    other = create_entity(session, "Other", EntityType.INDIVIDUAL)
    process = start_process(session, baker, "BAKE_BREAD")
    with pytest.raises(ValueError, match="own"):
        cancel_process(session, process.id, other.id)
    cancel_process(session, process.id, baker.id)
    with pytest.raises(ValueError, match="cancelled"):
        cancel_process(session, process.id, baker.id)
    with pytest.raises(ValueError, match="unknown"):
        cancel_process(session, "nope", baker.id)


def test_next_tick_number_empty_db(session):
    assert next_tick_number(session) == 1
