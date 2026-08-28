import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from econengine.markets import InsufficientHoldingsError, adjust_holding, get_holding
from econengine.models import (
    Base, EntityType, ProcessStatus, Transaction, TransactionType,
)
from econengine.production import (
    cancel_process,
    complete_processes,
    create_recipe,
    get_recipe,
    next_tick_number,
    start_process,
)
from econengine.services import create_entity


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
    # the 15.4 installer rule: a duplicate key is refused with a clean
    # error naming the owner, not an IntegrityError from the constraint
    with pytest.raises(ValueError, match="already installed by the platform"):
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
    with pytest.raises(ValueError, match=r"available:.*BAKE_BREAD") as err:
        start_process(session, baker, "FARMING")   # unlock name, not code
    assert "no recipe 'FARMING'" in str(err.value)
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


# --- production-minted money (_credit_output) -------------------------------

def _digger(session):
    """An entity and a one-tick DIG recipe that always finds 1 COIN —
    the stone age's shiny-stone gather, made deterministic."""
    digger = create_entity(session, "Digger", EntityType.INDIVIDUAL)
    adjust_holding(session, digger, "LABOR", Decimal("1"))
    recipe = create_recipe(
        session, "DIG", inputs={"LABOR": Decimal("1")}, outputs={},
        duration_ticks=1,
        branches=[{"weight": Decimal("1"), "outputs": {"COIN": Decimal("1")}}],
    )
    return digger, recipe


def test_money_output_mints_to_account(session):
    """A branch output the world banks in lands in the account, rides
    the ledger, and never becomes a holding — money is money."""
    from econengine.services import create_account, deposit

    digger, _ = _digger(session)
    # someone — anyone — banks COIN: that is what makes it a currency
    rich = create_entity(session, "Rich", EntityType.INDIVIDUAL)
    rich_acc = create_account(session, rich, "COIN")
    deposit(session, rich_acc, Decimal("10"), "seat stake")

    process = start_process(session, digger, "DIG")
    complete_processes(session, tick_number=2)
    assert process.status == ProcessStatus.COMPLETED

    acc = next(a for a in digger.accounts if a.currency == "COIN")  # auto-created
    assert acc.balance == Decimal("1")
    assert rich_acc.balance == Decimal("10")            # untouched: no transfer
    tx = session.query(Transaction).filter_by(account_id=acc.id).one()
    assert tx.tx_type == TransactionType.CREDIT
    assert tx.reference == "mint DIG"
    assert get_holding(session, digger.id, "COIN") is None or \
        get_holding(session, digger.id, "COIN").quantity == Decimal("0")


def test_unbanked_output_stays_a_good(session):
    """No account in the world holds COIN — it is just another good,
    credited to holdings exactly as before."""
    digger, _ = _digger(session)
    process = start_process(session, digger, "DIG")
    complete_processes(session, tick_number=2)
    assert digger.accounts == []
    assert get_holding(session, digger.id, "COIN").quantity == Decimal("1")
    assert session.query(Transaction).count() == 0


# --- facility auto-bind (run 15: 20 "must be bound to a parcel" refusals) ---

def _fire_world(session):
    from econengine.parcels import add_facility, create_parcel
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    adjust_holding(session, alice, "LABOR", Decimal("2"))
    camp = create_parcel(session, "CAMP", owner=alice)
    create_recipe(session, "TEND_FIRE", inputs={"LABOR": Decimal("1")},
                  outputs={"WARMTH": Decimal("8")}, duration_ticks=2,
                  requires_facility="FIRE")
    return alice, camp, add_facility(session, camp, "FIRE")


def test_facility_recipe_auto_binds_to_owned_facility(session):
    """A facility recipe started without parcel_id binds itself to the
    first owned parcel with a free facility of that type -- where the
    facility lives is bookkeeping, not strategy."""
    alice, camp, _fire = _fire_world(session)
    process = start_process(session, alice, "TEND_FIRE")  # no parcel_id
    assert process.parcel_id == camp.id


def test_facility_recipe_without_facility_names_what_is_missing(session):
    """Owning parcels is not owning the facility: auto-bind refuses and
    names what is missing rather than the old bare 'must be bound to a
    parcel' (which read like a call-syntax error, not a world fact)."""
    _alice, _camp, _fire = _fire_world(session)
    from econengine.parcels import create_parcel
    other = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    adjust_holding(session, other, "LABOR", Decimal("1"))
    create_parcel(session, "PLOT", owner=other)  # owns a parcel, no FIRE on it
    with pytest.raises(ValueError, match="FIRE facility"):
        start_process(session, other, "TEND_FIRE")


def test_facility_recipe_when_all_facilities_reserved(session):
    alice, _camp, _fire = _fire_world(session)
    start_process(session, alice, "TEND_FIRE")  # RUNNING: reserves the FIRE
    with pytest.raises(ValueError, match="fully reserved"):
        start_process(session, alice, "TEND_FIRE")


def test_input_refusal_names_running_reservers(session):
    """The refusal names the balance the check drew on and WHO holds the
    reservation -- the spendable side was invisible in run 15 (144
    'insufficient unreserved' bounces against a fine-looking pantry)."""
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    adjust_holding(session, alice, "OVEN", Decimal("1"))
    adjust_holding(session, alice, "FLOUR", Decimal("10"))
    create_recipe(session, "BAKE", inputs={"FLOUR": Decimal("1")},
                  outputs={"BREAD": Decimal("2")}, duration_ticks=2,
                  good_requirements={"OVEN": Decimal("1")})
    create_recipe(session, "SCRAP_OVEN", inputs={"OVEN": Decimal("1")},
                  outputs={"SCRAP": Decimal("5")}, duration_ticks=1)
    start_process(session, alice, "BAKE")  # RUNNING: reserves the OVEN
    with pytest.raises(InsufficientHoldingsError,
                       match=r"unreserved OVEN of [\d.]+ held, "
                             r"reserved by your running BAKE"):
        start_process(session, alice, "SCRAP_OVEN")
