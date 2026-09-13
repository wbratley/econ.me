"""Stone-age pack tests: focused feature coverage + the balance policies.

Focused tests exercise each mechanic (loot tables, tool requirements,
facility gates, EAT_RAW risk, the warmth ladder) in isolation. The
POLICY tests are the pack's balance contract, straight from the design
doctrine:

  * doing nothing kills you (conditions have teeth),
  * the starter script survives (hand-to-mouth works),
  * tools pay (bag/spear beat bare hands by a wide margin).

If a constants change breaks one of those three, the pack has drifted:
rebalance, don't relax the assert.

Direct engine-call policies act BETWEEN ticks, but LABOR auto-issues
DURING a tick (and halves at its end when unspent), so `_act` tops the
holding up to 1 first -- the between-tick stand-in for the mid-tick
window scripts act in.
"""

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econengine import markets, parcels, production, services
from econengine.models import (
    Base, Entity, EntityStatus, EntityType, Holding, Market, Order,
    OrderSide, OrderStatus, Script, ScriptType, Tick,
)
from econengine.tick import run_tick

from experiments.world import stone_age
from experiments.world.stone_age import (
    BERRY_BUFFER, COIN, FIRE_FUEL_BURN, FIRE_FUEL_CAP, FIRE_FUEL_START,
    FIRE_SEATS, POST_FOOD, SEAT_COIN, WARMTH_BUFFER, WARMTH_CAP,
    create_content, make_house,
)


@pytest.fixture
def session():
    # check_same_thread off: ctx.query.* callbacks run on the script
    # thread (lua_engine.run executes in a worker) -- the starter reads
    # world.lit_fires() every tick now (P1)
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _hold(session, entity_id, symbol):
    qty = session.execute(
        select(Holding.quantity)
        .where(Holding.entity_id == entity_id, Holding.symbol == symbol)
    ).scalar_one_or_none()
    return qty if qty is not None else Decimal("0")


def _post(session):
    return session.execute(
        select(Entity).where(Entity.name == "Trading Post")
    ).scalar_one()


def _post_script(session):
    return session.execute(
        select(Script).where(
            Script.entity_id == _post(session).id,
            Script.script_type == ScriptType.BEHAVIOUR,
            Script.is_active.is_(True),
        )
    ).scalar_one()


def _open_orders(session, entity_id, symbol=None, side=None):
    q = select(Order).join(Market, Order.market_id == Market.id).where(
        Order.entity_id == entity_id, Order.status == OrderStatus.OPEN)
    if symbol:
        q = q.where(Market.symbol == symbol)
    if side:
        q = q.where(Order.side == side)
    return session.execute(q).scalars().all()


def _run(session, ticks):
    for _ in range(ticks):
        run_tick(session)
        session.commit()


def _events(session, etype):
    out = []
    for tick in session.execute(select(Tick).order_by(Tick.number)).scalars():
        out.extend(e for e in (tick.events or []) if e.get("type") == etype)
    return out


def _set(session, entity, symbol, qty):
    """Set a holding to an exact quantity (adjust_holding adds; tests
    want known arithmetic)."""
    markets.adjust_holding(
        session, entity, symbol,
        Decimal(str(qty)) - _hold(session, entity.id, symbol))


def _seat(session, name="Worker"):
    """A stone-age INDIVIDUAL: buffers and a camp, no behaviour script."""
    return make_house(session, name)


def _biz(session, name):
    """A BUSINESS worker for pure-mechanics tests: needs are
    INDIVIDUAL-only, so it cannot starve or freeze mid-test, and `_act`
    tops its LABOR up the same way auto-issue would."""
    from econengine import services as _services
    entity = _services.create_entity(session, name, EntityType.BUSINESS)
    _services.create_account(session, entity, COIN, initial_balance=SEAT_COIN)
    return entity


def _camp(session, entity):
    return session.execute(
        select(parcels.Parcel).where(parcels.Parcel.owner_id == entity.id)
    ).scalar_one()


def _at(session, entity, key):
    """Stand somewhere (S4): work is where you are -- the map's gates
    bind recipes to places, markets to the post, bites to co-location.
    Genesis places seats at the hearth, wolves at the forest, the post
    at the post; tests walk their subjects where the subject works."""
    from econengine import places as places_mod
    return places_mod.move_entity(session, entity, key)


def _facilities(session, entity):
    camp = _camp(session, entity)
    return sorted(f.facility_type for f in
                  session.execute(select(parcels.Facility)
                                   .where(parcels.Facility.parcel_id == camp.id))
                  .scalars())


def _act(session, entity, code, parcel_id=None):
    """Try to start one process in the post-completion window (call AFTER
    run_tick: yesterday's processes have completed, this tick's LABOR is
    issued and undecayed), LABOR topped to 1. Returns False (and swallows)
    on input shortfall -- the policy just misses this tick, exactly like a
    script whose intent fails."""
    if _hold(session, entity.id, "LABOR") < 1:
        markets.adjust_holding(
            session, entity, "LABOR",
            Decimal("1") - _hold(session, entity.id, "LABOR"))
    try:
        production.start_process(session, entity, code, parcel_id)
        return True
    except markets.InsufficientHoldingsError:
        return False


def _act_day(session, entity, code, parcel_id=None):
    """_act for daylight-gated recipes (the clock, run 18): if the next
    tick would be dark, run idle ticks to dawn first. The night hours are
    not this test's subject -- the darkness refusal has its own."""
    from econengine import clock
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session)
        session.commit()
    return _act(session, entity, code, parcel_id)


# ===========================================================================
# FOCUSED FEATURE TESTS
# ===========================================================================

def test_content_and_coin_markets(session):
    create_content(session)
    rows = list(session.execute(select(markets.Market)).scalars())
    assert {m.symbol for m in rows} == {
        "LABOR", "BERRIES", "APPLES", "MEAT", "COOKED_MEAT", "JERKY",
        "EGGS", "CHICKEN", "WOOD", "YARN",
        "FLINT", "SPEAR", "AXE", "BAG", "BOW", "TRAP", "CLOTHES",
        "BED", "PELT", "WATERSKIN"}
    assert all(m.currency == COIN for m in rows)


def test_seat_endowment(session):
    """A seat: walking money (10 COIN -- the rest is found, not endowed),
    a day of berries, a night of warmth, a bare camp."""
    create_content(session)
    seat = _seat(session, "Seat")
    assert _hold(session, seat.id, "BERRIES") == BERRY_BUFFER
    assert _hold(session, seat.id, "WARMTH") == WARMTH_BUFFER
    assert seat.accounts[0].balance == SEAT_COIN
    assert _facilities(session, seat) == []


def test_gather_loot_table(session):
    """Every gather lands on the declared branch table -- one resource per
    roll, quantities exact."""
    create_content(session)
    _no_wolves(session)
    w = _biz(session, "Roller")   # no needs: nothing eats mid-test (run 19)
    _at(session, w, "THICKET")
    for _ in range(24):
        _act_day(session, w, "GATHER")
        run_tick(session); session.commit()
    done = [e for e in _events(session, "process_completed")
            if e["recipe"] == "GATHER"]
    assert len(done) >= 20
    assert {e.get("branch_label") for e in done} <= {
        "berries", "apples", "wood", "yarn", "flint"}
    for e in done:
        assert len(e["outputs"]) == 1  # one resource per roll


def test_bag_doubles_the_gather(session):
    """The advantage contract, part one: a BAG holder gathers far more loot
    over the same ticks (event-based totals, so rot cannot blur it)."""
    create_content(session)
    _no_wolves(session)
    bare, bagged = _biz(session, "Bare"), _biz(session, "Bagged")
    _at(session, bare, "THICKET")
    _at(session, bagged, "THICKET")
    # Two BAGs: one stays reserved by the running gather between ticks
    # (scripts act in-tick after completions and need only one; the
    # between-tick harness needs a spare).
    markets.adjust_holding(session, bagged, "BAG", Decimal("2"))
    for _ in range(60):
        _act_day(session, bare, "GATHER")
        _act_day(session, bagged, "GATHER_BAG")
        run_tick(session); session.commit()

    def units(entity):
        total = Decimal("0")
        for e in _events(session, "process_completed"):
            if e["entity_id"] == entity.id:
                for q in e["outputs"].values():
                    total += Decimal(q)
        return total
    assert units(bagged) > units(bare) * Decimal("1.5")


def test_tool_and_facility_gates(session):
    """HUNT_SPEAR needs a held SPEAR; WARM_BY_FIRE/COOK_MEAT/SMOKE_MEAT
    need a LIT FIRE (the commons fire stands at genesis, but not on the
    seat's own camp); REST needs a SHELTER; HUDDLE needs CLOTHES.
    MAKE_FIRE builds another FIRE from scratch -- public, born lit."""
    create_content(session)
    w = _seat(session)
    camp = _camp(session, w)
    with pytest.raises(Exception, match="SPEAR"):
        production.start_process(session, w, "HUNT_SPEAR")
    with pytest.raises(Exception, match="no free FIRE"):
        production.start_process(session, w, "WARM_BY_FIRE", camp.id)
    with pytest.raises(Exception, match="no free FIRE"):
        production.start_process(session, w, "COOK_MEAT", camp.id)
    with pytest.raises(Exception, match="no free FIRE"):
        production.start_process(session, w, "SMOKE_MEAT", camp.id)
    with pytest.raises(Exception, match="no free SHELTER"):
        production.start_process(session, w, "SLEEP_SHELTERED", camp.id)
    with pytest.raises(Exception, match="CLOTHES"):
        production.start_process(session, w, "HUDDLE")
    # The commons fire warms a house that owns no fire (auto-bind finds
    # the public fire-ground; no parcel id needed). Seated at tick 1's
    # start, the hour lands at tick 2's top: +6 capped at 6, minus that
    # tick's 3-draw and the 0.2 fade on what remains -> 2.4 exactly.
    markets.adjust_holding(session, w, "WARMTH", -WARMTH_BUFFER)
    assert _act(session, w, "WARM_BY_FIRE")
    _run(session, 2)
    assert _hold(session, w.id, "WARMTH") == Decimal("2.4")
    # MAKE_FIRE builds the house's own -- public on build, born burning
    # (2 fuel: the wood that made it), four seats, one fuel an hour.
    markets.adjust_holding(session, w, "WOOD", Decimal("2"))
    assert _act(session, w, "MAKE_FIRE", camp.id)
    _run(session, 2)
    assert _facilities(session, w) == ["FIRE"]
    fire = session.execute(
        select(parcels.Facility)
        .where(parcels.Facility.parcel_id == camp.id)
    ).scalar_one()
    assert (fire.access, fire.capacity) == ("PLACE", FIRE_SEATS)
    assert fire.fuel_burn_per_tick == FIRE_FUEL_BURN
    assert fire.fuel_capacity == FIRE_FUEL_CAP
    assert fire.fuel > 0


def test_axe_chops_certain_wood_and_fights_at_two(session):
    """Run 22's variable: the stone axe. A held tool like the spear --
    MAKE_AXE costs an afternoon, CHOP_WOOD turns an hour at the treeline
    into three certain logs (no loot table: the fire never wants again),
    and the weapons table prices it at +2 ATK, behind the spear's 3."""
    from econengine import combat
    create_content(session)
    _no_wolves(session)
    w = _biz(session, "Woodcutter")
    _at(session, w, "THICKET")   # the treeline is where the axe works
    # the gate: no axe, no chop (the world starts at hour 00: wait for
    # dawn first -- the daylight gate raises before the tool gate)
    from econengine import clock
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session)
        session.commit()
    with pytest.raises(Exception, match="AXE"):
        production.start_process(session, w, "CHOP_WOOD")
    # the craft: flint + wood + yarn + an afternoon
    for sym in ("FLINT", "WOOD", "YARN"):
        markets.adjust_holding(session, w, sym, Decimal("1"))
    assert _act_day(session, w, "MAKE_AXE")
    _run(session, 4)
    assert _hold(session, w.id, "AXE") == 1
    # the certain logs: deterministic, no branch -- the gather table's
    # wood is a 25% shot at 2; the axe pays 3, every time
    assert _act_day(session, w, "CHOP_WOOD")
    _run(session, 2)
    assert _hold(session, w.id, "WOOD") == Decimal("3")
    # the weapon: priced behind the spear, stacked when both are held
    assert combat.get_rules(session)["weapons"] == {
        "SPEAR": 3, "AXE": 2, "BOW": 3}


