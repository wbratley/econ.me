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


# --- the commons (P1: facility access, capacity, fuel, holding caps) --------

def _commons_world(session, seats=1, fuel=Decimal("6"), cap=Decimal("6"),
                   burn=Decimal("1")):
    """A village: an unowned COMMONS parcel with a PUBLIC fire, a villager
    who owns nothing, and WARM (duration 1, occupies a seat) and STOKE
    (instant, banks fuel) recipes against it."""
    from econengine.parcels import add_facility, create_parcel
    villager = create_entity(session, "Villager", EntityType.INDIVIDUAL)
    ground = create_parcel(session, "COMMONS", name="fire-ground")
    add_facility(session, ground, "FIRE", access="PLACE", capacity=seats,
                 fuel=fuel, fuel_capacity=cap, fuel_burn_per_tick=burn)
    create_recipe(session, "WARM", inputs={},
                  outputs={"WARMTH": Decimal("6")}, duration_ticks=1,
                  requires_facility="FIRE", requires_facility_lit=True)
    create_recipe(session, "STOKE", inputs={"WOOD": Decimal("1")},
                  outputs={}, duration_ticks=0, requires_facility="FIRE",
                  facility_fuel_output=Decimal("2"))
    adjust_holding(session, villager, "WOOD", Decimal("5"))
    return villager, ground


def test_public_facility_binds_the_parcel_less(session):
    """PLACE access is the commons: an entity that owns NO parcel warms at
    the public fire by auto-bind, and may bind it explicitly too. Owning
    parcels is not the price of admission -- presence is the recipe's
    business."""
    villager, ground = _commons_world(session)
    process = start_process(session, villager, "WARM")       # auto-bind
    assert process.parcel_id == ground.id
    complete_processes(session, tick_number=process.completes_tick)
    process = start_process(session, villager, "WARM", ground.id)  # explicit
    assert process.parcel_id == ground.id


def test_private_facility_still_refuses_strangers(session):
    """The commons is per-facility, not a world flag: a stranger binding a
    PRIVATE facility's parcel is refused exactly as before."""
    from econengine.parcels import add_facility, create_parcel
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    stranger = create_entity(session, "Stranger", EntityType.INDIVIDUAL)
    adjust_holding(session, stranger, "LABOR", Decimal("1"))
    camp = create_parcel(session, "CAMP", owner=alice)
    add_facility(session, camp, "FIRE", fuel=Decimal("6"))   # OWNER access
    create_recipe(session, "WARM", inputs={},
                  outputs={"WARMTH": Decimal("6")}, duration_ticks=1,
                  requires_facility="FIRE")
    with pytest.raises(ValueError, match="does not control"):
        start_process(session, stranger, "WARM", camp.id)
    # and auto-bind will not reach it either: no public fire exists
    with pytest.raises(ValueError, match="free seat"):
        start_process(session, stranger, "WARM")


def test_facility_capacity_is_seats(session):
    """capacity is concurrent binder slots: a two-seat fire warms two at
    once and refuses the third until an hour frees."""
    villagers = [create_entity(session, f"V{i}", EntityType.INDIVIDUAL)
                 for i in range(3)]
    villager, ground = _commons_world(session, seats=2)
    for v in villagers[:2]:
        start_process(session, v, "WARM")
    with pytest.raises(ValueError, match="free seat"):
        start_process(session, villagers[2], "WARM")
    complete_processes(session, tick_number=2)   # the two seats free
    start_process(session, villagers[2], "WARM")


def test_stoking_banks_fuel_and_the_cap_refuses(session):
    """A stoke credits fuel to the bound facility; a fully banked fire
    refuses wood BEFORE drawing it (banking is capped, like every input)."""
    villager, _ground = _commons_world(session, fuel=Decimal("4"),
                                       cap=Decimal("6"))
    process = start_process(session, villager, "STOKE")
    assert process.status == ProcessStatus.COMPLETED   # instant: duration 0
    fire = process.parcel.facilities[0]
    assert fire.fuel == Decimal("6")                   # 4 + 2, exactly at the cap
    with pytest.raises(ValueError, match="fully banked"):
        start_process(session, villager, "STOKE")
    assert get_holding(session, villager.id, "WOOD").quantity == Decimal("4")


