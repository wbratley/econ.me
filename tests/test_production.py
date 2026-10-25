import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from econengine import rng
from econengine.markets import InsufficientHoldingsError, adjust_holding, get_holding
from econengine.models import (
    Base, EntityType, Process, ProcessStatus, Transaction, TransactionType,
)
from econengine.production import (
    cancel_process,
    complete_processes,
    create_recipe,
    credited_outputs,
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
        "effort": "1.0000",
        "productivity": "1.0000",
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


# --- recipe currency_cost (run 49: the merchant's wholesale channel) --------

def test_vetoed_mint_rolls_back_whole_and_survives_the_tick(session):
    """A votable validator can refuse a completion mint; the veto must
    not escape complete_processes (run_tick calls it bare — the same due
    process would re-veto on every tick, a deterministic permanent halt:
    nothing can run to vote a fix). The completion rolls back whole, the
    process stays RUNNING (it retries the tick the constitution allows
    it), and one process_vetoed event lands per refused tick."""
    from econengine.models import Script, ScriptType
    from econengine.services import create_account, deposit

    digger, _ = _digger(session)
    # someone — anyone — banks COIN: that is what makes it a currency
    rich = create_entity(session, "Rich", EntityType.INDIVIDUAL)
    rich_acc = create_account(session, rich, "COIN")
    deposit(session, rich_acc, Decimal("10"), "seat stake")
    session.add(Script(name="mint cap", script_type=ScriptType.VALIDATOR,
                       source='return {allow=false, reason="mint cap"}'))
    session.flush()

    process = start_process(session, digger, "DIG")
    events = complete_processes(session, tick_number=2)

    vetoed = [e for e in events if e["type"] == "process_vetoed"]
    assert len(vetoed) == 1
    assert vetoed[0]["recipe"] == "DIG"
    assert vetoed[0]["entity_id"] == digger.id
    assert vetoed[0]["process_id"] == process.id
    assert "mint cap" in vetoed[0]["reason"]

    # rolled back whole: no completion event, no mint, no ledger row
    assert all(e["type"] != "process_completed" for e in events)
    assert process.status == ProcessStatus.RUNNING
    acc = next((a for a in digger.accounts if a.currency == "COIN"), None)
    assert acc is None or acc.balance == Decimal("0")
    assert session.query(Transaction).filter_by(reference="mint DIG").count() == 0

    # the tick AFTER is refused too — no escape, no halt, still honest
    events = complete_processes(session, tick_number=3)
    assert len([e for e in events if e["type"] == "process_vetoed"]) == 1
    assert process.status == ProcessStatus.RUNNING


def test_currency_cost_debits_the_account_at_start(session):
    """A recipe that COSTS coin draws it from the crafter's COIN account
    the moment it starts, riding the ledger like every other debit --
    the wholesale channel pays for its stock, coin first."""
    from econengine.services import create_account

    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    # someone banks COIN -- that is what makes it a currency
    bank = create_entity(session, "Bank", EntityType.INDIVIDUAL)
    create_account(session, bank, "COIN")
    acc = create_account(session, buyer, "COIN", initial_balance=Decimal("10"))
    adjust_holding(session, buyer, "LABOR", Decimal("1"))
    create_recipe(
        session, "HAUL", inputs={"LABOR": Decimal("1")},
        outputs={"WOOD": Decimal("2")}, duration_ticks=1,
        currency_cost=Decimal("5.40"),
    )

    proc = start_process(session, buyer, "HAUL")
    assert proc.status == ProcessStatus.RUNNING
    assert acc.balance == Decimal("4.60")
    tx = session.query(Transaction).filter_by(account_id=acc.id).one()
    assert tx.tx_type == TransactionType.DEBIT
    assert tx.reference == "cost HAUL"
    complete_processes(session, tick_number=2)
    assert get_holding(session, buyer.id, "WOOD").quantity == Decimal("2")


def test_currency_cost_refusal_is_honest_and_rolls_back(session):
    """Short coin refuses before any goods burn: the balance is named,
    the inputs stay held, and the hills do not extend credit."""
    from econengine.services import create_account

    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    bank = create_entity(session, "Bank", EntityType.INDIVIDUAL)
    create_account(session, bank, "COIN")
    acc = create_account(session, buyer, "COIN", initial_balance=Decimal("2"))
    adjust_holding(session, buyer, "LABOR", Decimal("1"))
    create_recipe(
        session, "HAUL", inputs={"LABOR": Decimal("1")},
        outputs={"WOOD": Decimal("2")}, duration_ticks=1,
        currency_cost=Decimal("5.40"),
    )

    with pytest.raises(InsufficientHoldingsError, match="has 2 COIN"):
        start_process(session, buyer, "HAUL")
    assert acc.balance == Decimal("2")
    assert get_holding(session, buyer.id, "LABOR").quantity == Decimal("1")
    assert session.query(Process).count() == 0


def test_currency_cost_without_account_names_the_zero_purse(session):
    """No COIN account at all is a purse of zero -- named, not a
    different error class."""
    from econengine.services import create_account

    buyer = create_entity(session, "Buyer", EntityType.INDIVIDUAL)
    bank = create_entity(session, "Bank", EntityType.INDIVIDUAL)
    create_account(session, bank, "COIN")
    create_recipe(
        session, "HAUL", inputs={}, outputs={"WOOD": Decimal("2")},
        duration_ticks=1, currency_cost=Decimal("1"),
    )
    with pytest.raises(InsufficientHoldingsError, match="has 0 COIN"):
        start_process(session, buyer, "HAUL")


def test_currency_cost_must_be_positive(session):
    with pytest.raises(ValueError, match="currency_cost"):
        create_recipe(
            session, "DUD", inputs={}, outputs={"WOOD": Decimal("1")},
            duration_ticks=0, currency_cost=Decimal("0"),
        )


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


# --- builder auto-bind (run 48: five MAKE_PEN "must be bound to a parcel
# you control (pass its parcel_id)" refusals -- the seat stood at its
# hearth with the wood in hand and starved holding the flock) ---

def _builder_world(session):
    from econengine.parcels import create_parcel
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    adjust_holding(session, alice, "TIMBER", Decimal("10"))
    camp = create_parcel(session, "CAMP", owner=alice)
    create_recipe(session, "BUILD_SHED", inputs={"TIMBER": Decimal("1")},
                  outputs={}, duration_ticks=1, builds_facility="SHED")
    return alice, camp


def test_builder_recipe_auto_binds_to_owned_parcel(session):
    """A builds_facility recipe started without parcel_id rises on the
    builder's own ground -- the engine knows where the camp is; a
    parcel_id the seat cannot observe is bookkeeping dressed as
    strategy (the run-15 call, made again for builders)."""
    alice, camp = _builder_world(session)
    process = start_process(session, alice, "BUILD_SHED")  # no parcel_id
    assert process.parcel_id == camp.id
    events = complete_processes(session, tick_number=2)
    from econengine.parcels import facility_capacity
    assert facility_capacity(session, camp.id, "SHED") >= 1
    event = next(e for e in events if e["type"] == "process_completed")
    assert event["facility"] == "SHED" and event["parcel_id"] == camp.id


def test_builder_recipe_prefers_parcel_at_current_place(session):
    """Two owned parcels: the builder binds the one where it stands
    (build where you stand), not merely the first by id."""
    from econengine import places
    from econengine.parcels import create_parcel
    alice, _camp = _builder_world(session)
    homestead = places.create_place(session, "HOME", kind="VILLAGE",
                                    name="Homestead")
    near = create_parcel(session, "NEAR", owner=alice)
    near.place_id = homestead.id
    alice.location_place_id = homestead.id
    process = start_process(session, alice, "BUILD_SHED")
    assert process.parcel_id == near.id


def test_builder_recipe_landless_names_the_world_fact(session):
    """No owned ground: the refusal says what is missing -- land, not
    call syntax."""
    alice, _camp = _builder_world(session)
    drifter = create_entity(session, "Drifter", EntityType.INDIVIDUAL)
    adjust_holding(session, drifter, "TIMBER", Decimal("2"))
    with pytest.raises(ValueError, match="control no parcel to build on"):
        start_process(session, drifter, "BUILD_SHED")
    assert get_holding(session, drifter.id, "TIMBER").quantity == Decimal("2")


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


def test_full_banked_facilities_drop_the_stoke_loudly(session):
    """F6: the stoke loop skips full facilities and never clips
    (credit_fuel RAISES on overflow; the loop pre-checks room). A stoke
    whose facilities fill between start and completion used to drop the
    fuel output SILENTLY -- wood burned, nothing banked, nothing said.
    The drop is news: one stoke_dropped event rides the completion."""
    from econengine.parcels import credit_fuel
    villager, ground = _commons_world(session, fuel=Decimal("2"), cap=Decimal("4"))
    create_recipe(session, "SLOW_STOKE", inputs={"WOOD": Decimal("1")},
                  outputs={}, duration_ticks=1, requires_facility="FIRE",
                  facility_fuel_output=Decimal("2"))
    process = start_process(session, villager, "SLOW_STOKE")  # 2 + 2 <= 4: ok
    # the race: a neighbour banks the fire to the cap between start and
    # completion (reservations are queries, not escrows)
    credit_fuel(session, ground.facilities[0], Decimal("2"), entity_id=villager.id)
    events = complete_processes(session, tick_number=process.completes_tick)
    assert [e["type"] for e in events] == ["process_completed", "stoke_dropped"]
    dropped = events[1]
    assert dropped["parcel_id"] == ground.id
    assert dropped["quantity"] == "2.0000"
    assert ground.facilities[0].fuel == Decimal("4")  # nothing spilled
    assert get_holding(session, villager.id, "WOOD").quantity == Decimal("4")


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


# --- scales_with (P5, pastoral capital) -------------------------------------

@pytest.fixture
def herd(session):
    """A henkeeper with a flock and a scaled laying recipe: 1 EGG per
    HEN held at completion, capped at 3 -- the pastoral shape."""
    from econengine.goods import create_good
    create_good(session, "HEN")
    create_good(session, "EGG", decay_per_tick=Decimal("0.05"))
    keeper = create_entity(session, "Keeper", EntityType.INDIVIDUAL)
    adjust_holding(session, keeper, "HEN", Decimal("4"))
    recipe = create_recipe(
        session, "LAY", inputs={"EGG_LABOR": Decimal("1")},
        outputs={"EGG": Decimal("1")}, duration_ticks=1,
        good_requirements={"HEN": Decimal("1")},
        scales_with={"HEN": 3},
    )
    # the labor input needs the good to exist and be held
    from econengine.goods import create_good as _cg
    _cg(session, "EGG_LABOR")
    adjust_holding(session, keeper, "EGG_LABOR", Decimal("5"))
    return session, keeper, recipe


def test_scales_with_validation_rejects_drift(session):
    # exactly one symbol
    with pytest.raises(ValueError, match="exactly one symbol"):
        create_recipe(session, "X1", inputs={}, outputs={"Y": Decimal("1")},
                      duration_ticks=1, good_requirements={"A": Decimal("1")},
                      scales_with={"A": 2, "B": 2})
    # cap >= 1
    with pytest.raises(ValueError, match="cap"):
        create_recipe(session, "X2", inputs={}, outputs={"Y": Decimal("1")},
                      duration_ticks=1, good_requirements={"A": Decimal("1")},
                      scales_with={"A": 0})
    # the symbol must also be a good requirement (refuse-at-zero comes
    # from that gate, not a second mechanism)
    with pytest.raises(ValueError, match="good_requirement"):
        create_recipe(session, "X3", inputs={}, outputs={"Y": Decimal("1")},
                      duration_ticks=1, scales_with={"A": 2})


def test_scaled_output_credits_one_per_hen_held(herd):
    session, keeper, recipe = herd
    process = start_process(session, keeper, "LAY")
    complete_processes(session, tick_number=2)
    # 4 held, cap 3: the flock serves three eggs
    assert get_holding(session, keeper.id, "EGG").quantity == Decimal("3")
    assert process.scale_factor == Decimal("3")
    assert credited_outputs(process) == {"EGG": "3.0000"}


def test_scaled_output_below_the_cap_is_the_flock(herd):
    session, keeper, recipe = herd
    adjust_holding(session, keeper, "HEN", Decimal("-3"))  # one hen left
    start_process(session, keeper, "LAY")
    complete_processes(session, tick_number=2)
    assert get_holding(session, keeper.id, "EGG").quantity == Decimal("1")


def test_scaled_recipe_refuses_a_sold_out_flock(herd):
    session, keeper, recipe = herd
    adjust_holding(session, keeper, "HEN", Decimal("-4"))
    with pytest.raises(InsufficientHoldingsError, match="HEN"):
        start_process(session, keeper, "LAY")


def test_selling_the_herd_mid_process_shrinks_the_harvest(herd):
    """The factor reads holdings at COMPLETION, not a start snapshot:
    hens sold while the harvest runs are gone when it lands -- the
    honest pastoral economy (no phantom eggs from a dispersed flock)."""
    session, keeper, recipe = herd
    process = start_process(session, keeper, "LAY")
    adjust_holding(session, keeper, "HEN", Decimal("-3"))  # sold 3 of 4
    complete_processes(session, tick_number=2)
    # factor 1 is the trivial default: stored as NULL, read as 1
    assert process.scale_factor is None
    assert get_holding(session, keeper.id, "EGG").quantity == Decimal("1")
    assert credited_outputs(process) == {"EGG": "1.0000"}


def test_unscaled_recipes_stamp_no_factor(herd):
    """Unscaled completions leave scale_factor NULL and credit the row
    verbatim -- NULL reads as 1 everywhere (credited_outputs, events)."""
    session, keeper, recipe = herd
    start_process(session, keeper, "LAY")
    complete_processes(session, tick_number=2)
    plain = create_recipe(
        session, "TAKE", inputs={}, outputs={"EGG": Decimal("1")},
        duration_ticks=0)
    adjust_holding(session, keeper, "EGG", Decimal("-3"))  # empty the basket
    process = start_process(session, keeper, "TAKE")
    assert process.scale_factor is None
    assert credited_outputs(process) == {"EGG": "1.0000"}
    assert get_holding(session, keeper.id, "EGG").quantity == Decimal("1")


# --- LABOR effort (docs/productivity.md §1): the ration, not the gate ---


@pytest.fixture
def gatherer(session):
    """A gatherer with the full daylight ration: 1 LABOR held, and a
    1-LABOR gather that yields 2 BERRIES (duration 0)."""
    from econengine.goods import create_good
    create_good(session, "LABOR")
    create_good(session, "BERRIES")
    g = create_entity(session, "Gatherer", EntityType.INDIVIDUAL)
    adjust_holding(session, g, "LABOR", Decimal("1"))
    recipe = create_recipe(
        session, "gather", inputs={"LABOR": Decimal("1")},
        outputs={"BERRIES": Decimal("2")}, duration_ticks=0,
        name="Gather")
    return session, g, recipe


def test_full_ration_is_the_status_quo(gatherer):
    """1.0 held against a 1.0 input: full effort is stored as NULL
    (reads as 1), the hour is paid, and the berries land verbatim."""
    session, g, recipe = gatherer
    process = start_process(session, g, "gather")
    assert process.effort_factor is None
    assert get_holding(session, g.id, "LABOR").quantity == Decimal("0")
    assert get_holding(session, g.id, "BERRIES").quantity == Decimal("2")
    assert credited_outputs(process) == {"BERRIES": "2.0000"}


def test_throttled_ration_scales_the_work_not_the_gate(gatherer):
    """The run-58 shape: SQUALOR issues the ration at 0.85. The gather
    still STARTS (no boolean gate), pays exactly what is held (no
    overdraft), and the berries land at the fraction worked."""
    session, g, recipe = gatherer
    adjust_holding(session, g, "LABOR", Decimal("-0.15"))  # ration 0.85
    process = start_process(session, g, "gather")
    assert process.effort_factor == Decimal("0.8500")
    assert get_holding(session, g.id, "LABOR").quantity == Decimal("0")
    assert get_holding(session, g.id, "BERRIES").quantity == Decimal("1.7")
    assert credited_outputs(process) == {"BERRIES": "1.7000"}


def test_exhaustion_is_the_only_refusal(gatherer):
    """Zero hours never start anything; below the floor (a quarter of
    the declared input) neither; at the floor the work starts at the
    floor's effort."""
    session, g, recipe = gatherer
    adjust_holding(session, g, "LABOR", Decimal("-1"))  # ration 0
    with pytest.raises(InsufficientHoldingsError, match="exhausted"):
        start_process(session, g, "gather")
    adjust_holding(session, g, "LABOR", Decimal("0.2"))  # 0.2 < 0.25 floor
    with pytest.raises(InsufficientHoldingsError, match="exhausted"):
        start_process(session, g, "gather")
    adjust_holding(session, g, "LABOR", Decimal("0.05"))  # 0.25 = the floor
    process = start_process(session, g, "gather")
    assert process.effort_factor == Decimal("0.2500")
    assert get_holding(session, g.id, "BERRIES").quantity == Decimal("0.5")


def test_materials_stay_hard_when_the_ration_is_soft(session):
    """Effort applies to LABOR only: a short material input still
    refuses, and the refusal does not burn the (soft) ration."""
    from econengine.goods import create_good
    create_good(session, "LABOR")
    create_good(session, "FLOUR")
    create_good(session, "BREAD")
    b = create_entity(session, "Baker", EntityType.INDIVIDUAL)
    adjust_holding(session, b, "LABOR", Decimal("0.85"))
    adjust_holding(session, b, "FLOUR", Decimal("1"))
    create_recipe(
        session, "bake", inputs={"LABOR": Decimal("1"), "FLOUR": Decimal("2")},
        outputs={"BREAD": Decimal("3")}, duration_ticks=0, name="Bake")
    with pytest.raises(InsufficientHoldingsError, match="FLOUR"):
        start_process(session, b, "bake")
    assert get_holding(session, b.id, "LABOR").quantity == Decimal("0.85")


def test_effort_persists_to_the_completion(gatherer):
    """The factor is stamped at start and applied at completion even
    ticks later: the duration-1 shape, and the completion event carries
    the effort fact."""
    session, g, recipe = gatherer
    create_recipe(
        session, "deep_gather", inputs={"LABOR": Decimal("1")},
        outputs={"BERRIES": Decimal("4")}, duration_ticks=1,
        name="Deep gather")
    adjust_holding(session, g, "LABOR", Decimal("-0.15"))  # ration 0.85
    process = start_process(session, g, "deep_gather")
    assert process.status == ProcessStatus.RUNNING
    events = complete_processes(session, tick_number=2)
    assert process.effort_factor == Decimal("0.8500")
    assert get_holding(session, g.id, "BERRIES").quantity == Decimal("3.4")
    assert credited_outputs(process) == {"BERRIES": "3.4000"}
    assert events[0]["effort"] == "0.8500"


def test_full_effort_completion_event_reads_one(gatherer):
    session, g, recipe = gatherer
    create_recipe(
        session, "walk_gather", inputs={"LABOR": Decimal("1")},
        outputs={"BERRIES": Decimal("2")}, duration_ticks=1,
        name="Walk gather")
    process = start_process(session, g, "walk_gather")
    events = complete_processes(session, tick_number=2)
    assert process.effort_factor is None
    assert events[0]["effort"] == "1.0000"


def test_the_herd_and_the_ration_price_together(session):
    """P5 × effort: 1 EGG per HEN held (cap 3) at the fraction of the
    declared hour paid — the flock and the fatigue both price the
    basket, and both facts stay on the row."""
    from econengine.goods import create_good
    create_good(session, "HEN")
    create_good(session, "EGG")
    create_good(session, "LABOR")
    k = create_entity(session, "Keeper", EntityType.INDIVIDUAL)
    adjust_holding(session, k, "HEN", Decimal("4"))
    adjust_holding(session, k, "LABOR", Decimal("0.85"))
    create_recipe(
        session, "lay", inputs={"LABOR": Decimal("1")},
        outputs={"EGG": Decimal("1")}, duration_ticks=1,
        good_requirements={"HEN": Decimal("1")},
        scales_with={"HEN": 3}, name="Lay")
    process = start_process(session, k, "lay")
    complete_processes(session, tick_number=2)
    assert process.scale_factor == Decimal("3")
    assert process.effort_factor == Decimal("0.8500")
    assert get_holding(session, k.id, "EGG").quantity == Decimal("2.55")
    assert credited_outputs(process) == {"EGG": "2.5500"}


def test_effort_scales_banked_outputs_too(gatherer):
    """Production-minted currency (the stone age finds shiny stones
    while gathering) lands at the effort fraction: the money path
    prices the same factor as the goods path."""
    session, g, recipe = gatherer
    from econengine import services
    services.create_account(session, g, "COIN")
    create_recipe(
        session, "mine", inputs={"LABOR": Decimal("1")},
        outputs={"COIN": Decimal("2")}, duration_ticks=0, name="Mine")
    adjust_holding(session, g, "LABOR", Decimal("-0.15"))
    start_process(session, g, "mine")
    assert g.accounts[0].balance == Decimal("1.7")


# --- Productivity (docs/productivity.md §2): conditions as modifiers ---


def test_productivity_factor_is_for_condition_goods(session):
    """A held berry must not tax work: the factor is refused on a
    non-condition good, accepted on a condition good (either condition
    property qualifies), and bounded to [0, 1]."""
    from econengine.goods import create_good
    with pytest.raises(ValueError, match="condition goods only"):
        create_good(session, "BERRIES", productivity_factor=Decimal("0.9"))
    f = create_good(
        session, "FATIGUE", incapacitates_at=Decimal("7.5"),
        productivity_factor=Decimal("0.9"))
    assert f.productivity_factor == Decimal("0.9000")
    with pytest.raises(ValueError, match="between 0 and 1"):
        create_good(
            session, "WARM", modifies_pattern="LABOR",
            modifies_factor=Decimal("0.5"), productivity_factor=Decimal("1.5"))


def test_effect_kind_validation(session):
    """The axis vocabulary is none/yield/success; 'success' needs a failure
    branch to shift into (the last, highest position); 'none' normalizes
    to NULL."""
    with pytest.raises(ValueError, match="must be none, yield, or success"):
        create_recipe(session, "X", inputs={}, outputs={"Y": Decimal("1")},
                      duration_ticks=0, effect_kind="meh")
    with pytest.raises(ValueError, match="at least two branches"):
        create_recipe(
            session, "X2", inputs={}, outputs={}, duration_ticks=0,
            branches=[{"weight": Decimal("1"), "outputs": {"B": Decimal("1")}}],
            effect_kind="success")
    r = create_recipe(
        session, "X3", inputs={}, outputs={}, duration_ticks=0,
        branches=[{"weight": Decimal("1"), "outputs": {"B": Decimal("1")}},
                  {"weight": Decimal("1"), "outputs": {}}],
        effect_kind="success")
    assert r.effect_kind == "success"
    r2 = create_recipe(
        session, "X4", inputs={}, outputs={"Y": Decimal("1")},
        duration_ticks=0, effect_kind="none")
    assert r2.effect_kind is None


@pytest.fixture
def worker(session):
    """A gatherer and a yield-axis gather (1 LABOR -> 2 BERRIES, duration 0),
    plus the productivity-taxing conditions the tests may hand them."""
    from econengine.goods import create_good
    create_good(session, "LABOR")
    create_good(session, "BERRIES")
    create_good(session, "FATIGUE", incapacitates_at=Decimal("7.5"),
                productivity_factor=Decimal("0.9"))
    create_good(session, "DISEASE", incapacitates_at=Decimal("2.5"),
                productivity_factor=Decimal("0.8"))
    w = create_entity(session, "Gatherer", EntityType.INDIVIDUAL)
    adjust_holding(session, w, "LABOR", Decimal("1"))
    recipe = create_recipe(
        session, "gather", inputs={"LABOR": Decimal("1")},
        outputs={"BERRIES": Decimal("2")}, duration_ticks=0,
        effect_kind="yield", name="Gather")
    return session, w, recipe


def test_no_productivity_condition_is_the_identity(worker):
    """No productivity-taxing condition held: P = 1, stamped NULL (reads
    1), the yield recipe credits its declared outputs verbatim."""
    session, w, recipe = worker
    process = start_process(session, w, "gather")
    assert process.productivity is None
    assert get_holding(session, w.id, "BERRIES").quantity == Decimal("2")
    assert credited_outputs(process) == {"BERRIES": "2.0000"}


def test_held_conditions_multiply_to_the_tax(worker):
    """P is the product of the held conditions' factors (symbol-ascending):
    FATIGUE 0.9 and DISEASE 0.8 held together read 0.72, and the yield axis
    prices the credited berries at that fraction."""
    session, w, recipe = worker
    adjust_holding(session, w, "FATIGUE", Decimal("1"))
    adjust_holding(session, w, "DISEASE", Decimal("1"))
    process = start_process(session, w, "gather")
    assert process.productivity == Decimal("0.7200")
    assert get_holding(session, w.id, "BERRIES").quantity == Decimal("1.44")
    assert credited_outputs(process) == {"BERRIES": "1.4400"}


def test_yield_axis_prices_the_completion_and_the_event(worker):
    """Duration-1 shape: the applied P is stamped on the row and rides the
    completion event next to the effort fact, and the credited berries are
    the declared output priced by it."""
    session, w, recipe = worker
    create_recipe(
        session, "deep_gather", inputs={"LABOR": Decimal("1")},
        outputs={"BERRIES": Decimal("4")}, duration_ticks=1,
        effect_kind="yield", name="Deep gather")
    adjust_holding(session, w, "FATIGUE", Decimal("1"))
    adjust_holding(session, w, "DISEASE", Decimal("1"))
    process = start_process(session, w, "deep_gather")
    events = complete_processes(session, tick_number=2)
    assert process.productivity == Decimal("0.7200")
    assert get_holding(session, w.id, "BERRIES").quantity == Decimal("2.88")
    assert credited_outputs(process) == {"BERRIES": "2.8800"}
    assert events[0]["effort"] == "1.0000"
    assert events[0]["productivity"] == "0.7200"
    assert events[0]["outputs"] == {"BERRIES": "2.8800"}


def test_yield_only_shrinks_and_never_mints(worker):
    """Conservation: the yield axis can only shrink the credited output
    (P <= 1). Nothing is minted that was not declared; the uncredited
    fraction is unproduced, not destroyed-and-replaced."""
    session, w, recipe = worker
    declared = Decimal("2")
    adjust_holding(session, w, "FATIGUE", Decimal("1"))
    adjust_holding(session, w, "DISEASE", Decimal("1"))
    process = start_process(session, w, "gather")
    credited = credited_outputs(process)["BERRIES"]
    assert Decimal(credited) <= declared
    assert Decimal(credited) == (declared * Decimal("0.72")).quantize(
        Decimal("0.0001"))


def test_all_three_factors_read_one_when_absent(session):
    """NULL-reads-1 across the board: no herd (no scales_with), full effort
    (no shortfall), no condition — the credit factor is 1 and the event
    reads every fact as 1.0000."""
    from econengine.goods import create_good
    create_good(session, "LABOR")
    create_good(session, "BERRIES")
    w = create_entity(session, "Gatherer", EntityType.INDIVIDUAL)
    adjust_holding(session, w, "LABOR", Decimal("1"))
    create_recipe(
        session, "gather", inputs={"LABOR": Decimal("1")},
        outputs={"BERRIES": Decimal("2")}, duration_ticks=0, name="Gather")
    process = start_process(session, w, "gather")
    assert process.scale_factor is None
    assert process.effort_factor is None
    assert process.productivity is None
    assert get_holding(session, w.id, "BERRIES").quantity == Decimal("2")
    assert credited_outputs(process) == {"BERRIES": "2.0000"}


def test_the_herd_the_ration_and_the_conditions_price_together(session):
    """All three factors on one row: 1 EGG per HEN held (cap 3), at the
    fraction of the declared hour paid, at the fraction the held conditions
    convert — the flock, the fatigue, and the fever all price the basket,
    and all three facts stay stamped."""
    from econengine.goods import create_good
    create_good(session, "HEN")
    create_good(session, "EGG")
    create_good(session, "LABOR")
    create_good(session, "FATIGUE", incapacitates_at=Decimal("7.5"),
                productivity_factor=Decimal("0.9"))
    k = create_entity(session, "Keeper", EntityType.INDIVIDUAL)
    adjust_holding(session, k, "HEN", Decimal("4"))
    adjust_holding(session, k, "LABOR", Decimal("0.85"))
    adjust_holding(session, k, "FATIGUE", Decimal("1"))
    create_recipe(
        session, "lay", inputs={"LABOR": Decimal("1")},
        outputs={"EGG": Decimal("1")}, duration_ticks=1,
        good_requirements={"HEN": Decimal("1")},
        scales_with={"HEN": 3}, effect_kind="yield", name="Lay")
    process = start_process(session, k, "lay")
    complete_processes(session, tick_number=2)
    # herd 3 (cap) x effort 0.85 x productivity 0.9 = 2.2950 eggs
    assert process.scale_factor == Decimal("3")
    assert process.effort_factor == Decimal("0.8500")
    assert process.productivity == Decimal("0.9000")
    assert get_holding(session, k.id, "EGG").quantity == Decimal("2.295")
    assert credited_outputs(process) == {"EGG": "2.2950"}


# --- the success axis: the roll and its seed are untouched ---


def test_shift_success_weights_preserves_the_total():
    """The success-axis transform: success branches (all but last) lose
    mass, the failure branch (last) receives it, the total is preserved,
    P = 1 is the identity, and P -> 0 sends all success mass to failure."""
    from econengine.production import _shift_success_weights
    declared = [Decimal("0.5"), Decimal("0.3"), Decimal("0.2")]
    total = sum(declared)
    assert _shift_success_weights(declared, Decimal("1")) == declared
    for P in (Decimal("0.9"), Decimal("0.8"), Decimal("0.5"), Decimal("0.1")):
        shifted = _shift_success_weights(declared, P)
        assert sum(shifted) == total
        assert shifted[0] == Decimal("0.5") * P
        assert shifted[1] == Decimal("0.3") * P
        assert shifted[2] == Decimal("0.2") + (Decimal("1") - P) * Decimal("0.8")
    near_zero = _shift_success_weights(declared, Decimal("0.0001"))
    assert sum(near_zero) == total
    assert near_zero[2] > Decimal("0.99")
    # a single-branch table has no failure branch to shift into: identity
    assert _shift_success_weights([Decimal("1")], Decimal("0.5")) == [Decimal("1")]


def test_success_axis_shifts_the_roll_toward_the_failure_branch(session):
    """The success axis end to end: the roll and its seed are untouched;
    only the branch weights move. P = 1 reproduces today's branch exactly;
    a held condition shifts the (different) roll toward the last (failure)
    branch on the shifted table, and the total is preserved so the roll
    still maps onto [0, total)."""
    from econengine.goods import create_good
    from econengine.production import _shift_success_weights
    create_good(session, "LABOR")
    create_good(session, "MEAT")
    create_good(session, "FATIGUE", incapacitates_at=Decimal("7.5"),
                productivity_factor=Decimal("0.5"))
    recipe = create_recipe(
        session, "fish", inputs={"LABOR": Decimal("1")}, outputs={},
        duration_ticks=0, effect_kind="success", name="Fish",
        branches=[
            {"weight": Decimal("0.45"), "outputs": {"MEAT": Decimal("2")},
             "label": "small"},
            {"weight": Decimal("0.25"), "outputs": {"MEAT": Decimal("4")},
             "label": "big"},
            {"weight": Decimal("0.30"), "outputs": {}, "label": "nothing"},
        ])
    declared = [b.weight for b in recipe.branches]

    # P = 1 (no condition): the same table the roll has always used
    w1 = create_entity(session, "Fisher", EntityType.INDIVIDUAL)
    adjust_holding(session, w1, "LABOR", Decimal("1"))
    p1 = start_process(session, w1, "fish")
    roll1 = rng.outcome_roll(rng.GENESIS_HASH, p1.id)
    assert p1.productivity is None
    assert p1.outcome_branch == rng.weighted_index(roll1, declared)

    # P = 0.5 (FATIGUE held): the roll is recomputed fresh for this process,
    # and the engine picks the branch the SHIFTED table says — the failure
    # branch is the last position
    w2 = create_entity(session, "Fisher2", EntityType.INDIVIDUAL)
    adjust_holding(session, w2, "LABOR", Decimal("1"))
    adjust_holding(session, w2, "FATIGUE", Decimal("1"))
    p2 = start_process(session, w2, "fish")
    roll2 = rng.outcome_roll(rng.GENESIS_HASH, p2.id)
    shifted = _shift_success_weights(declared, Decimal("0.5"))
    assert p2.productivity == Decimal("0.5000")
    assert p2.outcome_branch == rng.weighted_index(roll2, shifted)
    # conservation: the failure branch credits only what it declares
    # (here, nothing) — a shifted miss is a miss, not a silent shrink
    if p2.outcome_branch == 2:
        held = get_holding(session, w2.id, "MEAT")
        assert held is None or held.quantity == Decimal("0")


def test_success_axis_failure_branch_credits_its_own_outputs(session):
    """A shifted failure branch is still an ordinary branch: it credits
    exactly what it declares (here, a partial catch), scaled like any
    other credited output — the failure is a real outcome, not an error."""
    from econengine.goods import create_good
    create_good(session, "LABOR")
    create_good(session, "MEAT")
    create_good(session, "FATIGUE", incapacitates_at=Decimal("7.5"),
                productivity_factor=Decimal("0.2"))
    # a success branch, a partial branch, and a partial-loss failure branch
    # last (the catch spoiled in the fever heat — not a total loss)
    recipe = create_recipe(
        session, "hunt", inputs={"LABOR": Decimal("1")}, outputs={},
        duration_ticks=0, effect_kind="success", name="Hunt",
        branches=[
            {"weight": Decimal("0.5"), "outputs": {"MEAT": Decimal("4")},
             "label": "big"},
            {"weight": Decimal("0.3"), "outputs": {"MEAT": Decimal("2")},
             "label": "a scrap"},
            {"weight": Decimal("0.2"), "outputs": {"MEAT": Decimal("1")},
             "label": "spoiled"},
        ])
    w = create_entity(session, "Hunter", EntityType.INDIVIDUAL)
    adjust_holding(session, w, "LABOR", Decimal("1"))
    adjust_holding(session, w, "FATIGUE", Decimal("1"))
    process = start_process(session, w, "hunt")
    # whichever branch the shifted roll lands on, it credits exactly what
    # that branch declares — a failure branch is an ordinary branch, and
    # the success axis never multiplies its credited outputs (only the
    # weights move); the herd and effort read 1 here
    branch = recipe.branches[process.outcome_branch]
    for output in branch.outputs:
        held = get_holding(session, w.id, output.symbol)
        assert held is not None and held.quantity == output.quantity