def test_cook_meat_converts(session):
    create_content(session)
    w = _seat(session)
    camp = _camp(session, w)
    parcels.add_facility(session, camp, "FIRE", fuel=Decimal("6"))
    markets.adjust_holding(session, w, "MEAT", Decimal("2"))
    assert _act(session, w, "COOK_MEAT", camp.id)
    _run(session, 2)
    assert _hold(session, w.id, "COOKED_MEAT") >= Decimal("1.5")
    assert _hold(session, w.id, "MEAT") == Decimal("0")


def test_smoke_meat_converts_slowly_and_keeps(session):
    """SMOKE_MEAT (run 17's pack delta): 2 MEAT + 1 WOOD + the fire, five
    slow ticks, -> 2 JERKY that NEVER decay. Preservation is a craft a
    house can own -- run 16's houses starved beside rotting larders and
    a post-only shelf they could not restock. Mechanics on a BUSINESS
    seat (no FOOD draw to eat the output); feeding on an INDIVIDUAL."""
    create_content(session)
    w = _biz(session, "Smoker")
    parcels.create_parcel(session, "LAND", name="Smoker's Camp", owner=w)
    camp = _camp(session, w)
    parcels.add_facility(session, camp, "FIRE", fuel=Decimal("6"))
    markets.adjust_holding(session, w, "MEAT", Decimal("2"))
    markets.adjust_holding(session, w, "WOOD", Decimal("1"))
    assert _act(session, w, "SMOKE_MEAT", camp.id)
    # inputs are spent when the process starts; the jerky lands only
    # after the long smoke -- nothing edible at tick 2
    assert _hold(session, w.id, "MEAT") == Decimal("0")
    assert _hold(session, w.id, "WOOD") == Decimal("0")
    _run(session, 2)
    assert _hold(session, w.id, "JERKY") == Decimal("0")
    _run(session, 4)
    assert _hold(session, w.id, "JERKY") == Decimal("2")
    # ...and unlike every other food, time does not eat it
    _run(session, 10)
    assert _hold(session, w.id, "JERKY") == Decimal("2")
    # conscious eating (run 19): a jerky larder feeds nobody by itself --
    # the FOOD need drinks only SATIETY. Feeding is a meal, not a draw.
    seat = _seat(session, "JerkyEater")
    markets.adjust_holding(session, seat, "BERRIES", -BERRY_BUFFER)
    markets.adjust_holding(session, seat, "JERKY", Decimal("5"))
    hunger0 = _hold(session, seat.id, "HUNGER")
    _run(session, 4)                       # four hungry hours beside a full larder
    assert _hold(session, seat.id, "JERKY") == Decimal("5")   # untouched
    assert _hold(session, seat.id, "HUNGER") > hunger0         # the engine did not chew
    assert _act(session, seat, "EAT_JERKY")                    # the meal is the decision
    assert _hold(session, seat.id, "JERKY") == Decimal("4")
    assert _hold(session, seat.id, "SATIETY") == Decimal("3.6")


def test_conscious_eating_makes_meals_decisions(session):
    """Run 19's variable: the FOOD need drinks only SATIETY, and only
    EAT recipes fill the stomach -- a larder is not a meal. The density
    ladder is real: berries thin (~3h fed), jerky dense (~6h)."""
    create_content(session)
    w = _seat(session, "Diner")
    markets.adjust_holding(session, w, "BERRIES", -BERRY_BUFFER)
    markets.adjust_holding(session, w, "JERKY", Decimal("3"))
    hunger0 = _hold(session, w.id, "HUNGER")
    _run(session, 4)                     # four hungry hours beside the larder
    assert _hold(session, w.id, "JERKY") == Decimal("3")   # untouched
    assert _hold(session, w.id, "SATIETY") == Decimal("0")
    assert _hold(session, w.id, "HUNGER") > hunger0        # nothing was auto-eaten
    # EAT_BERRIES: instant, labor-free, night-legal (hours 0-4 are dark)
    markets.adjust_holding(session, w, "BERRIES", Decimal("1.5"))
    assert _act(session, w, "EAT_BERRIES")
    assert _hold(session, w.id, "SATIETY") == Decimal("2")
    assert _hold(session, w.id, "BERRIES") == Decimal("0")
    _run(session, 1)
    assert _hold(session, w.id, "SATIETY") == Decimal("1.35")  # 0.5 need + 10% of the 1.5 left
    # the dense meal: one strip carries ~5½ hours
    assert _act(session, w, "EAT_JERKY")
    assert _hold(session, w.id, "SATIETY") == Decimal("4.95")   # 1.35 + 3.6
    assert _hold(session, w.id, "JERKY") == Decimal("2")


def test_the_clock_rations_labor_and_gates_the_dark(session):
    """The clock (run 18): LABOR auto-issues only in daylight (one
    labor-hour per daylight hour, none at night), and the dark refuses
    gathering -- with the window named in the error, not advice."""
    from econengine import clock
    create_content(session)
    w = _seat(session, "NightOwl")
    # tick 1 opens at hour 0 (midnight): the ration is NOT issued in dark
    run_tick(session); session.commit()
    assert clock.hour_of(1) == 0 and clock.is_night(1)
    assert _hold(session, w.id, "LABOR") == Decimal("0")
    with pytest.raises(ValueError, match="too dark for GATHER.*hour 01"):
        production.start_process(session, w, "GATHER")
    # dawn (hour 6, tick 7): the ration flows and the work is legal
    # (the thicket is where gathering lives -- S4)
    _at(session, w, "THICKET")
    while production.next_tick_number(session) != 7:
        run_tick(session); session.commit()
    run_tick(session); session.commit()          # tick 7 = hour 6
    # issued 1 at the top of the tick; the end-of-tick labor fade took
    # its half (scripts act between the two -- they see the full ration)
    assert _hold(session, w.id, "LABOR") >= Decimal("0.5")
    assert _act(session, w, "GATHER")


def test_warmth_draws_three_an_hour_at_night(session):
    """Night is the expensive half of the day: the consumption pass
    takes 3 WARMTH per dark hour and 1 per daylight hour."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Cold")
    markets.adjust_holding(session, w, "WARMTH", -WARMTH_BUFFER)
    markets.adjust_holding(session, w, "WARMTH", Decimal("3"))
    while production.next_tick_number(session) != 1:   # stay before tick 1
        break
    run_tick(session); session.commit()                 # tick 1 = hour 0, night
    night = [e for e in _events(session, "need_satisfied")
             if e["entity_id"] == w.id and e["need"] == "WARMTH"][0]
    assert night["consumed"] == "3.0000"                # the night draw
    markets.adjust_holding(session, w, "WARMTH", -_hold(session, w.id, "WARMTH"))
    while production.next_tick_number(session) != 8:    # to hour 7 (day)
        run_tick(session); session.commit()
    markets.adjust_holding(session, w, "WARMTH", Decimal("1"))
    run_tick(session); session.commit()
    day = [e for e in _events(session, "need_satisfied")
           if e["entity_id"] == w.id and e["need"] == "WARMTH"][-1]
    assert day["consumed"] == "1.0000"                  # the mild day draw


def test_eat_raw_feeds_now_and_risks_disease(session):
    """EAT_RAW is instant food (SATIETY lands at start_process, before
    the same tick's consumption pass) -- and a 25%-per-meal disease
    lottery. A BUSINESS worker: the starvation/exposure deaths it is too
    stubborn to avoid are not this test's subject. 30 meals with zero
    DISEASE grants is a 0.2% event."""
    create_content(session)
    w = _biz(session, "Desperate")
    saw_disease, fed_instantly, meals, died = False, 0, 0, False
    for _ in range(40):
        markets.adjust_holding(session, w, "MEAT", Decimal("1.5"))  # keep ahead of rot
        try:
            production.start_process(session, w, "EAT_RAW")  # labor-free, instant
        except ValueError:
            died = True   # dysentery won: three grants outpaced the decay
            break
        meals += 1
        if _hold(session, w.id, "SATIETY") > 0:
            fed_instantly += 1          # credited before the tick even ran
        run_tick(session); session.commit()
        saw_disease = saw_disease or _hold(session, w.id, "DISEASE") > 0
    assert fed_instantly == meals      # every meal landed SATIETY instantly
    assert meals >= 3                  # death needs 3 grants (1+.95+.90 > 2.5)
    assert saw_disease or died
    # And it actually fed him: hunger never grew past a scare.
    assert _hold(session, w.id, "HUNGER") < Decimal("2")


def test_spear_hunt_beats_bare_hunt(session):
    """The advantage contract, part two: hunting with a spear yields far
    more meat than bare hands over the same hunts."""
    create_content(session)
    bare, hunter = _biz(session, "Bare"), _biz(session, "Hunter")
    _at(session, bare, "FOREST")
    _at(session, hunter, "FOREST")   # the hunts live in the deep forest (S4)
    markets.adjust_holding(session, hunter, "SPEAR", Decimal("3"))  # see BAG note; duration-2 hunts can hold two
    for _ in range(100):
        run_tick(session); session.commit()
        _act_day(session, bare, "HUNT")
        _act_day(session, hunter, "HUNT_SPEAR")

    def meat(entity):
        total = Decimal("0")
        for e in _events(session, "process_completed"):
            if e["entity_id"] == entity.id:
                total += Decimal(e["outputs"].get("MEAT", "0"))
        return total
    assert meat(hunter) > meat(bare) * Decimal("1.5")


# ===========================================================================
# THE COMMONS FIRE (P1: the fire rework -- one fire warms many)
# ===========================================================================

def _commons_fire(session):
    ground = session.execute(
        select(parcels.Parcel).where(parcels.Parcel.parcel_type == "COMMONS")
    ).scalar_one()
    return ground, session.execute(
        select(parcels.Facility)
        .where(parcels.Facility.parcel_id == ground.id)
    ).scalar_one()


def _fuel(session):
    return _commons_fire(session)[1].fuel


def test_the_commons_fire_stands(session):
    """Genesis furniture: one UNOWNED fire-ground at the hearth carrying a
    PUBLIC fire -- four seats, six banked hours, burning one an hour --
    and the catalog prices stoking (1 WOOD = 2 hours) and warming (+6
    seated, a body caps at 6) around it. TEND_FIRE is gone: banking
    warmth stopped being a strategy."""
    create_content(session)
    ground, fire = _commons_fire(session)
    assert ground.owner_id is None
    assert ground.place.key == "HEARTH"
    assert (fire.access, fire.capacity) == ("PLACE", FIRE_SEATS)
    assert fire.fuel == FIRE_FUEL_START
    assert fire.fuel_capacity == FIRE_FUEL_CAP
    assert fire.fuel_burn_per_tick == FIRE_FUEL_BURN
    assert production.get_recipe(session, "TEND_FIRE") is None
    stoke = production.get_recipe(session, "STOKE_FIRE")
    assert stoke.facility_fuel_output == Decimal("2")
    assert stoke.duration_ticks == 0 and not stoke.requires_daylight
    warm = production.get_recipe(session, "WARM_BY_FIRE")
    assert warm.outputs[0].quantity == WARMTH_CAP and warm.requires_facility_lit
    for code in ("COOK_MEAT", "SMOKE_MEAT"):
        assert production.get_recipe(session, code).requires_facility_lit
    from econengine.goods import get_good
    assert get_good(session, "WARMTH").max_holding == WARMTH_CAP


def test_stoke_burns_and_goes_dark(session):
    """The fuel cycle: the bank burns one an hour whether anyone sits,
    runout is a facility_dark fact, a dark fire warms no one, and a log
    relights it (1 WOOD = 2 hours -- a full bank refuses more)."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Stoker")
    markets.adjust_holding(session, w, "WOOD", Decimal("3"))
    # genesis bank is full: stoking bounces (and keeps its wood)
    with pytest.raises(Exception, match="fully banked"):
        production.start_process(session, w, "STOKE_FIRE")
    assert _hold(session, w.id, "WOOD") == Decimal("3")
    _run(session, 5)
    assert _fuel(session) == Decimal("1")     # six banked, five burned
    assert _events(session, "facility_dark") == []
    _run(session, 1)
    assert _fuel(session) == Decimal("0")     # the beacon goes out
    assert len(_events(session, "facility_dark")) == 1
    # dark: the seat refuses, the stoke relights
    markets.adjust_holding(
        session, w, "WARMTH", -_hold(session, w.id, "WARMTH"))  # to zero
    with pytest.raises(Exception, match="dark"):
        production.start_process(session, w, "WARM_BY_FIRE")
    assert _act(session, w, "STOKE_FIRE")
    assert _fuel(session) == Decimal("2")
    assert _act(session, w, "WARM_BY_FIRE")