def test_lit_gate_refuses_a_dark_fire(session):
    """requires_facility_lit: warming (or cooking) wants a BURNING fire;
    dark, the refusal names the fix (stoke it), and a stoke relights it."""
    villager, _ground = _commons_world(session, fuel=Decimal("1"))
    from econengine.parcels import burn_facility_fuel
    burn_facility_fuel(session, tick_number=1)          # fuel 1 -> 0: dark
    with pytest.raises(ValueError, match="dark.*stoke"):
        start_process(session, villager, "WARM")
    start_process(session, villager, "STOKE")          # +2: lit again
    start_process(session, villager, "WARM")


def test_burn_pass_empties_the_bank_and_emits_dark(session):
    """The tick pass burns one fuel an hour whether anyone sits, floors at
    zero, and the runout is a facility_dark event -- the beacon going out."""
    _v, ground = _commons_world(session, fuel=Decimal("2"))
    from econengine.parcels import burn_facility_fuel
    events = burn_facility_fuel(session, tick_number=1)
    assert events == [] and ground.facilities[0].fuel == Decimal("1")
    events = burn_facility_fuel(session, tick_number=2)
    assert [e["type"] for e in events] == ["facility_dark"]
    assert events[0]["facility_type"] == "FIRE"
    assert ground.facilities[0].fuel == Decimal("0")
    assert burn_facility_fuel(session, tick_number=3) == []  # stays dark, no noise


def test_stoking_a_dark_fire_emits_the_beacon(session):
    """Relighting is news: fuel crossing 0 -> positive is a facility_lit
    event (visible from far off); topping a lit fire is quiet telemetry."""
    villager, _ground = _commons_world(session, fuel=Decimal("0"))
    process = start_process(session, villager, "STOKE")
    # duration-0 stokes complete inline; the beacon rides only the
    # tick-completion path, so read the state and re-run the event through
    # parcels.credit_fuel's contract: lit once, fuel-facts after
    from econengine.parcels import credit_fuel
    fire = process.parcel.facilities[0]
    assert fire.fuel == Decimal("2")
    event = credit_fuel(session, fire, Decimal("2"), entity_id=villager.id)
    assert event["type"] == "facility_fuel"      # already lit: not news
    fire.fuel = Decimal("0")
    event = credit_fuel(session, fire, Decimal("2"), entity_id=villager.id)
    assert event["type"] == "facility_lit"       # dark -> lit: the beacon


def test_built_facility_carries_birth_params(session):
    """builds_facility_config: a recipe erects its facility with the
    commons knobs -- the fire MAKE_FIRE builds burns like the standing
    one (four seats, six-hour bank, one an hour, born lit)."""
    from econengine.parcels import create_parcel, facility_capacity
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    adjust_holding(session, alice, "LABOR", Decimal("1"))
    adjust_holding(session, alice, "WOOD", Decimal("2"))
    camp = create_parcel(session, "CAMP", owner=alice)
    create_recipe(session, "MAKE_FIRE", inputs={"LABOR": Decimal("1"),
                  "WOOD": Decimal("2")}, outputs={}, duration_ticks=1,
                  builds_facility="FIRE", builds_facility_access="PLACE",
                  builds_facility_config={"capacity": 4, "fuel": Decimal("2"),
                                          "fuel_capacity": Decimal("6"),
                                          "fuel_burn_per_tick": Decimal("1")})
    process = start_process(session, alice, "MAKE_FIRE", camp.id)
    complete_processes(session, tick_number=process.completes_tick)
    fire = camp.facilities[0]
    assert (fire.access, fire.capacity) == ("PLACE", 4)
    assert fire.fuel == Decimal("2") and fire.fuel_capacity == Decimal("6")
    assert fire.fuel_burn_per_tick == Decimal("1")
    assert facility_capacity(session, camp.id, "FIRE") == 4


def test_holding_cap_clips_positive_credits(session):
    """max_holding: positive credits clip at the cap (never an error),
    debits are untouched, and a cap below the auto-issue target is a loud
    content bug at create time."""
    from econengine.goods import create_good, get_good
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    create_good(session, "WARMTH", max_holding=Decimal("6"))
    adjust_holding(session, alice, "WARMTH", Decimal("4"))
    adjust_holding(session, alice, "WARMTH", Decimal("4"))   # +4 -> clip at 6
    assert get_holding(session, alice.id, "WARMTH").quantity == Decimal("6")
    adjust_holding(session, alice, "WARMTH", Decimal("-6"))  # debits untouched
    assert get_holding(session, alice.id, "WARMTH").quantity == Decimal("0")
    with pytest.raises(ValueError, match="fights the cap"):
        create_good(session, "RATION", auto_issue_quantity=Decimal("2"),
                    max_holding=Decimal("1"))