def test_the_fire_seats_four(session):
    """Capacity is seats: four houses warm at the commons fire in the
    same hour; the fifth bounces (and can build a second fire -- MAKE_FIRE
    is public on build)."""
    create_content(session)
    _no_wolves(session)
    houses = [_seat(session, f"House {i}") for i in range(5)]
    for h in houses[:4]:
        assert _act(session, h, "WARM_BY_FIRE")
    with pytest.raises(Exception, match="free seat"):
        production.start_process(session, houses[4], "WARM_BY_FIRE")
    _run(session, 2)                               # the seated hour completes
    assert _act(session, houses[4], "WARM_BY_FIRE")   # a seat freed


def test_warmth_cannot_be_banked(session):
    """The holding cap: a body holds at most 6 WARMTH. Seated warmth
    clips instead of banking (freeze-to-warm is one seated hour), and
    an estate transfer cannot bank more than the living could."""
    create_content(session)
    _no_wolves(session)
    w, other = _seat(session, "Warm"), _seat(session, "Heir")
    markets.adjust_holding(session, w, "WARMTH", -WARMTH_BUFFER)
    markets.adjust_holding(session, w, "WARMTH", Decimal("50"))  # clip at 6
    assert _hold(session, w.id, "WARMTH") == WARMTH_CAP
    # the same clip on the estate path (conditions._credit_holding)
    from econengine import conditions
    conditions._credit_holding(session, other, "WARMTH", Decimal("9"))
    assert _hold(session, other.id, "WARMTH") == WARMTH_CAP


# ===========================================================================
# POLICY TESTS -- the balance contract
# ===========================================================================

def _no_wolves(session):
    """Isolate a mechanics test from the predator variable: the packs
    go dormant (rows stay -- spawn math counts ACTIVE only). Tests that
    are ABOUT wolves (creature, combat, floor) do not call this."""
    for e in session.execute(select(Entity)).scalars():
        if e.name.startswith("Wolf Pack"):
            e.status = EntityStatus.INCAPACITATED
    session.commit()


def test_neglect_kills(session):
    """Doing nothing is fatal: the buffers run out and the conditions --
    EXPOSURE first, then HUNGER -- reach their thresholds. Death lands
    inside two DAYS (24-tick days): after tick 18, before tick 40."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Doomed")
    _run(session, 40)
    assert session.get(Entity, w.id).status == EntityStatus.INCAPACITATED
    died_at = None
    for tick in session.execute(select(Tick).order_by(Tick.number)).scalars():
        if any(e.get("type") == "entity_incapacitated" for e in (tick.events or [])):
            died_at = tick.number
            break
    assert died_at is not None and 18 <= died_at <= 40, died_at


def test_shelter_alone_is_misery_not_death(session):
    """The graded ladder under the clock: REST under a SHELTER covers
    the whole 1/hour DAYTIME draw; nights still gap (3 draw vs 1 drip
    per hour) -- a chronic shortfall that equilibrates far under the 18
    threshold. Cold, uncomfortable, alive (REST is labor-free, so the
    daylight LABOR budget still goes to gathering food)."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Sheltered")
    _at(session, w, "THICKET")   # gathering country (S4)
    parcels.add_facility(session, _camp(session, w), "SHELTER")
    for _ in range(60):
        run_tick(session); session.commit()
        try:
            production.start_process(session, w, "SLEEP_SHELTERED",
                                     _camp(session, w).id)   # labor-free
        except Exception:
            pass
        if _hold(session, w.id, "SATIETY") < 1:      # conscious eating (run 19):
            try:                                      # gathered food is not a meal
                production.start_process(session, w, "EAT_BERRIES")
            except Exception:
                pass
        if _hold(session, w.id, "SATIETY") < 1:      # the larder's second shelf:
            try:                                      # a subsister eats its apples too
                production.start_process(session, w, "EAT_APPLES")
            except Exception:
                pass
        _act_day(session, w, "GATHER")
    assert session.get(Entity, w.id).status == EntityStatus.ACTIVE
    assert _hold(session, w.id, "EXPOSURE") < Decimal("15")


def test_starter_survives(session):
    """The inherited script keeps a seat alive indefinitely at a
    hand-to-mouth pace: no incapacity, no script errors, both conditions
    well under their thresholds."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Starter")
    session.add(Script(
        name=f"starter-behaviour-{seat.id}",
        script_type=ScriptType.BEHAVIOUR,
        source=stone_age._gate_pack_script(stone_age.STARTER),
        entity_id=seat.id,
        timeout_ms=200,
        state={},
    ))
    session.commit()
    _run(session, 40)
    assert session.get(Entity, seat.id).status == EntityStatus.ACTIVE
    assert _hold(session, seat.id, "HUNGER") < Decimal("15")
    assert _hold(session, seat.id, "EXPOSURE") < Decimal("18")
    assert _events(session, "script_error") == []


# ===========================================================================
# MONEY, MANUAL, PRIVACY
# ===========================================================================

def test_money_comes_from_the_ground(session):
    """Coins are found, not endowed: a bagged digger mints; a bare one
    never does (the bare table has no coin branch -- that half is
    deterministic)."""
    create_content(session)
    digger, bare = _biz(session, "Digger"), _biz(session, "Bare")
    _at(session, digger, "THICKET")
    _at(session, bare, "THICKET")
    # Spares for the between-tick reservation window (see the bag test).
    markets.adjust_holding(session, digger, "BAG", Decimal("3"))
    for _ in range(200):
        _act_day(session, digger, "GATHER_BAG")
        _act_day(session, bare, "GATHER")
        run_tick(session); session.commit()

    digs = [e for e in _events(session, "process_completed")
            if e["entity_id"] == digger.id and e["recipe"] == "GATHER_BAG"]
    found = [e for e in digs if "COIN" in e["outputs"]]
    # 5%/dig, 200 digs: P(zero finds) = 0.95^200 ≈ 3.5e-5
    assert len(digs) >= 150
    assert len(found) >= 1
    acc = next(a for a in digger.accounts if a.currency == COIN)
    assert acc.balance == SEAT_COIN + len(found)   # every find minted, exact
    # the bare table cannot mint: its account is untouched
    bare_acc = next(a for a in bare.accounts if a.currency == COIN)
    assert bare_acc.balance == SEAT_COIN
    assert _hold(session, digger.id, COIN) == Decimal("0")  # money ≠ good


def test_world_ships_a_legible_manual(session):
    """The 3a fold: the manual WorldSetting keeps only the authored
    notes (the ladder, the post, privacy) -- the tables of goods,
    actions and death thresholds are now GENERATED, rendered from the
    installed content by catalog_text."""
    from econengine.catalog import catalog_state, catalog_text
    from econengine.models import WorldSetting

    create_content(session)
    row = session.get(WorldSetting, stone_age.MANUAL_KEY)
    assert row is not None
    text = row.value["text"]
    flat = " ".join(text.split()).lower()   # needles must survive line wraps
    for needle in ("the ladder", "trading post", "privacy", "scarcity"):
        assert needle in flat, needle

    # The derived numbers moved to the generated catalog -- and they are
    # all there: actions, tools, conditions, thresholds, threats.
    cat = " ".join(catalog_text(catalog_state(session)).split()).lower()
    for needle in ("gather_bag", "hunt_spear", "make_shelter", "eat_raw",
                   "pace", "hits", "pelt", "spear", "bag",
                   "incapacitates at 15",
                   "incapacitates at 2.5", "== needs", "== markets"):
        assert needle in cat, needle


def test_pack_sets_rival_privacy(session):
    """create_content turns on world.private_holdings: scripts see only
    their own pantry in this world."""
    from econengine.models import WorldSetting
    from econengine.scripting import PRIVATE_HOLDINGS_KEY

    create_content(session)
    row = session.get(WorldSetting, PRIVATE_HOLDINGS_KEY)
    assert row is not None and row.value


# ===========================================================================
# The trading post: the standing counterparty, and its price discovery
# ===========================================================================
# Three runs, zero trades: coin existed, hunger existed, and nobody had a
# reason (or a counterparty, or a price) to sell. The post is the pack's
# answer -- a BUSINESS market maker whose quotes ARE the price reference.
# These tests pin its spawn and each haggling rule.

def test_post_spawns_with_purse_larder_and_haggler(session):
    create_content(session)
    post = _post(session)
    assert post.entity_type == EntityType.BUSINESS
    # a BUSINESS draws no needs and no LABOR (engine rules): the post
    # can neither starve nor freeze -- it is terrain, not a player
    acc = next(a for a in post.accounts if a.currency == COIN)
    assert acc.balance == stone_age.POST_COIN
    assert _hold(session, post.id, "BERRIES") == Decimal("60")
    assert _hold(session, post.id, "COOKED_MEAT") == Decimal("20")
    script = session.execute(
        select(Script).where(Script.entity_id == post.id)).scalar_one()
    assert script.script_type == ScriptType.BEHAVIOUR
    assert script.state == {}          # fresh haggler, no prices yet


def test_post_quotes_both_sides_on_its_first_tick(session):
    create_content(session)
    post = _post(session)
    _run(session, 1)
    sells = {o.market_id: o for o in _open_orders(session, post.id, side=OrderSide.SELL)}
    assert sells                      # food on the ask, whole larder out
    buys = _open_orders(session, post.id, side=OrderSide.BUY)
    by_sym = {}
    mid = {m.id: m.symbol for m in session.execute(select(Market)).scalars()}
    for o in sells.values():
        by_sym[("sell", mid[o.market_id])] = o
    for o in buys:
        by_sym[("buy", mid[o.market_id])] = o
    assert by_sym[("sell", "BERRIES")].limit_price == Decimal("1.25")
    assert by_sym[("sell", "BERRIES")].quantity == Decimal("60")
    assert by_sym[("sell", "COOKED_MEAT")].limit_price == Decimal("1.50")
    # the larder's seed stock (P5): two hens on the shelf at the ask
    assert by_sym[("sell", "CHICKEN")].limit_price == Decimal("4.00")
    assert by_sym[("sell", "CHICKEN")].quantity == Decimal("2")
    # bids on every raw good, pro-rata across the P5 purse (7 goods
    # x 4 = 44 against 30 COIN -> every quantity floors to 2);
    # BERRIES itself is skipped -- the ladder is already stuffed (60 >= 20)
    for sym in ("MEAT", "WOOD", "YARN", "FLINT", "APPLES", "EGGS", "PELT"):
        assert by_sym[("buy", sym)].quantity == Decimal("2")
    assert ("buy", "BERRIES") not in by_sym
    assert by_sym[("buy", "MEAT")].limit_price == Decimal("1.00")
    assert by_sym[("buy", "YARN")].limit_price == Decimal("2.00")
    assert by_sym[("buy", "EGGS")].limit_price == Decimal("1.20")
    assert by_sym[("buy", "APPLES")].limit_price == Decimal("0.80")
    # it never crosses itself: BERRIES ask stands even with no bid


def test_post_peddles_its_menu_on_the_cadence(session):
    """The post hawks its counter through say: the standing menu,
    twice a 20-tick round, pitched at both directions of trade."""
    create_content(session)
    _no_wolves(session)
    _run(session, 21)
    heard = [
        (t.number, e)
        for t in session.execute(select(Tick).order_by(Tick.number)).scalars()
        for e in (t.events or []) if e.get("type") == "say"
    ]
    post = _post(session)
    assert [t for t, _ in heard] == [10, 20]   # every 10th tick, no more
    assert all(e["entity_id"] == post.id for _, e in heard)
    text = heard[0][1]["params"]["text"]
    assert "selling" in text and "JERKY" in text   # larder-side quotes
    assert "buying" in text and "WOOD" in text      # purse-side quotes
    assert "surplus" in text                        # the pitch, not just prices
    assert all(len(e["params"]["text"]) <= 256 for _, e in heard)


def test_post_ask_rises_when_food_sells(session):
    """Demand moves the ask up 5% the tick after a fill."""
    create_content(session)
    post, buyer = _post(session), _biz(session, "Buyer")
    _at(session, buyer, "POST")   # every market trades AT the post (S4)
    _run(session, 1)
    markets.place_order(session, buyer.id, "JERKY", "buy",
                        Decimal("2"), Decimal("2.00"),
                        next(a.id for a in buyer.accounts if a.currency == COIN))
    _run(session, 1)                    # the fill
    trades = [e for e in _events(session, "trade")]
    assert trades and Decimal(trades[-1]["quantity"]) == Decimal("2")
    assert buyer.accounts[0].balance == SEAT_COIN - Decimal("4")
    _run(session, 1)                    # the post reads its fill
    ask = _open_orders(session, post.id, "JERKY", OrderSide.SELL)
    assert [o.limit_price for o in ask] == [Decimal("2.10")]


def test_post_bid_falls_when_supply_arrives(session):
    """Sellers filling the bid is supply: the bid eases 5% next tick."""
    create_content(session)
    post, seller = _post(session), _biz(session, "Seller")
    _at(session, seller, "POST")  # orders fill only where the book lives
    markets.adjust_holding(session, seller, "WOOD", Decimal("10"))
    _run(session, 1)
    markets.place_order(session, seller.id, "WOOD", "sell",
                        Decimal("3"), Decimal("1.00"),
                        next(a.id for a in seller.accounts if a.currency == COIN))
    _run(session, 1)                    # the fill (pro-rata purse bids 2)
    filled = _hold(session, post.id, "WOOD")
    assert filled >= Decimal("2")
    assert next(a for a in seller.accounts if a.currency == COIN).balance \
        == SEAT_COIN + filled
    _run(session, 1)                    # the post reads its fill
    bids = _open_orders(session, post.id, "WOOD", OrderSide.BUY)
    assert [o.limit_price for o in bids] == [Decimal("0.95")]


def test_post_prices_drift_toward_trade_when_quiet(session):
    """3 LIVE ticks with no fills: the ask eases down 5%, the bid creeps
    up 3%. Quiet is counted from the tick after placement (an order
    cannot be quiet before it exists), so the drift lands on tick 4."""
    create_content(session)
    post = _post(session)
    _run(session, 3)
    # three ticks of resting, only two aged: prices hold
    ask = _open_orders(session, post.id, "BERRIES", OrderSide.SELL)
    assert [o.limit_price for o in ask] == [Decimal("1.25")]
    _run(session, 1)
    ask = _open_orders(session, post.id, "BERRIES", OrderSide.SELL)
    assert [o.limit_price for o in ask] == [Decimal("1.19")]   # 1.25*0.95
    bid = _open_orders(session, post.id, "MEAT", OrderSide.BUY)
    assert [o.limit_price for o in bid] == [Decimal("1.03")]   # 1.00*1.03


def test_post_never_bids_beyond_its_coin(session):
    """The purse runs out: the coin is split pro-rata across every good
    the post wants -- a lean budget shrinks all bids together instead
    of letting the head of the list eat the coin and starve the tail
    (run 4: MEAT/WOOD bids at the 5.00 cap consumed the purse; YARN and
    FLINT never quoted, so 57 FLINT of surplus found no bid)."""
    create_content(session)
    post = _post(session)
    acc = next(a for a in post.accounts if a.currency == COIN)
    acc.balance = Decimal("5.5")       # a lean purse
    _run(session, 1)
    buys = _open_orders(session, post.id, side=OrderSide.BUY)
    committed = sum(o.quantity * o.limit_price for o in buys)
    assert committed <= Decimal("5.5")
    mid = {m.id: m.symbol for m in session.execute(select(Market)).scalars()}
    by_sym = {mid[o.market_id]: o for o in buys}
    # too thin to spread 4 each (P5: the purse serves eight wants now):
    # one unit of the cheapest four fits (APPLES 0.80 + MEAT 1.00 +
    # WOOD 1.00 + EGGS 1.20 = 4.00 <= 5.5); YARN's 2.00 does not --
    # and NO good hoards the purse
    assert by_sym["APPLES"].quantity == Decimal("1")
    assert by_sym["MEAT"].quantity == Decimal("1")
    assert by_sym["WOOD"].quantity == Decimal("1")
    assert by_sym["EGGS"].quantity == Decimal("1")
    assert "YARN" not in by_sym and "FLINT" not in by_sym


def test_post_dark_bids_freeze_instead_of_drifting(session):
    """A bid that cannot afford to stand (purse drained) goes DARK --
    and a dark order does not drift. Run 4 walked dark bids to the 5.00
    cap for nothing; the frozen price returns the moment coin does."""
    create_content(session)
    post = _post(session)
    acc = next(a for a in post.accounts if a.currency == COIN)
    acc.balance = Decimal("0.10")      # nothing is affordable
    _run(session, 8)
    assert _open_orders(session, post.id, side=OrderSide.BUY) == []
    state = _post_script(session).state or {}
    assert Decimal(str(state["bid"]["MEAT"])) == Decimal("1.00")  # frozen
    assert Decimal(str(state["bid"]["YARN"])) == Decimal("2.00")
    # coin returns: the bids come back at the frozen prices
    acc.balance = Decimal("12")
    _run(session, 1)
    mid = {m.id: m.symbol for m in session.execute(select(Market)).scalars()}
    buys = {mid[o.market_id]: o for o in
            _open_orders(session, post.id, side=OrderSide.BUY)}
    assert Decimal(str(buys["MEAT"].limit_price)) == Decimal("1.00")
    # every want now (the rotted larder re-opened the BERRIES appetite):
    # 12 coin against a 44-coin want -- every bid comes back, pro-rated
    # to one unit each (P5: the purse serves the larder goods too)
    assert set(buys) == {"MEAT", "WOOD", "YARN", "FLINT", "BERRIES",
                         "APPLES", "EGGS", "PELT"}
    assert all(o.quantity == Decimal("1") for o in buys.values())


def test_post_jerky_never_rots_and_feeds(session):
    """JERKY is the salted shelf: it does not decay, it feeds EAT_JERKY,
    and the post spawns stocking it -- late coin always has something
    to buy (run 4: OSS died holding 17 COIN beside a rotted-empty
    larder)."""
    create_content(session)
    post = _post(session)
    assert markets.get_holding(session, post.id, "JERKY").quantity \
        == Decimal("30")
    # BERRIES and COOKED_MEAT rot beside an untouched JERKY stack
    _run(session, 10)
    assert markets.get_holding(session, post.id, "JERKY").quantity \
        == Decimal("30")
    assert markets.get_holding(session, post.id, "BERRIES").quantity \
        < POST_FOOD["BERRIES"]
    # and it feeds: a seat whose only food is JERKY runs the densest
    # meal out of it (run 19: feeding is a decision, not a draw)
    seat = _seat(session, "JerkyEater")
    markets.adjust_holding(session, seat, "BERRIES", -BERRY_BUFFER)
    markets.adjust_holding(session, seat, "JERKY", Decimal("5"))
    assert _act(session, seat, "EAT_JERKY")
    assert markets.get_holding(session, seat.id, "JERKY").quantity \
        == Decimal("4")                    # eaten, not rotted (0 decay)
    assert _hold(session, seat.id, "SATIETY") == Decimal("3.6")


# ===========================================================================
# THE MAP (S4): places, roads, gates, and the post's seat
# ===========================================================================

def test_the_map_six_places_nine_roads(session):
    """docs/spatial.md S4's stone-age map, as content rows: the hearth
    (start), thicket 1h, river and flint scrape 2h, deep forest 3h,
    post 1h down the valley (run 26's census: at four hours the post
    was a trip houses died taking -- two starved at its counter) --
    the forest and river roads stay the long ways round. P3 adds the
    river road past the thicket: the gather commute drinks, and the
    boar reaches the tap without the fire-ground."""
    from econengine import edges, places as places_mod
    create_content(session)
    keys = {p.key: p for p in places_mod.list_places(session)}
    assert set(keys) == {"HEARTH", "THICKET", "RIVER", "FLINT",
                         "FOREST", "POST"}
    assert all(p.kind == p.key for p in keys.values())
    roads = {(e.from_place.key, e.to_place.key): e.cost_ticks
             for e in edges.list_edges(session)}
    assert roads == {
        ("HEARTH", "THICKET"): 1,
        ("HEARTH", "RIVER"): 2,
        ("HEARTH", "FLINT"): 2,
        ("HEARTH", "FOREST"): 3,
        ("THICKET", "FOREST"): 2,
        ("FOREST", "POST"): 1,
        ("RIVER", "POST"): 2,
        ("HEARTH", "POST"): 1,
        ("THICKET", "RIVER"): 2,
    }
    hearth, post = keys["HEARTH"], keys["POST"]
    assert edges.distance_ticks(session, hearth, post) == 1
    # the valley road shortens the forest to 2h through the post
    assert edges.distance_ticks(session, hearth, keys["FOREST"]) == 2
    # P3: the thicket is 2h from the river -- the gather road drinks
    assert edges.distance_ticks(
        session, keys["THICKET"], keys["RIVER"]) == 2
    # the walk itself is a recipe, priced by the road not the template
    walk = production.get_recipe(session, "TRAVEL_WALK")
    assert walk is not None and walk.duration_ticks == 1
    assert not walk.branches          # no outputs: arrival is the product


def test_work_is_where_you_are(session):
    """Presence gates bind the work to the map: the refusal names where
    you stand and what the work wants -- and the right ground opens the
    gate."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Walker")          # wakes at the hearth
    from econengine import clock
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    # the thicket's work, tried from the hearth:
    with pytest.raises(ValueError, match="must be at a THICKET"):
        production.start_process(session, w, "GATHER")
    # the forest's work, tried from the hearth:
    with pytest.raises(ValueError, match="must be at a FOREST"):
        production.start_process(session, w, "HUNT")
    # the hearth's own work: presence passes at the hearth, and the
    # FACILITY refuses (no fire yet) -- the place gate is behind us
    _at(session, w, "HEARTH")
    with pytest.raises(ValueError, match="FIRE"):
        production.start_process(session, w, "TEND_FIRE", _camp(session, w).id)
    # the right grounds open the gates (daylight waits for dawn):
    _at(session, w, "THICKET")
    assert _act_day(session, w, "GATHER")
    _at(session, w, "FLINT")
    assert _act_day(session, w, "DIG_FLINT")
    _at(session, w, "RIVER")
    assert _act_day(session, w, "FISH")
    _at(session, w, "FOREST")
    assert _act_day(session, w, "HUNT")
    # the work landed where it should: certain flint from the scrape
    # (DIG_FLINT has no branch table), and the river/forest recipes
    # completed (their yields are rolls — the gates are the subject)
    _run(session, 4)
    assert _hold(session, w.id, "FLINT") >= Decimal("2")
    done = {e["recipe"] for e in _events(session, "process_completed")
            if e["entity_id"] == w.id}
    assert {"GATHER", "DIG_FLINT", "FISH", "HUNT"} <= done


def test_markets_live_at_the_post(session):
    """Every book trades AT the post: a seat at the hearth is refused
    with the road named; standing there, the order rests."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Trader")
    acc = next(a.id for a in w.accounts if a.currency == COIN)
    with pytest.raises(ValueError, match="trades at Trading post"):
        markets.place_order(session, w.id, "BERRIES", "buy",
                            Decimal("1"), Decimal("2.00"), acc)
    _at(session, w, "POST")
    order = markets.place_order(session, w.id, "BERRIES", "buy",
                                 Decimal("1"), Decimal("2.00"), acc)
    assert order.status.value == "open"


def test_everyone_wakes_on_their_ground(session):
    """Genesis placement: seats at the hearth clearing (the fire-ground
    start), wolves denned in the deep forest (their range), the post at
    the post-place where every market trades."""
    create_content(session)
    seats = [e for e in session.execute(select(Entity)).scalars()
             if e.entity_type == EntityType.INDIVIDUAL and not
             e.name.startswith("Wolf Pack")]
    assert seats == []          # genesis installs no seats; the post and
    #                         wolves below ARE the placed population
    post = [e for e in session.execute(select(Entity)).scalars()
            if e.name == "Trading Post"][0]
    assert post.place.key == "POST"
    wolves = [e for e in session.execute(select(Entity)).scalars()
              if e.name.startswith("Wolf Pack")]
    assert len(wolves) == 2
    assert {w.place.key for w in wolves} == {"FOREST"}
    # and a born seat wakes at the hearth:
    seat = _seat(session, "Seat")
    assert seat.place.key == "HEARTH"
    assert _camp(session, seat).place.key == "HEARTH"   # the camp too


def test_the_starter_walks_to_eat(session):
    """The floor inherits a commute: the starter script walks to the
    thicket for food and home for the fire -- travel events on the
    record, the thicket reached, and arrival at home by the end."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Starter")
    session.add(Script(
        name=f"walker-behaviour-{seat.id}",
        script_type=ScriptType.BEHAVIOUR,
        source=stone_age._gate_pack_script(stone_age.STARTER),
        entity_id=seat.id,
        timeout_ms=200,
        state={},
    ))
    session.commit()
    _run(session, 40)
    arrived = _events(session, "travel_arrived")
    assert any(e["place"] == "THICKET" for e in arrived)
    assert any(e["place"] == "HEARTH" for e in arrived)
    assert _events(session, "script_error") == []
    assert session.get(Entity, seat.id).status == EntityStatus.ACTIVE


def test_wolves_are_creatures_with_stats_and_health(session):
    """Run 20's variable: a wolf is an ENTITY -- same physics as a house
    (needs, hunger), stats it was born with, health it can lose, and a
    hunting program. Genesis installs two; the breeding rule renews
    them (from day 5, every 5 days, up to 2, never more than 3 alive)."""
    from econengine import combat, spawns
    from econengine.models import Script
    create_content(session)
    wolves = [e for e in session.execute(select(Entity)).scalars()
              if e.name.startswith("Wolf Pack")]
    assert len(wolves) == 2
    w = wolves[0]
    assert w.entity_type.value == "individual"      # same physics
    assert combat.get_stats(session, w.id) == {
        "ATTACK": Decimal("4"), "DEFENSE": Decimal("1"),
        "HITS": Decimal("12")}
    assert _hold(session, w.id, "HITS") == Decimal("12")
    assert _hold(session, w.id, "MEAT") == Decimal("1")
    assert _hold(session, w.id, "PELT") == Decimal("1")   # it wears it
    assert session.execute(select(Script).where(
        Script.entity_id == w.id)).scalars().first() is not None
    # the trader is a man, not a building: killable flesh (innate HITS),
    # armed and careful (4/4), with hands -- and the world keeps his
    # hearth lit, so wolves are turned at his door
    post = [e for e in session.execute(select(Entity)).scalars()
            if e.name == "Trading Post"][0]
    assert combat.is_creature(session, post.id) is True
    assert combat.get_stats(session, post.id)["DEFENSE"] == Decimal("4")
    markets.adjust_holding(session, post, "WARMTH", Decimal("1"))
    _at(session, w, "POST")   # a wolf at the door bites (S4: up close)
    ev = combat.resolve_attack(session, w.id, post.id, 21)
    assert ev.get("deterred") is True and not ev.get("hit")
    # breeding cadence (P5: wolves and boars are separate programs --
    # filter by name; the boars keep their own clock, day 3 and every
    # third day): rounds 1-4 no wolves; round 5 tops up toward 3;
    # round 10 stays at the cap
    wolves = lambda rows: [r for r in rows
                           if r["name"].startswith("Wolf Pack")]
    boars = lambda rows: [r for r in rows
                          if r["name"].startswith("Wild Boar")]
    assert spawns.apply_on_round(session, 4) == []
    assert [r["name"] for r in boars(spawns.apply_on_round(session, 3))] \
        == ["Wild Boar I"]
    assert len(wolves(spawns.apply_on_round(session, 5))) == 1   # cap 3
    assert wolves(spawns.apply_on_round(session, 9)) == []
    assert spawns.apply_on_round(session, 10) == []  # both at their caps


def test_combat_between_entities(session):
    """Fighting is between creatures: a lit hearth turns the wolf at the
    door (a loud miss), a spear prices into the fight, and a kill
    seizes the loot through the ordinary estate machinery."""
    from econengine import combat
    from econengine.models import EntityStatus
    create_content(session)
    house = _seat(session, "Doomed")
    wolf = next(e for e in session.execute(select(Entity)).scalars()
                if e.name.startswith("Wolf Pack"))
    _at(session, wolf, "HEARTH")   # S4: hunting is up close -- the wolf
    #                             comes to the house's door for this test
    # firelight: a warm house cannot be bitten (the miss is loud)
    markets.adjust_holding(session, house, "WARMTH", Decimal("5"))
    ev = combat.resolve_attack(session, wolf.id, house.id, 1)
    assert ev["deterred"] is True and ev["hit"] is False
    assert _hold(session, house.id, "HITS") == Decimal("20")
    # the fire dies; the hunt is on. Unarmed house vs wolf: 4 v 1.
    # (One attack per (attacker, defender, tick) is the honest cadence --
    # the RNG seed is the triple -- so the hunt spans the dark hours.)
    markets.adjust_holding(session, house, "WARMTH", -_hold(session, house.id, "WARMTH"))
    night_hours = [d * 24 + h for d in range(9) for h in (1, 2, 3, 4, 5, 21, 22, 23)]
    hits = 0
    for tick in night_hours:                       # ~65% each: plenty
        if session.get(Entity, house.id).status != EntityStatus.ACTIVE:
            break
        ev = combat.resolve_attack(session, wolf.id, house.id, tick)
        assert ev["hit"] in (True, False)
        if ev.get("hit"):
            hits += 1
    assert hits >= 4                                # 20 HITS, 3 a bite
    assert session.get(Entity, house.id).status != EntityStatus.ACTIVE
    # a kill is a carcass: the wolf ate (bites + MEAT torn from it),
    # but it cannot CARRY -- the house's estate burned, and the pelt
    # on the wolf's back is still the one it was born wearing
    assert _hold(session, wolf.id, "PELT") == Decimal("1")
    assert _hold(session, wolf.id, "MEAT") >= Decimal("4")
    assert _hold(session, house.id, "HITS") == Decimal("0")
    # and the house can fight back: a spear makes it a duel (stats are
    # born, weapons are carried)
    hunter = _seat(session, "Hunter")
    wolf2 = spawns_spawn(session, "Wolf Pack X")
    _at(session, hunter, "FOREST")   # the duel is in the wolf's range
    markets.adjust_holding(session, hunter, "SPEAR", Decimal("1"))
    assert combat.effective_attack(session, hunter.id) == Decimal("4")
    ev = combat.resolve_attack(session, hunter.id, wolf2.id, 1)
    assert Decimal(ev["attack"]) == Decimal("4")         # 1 born + 3 carried
    # daylight refuses the hunt entirely
    ev = combat.resolve_attack(session, hunter.id, wolf2.id, 10)
    assert ev.get("status") == "rejected" and "too bright" in ev["reason"]
    # hands inherit: the hunter kills wolf2 and takes everything it
    # carried (pelt, meat) plus the carcass MEAT -- that is what
    # CARRY means
    for t in range(49, 120):
        if session.get(Entity, wolf2.id).status != EntityStatus.ACTIVE:
            break
        markets.adjust_holding(session, hunter, "WARMTH", -_hold(session, hunter.id, "WARMTH"))
        ev = combat.resolve_attack(session, hunter.id, wolf2.id, t)
        if ev.get("killed"):
            assert ev["loot"]["PELT"] == "1.0000"
    assert session.get(Entity, wolf2.id).status != EntityStatus.ACTIVE
    assert _hold(session, hunter.id, "PELT") == Decimal("1")
    assert _hold(session, hunter.id, "MEAT") >= Decimal("4")


def spawns_spawn(session, name):
    return stone_age.make_wolf(session, name)


def test_wolves_live_off_the_land(session):
    """Run 26's census: denned packs starved once the houses slept out
    of reach -- the human table (EAT_RAW at 0.6 satiety a strip) cannot
    cover a 0.5/hour draw. The born CARNIVORE stomach fixes the
    conversion (carrion eats like jerky: 3.6 a strip, no disease) and
    is the wolf's alone: a house reading the catalog cannot farm it."""
    from econengine import tech
    create_content(session)
    wolf = next(e for e in session.execute(select(Entity)).scalars()
                if e.name.startswith("Wolf Pack"))
    assert tech.entity_unlocks(session, wolf.id) == ["CARNIVORE"]
    # the meal: 1 MEAT -> 3.6 SATIETY, instant, and no disease roll at all
    assert _hold(session, wolf.id, "MEAT") == Decimal("1")
    production.start_process(session, wolf, "EAT_CARRION")
    assert _hold(session, wolf.id, "SATIETY") == Decimal("3.6")
    assert _hold(session, wolf.id, "MEAT") == Decimal("0")
    assert _hold(session, wolf.id, "DISEASE") == Decimal("0")
    # and it is not the houses' to copy: the refusal names the trait
    house = _seat(session, "Copycat")
    markets.adjust_holding(session, house, "MEAT", Decimal("1"))
    with pytest.raises(ValueError, match="requires CARNIVORE"):
        production.start_process(session, house, "EAT_CARRION")


def test_a_hungry_pack_walks_to_where_the_people_sleep(session):
    """The range (run 26's census, P2's dusk): a denned wolf that cannot
    reach prey no longer waits to starve -- in the LAST daylight a
    hungry pack travels toward the hearth (the raid walk, arrived with
    the light: wolves carry no torch, so the dark road is not theirs to
    walk), and by day it works its way home to the forest (the game).
    The applied travel intents carry the itinerary."""
    from econengine import clock
    create_content(session)
    wolf = next(e for e in session.execute(select(Entity)).scalars()
                if e.name.startswith("Wolf Pack"))
    assert wolf.place.key == "FOREST"
    # hungry (an empty stomach keeps granting HUNGER): the raid walk
    # begins at dusk -- three hours of road, walked while the light lasts
    markets.adjust_holding(session, wolf, "HUNGER", Decimal("4"))
    markets.adjust_holding(session, wolf, "SATIETY", Decimal("0"))
    while clock.hour_of(production.next_tick_number(session)) < 16:
        run_tick(session); session.commit()
    # dusk falls: out to the hearth, a night of prowling, home by day
    for _ in range(24):
        run_tick(session); session.commit()
    walks = [e["to"] for e in _events(session, "travel")
             if e["entity_id"] == wolf.id and e["status"] == "applied"]
    assert "HEARTH" in walks               # the dusk raid walk
    assert "FOREST" in walks                # and home again by day


def test_starter_floor_survives_the_wolves(session):
    """The integration contract: two hunting packs, a silent floor with
    a fire -- no incapacity across two days. Wolves that find nothing
    loud pace and starve or prowl; the floor banks warmth and answers
    what bites it."""
    create_content(session)
    seat = _seat(session, "Starter")
    session.add(Script(
        name=f"starter-behaviour-{seat.id}",
        source=stone_age._gate_pack_script(stone_age.STARTER),
        script_type=ScriptType.BEHAVIOUR,
        entity_id=seat.id,
        timeout_ms=200,
        state={},
    ))
    session.commit()
    _run(session, 48)                               # two full days
    assert session.get(Entity, seat.id).status == EntityStatus.ACTIVE


def test_the_torch_chain_make_light_and_chain_off_the_ember(session):
    """P2, the night kit: a brand is made by daylight labor (1 WOOD +
    1 YARN), lit at the commons fire, chained off your own flame, and
    -- once -- off the dying ember of the last (the register's two-tick
    window after a burnout; after that the dark is the dark)."""
    create_content(session)
    seat = _seat(session, "Torchbearer")
    assert seat.place.key == "HEARTH"
    _run(session, 6)                                # hour 06: daylight
    # make: the day's craft is the night's road (daylight-gated)
    markets.adjust_holding(session, seat, "LABOR", Decimal("1"))
    markets.adjust_holding(session, seat, "WOOD", Decimal("2"))
    markets.adjust_holding(session, seat, "YARN", Decimal("1"))
    production.start_process(session, seat, "MAKE_TORCH")
    _run(session, 3)
    assert _hold(session, seat.id, "TORCH") == Decimal("1")
    assert _hold(session, seat.id, "LIT_TORCH") == Decimal("0")
    # light: the commons fire (stoked -- it has been burning since tick 1)
    production.start_process(session, seat, "STOKE_FIRE")
    production.start_process(session, seat, "LIGHT_TORCH")
    assert _hold(session, seat.id, "TORCH") == Decimal("0")
    assert _hold(session, seat.id, "LIT_TORCH") == Decimal("1")
    # chain off your own flame: anywhere, instant
    markets.adjust_holding(session, seat, "TORCH", Decimal("1"))
    production.start_process(session, seat, "CHAIN_TORCH")
    assert _hold(session, seat.id, "LIT_TORCH") >= Decimal("1.5")  # the cap: 2
    # the flame burns out mid-road: the ember still strikes, once
    _run(session, 1)                                # stamps the last lit tick
    markets.adjust_holding(
        session, seat, "LIT_TORCH", -_hold(session, seat.id, "LIT_TORCH"))
    assert _hold(session, seat.id, "LIT_TORCH") == Decimal("0")
    markets.adjust_holding(session, seat, "TORCH", Decimal("1"))
    production.start_process(session, seat, "CHAIN_TORCH")
    assert _hold(session, seat.id, "LIT_TORCH") == Decimal("1")
    # ...but the window shuts: no LIT, no EMBER, no chaining
    markets.adjust_holding(
        session, seat, "LIT_TORCH", -_hold(session, seat.id, "LIT_TORCH"))
    _run(session, 3)                                # past the two-tick grace
    markets.adjust_holding(session, seat, "TORCH", Decimal("1"))
    with pytest.raises(ValueError, match="EMBER"):
        production.start_process(session, seat, "CHAIN_TORCH")


def test_the_dark_road_wants_a_flame(session):
    """P2, the ambient night gate: a bare-handed house cannot start the
    road at night (a readable refusal naming LIT), and a burning torch
    both opens the road and marks the departure loud -- every pack in
    the dark hears the walker."""
    from econengine.lua_engine import Intent
    from econengine.scripting import resolve_intent

    def _walk(to):
        return resolve_intent(session, Intent(
            entity_id=seat.id, intent_type="travel", params={"to": to},
            resource_ids=[],
        ))

    create_content(session)
    seat = _seat(session, "Walker")
    _run(session, 20)                               # tick 20: dark falls
    ev = _walk("THICKET")
    assert ev["status"] == "rejected"
    assert "LIT" in ev["reason"]
    # a burning brand opens the road -- loudly
    markets.adjust_holding(session, seat, "LIT_TORCH", Decimal("1"))
    ev = _walk("THICKET")
    assert ev["status"] == "applied"
    assert ev.get("loud") is True
    # and by daylight neither gate nor noise: a quiet road
    _run(session, 10)                               # into the next day
    markets.adjust_holding(
        session, seat, "LIT_TORCH", -_hold(session, seat.id, "LIT_TORCH"))
    ev = _walk("HEARTH")
    assert ev["status"] == "applied"
    assert ev.get("loud") is None


# ===========================================================================
# P5: THE LARDER
# ===========================================================================

def test_the_orchard_branch_feeds_the_bare_hand(session):
    """P5's arithmetic, asserted from the installed rows: both gather
    tables carry the orchard branch, the weights sum to 100, and bare
    food income clears ~3.0 satiety-equivalent/hour (the income wall's
    first break -- apples keep a day and a half where berries rot in a
    morning, so the surplus is worth banking; run 38's wood-rich lever
    took the berry share as the donor: 3.53 -> 3.00, food had slack --
    FOOD sat 0.771, zero hunger deaths -- while the fire starved)."""
    create_content(session)
    gather = production.get_recipe(session, "GATHER")
    bag = production.get_recipe(session, "GATHER_BAG")
    per_hen = {"berries": 2 / 1.5, "apples": Decimal("2.8") / Decimal("1.5")}

    def table(recipe):
        rows = {}
        for b in recipe.branches:
            rows[b.label] = (b.weight, {o.symbol: o.quantity
                                        for o in b.outputs})
        assert sum(b.weight for b in recipe.branches) == Decimal("100")
        return rows

    bare, bagged = table(gather), table(bag)
    assert set(bare) == {"berries", "apples", "wood", "yarn", "flint"}
    assert bare["berries"][1] == {"BERRIES": Decimal("4")}
    assert bare["apples"][1] == {"APPLES": Decimal("3")}
    assert bagged["berries"][1] == {"BERRIES": Decimal("8")}
    assert bagged["apples"][1] == {"APPLES": Decimal("6")}

    def food_ev(rows):
        ev = Decimal("0")
        for label in ("berries", "apples"):
            weight, outs = rows[label]
            qty = Decimal(str(sum(outs.values()))) / Decimal("100")
            ev += weight * qty * Decimal(str(per_hen[label]))
        return ev

    assert food_ev(bare) >= Decimal("2.99")    # the wall breaks bare-handed
    assert food_ev(bagged) >= Decimal("6.2")   # and the bag doubles down


def test_the_thicket_funds_the_night(session):
    """Run 38's lever, asserted from the installed rows: the wood roll
    pays for the night. All three houses died of FATIGUE beside a dark
    fire (d10-d12; food 0.771, water 0.950 -- every prior clock quiet)
    because the old tables banked ~0.3 wood per gather against a
    ~10-log night. The branch is now a quarter of every gather, and
    quantity over size -- frequency beats magnitude for famine: a
    six-gather day finds zero wood 18% of the time, not 38%."""
    create_content(session)
    gather = production.get_recipe(session, "GATHER")
    bag = production.get_recipe(session, "GATHER_BAG")

    def table(recipe):
        rows = {}
        for b in recipe.branches:
            rows[b.label] = (b.weight, {o.symbol: o.quantity
                                        for o in b.outputs})
        assert sum(b.weight for b in recipe.branches) == Decimal("100")
        return rows

    def wood_ev(rows):
        weight, outs = rows["wood"]
        return weight * sum(outs.values()) / Decimal("100")

    bare, bagged = table(gather), table(bag)
    assert bare["wood"] == (Decimal("25"), {"WOOD": Decimal("3")})
    assert bagged["wood"] == (Decimal("20"), {"WOOD": Decimal("6")})
    # 2.5x the famine tables: 0.75 and 1.20 logs per roll
    assert wood_ev(bare) == Decimal("0.75")
    assert wood_ev(bagged) == Decimal("1.20")
    assert wood_ev(bagged) / wood_ev(bare) == Decimal("1.6")
    # the polity break-even: three houses gathering ~5 apiece bank
    # ~11 logs a day against a ~10-log night -- the famine becomes a
    # margin economy, not a structural impossibility
    assert 3 * 5 * wood_ev(bare) >= Decimal("11")
    # frequency beats size: P(zero wood in a six-gather day)
    miss = (Decimal("100") - bare["wood"][0]) / Decimal("100")
    assert miss ** 6 <= Decimal("0.18")


def test_the_pen_serves_the_whole_flock_for_one_labor(session):
    """Pastoral compounding (P5): a penned house with N hens collects N
    eggs for a single labor hour -- one egg per hen held at completion,
    capped at four (the pen's worth, which is also the body's carry
    cap). The hens are held, never consumed; the flock is capital."""
    from econengine import parcels
    from econengine.models import Parcel
    create_content(session)
    _no_wolves(session)
    house = _seat(session, "Henkeeper")
    _at(session, house, "HEARTH")           # the pen is built on the camp
    camp = _camp(session, house)
    markets.adjust_holding(session, house, "WOOD", Decimal("3"))
    assert _act_day(session, house, "MAKE_PEN", parcel_id=camp.id)
    _run(session, 3)
    assert parcels.facility_capacity(session, camp.id, "PEN") >= 1
    # four hens penned: one hour, four eggs
    markets.adjust_holding(session, house, "CHICKEN", Decimal("4"))
    assert _act_day(session, house, "COLLECT_EGGS")
    _run(session, 2)
    done = [e for e in _events(session, "process_completed")
            if e.get("recipe") == "COLLECT_EGGS" and e["entity_id"] == house.id]
    assert done[-1]["outputs"] == {"EGGS": "4.0000"}
    assert _hold(session, house.id, "EGGS") >= Decimal("3.8")  # minus the hour's decay
    assert _hold(session, house.id, "CHICKEN") == Decimal("4")  # unharmed
    # two sold: the day's laying shrinks with the flock (honest scaling)
    markets.adjust_holding(session, house, "CHICKEN", Decimal("-2"))
    assert _act_day(session, house, "COLLECT_EGGS")
    _run(session, 2)
    done = [e for e in _events(session, "process_completed")
            if e.get("recipe") == "COLLECT_EGGS" and e["entity_id"] == house.id]
    assert done[-1]["outputs"] == {"EGGS": "2.0000"}
    # no flock, no harvest: the good_requirements gate refuses at zero
    markets.adjust_holding(session, house, "CHICKEN",
                           -_hold(session, house.id, "CHICKEN"))
    from econengine.markets import InsufficientHoldingsError
    with pytest.raises(InsufficientHoldingsError, match="CHICKEN"):
        production.start_process(session, house, "COLLECT_EGGS")


def test_hens_are_capped_at_the_pens_worth(session):
    """CHICKEN is durable, never consumed, and a body holds at most
    four -- the cap that keeps one house from becoming a ranch."""
    from econengine import goods as goods_mod
    create_content(session)
    hen = goods_mod.get_good(session, "CHICKEN")
    assert hen.max_holding == Decimal("4")
    assert hen.decay_per_tick is None or hen.decay_per_tick == Decimal("0")
    apples = goods_mod.get_good(session, "APPLES")
    assert apples.decay_per_tick == Decimal("0.08")
    eggs = goods_mod.get_good(session, "EGGS")
    assert eggs.decay_per_tick == Decimal("0.05")


def test_the_boar_answers_a_spear_and_remembers(session):
    """Dangerous prey (P5): the boar dens at the thicket, born carrying
    its own carcass (the guarded shelf), and when a hunter comes for it
    in the dark the boar retaliates -- and keeps answering while the
    foe stands on its ground."""
    from econengine import combat, spawns
    from econengine.models import EntityStatus, Tick
    create_content(session)
    hunter = _seat(session, "Boarhunter")
    _at(session, hunter, "THICKET")     # the boar's ground
    born = spawns.apply_on_round(session, 3)
    assert [b["name"] for b in born] == ["Wild Boar I"]
    boar = session.get(Entity, born[0]["entity_id"])
    assert boar.place.key == "THICKET"  # an hour's walk, not the wolves' forest
    assert _hold(session, boar.id, "MEAT") == Decimal("6")
    assert _hold(session, boar.id, "PELT") == Decimal("2")
    assert combat.get_stats(session, boar.id) == {
        "ATTACK": Decimal("4"), "DEFENSE": Decimal("2"),
        "HITS": Decimal("12")}
    # the hunter's program: spear the boar every night hour
    src = (f"-- provoke\n"
           f"local BOAR = {boar.id!r}\n"
           "if std.is_night() then ctx.action.attack(BOAR) end\n")
    session.add(Script(name=f"hunter-{hunter.id}", source=src,
                       script_type=ScriptType.BEHAVIOUR,
                       entity_id=hunter.id, timeout_ms=200, state={}))
    markets.adjust_holding(session, hunter, "SPEAR", Decimal("1"))
    session.commit()
    _run(session, 30)                    # into the night and through it
    fights = [e for e in _events(session, "combat")]
    boar_answers = [e for e in fights
                    if e.get("entity_id") == boar.id
                    and e.get("target_id") == hunter.id]
    assert boar_answers                  # tusk for spear
    # it never hunts anyone: every boar swing targets its provoker
    assert all(e.get("target_id") == hunter.id for e in boar_answers)


def test_the_bow_hunts_the_day_table_and_prices_into_the_fight(session):
    """Ranged capital (P5): HUNT_BOW is the best daylight table a lone
    crafter can buy (held bow, never consumed, EV above the spear
    hunt), and the weapons table prices it at +3 ATTACK like a spear."""
    from econengine import combat
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Bowman")
    _at(session, w, "FOREST")
    with pytest.raises(Exception, match="BOW"):
        production.start_process(session, w, "HUNT_BOW")
    markets.adjust_holding(session, w, "BOW", Decimal("1"))
    # the table: 15 nothing / 55 small (3) / 30 big (6) -> EV 3.45
    recipe = production.get_recipe(session, "HUNT_BOW")
    branches = {b.label: (b.weight, {o.symbol: o.quantity
                                     for o in b.outputs})
                for b in recipe.branches}
    ev = sum(Decimal(str(sum(branches[l][1].values()))) * branches[l][0]
             / Decimal("100") for l in branches)
    assert ev == Decimal("3.45")
    spear = production.get_recipe(session, "HUNT_SPEAR")
    spear_ev = sum(Decimal(str(sum({o.symbol: o.quantity for o in b.outputs}.values()))) * b.weight
                   / Decimal("100") for b in spear.branches)
    assert ev > spear_ev
    assert combat.effective_attack(session, w.id) == Decimal("4")  # 1+3


def test_the_starter_eats_the_apples_it_finds(session):
    """The floor learns the orchard (P5): a starter holding apples and
    berries eats the berries first (they rot faster), then the apples
    (a proper meal off the same gather)."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Orchardfloor")
    _at(session, seat, "HEARTH")
    markets.adjust_holding(
        session, seat, "BERRIES", -_hold(session, seat.id, "BERRIES"))
    markets.adjust_holding(session, seat, "SATIETY", Decimal("0"))
    markets.adjust_holding(session, seat, "BERRIES", Decimal("2"))
    markets.adjust_holding(session, seat, "APPLES", Decimal("2"))
    session.add(Script(
        name=f"starter-behaviour-{seat.id}",
        source=stone_age._gate_pack_script(stone_age.STARTER),
        script_type=ScriptType.BEHAVIOUR,
        entity_id=seat.id, timeout_ms=200, state={}))
    session.commit()
    _run(session, 1)
    # berries first (they rot faster): the meal came off the berry
    # basket, and the apple sack is still full
    assert Decimal("0") < _hold(session, seat.id, "BERRIES") < Decimal("1")
    assert _hold(session, seat.id, "APPLES") > Decimal("1.8")
    assert _hold(session, seat.id, "SATIETY") > Decimal("1")


# ===========================================================================
# WATER (P3): the thirst clock, the tap, the skin
# ===========================================================================

def test_drink_is_free_at_the_river(session):
    """The tap (P3): DRINK is free (no LABOR), instant (duration 0, no
    process_completed -- the credit is a fact of the tick) and river-
    bound: the refusal names where you stand. It fills the carried cup
    to the seam (4), whatever was in it."""
    create_content(session)
    _no_wolves(session)
    worker = _biz(session, "Drinker")       # INDIVIDUAL-free: no need draw
    _at(session, worker, "RIVER")
    assert production.get_recipe(session, "DRINK").duration_ticks == 0
    assert not production.get_recipe(session, "DRINK").inputs  # free
    assert _act(session, worker, "DRINK")
    assert _hold(session, worker.id, "WATER") == Decimal("4")
    # a full cup does not overflow (clip, never refuse)
    assert _act(session, worker, "DRINK")
    assert _hold(session, worker.id, "WATER") == Decimal("4")
    # the bank is the tap: standing at the hearth, the refusal names it
    _at(session, worker, "HEARTH")
    with pytest.raises(ValueError, match="RIVER"):
        production.start_process(session, worker, "DRINK")


def test_the_skin_is_a_dead_wolf_and_carries_a_day(session):
    """The water kit's craft (P3): one PELT sewn shut is a WATERSKIN
    (2 hours' work), and FILL_SKIN tops it at the bank -- the skin must
    be held, and it carries eight hours where cupped hands carry four.
    The WATER need draws the skin FIRST (symbol order), the cup is the
    reserve."""
    create_content(session)
    _no_wolves(session)
    house = _seat(session, "Skinner")
    _at(session, house, "HEARTH")
    markets.adjust_holding(session, house, "PELT", Decimal("1"))
    assert _act(session, house, "MAKE_WATERSKIN")
    _run(session, 3)                        # 2 ticks + the completion tick
    assert _hold(session, house.id, "WATERSKIN") == Decimal("1")
    assert _hold(session, house.id, "PELT") == Decimal("0")
    # fill at the bank: skin in hand, topped to the seam
    _at(session, house, "RIVER")
    assert _act(session, house, "FILL_SKIN")
    assert _hold(session, house.id, "SKINWATER") == Decimal("8")
    # no skin, no fill (the gate is a held good, like the pen's hens)
    markets.adjust_holding(
        session, house, "WATERSKIN",
        -_hold(session, house.id, "WATERSKIN"))
    from econengine.markets import InsufficientHoldingsError
    with pytest.raises(InsufficientHoldingsError, match="WATERSKIN"):
        production.start_process(session, house, "FILL_SKIN")
    # the draw order: skin first, cup in reserve
    markets.adjust_holding(session, house, "WATERSKIN", Decimal("1"))
    markets.adjust_holding(session, house, "SKINWATER", Decimal("8"))
    markets.adjust_holding(session, house, "WATER", Decimal("4"))
    _run(session, 1)
    assert _hold(session, house.id, "SKINWATER") < Decimal("8")
    assert _hold(session, house.id, "WATER") == Decimal("4")


def test_meals_part_hydrate(session):
    """The wet-diet clause (P3): every meal carries some water -- berries
    and apples wet (0.6), jerky nearly dry (0.2). Eating is drinking,
    badly: the second stomach fills thinner than the first."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Wetmouth")
    for sym in ("WATER", "SKINWATER"):
        markets.adjust_holding(session, seat, sym, -_hold(session, seat.id, sym))
    markets.adjust_holding(session, seat, "BERRIES", Decimal("2"))
    assert _act(session, seat, "EAT_BERRIES")
    assert _hold(session, seat.id, "WATER") == Decimal("0.6")
    markets.adjust_holding(session, seat, "WATER",
                           -_hold(session, seat.id, "WATER"))
    markets.adjust_holding(session, seat, "JERKY", Decimal("1"))
    assert _act(session, seat, "EAT_JERKY")
    assert _hold(session, seat.id, "WATER") == Decimal("0.2")


def _keep_fed_and_warm(session, house, stock=None, sleep=True):
    """Test scaffolding, not a policy: keep every need but WATER met so
    the thirst clock is the only clock under test. `stock` names the
    diet's symbol and its floor -- the scaffold keeps the basket fed
    (berries rot in hours; jerky never does), the THIRST arithmetic is
    the subject. `sleep=False` leaves REST alone too -- for the nights
    the FATIGUE clock is the subject."""
    if stock is not None:
        sym, floor, top = stock
        if _hold(session, house.id, sym) < Decimal(str(floor)):
            markets.adjust_holding(session, house, sym, Decimal(str(top)))
    if _hold(session, house.id, "SATIETY") < Decimal("1.5"):
        if _hold(session, house.id, "BERRIES") >= Decimal("1.5"):
            _act(session, house, "EAT_BERRIES")
        elif _hold(session, house.id, "JERKY") >= Decimal("1"):
            _act(session, house, "EAT_JERKY")
    if _hold(session, house.id, "WARMTH") < Decimal("3"):
        markets.adjust_holding(session, house, "WARMTH", Decimal("6"))
    if sleep and _hold(session, house.id, "REST") < Decimal("2"):
        markets.adjust_holding(session, house, "REST", Decimal("6"))


def test_the_wet_diet_rides_below_the_threshold(session):
    """Day one is not a death march (P3's promise): a berry-grazer that
    never walks to the river still rides UNDER the thirst threshold --
    meal credits (0.6 x ~8 meals) cover four-fifths of the draw, so the
    THIRST equilibrium sits at ~2, far from the 7.5 that kills. Thirst
    is felt, not fatal, on a wet diet."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Grazer")
    markets.adjust_holding(
        session, seat, "WATER", -_hold(session, seat.id, "WATER"))
    markets.adjust_holding(session, seat, "BERRIES", Decimal("40"))
    markets.adjust_holding(session, seat, "JERKY", Decimal("5"))
    for _ in range(72):                     # three days, no river
        _keep_fed_and_warm(session, seat, ("BERRIES", 3, 10))
        _run(session, 1)
    assert session.get(Entity, seat.id).status == EntityStatus.ACTIVE
    thirst = _hold(session, seat.id, "THIRST")
    assert Decimal("0") < thirst < Decimal("7.5")   # felt, never fatal


def test_thirst_kills_the_dry_larder_on_day_three(session):
    """THIRST targets the rich (P3's point): a FED house on the dry
    larder -- jerky three times a day, water credits a trickle -- dies
    of thirst on the third day, while hunger stays fed. Preservation
    is dry; the river is the price of the deep pantry."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Pantler")
    markets.adjust_holding(
        session, seat, "WATER", -_hold(session, seat.id, "WATER"))
    markets.adjust_holding(session, seat, "JERKY", Decimal("40"))
    markets.adjust_holding(session, seat, "BERRIES", Decimal("0"))
    died_at = None
    for _ in range(96):
        _keep_fed_and_warm(session, seat, ("JERKY", 3, 10))
        _run(session, 1)
        if session.get(Entity, seat.id).status != EntityStatus.ACTIVE:
            died_at = production.next_tick_number(session) - 1
            break
    assert died_at is not None and 48 <= died_at <= 80    # ~day three
    kills = [e for e in _events(session, "entity_incapacitated")
             if e.get("entity_id") == seat.id]
    assert kills[-1]["condition"] == "THIRST"
    assert _hold(session, seat.id, "HUNGER") < Decimal("15")   # fed, not starved


def test_the_post_anchors_a_waterskin(session):
    """The price anchor (P3): one waterskin on the post's shelf, asked
    at 6.00 -- between \"sew it yourself\" (one pelt, bid 3.00) and the
    egg trade it unlocks. The market exists; the shelf holds exactly
    one; water itself is never sold (the tap is free)."""
    create_content(session)
    post = _post(session)
    assert _hold(session, post.id, "WATERSKIN") == Decimal("1")
    _run(session, 2)
    sell = _open_orders(session, post.id, "WATERSKIN", OrderSide.SELL)
    assert len(sell) == 1
    assert Decimal(next(iter(sell)).limit_price) == Decimal("6.00")
    # water itself is never sold: the tap is free, the SKIN is the stock
    assert not any(m.symbol in ("WATER", "SKINWATER")
                   for m in session.execute(select(Market)).scalars())


def test_the_starter_drinks_at_the_river(session):
    """The floor learns the tap (P3): a dry starter standing at the bank
    drinks its fill before any gathering intent -- thirst is as urgent
    as hunger at the floor, and the walk to the river is the cure."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Waterfloor")
    _at(session, seat, "RIVER")
    markets.adjust_holding(
        session, seat, "WATER", -_hold(session, seat.id, "WATER"))
    markets.adjust_holding(session, seat, "WATER", Decimal("0.5"))
    markets.adjust_holding(session, seat, "SATIETY", Decimal("4"))
    markets.adjust_holding(session, seat, "WOOD", Decimal("6"))
    session.add(Script(
        name=f"starter-behaviour-{seat.id}",
        source=stone_age._gate_pack_script(stone_age.STARTER),
        script_type=ScriptType.BEHAVIOUR,
        entity_id=seat.id, timeout_ms=200, state={}))
    session.commit()
    _run(session, 1)
    assert _hold(session, seat.id, "WATER") > Decimal("3")   # drank its fill


def test_the_beasts_walk_to_water(session):
    """The river is the one place every body visits (P3): a dry wolf
    leaves the forest for the bank by the post road, and a dry boar
    takes the thicket road -- without ever touching the fire-ground.
    (Boars spawn from day 3; the test makes one by hand.)"""
    create_content(session)
    from econengine import spawns
    wolf = next(e for e in session.execute(select(Entity)).scalars()
                if e.name.startswith("Wolf Pack"))
    boar = spawns.spawn_one(session, "Wild Boar Test", {
        "entity_type": "individual",
        "stats": {"ATTACK": 4, "DEFENSE": 2, "HITS": 12},
        "holdings": {"MEAT": 6, "PELT": 2, "WATER": 0},
        "script_setting": "boar",
        "account": {"COIN": 0},
        "place": "THICKET",
        "technologies": ["CARNIVORE"],
    })
    session.commit()
    # dawn first: the beasts' day blocks sleep at night (tick 1 is hour
    # 01 -- run the dark hours out before drying the cups)
    from econengine import clock
    while clock.is_night(production.next_tick_number(session)):
        _run(session, 1)
    markets.adjust_holding(
        session, wolf, "WATER", -_hold(session, wolf.id, "WATER"))
    _run(session, 3)   # the boundary tick may still read as night (the
                       # script sees the PREVIOUS hour); a dry day walk lands
    walks = {e["entity_id"]: e["to"] for e in _events(session, "travel")
             if e.get("status") == "applied"
             and e["entity_id"] in (wolf.id, boar.id)}
    assert walks.get(wolf.id) == "RIVER"
    assert walks.get(boar.id) == "RIVER"


# ===========================================================================
# P4 — SLEEP: the night becomes a budget
# ===========================================================================

def test_rest_need_registered_and_the_clock(session):
    """The sleep clock (P4): REST drains 0.25 an hour awake (six a day),
    holds at most a day and a third, and an unmet draw grants FATIGUE
    0.5 an hour -- the THIRST twin's arithmetic, on the night's stock.
    An idle hour restores nothing: only SLEEP recipes fill it."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Awake")
    assert _hold(session, w.id, "REST") == Decimal("4")   # born buffer
    markets.adjust_holding(session, w, "REST", Decimal("20"))
    assert _hold(session, w.id, "REST") == Decimal("8")   # the cap
    from econengine import needs as needs_mod
    need = needs_mod.get_need(session, "REST")
    assert need.condition_symbol == "FATIGUE"
    assert need.condition_quantity == Decimal("0.5")
    # sixteen awake hours empty the born buffer; the grants then climb
    # (a fresh body, alive past the sixteenth hour: neglect kills at 18+)
    fresh = _seat(session, "Awake2")
    _run(session, 16)
    assert _hold(session, fresh.id, "REST") == Decimal("0")
    assert _hold(session, fresh.id, "FATIGUE") == Decimal("0")
    _run(session, 1)                     # the first unmet hour grants
    assert _hold(session, fresh.id, "FATIGUE") > Decimal("0.4")
    assert _hold(session, fresh.id, "REST") == Decimal("0")   # idle restores nothing


def test_the_sleepless_clock_kills_on_the_third_night(session):
    """Two sleepless nights tired, three dead (P4): fed, warm, and
    watered -- but never sleeping -- the house dies of FATIGUE at
    ~48h (the REST buffer buys 16h, the climb crosses 7.5 about 32h
    later): the third night, never sooner. THIRST's twin arithmetic,
    priced for a night the house already spends idle."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Insomniac")
    died_at = None
    for _ in range(80):
        _keep_fed_and_warm(session, seat, ("JERKY", 3, 10), sleep=False)
        _run(session, 1)
        if session.get(Entity, seat.id).status != EntityStatus.ACTIVE:
            died_at = production.next_tick_number(session) - 1
            break
    assert died_at is not None and 40 <= died_at <= 60, died_at
    kills = [e for e in _events(session, "entity_incapacitated")
             if e.get("entity_id") == seat.id]
    assert kills[-1]["condition"] == "FATIGUE"
    # the impaired floor is crossed on the way (the tired fight worse:
    # -1/-1 while FATIGUE rides at or above 5 -- the estate burn
    # zeroes the register at death, so assert it on the living)
    from econengine import combat as combat_mod
    tired = _seat(session, "Tired")
    markets.adjust_holding(session, tired, "FATIGUE", Decimal("4"))
    assert combat_mod.effective_attack(session, tired.id) == Decimal("1")
    markets.adjust_holding(session, tired, "FATIGUE", Decimal("1"))  # at 5
    assert combat_mod.effective_attack(session, tired.id) == Decimal("0")
    assert combat_mod.effective_defense(session, tired.id) == Decimal("0")


def test_sleep_by_fire_pays_both_clocks(session):
    """SLEEP_BY_FIRE is the duration-1 watch (P4): an hour by a lit
    commons fire credits REST +1 and WARMTH +3 -- the fire does double
    duty, covering the night's 3/hour draw exactly while it burns
    (the stock holds, nothing banks). A dark fire warms no sleeper.
    """
    from econengine import parcels as parcels_mod
    create_content(session)
    w = _seat(session, "Watcher")
    _set(session, w, "WARMTH", Decimal("6"))
    # the watch hour: outputs land at the NEXT tick's top (t1 runs,
    # t2 completes) -- the first sleeping hour's draw precedes its
    # credit, the seam every real watch repeats hour by hour
    production.start_process(session, w, "SLEEP_BY_FIRE")   # commons fire
    _run(session, 2)
    assert _hold(session, w.id, "REST") == Decimal("4.5")    # +1, -0.5
    # warmth fades a fifth an hour: 6 ->3 ->x0.8 ->2.4, +3 -3, ->1.92
    assert _hold(session, w.id, "WARMTH") == Decimal("1.92")
    assert _hold(session, w.id, "EXPOSURE") == Decimal("0")
    # the lit gate: a dark fire refuses the sleeper like the sitter
    for fire in session.execute(select(parcels_mod.Facility)).scalars():
        if fire.facility_type == "FIRE":
            fire.fuel = Decimal("0")
    session.commit()
    with pytest.raises(Exception, match="dark"):
        production.start_process(session, w, "SLEEP_BY_FIRE")


def test_the_comfort_ladder_shelter_and_bed_sleep(session):
    """The ladder's rungs (P4): shelter sleeps leaky-warm (REST+1,
    WARMTH+2, survivable misery), a bed under a roof sleeps deep
    (REST+1.5, WARMTH+3 -- recovery half again as fast, the inert
    hook pays off), and the den is the beasts' alone: a house reading
    the catalog cannot curl up in it."""
    create_content(session)
    w = _seat(session, "Sleeper")
    camp = _camp(session, w)
    with pytest.raises(Exception, match="no free SHELTER"):
        production.start_process(session, w, "SLEEP_SHELTERED", camp.id)
    with pytest.raises(Exception, match="no free SHELTER"):
        production.start_process(session, w, "SLEEP_IN_BED", camp.id)
    parcels.add_facility(session, camp, "SHELTER")
    with pytest.raises(Exception, match="BED"):
        production.start_process(session, w, "SLEEP_IN_BED", camp.id)
    # shelter rung: the hour lands at the next tick's top (2-tick
    # arithmetic -- see the fire test)
    _set(session, w, "REST", Decimal("1"))
    _set(session, w, "WARMTH", Decimal("6"))
    production.start_process(session, w, "SLEEP_SHELTERED", camp.id)
    _run(session, 2)
    assert _hold(session, w.id, "REST") == Decimal("1.5")    # +1, -0.5
    # +2 credit against the 3-draw, warmth fading a fifth an hour:
    # leaky by design -- but never unmet while the bank lasts
    assert Decimal("1") < _hold(session, w.id, "WARMTH") < Decimal("3")
    assert _hold(session, w.id, "EXPOSURE") == Decimal("0")
    # bed rung: +1.5 rest an hour
    markets.adjust_holding(session, w, "BED", Decimal("1"))
    _set(session, w, "REST", Decimal("1"))
    production.start_process(session, w, "SLEEP_IN_BED", camp.id)
    _run(session, 2)
    assert _hold(session, w.id, "REST") == Decimal("2.0")    # +1.5, -0.5
    # the den is born, not learned: the CARNIVORE's alone
    with pytest.raises(ValueError, match="requires CARNIVORE"):
        production.start_process(session, w, "SLEEP_DEN")


def test_wolf_dens_at_night_and_pays_prowl_with_rest(session):
    """The beasts sleep too (P4): a fed wolf dens through the night
    (SLEEP_DEN: REST+1, WARMTH+2 -- the CARNIVORE's watch), and the
    spawn templates carry the born buffer. The pack that prowls every
    dark instead grows tired: prowling has a price."""
    create_content(session)
    wolf = next(e for e in session.execute(select(Entity)).scalars()
                if e.name.startswith("Wolf Pack"))
    assert _hold(session, wolf.id, "REST") == Decimal("4")
    from econengine import spawns
    boar = spawns.spawn_one(session, "Wild Boar Test", {
        "entity_type": "individual",
        "stats": {"ATTACK": 4, "DEFENSE": 2, "HITS": 12},
        "holdings": {"MEAT": 6, "PELT": 2, "WATER": 2,
                     "REST": float(stone_age.REST_BEAST_BUFFER)},
        "script_setting": "boar",
        "account": {"COIN": 0},
        "place": "THICKET",
        "technologies": ["CARNIVORE"],
    })
    assert _hold(session, boar.id, "REST") == Decimal("4")
    # a fed pack dens: run the wolf's program through a night with the
    # stomach kept full (hunger outranks sleep -- see the block order)
    markets.adjust_holding(session, wolf, "SATIETY", Decimal("6"))
    markets.adjust_holding(session, wolf, "REST", Decimal("2"))
    for _ in range(4):
        markets.adjust_holding(session, wolf, "SATIETY",
                               Decimal("6") - _hold(session, wolf.id, "SATIETY"))
        _run(session, 1)                 # ticks 1..4: the deep of night
    assert _hold(session, wolf.id, "REST") > Decimal("2")   # it slept
    assert _hold(session, wolf.id, "FATIGUE") == Decimal("0")


def test_an_attack_wakes_the_sleeper(session):
    """Wolves counter sleep (P4): the pack declares the sleep_recipes
    convention, and any resolved attempt on a sleeper -- the deterred
    bark at the door included -- cancels their running sleep hour.
    A sleeper by the fire holds little WARMTH (the seat refills the
    stock; the sleep only covers the draw), so deterrence does not
    cover them: the watch's price is vigilance's absence."""
    create_content(session)
    w = _seat(session, "Sleeper")
    _run(session, 1)
    wolf = next(e for e in session.execute(select(Entity)).scalars()
                if e.name.startswith("Wolf Pack"))
    _at(session, wolf, "HEARTH")        # up close (S4)
    session.commit()
    from econengine import combat as combat_mod
    assert combat_mod.get_rules(session)["sleep_recipes"] == ["SLEEP_*"]
    sleep = production.start_process(session, w, "SLEEP_BY_FIRE")
    from econengine.models import ProcessStatus
    ev = combat_mod.resolve_attack(session, wolf.id, w.id, 2)   # night
    assert ev.get("interrupted") == ["SLEEP_BY_FIRE"]
    assert sleep.status == ProcessStatus.CANCELLED


def test_the_starter_sleeps_the_night_shift(session):
    """The inherited floor now sleeps (P4): by a lit commons fire when
    the body wants rest (the watch emerges -- sleep ticks, stoke
    between them), seated when merely cold. The 40-tick window is the
    suite's operational "indefinitely" (test_starter_survives) -- and
    the commons fire is a SHARED cost: a lone floor house rides tired
    once the genesis fuel burns (gather rolls ~1 wood a day against a
    14-hour night), exactly the pressure that should pull houses to
    keep one fire, together. Under it: alive, and the clock under its
    killing threshold."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Floor")
    session.add(Script(
        name=f"starter-behaviour-{w.id}",
        script_type=ScriptType.BEHAVIOUR,
        source=stone_age._gate_pack_script(stone_age.STARTER),
        entity_id=w.id,
        timeout_ms=200,
        state={},
    ))
    session.commit()
    # night one, fire lit: the watch works -- REST climbs past born
    _run(session, 3)
    assert _hold(session, w.id, "REST") > Decimal("4")
    _run(session, 37)
    assert session.get(Entity, w.id).status == EntityStatus.ACTIVE
    assert _hold(session, w.id, "FATIGUE") < Decimal("7.5")   # under death
    sleeps = [e for e in _events(session, "process_completed")
              if e.get("entity_id") == w.id
              and e.get("recipe") == "SLEEP_BY_FIRE"]
    assert sleeps                          # the floor sleeps, not idles


def test_the_post_shelves_a_bed(session):
    """The comfort ladder's storefront (P4): one bed on the shelf at
    5.00 -- priced between \"build it yourself\" (1 LABOR + 2 WOOD +
    3 YARN) and \"sleep rough\". When it is gone, it is gone: capital
    that pays every night forever."""
    create_content(session)
    assert stone_age.POST_FOOD["BED"] == Decimal("1")
    post = _post(session)
    assert _hold(session, post.id, "BED") == Decimal("1")
    _run(session, 1)
    mid = {m.id: m.symbol for m in session.execute(select(Market)).scalars()}
    sells = {mid[o.market_id]: o for o in _open_orders(session, post.id,
                                                       side=OrderSide.SELL)}
    assert sells["BED"].limit_price == Decimal("5.00")
    assert sells["BED"].quantity == Decimal("1")
