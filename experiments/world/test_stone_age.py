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
    Account, Base, Entity, EntityStatus, EntityType, Holding, Market,
    MarketEscrow, Order, OrderSide, OrderStatus, Place, Script, ScriptType,
    Tick,
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
    """Total stock: pantry plus the entity's own delivered shelf (S5b).
    The post's counter splits back room and front shelf; both are its
    stock, and both rot where the good rots."""
    from sqlalchemy import func as _func
    qty = session.execute(
        select(Holding.quantity)
        .where(Holding.entity_id == entity_id, Holding.symbol == symbol)
    ).scalar_one_or_none()
    shelf = session.execute(
        select(_func.coalesce(_func.sum(MarketEscrow.quantity), 0))
        .where(MarketEscrow.seller_entity_id == entity_id,
               MarketEscrow.symbol == symbol)
    ).scalar_one()
    return (qty if qty is not None else Decimal("0")) + Decimal(shelf)


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
        "BED", "PELT", "WATERSKIN", "SALT", "IBEX_PELT"}
    assert all(m.currency == COIN for m in rows)
    # the far book (P3): the lick's pelt market sits beyond the ridge,
    # and ONLY there -- every other market stays at the post
    lick = session.execute(select(Place).where(Place.key == "SALT_LICK")
                           ).scalar_one()
    post = session.execute(select(Place).where(Place.key == "POST")
                           ).scalar_one()
    by = {m.symbol: m for m in rows}
    assert by["IBEX_PELT"].place_id == lick.id
    for sym in ("SALT", "FLINT", "CHICKEN", "PELT"):
        assert by[sym].place_id == post.id


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
    # the craft: flint + wood + yarn + an afternoon (the KNAPPING rung
    # granted outright -- the ladder's own tests climb it honestly)
    from econengine import tech
    tech.grant_unlock(
        session, w, tech.get_technology(session, "KNAPPING"),
        production.next_tick_number(session))
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


def test_forage_pot_feeds_the_fragment_basket(session):
    """Run 46's deadlock, dissolved: Lagertha starved holding 0.8
    berries + 0.7 apples + 0.6 meat -- every kind under its own meal's
    threshold, and no model invents mixing. The pot boils half of each
    staple into a real meal: thinner per unit than the right kind whole
    (the ladder keeps its point), safe where raw is not."""
    create_content(session)
    _no_wolves(session)
    w = _biz(session, "Basketcase")   # no needs: SATIETY lands and stays
    _grant_chain(session, w, "KNAPPING", "FIRE_HARDENING", "POTTERY")
    for sym, qty in (("BERRIES", "0.8"), ("APPLES", "0.7"), ("MEAT", "0.6")):
        markets.adjust_holding(session, w, sym, Decimal(qty))
    # no single-kind meal is affordable: the basket fits no recipe...
    for code in ("EAT_BERRIES", "EAT_APPLES", "EAT_COOKED", "EAT_JERKY",
                 "EAT_RAW"):
        with pytest.raises(markets.InsufficientHoldingsError):
            production.start_process(session, w, code)
    # ...but the pot takes it: half of each staple, boiled
    assert _act(session, w, "FORAGE_POT")
    assert _hold(session, w.id, "BERRIES") == Decimal("0.3")
    assert _hold(session, w.id, "APPLES") == Decimal("0.2")
    assert _hold(session, w.id, "MEAT") == Decimal("0.1")
    assert _hold(session, w.id, "SATIETY") == Decimal("2.4")
    assert _hold(session, w.id, "WATER") == Decimal("0.4")
    # the boil makes raw meat safe: no disease ever lands from the pot
    # (unbranched recipe -- nothing to roll; EAT_RAW's table is the only
    # meal that carries the risk)
    assert _hold(session, w.id, "DISEASE") == Decimal("0")


def test_the_starter_boils_the_fragment_basket(session):
    """The lever has to be REACHABLE: models never invent the mixed
    meal, so the inherited starter boils the pot itself when no single
    kind covers its own recipe -- the exact run-46 basket feeds, and
    the raw-meat last resort stays below the pot (2.4 safe beats 0.6
    at one-in-four sick)."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Fragment")
    _grant_chain(session, seat, "KNAPPING", "FIRE_HARDENING", "POTTERY")
    session.add(Script(
        name=f"starter-behaviour-{seat.id}",
        script_type=ScriptType.BEHAVIOUR,
        source=stone_age._gate_pack_script(stone_age.STARTER),
        entity_id=seat.id,
        timeout_ms=200,
        state={},
    ))
    # strip the seed larder down to the run-46 deadlock basket
    markets.adjust_holding(session, seat, "BERRIES",
                           Decimal("0.8") - BERRY_BUFFER)
    markets.adjust_holding(session, seat, "APPLES", Decimal("0.7"))
    markets.adjust_holding(session, seat, "MEAT", Decimal("0.6"))
    session.commit()
    _run(session, 1)
    # the pot boiled on the very first hungry tick: half of each staple
    # eaten (the rest is decay's), 2.4 satiety landed and the hour's
    # draw (0.5 + a tenth of what's left) left 1.71 -- the exact
    # run-46 basket feeds instead of starving
    assert _hold(session, seat.id, "SATIETY") == Decimal("1.71")
    assert _hold(session, seat.id, "BERRIES") < Decimal("0.5")
    assert _hold(session, seat.id, "APPLES") < Decimal("0.4")
    assert _hold(session, seat.id, "MEAT") < Decimal("0.2")
    assert _events(session, "script_error") == []
    assert session.get(Entity, seat.id).status == EntityStatus.ACTIVE


def test_the_starter_fishes_the_bank_when_the_meat_shelf_thins(session):
    """Run 47's lever, leg one: the world's meat never existed (the
    pot sat cold, the post bid 5.00 into an empty market all day), so
    the inherited floor now fishes while it stands at the bank with a
    thin meat shelf -- the drink errand doubles as the catch."""
    create_content(session)
    _no_wolves(session)
    _to_tick(session, 8)                 # hour 7, day 1: daylight at the bank
    seat = _starter_seat(session, "Fisher")
    _at(session, seat, "RIVER")
    for sym in ("MEAT", "COOKED_MEAT", "JERKY"):
        _set(session, seat, sym, 0)
    _set(session, seat, "WATER", 4)     # the cup is full: fish, not drink
    _set(session, seat, "WARMTH", 7)
    _set(session, seat, "SATIETY", 4)   # fed: the fish branch, not the meal
    session.commit()
    _run(session, 1)
    assert any(e["entity_id"] == seat.id
               and e.get("params", {}).get("recipe") == "FISH"
               for e in _events(session, "start_process"))
    assert _events(session, "script_error") == []


def test_the_starter_hafts_the_spear_rung(session):
    """Leg two: the floor's one rung of capital. A camp that has the
    materials and wood to spare spends an afternoon hafting -- the
    spear is the protein walk's key, held never spent."""
    create_content(session)
    _no_wolves(session)
    _to_tick(session, 8)                 # hour 7: three honest hours fit
    seat = _starter_seat(session, "Haft")
    from econengine import tech
    tech.grant_unlock(
        session, seat, tech.get_technology(session, "KNAPPING"),
        production.next_tick_number(session))  # the rank: its climb has
    _set(session, seat, "FLINT", 1)      # its own tests (the crafts ladder)
    _set(session, seat, "WOOD", 6)      # two for the spear, the fire keeps its
    _set(session, seat, "YARN", 1)
    _set(session, seat, "WARMTH", 7)
    _set(session, seat, "SATIETY", 4)   # the haft, not the meal
    session.commit()
    _run(session, 4)                     # a stoke may take a tick first
    assert any(e["entity_id"] == seat.id
               and e.get("params", {}).get("recipe") == "MAKE_SPEAR"
               for e in _events(session, "start_process"))
    assert _hold(session, seat.id, "SPEAR") == Decimal("1")
    assert _events(session, "script_error") == []


def test_the_starter_hunts_the_deep_forest_with_the_spear(session):
    """Leg three: a speared house with a thin meat shelf walks the
    deep forest's table -- the floor hunts meat INTO the world, which
    is the whole lever (every run-46/47 death ran through the empty
    protein shelf)."""
    create_content(session)
    _no_wolves(session)
    _to_tick(session, 9)                 # hour 8: the circuit fits in the day
    seat = _starter_seat(session, "Hunter")
    _at(session, seat, "FOREST")
    _set(session, seat, "SPEAR", 1)
    for sym in ("MEAT", "COOKED_MEAT", "JERKY", "BERRIES", "APPLES"):
        _set(session, seat, sym, 0)
    _set(session, seat, "WOOD", 0)
    _set(session, seat, "WARMTH", 7)
    _set(session, seat, "SATIETY", 4)   # starving on purpose, not eating now
    session.commit()
    _run(session, 1)
    assert any(e["entity_id"] == seat.id
               and e.get("params", {}).get("recipe") == "HUNT_SPEAR"
               for e in _events(session, "start_process"))
    assert _events(session, "script_error") == []


def test_a_fed_shelf_pauses_the_spear(session):
    """The hunt is a NEED, not a habit: a full vegetarian shelf does
    not spend the day walking -- the house goes back to gathering."""
    create_content(session)
    _no_wolves(session)
    _to_tick(session, 9)
    seat = _starter_seat(session, "Fed")
    _at(session, seat, "FOREST")
    _set(session, seat, "SPEAR", 1)
    _set(session, seat, "BERRIES", 8)
    for sym in ("MEAT", "COOKED_MEAT", "JERKY"):
        _set(session, seat, sym, 0)
    _set(session, seat, "WOOD", 0)
    _set(session, seat, "WARMTH", 7)
    _set(session, seat, "SATIETY", 4)   # fed: the walk is a gather, not a meal
    session.commit()
    _run(session, 3)                     # the 2h road to the thicket
    assert not any(e["entity_id"] == seat.id
                   and e.get("params", {}).get("recipe") == "HUNT_SPEAR"
                   for e in _events(session, "start_process"))
    assert any(e["entity_id"] == seat.id and e["place"] == "THICKET"
               for e in _events(session, "travel_arrived"))


def test_a_late_spear_does_not_walk_the_dark_road(session):
    """The dusk discipline survives the new errand: at hour 17 the
    hunt window is SHUT -- no HUNT_SPEAR starts, and the house prices
    the walk home from where it stands (the forest is two hours, not
    the thicket's one) and is firelit by dark."""
    create_content(session)
    _no_wolves(session)
    _to_tick(session, 18)                # hour 17, day 1: window shut
    seat = _starter_seat(session, "Late")
    _at(session, seat, "FOREST")
    _set(session, seat, "SPEAR", 1)
    for sym in ("MEAT", "COOKED_MEAT", "JERKY", "BERRIES", "APPLES"):
        _set(session, seat, sym, 0)
    _set(session, seat, "WOOD", 0)
    _set(session, seat, "WARMTH", 7)
    _set(session, seat, "SATIETY", 4)
    session.commit()
    _run(session, 4)                     # the 2h road home lands at hour 19
    assert not any(e["entity_id"] == seat.id
                   and e.get("params", {}).get("recipe") == "HUNT_SPEAR"
                   for e in _events(session, "start_process"))
    assert any(e["entity_id"] == seat.id and e["place"] == "HEARTH"
               for e in _events(session, "travel_arrived"))
    assert session.get(Entity, seat.id).status == EntityStatus.ACTIVE


def test_the_rancher_basket_boils_the_egg_pot(session):
    """Run 47, Lagertha's death: four hens lay ~9.6 satiety a day
    against a 12-a-day stomach -- ranching eaten whole could not
    out-climb the draw. The egg pot stretches the flock: the exact
    run-47 rancher basket (fragments + eggs, no meat) boils as thick
    as the hunted meal."""
    create_content(session)
    _no_wolves(session)
    seat = _starter_seat(session, "Rancher")
    _grant_chain(session, seat, "KNAPPING", "FIRE_HARDENING", "POTTERY")
    markets.adjust_holding(session, seat, "BERRIES",
                           Decimal("0.8") - BERRY_BUFFER)
    markets.adjust_holding(session, seat, "APPLES", Decimal("0.7"))
    markets.adjust_holding(session, seat, "EGGS", Decimal("0.6"))
    _set(session, seat, "MEAT", 0)
    session.commit()
    _run(session, 1)
    pots = [e for e in _events(session, "start_process")
            if e["entity_id"] == seat.id
            and e.get("params", {}).get("recipe")
            in ("FORAGE_POT", "FORAGE_POT_EGGS")]
    assert pots and pots[0].get("params", {}).get("recipe") == "FORAGE_POT_EGGS"
    assert _hold(session, seat.id, "SATIETY") == Decimal("1.71")
    assert _hold(session, seat.id, "EGGS") < Decimal("0.2")
    assert _events(session, "script_error") == []
    assert session.get(Entity, seat.id).status == EntityStatus.ACTIVE


def test_the_pot_takes_the_meat_leg_first(session):
    """Meat outranks eggs at the pot: the spear's kill feeds the boil
    before the flock's laying does (the egg leg is the SHORT -- the
    rancher's substitute, not the preference)."""
    create_content(session)
    _no_wolves(session)
    seat = _starter_seat(session, "Both")
    _grant_chain(session, seat, "KNAPPING", "FIRE_HARDENING", "POTTERY")
    markets.adjust_holding(session, seat, "BERRIES",
                           Decimal("0.8") - BERRY_BUFFER)
    markets.adjust_holding(session, seat, "APPLES", Decimal("0.7"))
    markets.adjust_holding(session, seat, "MEAT", Decimal("0.6"))
    markets.adjust_holding(session, seat, "EGGS", Decimal("0.6"))
    session.commit()
    _run(session, 1)
    pots = [e for e in _events(session, "start_process")
            if e["entity_id"] == seat.id
            and e.get("params", {}).get("recipe")
            in ("FORAGE_POT", "FORAGE_POT_EGGS")]
    assert pots and pots[0].get("params", {}).get("recipe") == "FORAGE_POT"
    # untouched by the meal: 0.57 = 0.6 less the egg's own week-keeping
    # decay (5%/tick) -- the boil spent the MEAT leg, not the flock's
    assert _hold(session, seat.id, "EGGS") == Decimal("0.57")


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
    assert stoke.facility_fuel_output == Decimal("4")
    assert stoke.duration_ticks == 0 and not stoke.requires_daylight
    warm = production.get_recipe(session, "WARM_BY_FIRE")
    warm_out = {o.symbol: o.quantity for o in warm.outputs}
    assert warm_out["WARMTH"] == WARMTH_CAP and warm.requires_facility_lit
    assert warm_out["COMFORT"] == Decimal("0.5")   # P2: a fire hour is a nice hour
    for code in ("COOK_MEAT", "SMOKE_MEAT"):
        assert production.get_recipe(session, code).requires_facility_lit
    from econengine.goods import get_good
    assert get_good(session, "WARMTH").max_holding == WARMTH_CAP


def test_stoke_burns_and_goes_dark(session):
    """The fuel cycle: the bank burns one an hour whether anyone sits,
    runout is a facility_dark fact, a dark fire warms no one, and a log
    relights it (1 WOOD = 4 hours -- a full bank refuses more)."""
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
    assert _fuel(session) == Decimal("4")
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


def _to_tick(session, n):
    """Advance the world to tick N's boundary (ticks 1..N-1 run on the
    way; a seat created after the call wakes with full buffers -- the
    hunt tests want a chosen HOUR, not a fast-forwarded larder)."""
    while production.next_tick_number(session) != n:
        run_tick(session)
        session.commit()


def _starter_seat(session, name):
    """A house running the pack's inherited STARTER, gate-packed the
    way genesis installs it."""
    seat = _seat(session, name)
    session.add(Script(
        name=f"starter-behaviour-{seat.id}",
        script_type=ScriptType.BEHAVIOUR,
        source=stone_age._gate_pack_script(stone_age.STARTER),
        entity_id=seat.id,
        timeout_ms=200,
        state={},
    ))
    session.flush()
    return seat


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
    # the post's own books price the opening book: retail =
    # genesis basis x 1.30 (JERKY 0.90 -> 1.17, BERRIES 0.50 -> 0.65,
    # CHICKEN 3.00 -> 3.90, WATERSKIN 4.50 -> 5.85)
    assert by_sym[("sell", "JERKY")].limit_price == Decimal("1.17")
    assert by_sym[("sell", "BERRIES")].limit_price == Decimal("0.65")
    assert by_sym[("sell", "BERRIES")].quantity == Decimal("60")
    assert by_sym[("sell", "COOKED_MEAT")].limit_price == Decimal("1.50")
    # the larder's seed stock (P5): two hens on the shelf at the ask
    assert by_sym[("sell", "CHICKEN")].limit_price == Decimal("3.90")
    assert by_sym[("sell", "CHICKEN")].quantity == Decimal("2")
    # bids on every raw good the larder allows, at half the genesis
    # basis -- never above 0.75 x basis, because paying near retail
    # for stock is how a shop dies (run 49's 5.00 egg bid, which
    # drained the purse into eggs it could not resell, is dead)
    for sym in ("MEAT", "WOOD", "YARN", "FLINT", "APPLES", "EGGS", "PELT"):
        assert by_sym[("buy", sym)].quantity == Decimal("4")
    assert ("buy", "BERRIES") not in by_sym   # 60 held > the larder's 20
    assert by_sym[("buy", "MEAT")].limit_price == Decimal("0.50")
    assert by_sym[("buy", "YARN")].limit_price == Decimal("0.75")
    assert by_sym[("buy", "EGGS")].limit_price == Decimal("0.35")
    assert by_sym[("buy", "APPLES")].limit_price == Decimal("0.35")
    # the backdoor is off: no errand left, no coin leaves the purse --
    # the 60-berry overstock just sits (there is no dump door), and
    # only rot walks it down
    started = [e for e in _events(session, "start_process")
               if e.get("entity_id") == post.id]
    assert started == []
    assert _hold(session, post.id, "BERRIES") == Decimal("51")  # 60 - rot
    acc = next(a for a in post.accounts if a.currency == COIN)
    assert acc.balance == stone_age.POST_COIN


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
    """Demand moves the retail ask up 5% the tick after a fill."""
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
    assert Decimal(str(trades[-1]["price"])) == Decimal("1.17")
    assert buyer.accounts[0].balance == SEAT_COIN - Decimal("2.34")
    _run(session, 1)                    # the post reads its fill
    ask = _open_orders(session, post.id, "JERKY", OrderSide.SELL)
    assert [o.limit_price for o in ask] == [Decimal("1.23")]   # 1.17 x 1.05


def test_post_bid_falls_when_supply_arrives(session):
    """Sellers filling the bid is supply: the bid eases 5% next tick."""
    create_content(session)
    post, seller = _post(session), _biz(session, "Seller")
    _at(session, seller, "POST")  # orders fill only where the book lives
    markets.adjust_holding(session, seller, "WOOD", Decimal("10"))
    _run(session, 1)
    markets.place_order(session, seller.id, "WOOD", "sell",
                        Decimal("3"), Decimal("0.50"),
                        next(a.id for a in seller.accounts if a.currency == COIN))
    _run(session, 1)                    # the fill (the purse bids 4)
    filled = _hold(session, post.id, "WOOD") - Decimal("6")
    assert filled >= Decimal("2")
    assert next(a for a in seller.accounts if a.currency == COIN).balance \
        == SEAT_COIN + filled * Decimal("0.50")
    _run(session, 1)                    # the post reads its fill
    bids = _open_orders(session, post.id, "WOOD", OrderSide.BUY)
    assert [o.limit_price for o in bids] == [Decimal("0.47")]   # 0.50 x 0.95


def test_post_prices_drift_toward_trade_when_quiet(session):
    """3 LIVE ticks with no fills: the ask eases down 5%, the bid creeps
    up 3%. Quiet is counted from the tick after placement (an order
    cannot be quiet before it exists), so the drift lands on tick 4."""
    create_content(session)
    post = _post(session)
    _run(session, 3)
    # three ticks of resting, only two aged: prices hold
    ask = _open_orders(session, post.id, "BERRIES", OrderSide.SELL)
    assert [o.limit_price for o in ask] == [Decimal("0.65")]
    _run(session, 1)
    ask = _open_orders(session, post.id, "BERRIES", OrderSide.SELL)
    assert [o.limit_price for o in ask] == [Decimal("0.62")]   # 0.65 x 0.95
    bid = _open_orders(session, post.id, "MEAT", OrderSide.BUY)
    assert [o.limit_price for o in bid] == [Decimal("0.52")]   # 0.50 x 1.03


def test_post_never_bids_beyond_its_coin(session):
    """The purse runs out: the coin on hand is split pro-rata across
    every good the post wants -- a lean budget shrinks all bids
    together instead of letting the head of the list eat the coin and
    starve the tail (run 4: MEAT/WOOD bids at the 5.00 cap consumed
    the purse; YARN and FLINT never quoted, so 57 FLINT of surplus
    found no bid)."""
    create_content(session)
    post = _post(session)
    acc = next(a for a in post.accounts if a.currency == COIN)
    acc.balance = Decimal("4")         # too thin to spread 4 each
    _run(session, 1)
    buys = _open_orders(session, post.id, side=OrderSide.BUY)
    committed = sum(o.quantity * o.limit_price for o in buys)
    assert committed <= Decimal("4")
    mid = {m.id: m.symbol for m in session.execute(select(Market)).scalars()}
    by_sym = {mid[o.market_id]: o for o in buys}
    # the cheapest goods get one unit apiece (APPLES 0.35 + EGGS 0.35
    # + MEAT 0.50 + WOOD 0.50 + YARN 0.75 + FLINT 0.75 + SALT 0.75
    # (P3: the lick is open, the counter bids) = 3.95 <= 4); PELT's
    # 1.50 does not fit -- and NO good hoards the purse
    assert set(by_sym) == {"APPLES", "EGGS", "MEAT", "WOOD",
                           "YARN", "FLINT", "SALT"}
    assert all(o.quantity == Decimal("1") for o in buys)


def test_post_dark_bids_freeze_instead_of_drifting(session):
    """A bid that cannot afford to stand (purse drained dry) goes
    DARK -- and a dark order does not drift. Run 4 walked dark bids
    to the 5.00 cap for nothing; the frozen price returns the moment
    coin does."""
    create_content(session)
    post = _post(session)
    acc = next(a for a in post.accounts if a.currency == COIN)
    acc.balance = Decimal("0.10")      # nothing to stand a bid with
    _run(session, 8)
    assert _open_orders(session, post.id, side=OrderSide.BUY) == []
    state = _post_script(session).state or {}
    assert Decimal(str(state["bid"]["MEAT"])) == Decimal("0.50")  # frozen
    assert Decimal(str(state["bid"]["YARN"])) == Decimal("0.75")
    # coin returns: the bids come back at the frozen prices
    acc.balance = Decimal("12")
    _run(session, 1)
    mid = {m.id: m.symbol for m in session.execute(select(Market)).scalars()}
    buys = {mid[o.market_id]: o for o in
            _open_orders(session, post.id, side=OrderSide.BUY)}
    assert Decimal(str(buys["MEAT"].limit_price)) == Decimal("0.50")
    # every want now: 12 coin against a ~22.8-coin want -- every bid
    # comes back, pro-rated to two units each (BERRIES too: rot alone
    # walked the 60-berry hoard under the larder's 20, so its
    # appetite is back -- there is no dump door to do it faster; and
    # SALT since P3 -- the counter bids on what can be dug)
    assert set(buys) == {"MEAT", "WOOD", "YARN", "FLINT", "SALT",
                         "BERRIES", "APPLES", "EGGS", "PELT"}
    assert all(o.quantity == Decimal("2") for o in buys.values())


def test_the_wholesale_door_is_off(session):
    """Run 52's closed economy, asserted: no WHOLESALE_* recipes exist
    (no caravan, no dump mint, no famine rungs -- the Lua reads none
    either), the shop's own errands stand (KEEP_SHOP_* and SALT_MEAT
    are the back room's whole work), and the post still quotes both
    sides of the book off its own genesis anchors. Coin only moves
    between houses and the post (plus the thicket's shiny-stone
    mint): a bare shelf stays bare until the forest sells."""
    from pathlib import Path

    create_content(session)
    post = _post(session)
    codes = {r.code for r in session.execute(
        select(production.Recipe)).scalars()}
    assert not any(c.startswith("WHOLESALE_") for c in codes)
    for kept in ("KEEP_SHOP_JERKY", "KEEP_SHOP_EGGS", "KEEP_SHOP_APPLES",
                 "KEEP_SHOP_BERRIES", "SALT_MEAT"):
        assert kept in codes
    lua = (Path(stone_age.__file__).parent / "lua"
           / "trading_post.lua").read_text()
    for gone in ("WHOLESALE", "RESERVE", "rungs", "hills", "caravan"):
        assert gone not in lua, gone
    # a lean shelf is not an errand: strip the larder bare, run a
    # while -- nothing starts, no coin moves, the shelf stays bare
    _no_wolves(session)
    acc = next(a for a in post.accounts if a.currency == COIN)
    for sym, qty in (("JERKY", -POST_FOOD["JERKY"]),
                     ("BERRIES", -POST_FOOD["BERRIES"] + 1),
                     ("COOKED_MEAT", -POST_FOOD["COOKED_MEAT"]),
                     ("EGGS", Decimal("0")), ("APPLES", Decimal("0")),
                     ("CHICKEN", Decimal("2")), ("WATERSKIN", Decimal("1"))):
        markets.adjust_holding(session, post, sym, qty)
    _run(session, 6)
    assert [e for e in _events(session, "start_process")] == []
    assert acc.balance == stone_age.POST_COIN
    assert _hold(session, post.id, "JERKY") == Decimal("0")  # bare stays bare


def test_the_dark_counter_pays_its_keep(session):
    """Upkeep (run 49's honest merchant): one log and one meal a day
    keep SHOP_OPEN lit. No wood and no meal and the counter goes DARK
    -- no quotes, no peddle, one honest say -- and the moment the
    keeper can pay again, the shop reopens."""
    create_content(session)
    _no_wolves(session)
    post = _post(session)
    # burn the bank: no wood, no meal on the shelf, the counter's day
    # nearly spent
    for sym, qty in (("WOOD", Decimal("6")),
                     ("JERKY", -POST_FOOD["JERKY"] + Decimal("1")),
                     ("BERRIES", -POST_FOOD["BERRIES"]),
                     ("COOKED_MEAT", -POST_FOOD["COOKED_MEAT"])):
        markets.adjust_holding(session, post, sym, qty)
    markets.adjust_holding(session, post, "EGGS", Decimal("0"))
    markets.adjust_holding(session, post, "SHOP_OPEN", -Decimal("0.96"))
    _run(session, 1)
    assert _open_orders(session, post.id, side=OrderSide.BUY) == []
    assert _open_orders(session, post.id, side=OrderSide.SELL) == []
    dark_says = [e for e in _events(session, "say")
                 if "dark" in e.get("params", {}).get("text", "")]
    assert dark_says                    # the honest word, said once
    # the keeper pays: a log and the last jerky relight the counter
    markets.adjust_holding(session, post, "WOOD", Decimal("1"))
    _run(session, 3)                    # KEEP_SHOP is 1 tick + reopen
    started = [e["params"]["recipe"] for e in _events(session, "start_process")
               if e.get("status") != "rejected"]
    assert "KEEP_SHOP_JERKY" in started
    assert _hold(session, post.id, "SHOP_OPEN") > Decimal("0.5")  # relit
    assert _open_orders(session, post.id, side=OrderSide.SELL)  # reopened


def test_post_salts_its_meat_into_jerky(session):
    """Run 46's finite faucet: the post sold its last jerky early and
    the market died with the purse. The back room turns bought MEAT
    into the never-rot shelf -- his own racks, no labor, no fire."""
    create_content(session)
    _no_wolves(session)
    post = _post(session)
    before = _hold(session, post.id, "JERKY")
    markets.adjust_holding(session, post, "MEAT", Decimal("6"))
    _run(session, 8)      # batches are 4 ticks, two racks -- 2 land
    done = [e for e in _events(session, "process_completed")
            if e["recipe"] == "SALT_MEAT"]
    assert len(done) == 2                 # two racks, four meat, no more
    for e in done:
        assert Decimal(str(e["outputs"]["JERKY"])) == Decimal("2")
    assert _hold(session, post.id, "JERKY") == before + Decimal("4")
    # what decay did not eat is salted: nothing raw worth a rack
    assert _hold(session, post.id, "MEAT") < Decimal("2")


def test_the_salt_shed_binds_the_post_alone(session):
    """The shed is the post's OWN back room: standing at the POST place
    is not salting at the post. A house holding meat cannot bind an
    OWNER facility -- restocking the counter is the merchant's edge."""
    create_content(session)
    _no_wolves(session)
    house = _seat(session, "Neighbor")
    _at(session, house, "POST")
    # the rank is granted so the FACILITY refusal is this test's
    # subject (the crafts-ladder tests cover the rank gate)
    from econengine import tech
    tech.grant_unlock(
        session, house, tech.get_technology(session, "SALT_CURING"),
        production.next_tick_number(session))
    markets.adjust_holding(session, house, "MEAT", Decimal("4"))
    with pytest.raises(ValueError, match="SALT_SHED"):
        production.start_process(session, house, "SALT_MEAT")
    post = _post(session)
    markets.adjust_holding(session, post, "MEAT", Decimal("2"))
    proc = production.start_process(session, post, "SALT_MEAT")
    assert proc.recipe.code == "SALT_MEAT"


def test_the_genesis_basis_prices_the_opening_book(session):
    """The lua GENESIS_BASIS table is the one anchor set: this parses
    the lua and pins every number, then pins the opening book to it
    (retail = basis x 1.30, bids = half basis) -- drift between the
    table and the quotes is the silent price bug."""
    import re
    from pathlib import Path

    create_content(session)
    src = (Path(stone_age.__file__).parent / "lua"
           / "trading_post.lua").read_text()
    m = re.search(r"local GENESIS_BASIS = \{(.*?)\}", src, re.S)
    assert m
    lua = dict(re.findall(r"(\w+)\s*=\s*([\d.]+)", m.group(1)))
    expected = {"JERKY": "0.90", "BERRIES": "0.50", "APPLES": "0.70",
                "EGGS": "0.70", "CHICKEN": "3.00", "WATERSKIN": "4.50",
                "WOOD": "1.00", "MEAT": "1.00", "YARN": "1.50",
                "FLINT": "1.50", "PELT": "3.00", "SALT": "1.50"}
    assert lua == expected
    _run(session, 1)
    post = _post(session)
    mid = {m.id: m.symbol for m in session.execute(select(Market)).scalars()}
    by = {(("sell" if o.side == OrderSide.SELL else "buy"), mid[o.market_id]): o
          for o in _open_orders(session, post.id, side=OrderSide.SELL)}
    for o in _open_orders(session, post.id, side=OrderSide.BUY):
        by[("buy", mid[o.market_id])] = o
    # retail opens at the margin over genesis basis -- only for the
    # stocked shelf (APPLES and EGGS start bare: an unstocked shelf
    # quotes nothing, and there is no restock but trade)
    for sym in ("JERKY", "BERRIES", "CHICKEN", "WATERSKIN"):
        assert by[("sell", sym)].limit_price \
            == (Decimal(expected[sym]) * Decimal("1.30")).quantize(
                Decimal("0.01"))
    assert ("sell", "APPLES") not in by and ("sell", "EGGS") not in by
    # bids open at half basis (BERRIES holds 60: over the larder max)
    for sym in ("MEAT", "WOOD", "YARN", "FLINT", "APPLES", "EGGS", "PELT"):
        assert by[("buy", sym)].limit_price \
            == (Decimal(expected[sym]) * Decimal("0.5")).quantize(
                Decimal("0.01"))


def test_post_reanchors_jerky_off_its_meat_buys(session):
    """The restock is PRICED off the post's own buy fills: the jerky
    ask follows twice the raw cost the counter actually paid (the
    opening book's 2:1 -- preservation is what a house pays for),
    clamped to the honest band. Seeded prices isolate the rule from
    the haggling its own tests already pin."""
    create_content(session)
    _no_wolves(session)
    post, hunter = _post(session), _biz(session, "Hunter")
    _at(session, hunter, "POST")
    script = _post_script(session)
    script = _post_script(session)
    script.state = {
        "ask": {"COOKED_MEAT": "1.50", "BED": "5.00"},
        "bid": {"BERRIES": "0.25", "MEAT": "0.85", "WOOD": "0.50",
                "YARN": "0.75", "FLINT": "0.75", "PELT": "1.50",
                "APPLES": "0.35", "EGGS": "0.35"},
        "cost": {}, "adj": {},
        "quiet": {}, "live": {}, "ids": {}, "answered": {},
    }
    markets.adjust_holding(session, hunter, "MEAT", Decimal("4"))
    _run(session, 1)                    # t1: the seeded book stands
    markets.place_order(session, hunter.id, "MEAT", "sell",
                        Decimal("2"), Decimal("0.85"),
                        next(a.id for a in hunter.accounts
                             if a.currency == COIN))
    _run(session, 1)                    # t2: the fill -- at 0.85
    _run(session, 1)                    # t3: the post reads its fill
    state = _post_script(session).state
    assert Decimal(str(state["cost"]["MEAT"])) == pytest.approx(Decimal("0.85"))
    # the salt anchor: two raw make two jerky, so the jerky basis IS
    # the meat fill -- retail follows at the margin
    assert Decimal(str(state["cost"]["JERKY"])) == pytest.approx(Decimal("0.85"))
    ask = _open_orders(session, post.id, "JERKY", OrderSide.SELL)
    assert [o.limit_price for o in ask] == [Decimal("1.10")]  # .85 x 1.3


def test_post_jerky_never_rots_and_feeds(session):
    """JERKY is the salted shelf: it does not decay, it feeds EAT_JERKY,
    and the post spawns stocking it -- late coin always has something
    to buy (run 4: OSS died holding 17 COIN beside a rotted-empty
    larder)."""
    create_content(session)
    post = _post(session)
    assert _hold(session, post.id, "JERKY") \
        == Decimal("30")
    # BERRIES and COOKED_MEAT rot beside an untouched JERKY stack
    _run(session, 10)
    assert _hold(session, post.id, "JERKY") \
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
    (start), thicket 1h, river 1h, flint scrape 2h, deep forest 3h,
    post 1h down the valley (run 26's census: at four hours the post
    was a trip houses died taking -- two starved at its counter) --
    the forest road stays the long way round. The old P3 added the
    river road past the thicket: the gather commute drinks, and the
    boar reaches the tap without the fire-ground. River proximity
    (run 40's parked lever, shipped for 48): both residential river
    roads dropped 2h -> 1h. Run 54 P3 adds the FAR VALLEY: the ridge
    road climbs from the scrape -- RIDGE_PASS 2h, then the salt lick
    2h on and the vale 2h down, the vale a single hour off the lick.
    Two valleys, one ridge, opposite scarcities; the far valley's
    only door is the ridge (no road bypasses it), and the hearth is
    six hours from the lick door to door."""
    from econengine import edges, places as places_mod
    create_content(session)
    keys = {p.key: p for p in places_mod.list_places(session)}
    assert set(keys) == {"HEARTH", "THICKET", "RIVER", "FLINT",
                         "FOREST", "POST",
                         "RIDGE_PASS", "SALT_LICK", "VALE"}
    # two valleys: the ridge is the far side's one door
    far = {"RIDGE_PASS", "SALT_LICK", "VALE"}
    assert keys["RIDGE_PASS"].kind == "RIDGE"
    assert keys["SALT_LICK"].kind == "SALT"      # DIG_SALT gates on it
    assert keys["VALE"].kind == "PASTURE"        # HEN_ROLL gates on it
    assert all(p.kind == p.key
               for k, p in keys.items() if k not in far)
    assert {k: p.region_id for k, p in keys.items() if k in far} == {
        k: "far-valley" for k in far}
    assert all(p.region_id == "home-valley"
               for k, p in keys.items() if k not in far)
    roads = {(e.from_place.key, e.to_place.key): e.cost_ticks
             for e in edges.list_edges(session)}
    assert roads == {
        ("HEARTH", "THICKET"): 1,
        ("HEARTH", "RIVER"): 1,
        ("HEARTH", "FLINT"): 2,
        ("HEARTH", "FOREST"): 3,
        ("THICKET", "FOREST"): 2,
        ("FOREST", "POST"): 1,
        ("RIVER", "POST"): 2,
        ("HEARTH", "POST"): 1,
        ("THICKET", "RIVER"): 1,
        ("FLINT", "RIDGE_PASS"): 2,
        ("RIDGE_PASS", "SALT_LICK"): 2,
        ("RIDGE_PASS", "VALE"): 2,
        ("VALE", "SALT_LICK"): 1,
    }


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
    # armed and careful (4/4), with hands -- and his hearth never dies
    # (a real genesis-lit FIRE at the post-place), so wolves are turned
    # at his door (run 47's wall was a WARMTH holding; his is firelight)
    post = [e for e in session.execute(select(Entity)).scalars()
            if e.name == "Trading Post"][0]
    assert combat.is_creature(session, post.id) is True
    assert combat.get_stats(session, post.id)["DEFENSE"] == Decimal("4")
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


def test_the_wall_is_firelight_not_warmth(session):
    """Run 47's rebalance: 73/73 wolf attacks on houses deterred, 0
    damage, all ten packs starved -- WARMTH >= 1 turned packs even at
    DARK camps (sleepers bank warmth by fires that have gone out).
    The wall is the burning fire where you stand: lit camp deters,
    dark camp is biteable however warm the body, and a carried torch
    holds the dark off."""
    from econengine import combat
    create_content(session)
    house = _seat(session, "Sleeper")
    wolf = next(e for e in session.execute(select(Entity)).scalars()
                if e.name.startswith("Wolf Pack"))
    _at(session, wolf, "HEARTH")   # up close (direct engine calls: no
    #                             wolf Lua ever runs -- _no_wolves would
    #                             only incapacitate the attacker)
    # a lit commons fire turns the pack -- with a COLD body (warmth 0):
    # warmth is no longer the wall
    markets.adjust_holding(
        session, house, "WARMTH", -_hold(session, house.id, "WARMTH"))
    ev = combat.resolve_attack(session, wolf.id, house.id, 1)
    assert ev.get("deterred") is True and not ev.get("hit")
    # the fire goes dark: a warm body (warmth 5) at a dark camp is
    # BITEABLE -- the banked heat of a fire that was is nothing to a
    # wolf (this is the exact run-47 shape that made the wall a bug)
    _commons_fire(session)[1].fuel = Decimal("0")
    session.flush()
    markets.adjust_holding(session, house, "WARMTH", Decimal("5"))
    ev = combat.resolve_attack(session, wolf.id, house.id, 2)
    assert not ev.get("deterred")
    # and the carried flame holds the dark off: LIT is the other wall
    markets.adjust_holding(session, house, "LIT_TORCH", Decimal("0.5"))
    ev = combat.resolve_attack(session, wolf.id, house.id, 3)
    assert ev.get("deterred") is True and not ev.get("hit")


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
    # firelight: the burning commons fire turns the wolf at the door
    # (a loud miss) -- banked body warmth means nothing now
    markets.adjust_holding(session, house, "WARMTH", Decimal("5"))
    ev = combat.resolve_attack(session, wolf.id, house.id, 1)
    assert ev["deterred"] is True and ev["hit"] is False
    assert _hold(session, house.id, "HITS") == Decimal("20")
    # the fire dies; the hunt is on. Unarmed house vs wolf: 4 v 1.
    # (One attack per (attacker, defender, tick) is the honest cadence --
    # the RNG seed is the triple -- so the hunt spans the dark hours.)
    _commons_fire(session)[1].fuel = Decimal("0")
    session.flush()
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


def test_kin_get_no_wall_at_a_dark_doorstep(session):
    """The firelight wall's sharp edge (run 47's rebalance): at a DARK
    place, body warmth turns NOTHING -- not even another pack. Two
    wolves co-placed where no fire burns can bite each other (the
    packs' old accidental warmth wall made kin un-biteable; dark
    doorsteps now cull the weak and the winner eats)."""
    from econengine import combat
    create_content(session)
    w1, w2 = [e for e in session.execute(select(Entity)).scalars()
              if e.name.startswith("Wolf Pack")][:2]
    _at(session, w1, "THICKET")      # no FIRE facility there: dark ground
    _at(session, w2, "THICKET")
    markets.adjust_holding(session, w1, "WARMTH", Decimal("6"))  # warm body
    ev = combat.resolve_attack(session, w2.id, w1.id, 22)          # night
    assert not ev.get("deterred")
    assert ev.get("hit") in (True, False)     # the roll happens -- no wall


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
    # isolate ONE pack's raid: the sibling pack is scenery here (the
    # firelight wall's sharp edge -- a co-raiding sibling at a DARK
    # doorstep is no longer turned by body warmth, packs cull each
    # other in the dark now; that behavior has its own test below)
    from econengine.models import EntityStatus
    for e in session.execute(select(Entity)).scalars():
        if e.name.startswith("Wolf Pack") and e.id != wolf.id:
            e.status = EntityStatus.INCAPACITATED
    session.flush()
    # hungry (an empty stomach keeps granting HUNGER): the raid walk
    # begins at dusk -- three hours of road, walked while the light
    # lasts. Run-44's gate change: the raid road opens only to a
    # genuinely starving pack (HUNGER past 5) -- a merely peckish one
    # works the range instead (the hunt feeds better than the fire-
    # ground's flocks). Pin the state so the decision is not a race:
    # a nearly-dry cup at dawn (the water errand departs at first
    # light and is home by noon -- it must not eat the dusk window),
    # a full stomach (no meal tops the cup), and HUNGER set exactly
    # past the bar just before the window opens (the day's hunt dice
    # must not decide the raid).
    markets.adjust_holding(session, wolf, "HUNGER", Decimal("6"))
    markets.adjust_holding(session, wolf, "SATIETY", Decimal("6"))
    markets.adjust_holding(session, wolf, "WATER", Decimal("-1.25"))
    while clock.hour_of(production.next_tick_number(session)) < 16:
        run_tick(session); session.commit()
    h = markets.get_holding(session, wolf.id, "HUNGER").quantity
    markets.adjust_holding(session, wolf, "HUNGER", Decimal("8") - h)
    # dusk falls: out to the hearth, a night of prowling, home by day
    for _ in range(24):
        run_tick(session); session.commit()
    walks = [e["to"] for e in _events(session, "travel")
             if e["entity_id"] == wolf.id and e["status"] == "applied"]
    assert "HEARTH" in walks               # the dusk raid walk
    assert "FOREST" in walks                # and home again by day


def test_the_raid_holds_the_doorstep_and_bites_in_the_dark(session):
    """Run 45's structural bug, fixed: both packs walked the raid road
    and about-faced on the ARRIVAL tick -- the day block's walk-home
    branch fired while the light still stood, the night block never
    saw the fire-ground, and the raid was a six-hour round walk that
    fed nobody (the packs died on the dark road home, of a feud it
    drove them into). Now arrival is arrival: the pack holds the
    fire-ground through the window, bites blind once hunger passes
    the raid's own bar -- 5, not the starving 8 -- and the road home
    waits for day."""
    from econengine import clock
    create_content(session)
    wolf = next(e for e in session.execute(select(Entity)).scalars()
                if e.name.startswith("Wolf Pack"))
    assert wolf.place.key == "FOREST"
    # isolate ONE pack's raid (the sibling is scenery: under the
    # firelight wall a co-raiding sibling at a DARK doorstep is no
    # longer turned by body warmth -- packs cull each other in the
    # dark now, and that behavior has its own test)
    from econengine.models import EntityStatus
    for e in session.execute(select(Entity)).scalars():
        if e.name.startswith("Wolf Pack") and e.id != wolf.id:
            e.status = EntityStatus.INCAPACITATED
    session.flush()
    # the run-44/45 shaping (a decision that is not a race): a full
    # stomach keeps the cup topped, a nearly-dry cup runs its errand
    # at first light (home by noon -- the dusk window stays free)
    markets.adjust_holding(session, wolf, "SATIETY", Decimal("6"))
    markets.adjust_holding(session, wolf, "WATER", Decimal("-1.25"))
    while clock.hour_of(production.next_tick_number(session)) < 15:
        run_tick(session); session.commit()
    # HUNGER set exactly past the raid bar as the window opens
    h = markets.get_holding(session, wolf.id, "HUNGER").quantity
    markets.adjust_holding(session, wolf, "HUNGER", Decimal("6") - h)
    # out to the hearth at dusk, three hours of road
    arrived = None
    for _ in range(30):
        run_tick(session); session.commit()
        if wolf.place.key == "HEARTH":
            arrived = len(_events(session, "travel"))
            break
    assert arrived is not None, "the raid walk never arrived"
    # the doorstep hold: no about-face while the light stands
    while not clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    def _to(e):
        return e.get("to") or (e.get("params") or {}).get("to")
    assert not [e for e in _events(session, "travel")[arrived:]
                if e["entity_id"] == wolf.id and _to(e) == "FOREST"]
    # the bite, armed by the raid's own bar: pin HUNGER at 6 -- between
    # 5 and 8, so only the new fire-ground clause can fire it -- and
    # take one dark tick (applied, deterred, or refused: it bit)
    h = markets.get_holding(session, wolf.id, "HUNGER").quantity
    markets.adjust_holding(session, wolf, "HUNGER", Decimal("6") - h)
    ncomb = len(_events(session, "combat"))
    run_tick(session); session.commit()
    assert [e for e in _events(session, "combat")[ncomb:]
            if e["entity_id"] == wolf.id]
    # the night passes in the yard; the road home is a day matter
    ntrav = len(_events(session, "travel"))
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
        assert not [e for e in _events(session, "travel")[ntrav:]
                    if e["entity_id"] == wolf.id and _to(e) == "FOREST"]
    for _ in range(12):
        if [e for e in _events(session, "travel")[ntrav:]
                if e["entity_id"] == wolf.id and _to(e) == "FOREST"]:
            break
        run_tick(session); session.commit()
    assert [e for e in _events(session, "travel")[ntrav:]
            if e["entity_id"] == wolf.id and _to(e) == "FOREST"]


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
    """The gather arithmetic, asserted from the installed rows: both
    tables carry the orchard branch, the weights sum to 100, and bare
    food income clears ~5.5 satiety-equivalent/hour (run 52's flip:
    runs 50-51 ended in total extinction with every house gathering
    for its life and losing -- now the thicket feeds a house
    bare-handed: food costs ~2.5 of the 14 daylight hours, and the
    surplus that rots in a morning is the trade's raw material).
    The bag still doubles every row: ~10.5 satiety/hour, the
    advantage contract's wide margin."""
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
    assert bare["berries"][1] == {"BERRIES": Decimal("6")}
    assert bare["apples"][1] == {"APPLES": Decimal("5")}
    assert bagged["berries"][1] == {"BERRIES": Decimal("12")}
    assert bagged["apples"][1] == {"APPLES": Decimal("10")}

    def food_ev(rows):
        ev = Decimal("0")
        for label in ("berries", "apples"):
            weight, outs = rows[label]
            qty = Decimal(str(sum(outs.values()))) / Decimal("100")
            ev += weight * qty * Decimal(str(per_hen[label]))
        return ev

    assert food_ev(bare) >= Decimal("5.4")    # the thicket feeds a house
    assert food_ev(bagged) >= Decimal("10.4")  # and the bag doubles down


def test_the_crafts_pay_the_larders_bill(session):
    """Run 39's lever, asserted from the installed rows: the berries
    come back and the crafts pay. The wood-rich donation (berries
    40 -> 30) was the margin, not slack -- run 39 funded the fire
    (36 logs gathered vs 33 stoked, zero exposure deaths) and starved
    the larder: HUNGER d3h16 and d5h03, on ~3.5 gather-hours a day,
    because run 38's 0.771 food sat was an average carried by apples
    and bought jerky. The berry rows return to their famine-era
    weights, the wood branch keeps its quarter (run 38's lever
    stands), and yarn/flint halve -- run 39 was not binding on
    crafts (pens built, torches lit)."""
    create_content(session)
    gather = production.get_recipe(session, "GATHER")
    bag = production.get_recipe(session, "GATHER_BAG")

    def rows(recipe):
        out = {}
        for b in recipe.branches:
            out[b.label] = (b.weight, {o.symbol: o.quantity
                                       for o in b.outputs})
        assert sum(b.weight for b in recipe.branches) == Decimal("100")
        return out

    def ev(table, label):
        weight, outs = table[label]
        return weight * sum(outs.values()) / Decimal("100")

    bare, bagged = rows(gather), rows(bag)
    # the berries are back at the wall-breaking weights
    assert bare["berries"][0] == Decimal("40")
    assert bagged["berries"][0] == Decimal("35")
    # the crafts pay, halved but proportional (the bag keeps the
    # famine-era 8:5 yarn:flint tilt at 5:3)
    assert bare["yarn"] == (Decimal("5"), {"YARN": Decimal("1")})
    assert bare["flint"] == (Decimal("5"), {"FLINT": Decimal("1")})
    assert bagged["yarn"] == (Decimal("5"), {"YARN": Decimal("2")})
    assert bagged["flint"] == (Decimal("3"), {"FLINT": Decimal("2")})
    # 0.10 -> 0.05 each bare-handed, 0.26 -> 0.16 bagged: the
    # toolmaker's walk (DIG_FLINT) and the spinner carry the craft
    # supply now
    assert ev(bare, "yarn") == Decimal("0.05")
    assert ev(bare, "flint") == Decimal("0.05")
    assert ev(bagged, "yarn") == Decimal("0.10")
    assert ev(bagged, "flint") == Decimal("0.06")


def test_the_thicket_funds_the_night(session):
    """Run 38's lever, asserted from the installed rows: the wood roll
    pays for the night. All three houses died of FATIGUE beside a dark
    fire (d10-d12; food 0.771, water 0.950 -- every prior clock quiet)
    because the old tables banked ~0.3 wood per gather against a
    ~10-log night. The branch is now a quarter of every gather, and
    quantity over size -- frequency beats magnitude for famine: a
    six-gather day finds zero wood 18% of the time, not 38%.

    Run 41's lever completes the arithmetic: run 40 funded the food
    (zero hunger deaths) and still died all-FATIGUE d4h19 -- the
    polity stoked 14 times and bought 28 of the ~80 dark fuel-hours,
    nights dark from ~h22. Doubling what a stoked log banks (STOKE
    1 WOOD -> +4 fuel) makes the OBSERVED watch sufficient: the same
    14 stokes buy 56 hours, and the ten dark hours cost a dusk relight
    (2 WOOD) plus two stokes -- 4 WOOD a night."""
    create_content(session)
    gather = production.get_recipe(session, "GATHER")
    bag = production.get_recipe(session, "GATHER_BAG")
    stoke = production.get_recipe(session, "STOKE_FIRE")

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
    # run 41's lever: one stoked log banks half a night
    assert stoke.facility_fuel_output == Decimal("4")
    # the night's price from dark: a dusk relight (2 WOOD = 2 hours)
    # plus two stokes (4 hours each) carries the ten dark hours
    night_price = Decimal("2") + (Decimal("10") - Decimal("2")) / stoke.facility_fuel_output
    assert night_price == Decimal("4")
    # the polity break-even: three houses gathering ~5 apiece bank
    # ~11 logs a day -- the night is paid TWICE over (run 40 died
    # inside the margin; the margin becomes a surplus economy)
    assert 3 * 5 * wood_ev(bare) >= 2 * night_price
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
    # no parcel_id -- the run-48 lesson: seats cannot observe their
    # parcel id, and five explicit-less MAKE_PEN calls bounced across a
    # whole run while the builder auto-bind's facility twin worked.
    # The pen rises on the camp the engine already knows is yours.
    assert _act_day(session, house, "MAKE_PEN")
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
    # the TANNING rung granted outright: this test's subject is the
    # skin itself, not the ladder up to it
    from econengine import tech
    tech.grant_unlock(
        session, house, tech.get_technology(session, "TANNING"),
        production.next_tick_number(session))
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
    off its own books (genesis basis 4.50 x 1.30 = 5.85 -- between "sew
    it yourself" and the egg trade it unlocks). The market exists; the
    shelf holds exactly one; water itself is never sold (the tap is
    free)."""
    create_content(session)
    post = _post(session)
    assert _hold(session, post.id, "WATERSKIN") == Decimal("1")
    _run(session, 2)
    sell = _open_orders(session, post.id, "WATERSKIN", OrderSide.SELL)
    assert len(sell) == 1
    assert Decimal(next(iter(sell)).limit_price) == Decimal("5.85")
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


def test_the_near_river_makes_the_drink_errand_a_morning_stroll(session):
    """River proximity (run 40's parked lever, shipped for run 48): the
    residential river roads dropped 2h -> 1h, so the water walk stops
    pricing a gather day (run 39: the daylight hour itself was the
    binding constraint -- 2h out, drink, 2h home was 5 of 14 hours).
    A dry-cupped house at the hearth walks, drinks, and holds a full
    cup by mid-morning: the bank is an errand, not a pilgrimage."""
    create_content(session)
    _no_wolves(session)
    seat = _seat(session, "Riverside")
    _at(session, seat, "HEARTH")
    session.add(Script(
        name=f"starter-behaviour-{seat.id}",
        source=stone_age._gate_pack_script(stone_age.STARTER),
        script_type=ScriptType.BEHAVIOUR,
        entity_id=seat.id, timeout_ms=200, state={}))
    # dawn first (tick 1 is hour 01 -- the starter sleeps the dark out)
    from econengine import clock
    while clock.is_night(production.next_tick_number(session)):
        _run(session, 1)
    markets.adjust_holding(
        session, seat, "WATER", -_hold(session, seat.id, "WATER"))
    markets.adjust_holding(session, seat, "WATER", Decimal("0.5"))
    markets.adjust_holding(session, seat, "SATIETY", Decimal("4"))
    markets.adjust_holding(session, seat, "WOOD", Decimal("6"))
    session.commit()
    _run(session, 3)          # hour ~6: walk (1h), drink (instant), fed
    walks = {e["to"] for e in _events(session, "travel")
             if e.get("status") == "applied" and e["entity_id"] == seat.id}
    assert "RIVER" in walks                   # the errand rides to the bank
    assert _hold(session, seat.id, "WATER") > Decimal("3")   # cup full


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


def test_the_counter_answers_a_bounced_order(session):
    """Run 45: Lagertha starved broke at a full larder, holding ten
    wood against a shouted bid of five. The book was legible; nobody
    crossed the aisle. Now the merchant answers the customer's own
    move: a buy that bounces for insufficient funds AT his counter --
    the empty purse he watched try to pay, a loud fact now -- gets the
    sell-side quote in speech, broadcast so the bounced house reads
    its own way out. One answer per house per six hours."""
    from econengine import clock
    create_content(session)
    post = _post(session)
    house = _seat(session, "Broke")
    _at(session, house, "POST")            # markets trade where they stand
    acct = session.execute(
        select(Account).where(Account.entity_id == house.id,
                              Account.currency == "COIN")
    ).scalar_one()
    acct.balance = Decimal("0")            # the empty purse
    session.commit()
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    # the shelf is quoted: the ask the broke buy is about to cross
    assert _open_orders(session, post.id, "JERKY", OrderSide.SELL)
    markets.place_order(session, house.id, "JERKY", "buy",
                        Decimal("1"), Decimal("2.00"), acct.id)
    session.commit()
    run_tick(session); session.commit()     # the auction: bounced, loudly
    assert [e for e in _events(session, "order_cancelled")
            if e.get("entity_id") == house.id
            and "insufficient funds" in (e.get("reason") or "")]
    nsays = len(_events(session, "say"))
    for _ in range(3):                      # the feed lands next tick
        run_tick(session); session.commit()
    replies = [e for e in _events(session, "say")[nsays:]
               if e.get("entity_id") == post.id
               and "no coin at my counter" in str(
                   (e.get("params") or {}).get("text"))]
    assert replies                          # the merchant answers


def test_the_counter_answers_a_house_that_speaks(session):
    """Speech becomes two-way: a house that SPEAKS at the counter gets
    the book -- both sides, current prices -- because speech is the
    one channel every house's prompt already reads. The merchant does
    not cold-call; he answers words with words, by day (after dark
    the counter stays quiet: speech is a beacon, wolves hunt by
    ear)."""
    from econengine import clock
    create_content(session)
    post = _post(session)
    house = _seat(session, "Chatty")
    hs = Script(
        name=f"chatty-{house.id}", lineage_id=None,
        description="says at the counter", script_type=ScriptType.BEHAVIOUR,
        source='ctx.action.say("Post -- what will you pay for wood?")',
        is_active=True, timeout_ms=5000, entity_id=house.id,
    )
    session.add(hs)
    session.commit()
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    nsays = len(_events(session, "say"))
    for _ in range(3):
        run_tick(session); session.commit()
    assert [e for e in _events(session, "say")[nsays:]
            if e.get("entity_id") == house.id]        # the house spoke
    assert [e for e in _events(session, "say")[nsays:]
            if e.get("entity_id") == post.id
            and "heard at the counter" in str(
                (e.get("params") or {}).get("text"))]


# ===========================================================================
# THE CRAFTS LADDER (run 54 P1)
# ===========================================================================
# Research is a recipe whose output is an unlock: five ENTITY-scope
# ranks, studied at honest places for honest inputs. The ladder gates
# the CRAFT AISLE only -- subsistence never grows a research
# prerequisite (run 52's doctrine, third rail), and the day-one pin is
# the balance contract: a bare house reaches a craftable spear by the
# end of day 1.

def _has(session, entity_id, code):
    from econengine import tech
    return tech.has_unlock(
        session, entity_id, tech.get_technology(session, code))


def _grant_chain(session, entity, *codes):
    """Grant ranks in ladder order (grant_unlock enforces the DAG:
    FIRE_HARDENING wants KNAPPING under it). For tests whose subject
    is the craft, not the climb -- the ladder has its own tests."""
    from econengine import tech
    tick = production.next_tick_number(session)
    for code in codes:
        tech.grant_unlock(
            session, entity, tech.get_technology(session, code), tick)


def test_the_craft_aisle_is_refused_without_the_rank(session):
    """KNAPPING gates MAKE_SPEAR/MAKE_AXE; FIRE_HARDENING gates
    MAKE_BOW/MAKE_TRAP; TANNING gates MAKE_CLOTHES/MAKE_WATERSKIN --
    and the refusal NAMES the rank, so a seat reading its events learns
    the ladder from the no itself."""
    from econengine import production as prod
    create_content(session)
    house = _seat(session, "Bare Hands")
    # dawn first: the daylight gate raises before the rank gate
    from econengine import clock
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    # the materials are on hand: the refusal is the rank's, not inputs'
    for sym, qty in (("FLINT", 2), ("WOOD", 4), ("YARN", 3), ("PELT", 1),
                     ("BERRIES", 1), ("APPLES", 1), ("MEAT", 1), ("EGGS", 1)):
        markets.adjust_holding(session, house, sym, Decimal(str(qty)))
    _at(session, house, "HEARTH")
    for code, rank in (("MAKE_SPEAR", "KNAPPING"), ("MAKE_AXE", "KNAPPING"),
                       ("MAKE_BOW", "FIRE_HARDENING"),
                       ("MAKE_TRAP", "FIRE_HARDENING"),
                       ("MAKE_CLOTHES", "TANNING"),
                       ("MAKE_WATERSKIN", "TANNING")):
        with pytest.raises(ValueError, match=f"requires {rank}"):
            prod.start_process(session, house, code)
    # and the pot aisle is the potter's alone
    for code in ("FORAGE_POT", "FORAGE_POT_EGGS"):
        with pytest.raises(ValueError, match="requires POTTERY"):
            prod.start_process(session, house, code)


def test_study_grants_the_rank_and_re_study_is_idempotent(session):
    """STUDY_KNAPPING at the scrape: 2 flint and 2 hours, and the
    unlock event lands. A second study runs, eats its flint, and
    grants nothing -- knowledge kept is not knowledge re-earned."""
    create_content(session)
    house = _seat(session, "Student")
    markets.adjust_holding(session, house, "FLINT", Decimal("4"))
    _at(session, house, "FLINT")
    assert _act_day(session, house, "STUDY_KNAPPING")
    _run(session, 3)  # the 2-tick study + the completion tick
    assert _has(session, house.id, "KNAPPING")
    unlocks = [e for e in _events(session, "unlocked")
               if e["entity_id"] == house.id]
    assert [u["technology"] for u in unlocks] == ["KNAPPING"]
    assert unlocks[0]["scope"] == "entity"
    # the craft it gated now starts (materials aside, the rank is held)
    markets.adjust_holding(session, house, "WOOD", Decimal("2"))
    markets.adjust_holding(session, house, "YARN", Decimal("1"))
    _at(session, house, "HEARTH")
    assert _act_day(session, house, "MAKE_SPEAR")
    # re-study: idempotent -- no second grant, no second event (the
    # haft ate one of the four; two more buy the re-run, one survives)
    markets.adjust_holding(session, house, "FLINT", Decimal("2"))
    _at(session, house, "FLINT")
    assert _act_day(session, house, "STUDY_KNAPPING")
    _run(session, 3)
    assert _hold(session, house.id, "FLINT") == Decimal("1")  # it still ate
    assert [e for e in _events(session, "unlocked")
            if e["entity_id"] == house.id
            and e["technology"] == "KNAPPING"]  # still exactly one
    assert len([e for e in _events(session, "unlocked")
                if e["entity_id"] == house.id]) == 1


def test_the_ladder_has_a_prerequisite_chain(session):
    """FIRE_HARDENING wants KNAPPING first, POTTERY wants
    FIRE_HARDENING: the DAG binds research exactly as it binds admin
    grants -- the engine enforces it at STUDY start, naming the rung."""
    from econengine import production as prod
    create_content(session)
    house = _seat(session, "Climber")
    markets.adjust_holding(session, house, "FLINT", Decimal("4"))
    markets.adjust_holding(session, house, "WOOD", Decimal("2"))
    markets.adjust_holding(session, house, "CLAY", Decimal("2"))
    markets.adjust_holding(session, house, "YARN", Decimal("2"))
    markets.adjust_holding(session, house, "PELT", Decimal("1"))
    _at(session, house, "HEARTH")
    with pytest.raises(ValueError,
                       match="technology FIRE_HARDENING requires KNAPPING"):
        prod.start_process(session, house, "STUDY_FIRE")
    _at(session, house, "RIVER")
    with pytest.raises(ValueError,
                       match="technology POTTERY requires FIRE_HARDENING"):
        prod.start_process(session, house, "STUDY_POTTERY")
    # climb the rungs: knapping, then fire (accepted), then pottery
    _at(session, house, "FLINT")
    assert _act_day(session, house, "STUDY_KNAPPING")
    _run(session, 3)
    _at(session, house, "HEARTH")
    assert _act_day(session, house, "STUDY_FIRE")
    _run(session, 3)
    assert _has(session, house.id, "FIRE_HARDENING")
    _at(session, house, "RIVER")
    assert _act_day(session, house, "STUDY_POTTERY")
    _run(session, 3)
    assert _has(session, house.id, "POTTERY")
    # and the pot aisle opens to the house that climbed
    markets.adjust_holding(session, house, "BERRIES", Decimal("1"))
    markets.adjust_holding(session, house, "APPLES", Decimal("1"))
    markets.adjust_holding(session, house, "MEAT", Decimal("1"))
    assert _act(session, house, "FORAGE_POT")


def test_survival_never_grows_a_research_prerequisite(session):
    """The ungated set, pinned: fire, torch, bag, gather, fish, hunt,
    drink, eat -- a bare house with bare materials starts every one,
    no rank asked. If a gate lands here the pack has broken the
    run-52 doctrine (solo survival must not cost research)."""
    from econengine import production as prod
    create_content(session)
    house = _seat(session, "Scavenger")
    for sym, qty in (("WOOD", 4), ("YARN", 3)):
        markets.adjust_holding(session, house, sym, Decimal(str(qty)))
    markets.adjust_holding(session, house, "BERRIES", Decimal("6"))
    for place, code in (("HEARTH", "MAKE_FIRE"), ("HEARTH", "MAKE_TORCH"),
                        ("THICKET", "GATHER"), ("RIVER", "FISH"),
                        ("RIVER", "DRINK"), ("FOREST", "HUNT"),
                        ("HEARTH", "MAKE_BAG")):
        _at(session, house, place)
        assert _act_day(session, house, code), f"{code} refused"
    _at(session, house, "HEARTH")
    assert _act(session, house, "EAT_BERRIES")


def test_the_day_one_ladder_reaches_the_spear(session):
    """The P1 balance pin, from the spec: a bare house -- a day and a
    half of buffer food, nothing else -- walks to the scrape (2h),
    digs flint (1h), studies knapping (2h): the spear is CRAFTABLE by
    the end of day 1 (tick 24). The rank must never be a treadmill."""
    from econengine import travel as travel_mod
    create_content(session)
    house = _seat(session, "Learner")
    # dawn of day 1 (ticks 1-6 are the night; the road wants daylight)
    while production.next_tick_number(session) < 7:
        run_tick(session); session.commit()
    # the walk: HEARTH -> FLINT, two hours by the valley road
    travel_mod.start_route(session, house, "FLINT")
    for _ in range(3):   # the road's two ticks + the arrival credit
        run_tick(session); session.commit()
    at = session.execute(
        select(Place.key).where(Place.id == house.location_place_id)
    ).scalar_one()
    assert at == "FLINT"
    # dig (1h) -> 2 flint; study (2h) -> KNAPPING in hand
    assert _act_day(session, house, "DIG_FLINT")
    _run(session, 2)
    assert _hold(session, house.id, "FLINT") == Decimal("2")
    assert _act_day(session, house, "STUDY_KNAPPING")
    _run(session, 3)
    assert _has(session, house.id, "KNAPPING")
    # still inside day 1, and the spear starts the moment the whetstone
    # work has materials (wood and yarn gathered on any walk)
    tick = production.next_tick_number(session)
    assert tick <= 24, f"the ladder took until tick {tick}"
    # the spear's own head plus the haft's wood and yarn (gathered on
    # any walk): the rank was the ladder's business, materials are not
    markets.adjust_holding(session, house, "FLINT", Decimal("1"))
    markets.adjust_holding(session, house, "WOOD", Decimal("2"))
    markets.adjust_holding(session, house, "YARN", Decimal("1"))
    _at(session, house, "HEARTH")
    assert _act_day(session, house, "MAKE_SPEAR")


def test_clay_is_dug_at_the_river_for_anyone(session):
    """DIG_CLAY: the scrape's bargain one place over -- an hour of
    daylight labor at the bank, two certain clay, no gamble."""
    create_content(session)
    house = _seat(session, "Digger")
    _at(session, house, "RIVER")
    assert _act_day(session, house, "DIG_CLAY")
    _run(session, 2)
    assert _hold(session, house.id, "CLAY") == Decimal("2")


def test_the_post_was_born_to_the_salt_trade(session):
    """The counter holds SALT_CURING outright (his shed salts as it
    always did, bit-identical), the shelf stocks the world's salt, and
    the rank is retail-only for houses: salt sells at the flint
    anchor, and the counter bids on it since P3 opened the lick --
    half basis, never near retail: paying near retail for stock is
    how a shop dies."""
    from econengine import production as prod
    from econengine import tech
    create_content(session)
    post = _post(session)
    assert "SALT_CURING" in tech.entity_unlocks(session, post.id)
    # his back room runs as before
    markets.adjust_holding(session, post, "MEAT", Decimal("2"))
    assert prod.start_process(session, post, "SALT_MEAT")
    # the shelf: 8 salt on genesis, a market at the post
    assert _hold(session, post.id, "SALT") == Decimal("8")
    assert session.execute(
        select(Market).where(Market.symbol == "SALT")).scalar_one()
    # a house is refused the shed's craft for want of the rank (the
    # facility is the post's own, but the rank binds first where held)
    house = _seat(session, "Saltless")
    _at(session, house, "POST")
    with pytest.raises(ValueError, match="requires SALT_CURING"):
        prod.start_process(session, house, "SALT_MEAT")
    # the book (P3): retail 1.95 (basis 1.50 x 1.30) -- and the bid
    # stands now that the lick can be dug: half the basis, four deep,
    # the salt road's home leg
    _run(session, 1)
    asks = _open_orders(session, post.id, "SALT", OrderSide.SELL)
    bids = _open_orders(session, post.id, "SALT", OrderSide.BUY)
    assert [o.limit_price for o in asks] == [Decimal("1.95")]
    assert [o.limit_price for o in bids] == [Decimal("0.75")]
    assert bids[0].quantity == Decimal("4")


# --- Run 54 P2: comfort, not status ---------------------------------------

def test_the_fire_seat_pays_ease_besides_warmth(session):
    """A fire hour is a nice hour (P2): WARM_BY_FIRE co-outputs COMFORT
    +0.5 alongside its +6 warmth -- comfort is the dividend of safety,
    not a scoreboard. Pre-stocked ease keeps the need met through the
    seam (the first seated hour's draw precedes its credit)."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Easy")
    _set(session, w, "COMFORT", Decimal("0.1"))
    assert _act(session, w, "WARM_BY_FIRE")     # no labor: the free seat
    _run(session, 2)                            # the hour lands at t2's top
    # 0.1 stocked -0.1 drawn, +0.5 seated, -0.1 drawn again, fading 5%
    assert _hold(session, w.id, "COMFORT") == Decimal("0.38")
    assert _hold(session, w.id, "SQUALOR") == Decimal("0")


def test_the_bed_pays_the_finest_hour(session):
    """SLEEP_IN_BED's second dividend: +1 comfort an hour on top of its
    rest and warmth -- the comfort top rung, where capital finally buys
    ease. The den pays the same as a fire seat (+0.5): the dumb comfort
    of a curled beast."""
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Sleeper")
    camp = _camp(session, w)
    parcels.add_facility(session, camp, "SHELTER")
    markets.adjust_holding(session, w, "BED", Decimal("1"))
    _set(session, w, "COMFORT", Decimal("0.2"))
    production.start_process(session, w, "SLEEP_IN_BED", camp.id)
    _run(session, 2)
    # 0.2 -0.1 = 0.1, -5% fade -> 0.095; +1.0 landed, -0.1 drawn,
    # -5% of 0.995 -> 0.9452 exactly
    assert _hold(session, w.id, "COMFORT") == Decimal("0.9452")
    den = production.get_recipe(session, "SLEEP_DEN")
    comfort_out = {o.symbol: o.quantity for o in den.outputs}["COMFORT"]
    assert comfort_out == Decimal("0.5")


def test_the_comfort_deficit_accrues_squalor(session):
    """The one want whose bite is physical: an unbanked comfort stock
    accrues SQUALOR 0.02 an hour; a stocked one satisfies the need and
    accrues nothing. Comfort fades 5% a tick on its own -- ease fades
    faster than misery."""
    create_content(session)
    _no_wolves(session)
    bare = _seat(session, "Bare")
    stocked = _seat(session, "Stocked")
    _set(session, bare, "COMFORT", 0)          # born at ease: stripped bare
    _set(session, stocked, "COMFORT", Decimal("5"))
    _run(session, 5)
    # bare: 0.02 granted a tick, decaying 1% -> 0.0980 after five
    assert Decimal("0.09") < _hold(session, bare.id, "SQUALOR") < Decimal("0.1")
    # stocked: the need met every tick, nothing accrued, the stock still fat
    assert _hold(session, stocked.id, "SQUALOR") == Decimal("0")
    # five hours of ease: drawn 0.5, faded to 3.4390 -- fat, and fading
    assert _hold(session, stocked.id, "COMFORT") == Decimal("3.4390")


def test_squalor_shrinks_the_daylight_ration(session):
    """The bite (the engine's two effective-quantity read sites, wired
    by one Good row): while any SQUALOR rides, the LABOR modifier x0.85
    shrinks the auto-issued ration -- a daylight tick tops a squalid
    house at 0.85 hours (0.4250 after labor's fast fade) where a clean
    house draws the full hour. Conditions scale CAPABILITY, not matter:
    labor bought or banked still spends at face value."""
    from econengine import conditions
    create_content(session)
    _no_wolves(session)
    _to_tick(session, 8)         # hour 7 first: the seats wake clean (an
    squalid = _seat(session, "Squalid")   # unattended night would accrue
    clean = _seat(session, "Clean")       # squalor and beg the question)
    _set(session, squalid, "SQUALOR", Decimal("2"))
    _set(session, squalid, "LABOR", 0)
    _set(session, clean, "LABOR", 0)
    assert conditions.effective_factor(session, squalid.id, "LABOR") == Decimal("0.85")
    assert conditions.effective_factor(session, clean.id, "LABOR") == Decimal("1")
    _run(session, 1)                              # the ration issues at its top
    assert _hold(session, squalid.id, "LABOR") == Decimal("0.4250")
    assert _hold(session, clean.id, "LABOR") == Decimal("0.5000")
    _at(session, squalid, "THICKET")
    # the starved ration cannot start whole-hour work...
    with pytest.raises(Exception, match="LABOR"):
        production.start_process(session, squalid, "GATHER")
    # ...but matter is matter: a banked hour spends at face value
    _set(session, squalid, "LABOR", 1)
    assert production.start_process(session, squalid, "GATHER")


def test_squalor_never_kills(session):
    """The one condition with no kill line: hold a hundred squalor --
    seven times any death threshold in the pack -- keep the body fed,
    and it stays ACTIVE forever, working at x0.85. Squalor is why
    you'd want to be safe rather than merely not-dead; nothing in the
    world pretends otherwise."""
    from econengine import conditions, models as eng_models
    create_content(session)
    _no_wolves(session)
    w = _seat(session, "Miserable")
    good = session.execute(select(eng_models.Good)
                           .where(eng_models.Good.symbol == "SQUALOR")
                           ).scalar_one()
    assert good.incapacitates_at is None
    assert good.modifies_pattern == "LABOR"
    assert good.modifies_factor == Decimal("0.85")
    _set(session, w, "SQUALOR", Decimal("100"))
    for _ in range(20):                          # a fed, squalid day
        for sym, qty in (("SATIETY", 6), ("WARMTH", 6), ("REST", 6),
                         ("SKINWATER", 6)):
            _set(session, w, sym, qty)
        for cond in ("HUNGER", "EXPOSURE", "FATIGUE", "THIRST"):
            _set(session, w, cond, 0)
        _run(session, 1)
    assert w.status == EntityStatus.ACTIVE
    assert _hold(session, w.id, "SQUALOR") > Decimal("80")
    assert conditions.effective_factor(session, w.id, "LABOR") == Decimal("0.85")


def test_the_feast_wants_a_lit_fire_and_pays_ease(session):
    """FEAST: four strips of the never-rotting surplus and a log on the
    commons, two honest hours of turning and sharing, five whole points
    of ease -- and no satiety: the feast is eaten on a fed body, or it
    is just dinner. The fire must be lit (the same gate as the seat and
    the sleeper); a dark fire refuses the evening."""
    from econengine import parcels as parcels_mod
    create_content(session)
    _no_wolves(session)
    _to_tick(session, 8)         # hour 7 first: a seat woken here holds no
    w = _seat(session, "Host")   # squalor -- an unattended dawn would have
    _set(session, w, "COMFORT", Decimal("0.5"))  # accrued it (the bite!)
    for fire in session.execute(select(parcels_mod.Facility)).scalars():
        if fire.facility_type == "FIRE":
            fire.fuel = Decimal("0")            # a dark commons
    session.commit()
    markets.adjust_holding(session, w, "JERKY", Decimal("4"))
    markets.adjust_holding(session, w, "WOOD", Decimal("1"))
    with pytest.raises(Exception, match="dark"):
        production.start_process(session, w, "FEAST")
    for fire in session.execute(select(parcels_mod.Facility)).scalars():
        if fire.facility_type == "FIRE":
            fire.fuel = Decimal("6")            # relit: the evening runs
    session.commit()
    assert _act_day(session, w, "FEAST")
    assert _hold(session, w.id, "JERKY") == Decimal("0")    # the surplus spent
    _run(session, 3)                             # two hours + the landing
    # 0.5 stocked fades to 0.266 over the two cooking hours; +5 lands,
    # -0.1 drawn, -5% of the 5.166 remainder -> 4.9077 exactly
    assert _hold(session, w.id, "COMFORT") == Decimal("4.9077")
    assert _hold(session, w.id, "SQUALOR") == Decimal("0")
    # pure ease, not dinner: the feast fed nobody (born empty, no meal)
    assert _hold(session, w.id, "SATIETY") == Decimal("0")


# --- Run 54 P3: the far valley ---------------------------------------

def _walk_to(session, house, key, torch=Decimal("0"), cap=40):
    """Walk a house to `key` and run ticks until it stands there. The
    loop (not a fixed count) is the E3 lesson: a hop of duration d
    lands d+1 ticks after the departure, and counting beats guessing.
    Toppable torch: a dark-tail road wants a flame in hand."""
    from econengine import travel as travel_mod
    travel_mod.start_route(session, house, key)
    for _ in range(cap):
        _run(session, 1)
        if torch > 0 and house.id:      # keep the brand fed (test grace)
            now = production.next_tick_number(session)
            if now % 24 >= 19:          # the dark tail: top the flame
                _set(session, house, "LIT_TORCH", torch)
        session.refresh(house)
        place = session.execute(
            select(Place.key).where(Place.id == house.location_place_id)
        ).scalar_one_or_none()
        if place == key:
            return
        # a walker in the cold is a walker dying: keep the body warm
        # (the test is the road and the book, not the thermostat)
        _set(session, house, "WARMTH", Decimal("4"))
    raise AssertionError(f"never arrived at {key}")


def test_the_far_valley_is_six_hours_out(session):
    """The P3 map pin, from the spec's ASCII: the ridge road climbs
    from the scrape -- RIDGE_PASS 2h, the lick 2h past the ridge, the
    vale 2h down and one hour off the lick. The hearth is SIX hours
    from the lick door to door (2+2+2): a committed day. And the
    ridge is the far side's only door -- no road bypasses it."""
    from econengine import edges, places as places_mod
    create_content(session)
    spots = {p.key: p for p in places_mod.list_places(session)}
    D_ = lambda a, b: edges.distance_ticks(session, spots[a], spots[b])
    assert D_("HEARTH", "SALT_LICK") == 6     # 2+2+2, door to door
    assert D_("HEARTH", "RIDGE_PASS") == 4
    assert D_("HEARTH", "VALE") == 6
    assert D_("FLINT", "SALT_LICK") == 4
    assert D_("VALE", "SALT_LICK") == 1
    # the home map is unmoved by the far one (the forest's CHEAPEST
    # walk is the valley road through the post: 1+1 -- the 3h edge
    # is the deep wood, the long way in)
    assert D_("HEARTH", "POST") == 1
    assert D_("HEARTH", "FOREST") == 2


def test_salt_is_dug_at_the_lick(session):
    """DIG_SALT: the scrape's bargain at the lick -- an hour of
    daylight, two certain salt, no gamble. Salt enters the world as a
    PRODUCT for the first time (before P3 it existed only as the
    post's shelf stock). The place gate names the lick; the night
    gate names the dark."""
    from econengine import clock
    create_content(session)
    _no_wolves(session)
    house = _seat(session, "Digger")
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    # refused from home: the work wants the lick
    with pytest.raises(ValueError, match="must be at a SALT"):
        production.start_process(session, house, "DIG_SALT")
    _at(session, house, "SALT_LICK")
    assert _act_day(session, house, "DIG_SALT")
    _run(session, 2)
    assert _hold(session, house.id, "SALT") == Decimal("2")


def test_hens_roll_in_the_vale(session):
    """HEN_ROLL: the vale's gamble -- an hour's daylight stalking the
    tufts, and three times in ten a hen joins you. The chicken
    opener's far source: the home valley keeps buying hens at the
    post or walking them back. The pinned table is 70/30, one hen."""
    from econengine import clock
    create_content(session)
    _no_wolves(session)
    house = _seat(session, "Stalker")
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    with pytest.raises(ValueError, match="must be at a PASTURE"):
        production.start_process(session, house, "HEN_ROLL")
    _at(session, house, "VALE")
    recipe = production.get_recipe(session, "HEN_ROLL")
    table = sorted(
        (b.weight, tuple(sorted((o.symbol, o.quantity) for o in b.outputs)))
        for b in recipe.branches)
    assert table == [(Decimal("30"), (("CHICKEN", Decimal("1")),)),
                     (Decimal("70"), ())]
    # the gamble pays eventually: 30 attempts at 70/30 is a sure thing
    # once survival stops being the variable. The stalker is scriptless,
    # so the world will not feed, warm, or rest him -- conscious eating
    # (a larder satisfies nothing until it is a meal), no fire in the
    # vale, and the worst case spans a day and a half; every run is a
    # fresh world (fresh ids -> fresh roll stream), so no fixed buffer
    # can carry the loop. Stand in for the body between ticks, the way
    # LABOR's auto-issue does: top the drawn flow goods before each
    # tick (WARMTH 4 covers the night's 3-draw; the cap clips the
    # rest). This test is about the gamble, not the stalker's lunch.
    # A rolled hen is carried, not penned.
    def _survive():
        for _sym, _q in (("SATIETY", "1"), ("WATER", "0.5"),
                         ("REST", "0.5"), ("WARMTH", "4")):
            markets.adjust_holding(session, house, _sym, Decimal(_q))
        run_tick(session)
        session.commit()
    hens = 0
    for _ in range(30):
        while clock.is_night(production.next_tick_number(session)):
            _survive()
        if _act_day(session, house, "HEN_ROLL"):
            _survive(); _survive(); _survive()   # +1 for the window
            hens = _hold(session, house.id, "CHICKEN")
            if hens >= 1:
                break
        _survive()       # the hour turns; the tufts reset
    assert hens >= 1


def test_ibex_want_the_ridge_and_a_weapon(session):
    """The ridge's quarry: HUNT_IBEX takes a held spear, HUNT_IBEX_BOW
    a held bow (the pack's per-weapon pattern -- either weapon opens
    the hunt), and both refuse every other ground. The pelt is the
    point: IBEX_PELT, the winter coat's whole material, and every
    winning branch carries one."""
    from econengine import clock
    create_content(session)
    _no_wolves(session)
    house = _seat(session, "Climber")
    _at(session, house, "RIDGE_PASS")
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    with pytest.raises(Exception, match="SPEAR"):
        production.start_process(session, house, "HUNT_IBEX")
    with pytest.raises(Exception, match="BOW"):
        production.start_process(session, house, "HUNT_IBEX_BOW")
    spear = production.get_recipe(session, "HUNT_IBEX")
    tables = sorted(
        (b.weight, tuple(sorted((o.symbol, o.quantity) for o in b.outputs)))
        for b in spear.branches)
    assert tables == sorted([
        (Decimal("30"), ()),
        (Decimal("50"), (("IBEX_PELT", Decimal("1")),
                         ("MEAT", Decimal("3")))),
        (Decimal("20"), (("IBEX_PELT", Decimal("1")),
                         ("MEAT", Decimal("6")))),
    ])
    bow = production.get_recipe(session, "HUNT_IBEX_BOW")
    assert sorted(b.weight for b in bow.branches) == [
        Decimal("20"), Decimal("25"), Decimal("55")]   # steadier table
    # the spear hunt pays eventually: a pelt in the pack (a fresh pelt
    # fades 0.05 in the very tick it lands -- pelts are perishable)
    _set(session, house, "SPEAR", Decimal("1"))
    landed = False
    for _ in range(30):
        while clock.is_night(production.next_tick_number(session)):
            _run(session, 1)
        # the ridge has no fire and no water: keep the hunter alive
        # (the test's subject is the hunt, not the thermostat)
        _set(session, house, "WARMTH", Decimal("4"))
        _set(session, house, "WATER", Decimal("4"))
        _set(session, house, "BERRIES", Decimal("6"))
        if _act_day(session, house, "HUNT_IBEX"):
            _run(session, 3)   # the window's tick + the hunt's two
            landed = _hold(session, house.id, "IBEX_PELT") > 0
            if landed:
                break
        _run(session, 1)       # the hour turns; the herd re-forms
    assert landed
    # and the ground is the ridge's: refused from home
    _at(session, house, "HEARTH")
    with pytest.raises(ValueError, match="must be at a RIDGE"):
        production.start_process(session, house, "HUNT_IBEX")


def test_the_fine_coat_wraps_twice_the_clothes(session):
    """MAKE_FINE_CLOTHES: the TANNING rank, one IBEX_PELT and two
    YARN, made at the hearth -- worn like clothes and twice the wrap:
    HUDDLE_FINE pays a FULL warmth an hour where HUDDLE (clothes)
    pays half. The tanner's masterwork is material, not status."""
    create_content(session)
    from econengine import clock
    w = _seat(session, "Tanner")
    # the rank binds first: a house without TANNING is refused (set
    # the inputs first -- the refusal must be the RANK's, not a
    # shortfall's)
    _set(session, w, "IBEX_PELT", Decimal("2"))
    _set(session, w, "YARN", Decimal("2"))
    _grant_chain(session, w, "TANNING")
    t2 = None
    t2 = _seat(session, "Rankless")
    _set(session, t2, "IBEX_PELT", Decimal("2"))
    _set(session, t2, "YARN", Decimal("2"))
    with pytest.raises(ValueError, match="TANNING"):
        production.start_process(session, t2, "MAKE_FINE_CLOTHES")
    # pelts rot while the night idles by (0.05/tick): wait for day,
    # THEN stock the fresh pelt -- a tanner works dawn hides
    while clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    _set(session, w, "IBEX_PELT", Decimal("1"))
    _set(session, w, "YARN", Decimal("2"))
    assert _act(session, w, "MAKE_FINE_CLOTHES")
    _run(session, 3)
    assert _hold(session, w.id, "FINE_CLOTHES") == Decimal("1")
    assert _hold(session, w.id, "IBEX_PELT") == Decimal("0")
    assert _hold(session, w.id, "YARN") == Decimal("0")
    # the wrap, measured: two houses, one in clothes and one in the
    # coat, huddle the same hour from the same warmth -- the coat
    # ends exactly 0.5 warmer (twice clothes' 0.5 leak)
    plain = _seat(session, "Plain")
    _set(session, plain, "CLOTHES", Decimal("1"))
    _set(session, plain, "WARMTH", Decimal("2"))
    _set(session, w, "WARMTH", Decimal("2"))
    assert _act(session, plain, "HUDDLE")
    assert _act(session, w, "HUDDLE_FINE")
    _run(session, 2)
    # the wrap lands +1.0 vs +0.5 two ticks on (the output's tick,
    # then the hour that draws it); the draw (-1) and the 20% fade
    # touch both alike -- the gap that survives is the 0.5 wrap faded
    # once: 0.5 x 0.8 = 0.4 exactly. TWICE CLOTHES' WRAP, measured on
    # the balance, not asserted from the label.
    assert _hold(session, plain.id, "WARMTH") == Decimal("0.24")
    assert _hold(session, w.id, "WARMTH") == Decimal("0.64")
    assert _hold(session, w.id, "WARMTH") - _hold(
        session, plain.id, "WARMTH") == Decimal("0.4")


def test_the_lick_trades_a_book_only_market(session):
    """The far book: IBEX_PELT trades at the salt lick, and ONLY
    there -- book-only, no NPC maker, no anchor prices. After
    genesis (and ticks of the world running), the lick's book is
    EMPTY: nobody quotes it but houses. And two houses CAN clear on
    it -- the lick's prices are what the trip was worth."""
    create_content(session)
    _no_wolves(session)
    _run(session, 6)          # the world runs; no maker appears
    lick_market = session.execute(
        select(Market).where(Market.symbol == "IBEX_PELT")).scalar_one()
    lick = session.execute(select(Place).where(Place.key == "SALT_LICK")
                           ).scalar_one()
    assert lick_market.place_id == lick.id
    assert session.execute(
        select(Order).where(Order.market_id == lick_market.id)
    ).scalars().all() == []
    # the far book clears house-to-house: a hunter sells, a buyer bids
    hunter = _seat(session, "Hunter")
    buyer = _seat(session, "Buyer")
    _at(session, hunter, "SALT_LICK")
    _at(session, buyer, "SALT_LICK")
    _set(session, hunter, "IBEX_PELT", Decimal("2"))
    hunter_acct = next(a for a in hunter.accounts if a.currency == COIN)
    buyer_acct = next(a for a in buyer.accounts if a.currency == COIN)
    markets.place_order(session, hunter.id, "IBEX_PELT", "sell",
                        Decimal("2"), Decimal("4.00"), hunter_acct.id)
    markets.place_order(session, buyer.id, "IBEX_PELT", "buy",
                        Decimal("1"), Decimal("4.50"), buyer_acct.id)
    _run(session, 1)
    # the fill's tick fades both pelts 0.05 (pelts are perishable)
    assert _hold(session, buyer.id, "IBEX_PELT") == Decimal("0.95")
    assert _hold(session, hunter.id, "IBEX_PELT") == Decimal("0.95")
    # the print paid the resting ask, coin moved hunter-ward
    assert hunter_acct.balance == SEAT_COIN + Decimal("4.00")
    assert buyer_acct.balance == SEAT_COIN - Decimal("4.00")


def test_the_salt_road_end_to_end(session):
    """The trip the expansion is named for: a house walks the six
    hours out (hearth -> flint scrape -> ridge -> lick), digs salt in
    the certain hours, and walks it home through the dark tail (the
    last hop departs at night -- the road wants a flame), then sells
    at the post's salt bid. The salt road's home leg is the post's
    own counter: dig at the lick, carry it back, sell here. (Carry
    limits and shelf delivery ride this same road once E2/E3 merge;
    this pins the pack's half -- the hours, the dig, the sale.)"""
    create_content(session)
    _no_wolves(session)
    house = _seat(session, "Salter")
    _set(session, house, "BERRIES", Decimal("10"))
    _set(session, house, "WATER", Decimal("4"))
    # out at dawn: the night ends, the road opens
    while production.next_tick_number(session) < 7:
        _run(session, 1)
    _walk_to(session, house, "SALT_LICK")
    assert _hold(session, house.id, "SALT") == Decimal("0")
    assert _act_day(session, house, "DIG_SALT")
    _run(session, 2)
    assert _act_day(session, house, "DIG_SALT")
    _run(session, 2)
    assert _hold(session, house.id, "SALT") == Decimal("4")
    # home through the dark tail, torch in hand
    _walk_to(session, house, "HEARTH", torch=Decimal("2"))
    _walk_to(session, house, "POST")
    acct = next(a for a in house.accounts if a.currency == COIN)
    post = _post(session)
    post_bid = _open_orders(session, post.id, "SALT", OrderSide.BUY)
    assert post_bid, "the counter's salt bid is standing"
    bid = post_bid[0].limit_price
    assert post_bid[0].quantity >= Decimal("4")   # four deep for the lot
    markets.place_order(session, house.id, "SALT", "sell",
                        Decimal("4"), bid, acct.id)
    _run(session, 1)
    # the post's standing salt bid took the lot at its own price
    assert _hold(session, house.id, "SALT") == Decimal("0")
    assert acct.balance == SEAT_COIN + Decimal("4") * bid


# --- The white season (run 54 P4) ---------------------------------------


def _pin_tick(session, tick_number):
    """Pin next_tick_number by inserting the tick rows before it (the
    season/daylight gates read the clock from the tick table -- the
    same trick tests/test_seasons.py uses, without running 288 ticks
    to get there)."""
    from econengine.models import Tick
    from sqlalchemy import func
    current = session.execute(
        select(func.count()).select_from(Tick)).scalar() or 0
    for n in range(current + 1, tick_number):
        session.add(Tick(number=n))
    session.flush()


def test_the_white_season_closes_the_thicket_and_the_river(session):
    """requires_season on the three open-land errands: GATHER,
    GATHER_BAG and FISH run in SPRING/SUMMER/AUTUMN and refuse in
    WINTER with the season named in the refusal -- an agent reading
    its events learns the calendar from the no. CHOP_WOOD stays legal
    (the treeline still gives deadwood; the fire must not die)."""
    from econengine import clock
    create_content(session)
    _no_wolves(session)
    house = _seat(session, "Winterhand")
    # spring first (the pin only walks the clock forward): tick 32 =
    # day 2 hour 8, an open-season morning -- all three doors open
    _pin_tick(session, 32)
    assert clock.season_of(production.next_tick_number(session)) == "SPRING"
    _at(session, house, "THICKET")
    _set(session, house, "LABOR", Decimal("1"))
    assert production.start_process(session, house, "GATHER")
    _set(session, house, "BAG", Decimal("1"))
    _set(session, house, "LABOR", Decimal("1"))
    assert production.start_process(session, house, "GATHER_BAG")
    _at(session, house, "RIVER")
    _set(session, house, "LABOR", Decimal("1"))
    assert production.start_process(session, house, "FISH")
    # then tick 440 = day 19 hour 8: the first winter morning, and all
    # three refuse with the season named in the refusal
    _pin_tick(session, 440)
    assert clock.season_of(production.next_tick_number(session)) == "WINTER"
    _at(session, house, "THICKET")
    for code in ("GATHER", "GATHER_BAG"):
        with pytest.raises(ValueError, match="WINTER"):
            production.start_process(session, house, code)
    _at(session, house, "RIVER")
    with pytest.raises(ValueError, match="WINTER"):
        production.start_process(session, house, "FISH")
    # the treeline still gives: CHOP_WOOD is winter-legal (an AXE held;
    # labor set by hand -- a pinned clock never ran the dawn's issue)
    _at(session, house, "THICKET")
    _set(session, house, "AXE", Decimal("1"))
    _set(session, house, "LABOR", Decimal("1"))
    assert production.start_process(session, house, "CHOP_WOOD")


def test_winter_runs_the_warmth_clock_a_tier_up(session):
    """The WARMTH need's season rows: 1/h by day and 3/h at night in
    the open seasons, 2/h by day and 4/h at night in winter --
    season-night beats season beats night beats base, resolved by the
    engine on the pack's own need row."""
    from econengine import clock, needs as needs_mod
    create_content(session)
    warmth = needs_mod.get_need(session, "WARMTH")
    assert Decimal(warmth.season_quantity_per_tick["WINTER"]) == Decimal("2")
    assert Decimal(warmth.season_night_quantity_per_tick["WINTER"]) \
        == Decimal("4")
    # tick 300 = day 13 hour 12 (autumn day); 310 = autumn night
    assert not clock.is_night(300) and clock.is_night(310)
    assert needs_mod.required_quantity(warmth, 300) == Decimal("1")
    assert needs_mod.required_quantity(warmth, 310) == Decimal("3")
    # tick 440 = day 19 hour 8 (winter day); 454 = winter night
    assert clock.season_of(440) == "WINTER"
    assert not clock.is_night(440) and clock.is_night(454)
    assert needs_mod.required_quantity(warmth, 440) == Decimal("2")
    assert needs_mod.required_quantity(warmth, 454) == Decimal("4")


def test_the_blizzard_lives_on_the_ridge(session):
    """BLIZZARD is the storm line: a place-scoped ambient that finds
    anyone standing on RIDGE_PASS in the dark hours and hands them
    half an EXPOSURE an hour. Day trips never feel it, and the hearth
    never does -- the weather belongs to the high ground."""
    from econengine import clock
    create_content(session)
    _no_wolves(session)
    climber = _seat(session, "Caught")
    warm = _seat(session, "Homebody")
    _at(session, climber, "RIDGE_PASS")
    # a night hour (tick 22 = day 1 hour 22): the storm finds the pass
    _pin_tick(session, 22)
    night = run_tick(session); session.commit()
    events = night.events or []
    blizzards = [e for e in events
                 if e.get("type") == "threat_pressure"
                 and e.get("threat") == "BLIZZARD"]
    assert len(blizzards) == 1 and blizzards[0]["entity_id"] == climber.id
    assert blizzards[0]["pressure"] == "0.5000"
    # 0.5 granted, minus the hour's own 5% decay on the condition
    assert _hold(session, climber.id, "EXPOSURE") == Decimal("0.4750")
    assert _hold(session, warm.id, "EXPOSURE") == Decimal("0")
    # daylight on the same pass: the storm sleeps (weather is a dark
    # -- and altitude -- fact, not a predator)
    _pin_tick(session, 440)          # winter DAY hour: no pressure even now
    day = run_tick(session); session.commit()
    assert not [e for e in (day.events or [])
                if e.get("type") == "threat_pressure"]


def test_a_house_can_raise_its_own_salt_shed(session):
    """BUILD_SALT_SHED: the SALT_CURING rank, six logs, four salt and
    an hour's labor raise an OWNER shed on the builder's own camp --
    after which SALT_MEAT salts at the hearth, no walk to the post's
    back room. The neighbour at the same clearing cannot bind it:
    OWNER is the builder's alone."""
    from econengine import tech
    create_content(session)
    _no_wolves(session)
    house = _seat(session, "Salter")
    _set(session, house, "WOOD", Decimal("6"))
    _set(session, house, "SALT", Decimal("4"))
    # no rank, no shed: the crafts gate fires first
    with pytest.raises(ValueError, match="SALT_CURING"):
        production.start_process(session, house, "BUILD_SALT_SHED")
    tech.grant_unlock(
        session, house, tech.get_technology(session, "SALT_CURING"),
        production.next_tick_number(session))
    assert _act(session, house, "BUILD_SALT_SHED")
    _run(session, 4)                # the window's tick + the three-hour build
    assert "SALT_SHED" in _facilities(session, house)
    assert _hold(session, house.id, "WOOD") == Decimal("0")
    assert _hold(session, house.id, "SALT") == Decimal("0")
    # SALT_MEAT resolves against the home shed now (OWNER on own ground)
    _set(session, house, "MEAT", Decimal("2"))
    assert _act(session, house, "SALT_MEAT")
    _run(session, 5)
    assert _hold(session, house.id, "JERKY") == Decimal("2")
    # the neighbour holds the rank and stands at the same clearing --
    # but the shed is not his (OWNER access, not PLACE)
    mate = _seat(session, "Neighbour")
    tech.grant_unlock(
        session, mate, tech.get_technology(session, "SALT_CURING"),
        production.next_tick_number(session))
    _set(session, mate, "MEAT", Decimal("2"))
    with pytest.raises(ValueError, match="SALT_SHED"):
        production.start_process(session, mate, "SALT_MEAT")


def test_the_white_season_two_houses(session):
    """The headline balance test (run 54 P4): the winter door. Two
    houses stand at day 19 with the SAME survival floor -- clothes,
    tools, a sheltered camp by the commons fire -- and one difference:
    the autumn-salter banked (a pen of hens, the never-rot strips, a
    purse of pelt-coin), and the autumn-idler lived hand-to-mouth on
    the open thicket and banked nothing. Six white days later the
    salter stands at the year's end with margin; the idler met the
    HUNGER kill-line inside winter (the thicket closed, the scraps
    rotted, and hunger is a bill); and the trade is the rescue -- a
    third house, coin-pursed and empty-lardered, buys the post
    shelf's jerky at the famine ask and lives. The autumn's WORK is
    setup here (its mechanics -- shelter, pen, smoke, the salt road,
    the boar -- each have their own tests); this test's subject is
    the winter BALANCE: what closes, what the draws become, and who
    stands at the end. Torch grace on night walks mirrors _walk_to;
    the carry gate (E2) is left ON and the policies travel light."""
    from sqlalchemy import func
    from econengine import clock, spawns
    from econengine import travel as travel_mod
    from econengine.models import Good, Place, Process, ProcessStatus
    create_content(session)
    _no_wolves(session)

    def place_of(h):
        return session.execute(
            select(Place.key).where(Place.id == h.location_place_id)
        ).scalar_one_or_none()

    def busy(h):
        return session.execute(
            select(Process).where(Process.entity_id == h.id,
                                  Process.status == ProcessStatus.RUNNING)
        ).scalars().first() is not None

    def load_of(h):
        """The road's weight (E2): every unit of a non-exempt good."""
        return session.execute(
            select(func.coalesce(func.sum(Holding.quantity), Decimal("0")))
            .join(Good, Good.symbol == Holding.symbol)
            .where(Holding.entity_id == h.id, Good.carry_exempt.is_(False),
                   Holding.quantity > 0)
        ).scalar_one()

    def try_act(h, code):
        """_act that also swallows season/place refusals (a policy
        that mis-times an errand misses the hour, like a script)."""
        if _hold(session, h.id, "LABOR") < 1:
            markets.adjust_holding(
                session, h, "LABOR",
                Decimal("1") - _hold(session, h.id, "LABOR"))
        try:
            production.start_process(session, h, code)
            return True
        except (markets.InsufficientHoldingsError, ValueError):
            return False

    def walk(h, key):
        if clock.is_night(production.next_tick_number(session)):
            _set(session, h, "LIT_TORCH", Decimal("0.5"))
        try:
            travel_mod.start_route(session, h, key)
            return True
        except ValueError:
            return False            # over-loaded: an hour's wait, not a crash

    def ask_price(sym):
        rows = _open_orders(session, _post(session).id, sym,
                            OrderSide.SELL)
        return min((o.limit_price for o in rows), default=None)

    def buy_strips(h, qty):
        """The famine basket: jerky first (never rots), the cooked
        shelf while it lasts -- whatever safe food the counter asks."""
        acct = next(a for a in h.accounts if a.currency == COIN)
        for sym in ("JERKY", "COOKED_MEAT"):
            p = ask_price(sym)
            if p and acct.balance >= p * qty:
                markets.place_order(session, h.id, sym, "buy",
                                    qty, p, acct.id)
                return True
        return False

    # the winter door: day 19 hour 1 (tick 433). The world wakes with
    # the thicket already bare and the river already iced.
    _pin_tick(session, 433)
    salter = _seat(session, "Salter")
    idler = _seat(session, "Idler")
    trader = _seat(session, "Trader")
    for h in (salter, idler, trader):
        _set(session, h, "CLOTHES", Decimal("1"))   # the winter baseline
        _set(session, h, "SPEAR", Decimal("1"))
        _set(session, h, "BAG", Decimal("1"))
        _set(session, h, "AXE", Decimal("1"))        # deadwood is certain
        _set(session, h, "WATERSKIN", Decimal("1"))  # the winter walk's
        # shape: a skin carries eight more hours of river -- the
        # drink errand runs every OTHER day, not every dawn
        _set(session, h, "WOOD", Decimal("8"))       # with an axe in hand
        markets.adjust_holding(session, h, "BERRIES", -BERRY_BUFFER)
        # THE FLOOR (all three): a sheltered camp by the commons fire
        # -- the roof's +2/h holds the small draws, the seat tops
        # the stock. Built across the open seasons; every house has one.
        camp = _camp(session, h)
        parcels.add_facility(session, camp, "SHELTER")
    # THE SALTER'S AUTUMN (setup -- its recipes have their own tests):
    # two boar kills smoked, a third strip-bought with pelt-coin, and
    # the pen -- three hens bought at the post and penned, eggs
    # laying all winter. THE IDLER'S AUTUMN: hand-to-mouth on the
    # thicket; the scraps of it are still in its basket, rotting.
    parcels.add_facility(session, _camp(session, salter), "PEN")
    markets.adjust_holding(session, salter, "CHICKEN", Decimal("4"))
    markets.adjust_holding(session, salter, "JERKY", Decimal("10"))
    markets.adjust_holding(session, salter, "APPLES", Decimal("6"))
    # the REST of the bank lives on the post's shelf (E2's road weighs
    # a full larder: a house carries ~20 units -- so the autumn
    # surplus was SOLD for coin, and winter buys it back strip by
    # strip, the shelf as the cellar)
    salter_purse = next(a for a in salter.accounts if a.currency == COIN)
    salter_purse.balance = salter_purse.balance + Decimal("12")
    # the famine purse: the trader's whole asset is spendable coin
    purse = next(a for a in trader.accounts if a.currency == COIN)
    purse.balance = purse.balance + Decimal("26")

    def drive(h, role, st):
        """One house-hour of winter policy: meals first, then the
        fire, then the errands that remain."""
        tick = production.next_tick_number(session)
        night = clock.is_night(tick)
        hid = h.id

        def hold(sym):
            return _hold(session, hid, sym)

        def eat():
            if hold("EGGS") >= 1:
                return try_act(h, "EAT_EGGS")     # renewable first
            if hold("JERKY") >= 1:
                return try_act(h, "EAT_JERKY")     # the bank second
            if hold("APPLES") >= 1.5:
                return try_act(h, "EAT_APPLES")
            if hold("BERRIES") >= 1.5:
                return try_act(h, "EAT_BERRIES")
            return False

        if hold("SATIETY") < (0.5 if night else 2.5):
            if eat():
                return
        if place_of(h) == "POST":
            # the pastoral seed: the counter SELLS hens (a purse buys
            # more than strips -- a hen is the larder that keeps
            # laying when the shelf runs bare)
            if (role == "trader" and hold("CHICKEN") < 2
                    and not st.get("hens")):
                acct = next(a for a in h.accounts if a.currency == COIN)
                hen_ask = ask_price("CHICKEN")
                if hen_ask and acct.balance >= hen_ask * Decimal("2"):
                    markets.place_order(
                        session, h.id, "CHICKEN", "buy", Decimal("2"),
                        hen_ask, acct.id)
                st["hens"] = True
                return
            # the counter's keep: a log a day keeps the shop lit
            # (KEEP_SHOP) -- the post bids wood, and the day's chop
            # carries a spare log for the counter that feeds you
            if hold("WOOD") >= 4 and not st.get("kept"):
                wood_bid = _open_orders(
                    session, _post(session).id, "WOOD", OrderSide.BUY)
                if wood_bid:
                    acct = next(a for a in h.accounts
                                if a.currency == COIN)
                    markets.place_order(
                        session, h.id, "WOOD", "sell", Decimal("1"),
                        wood_bid[0].limit_price, acct.id)
                    st["kept"] = True
                    return
            if st.get("buy") and not st.get("bought"):
                # buy BIG while the shelf holds: the winter shelf is a
                # lender, not a garden -- it empties, and a purse that
                # shops in dainty baskets winters hungry
                if buy_strips(h, Decimal("12")):
                    st["bought"] = True
                elif st.get("waits", 0) < 6:
                    # the book breathes (cancel-and-replace); the
                    # counter waits out the breath, up to six hours
                    st["waits"] = st.get("waits", 0) + 1
                    return
                else:
                    # a whole day stood at a dark counter: remember it
                    # (the memory outlives the dawn reset) and spend
                    # the next days' daylight on the treeline -- the
                    # counter wants wood nobody had to spare
                    dark_mem[h.id] = (tick - 1) // 24 + 3
            walk(h, "HEARTH"); return
        hour_now = (tick - 1) % 24 + 1
        # the fire is an EVENING fire (the manual's own budget: wood
        # 8-10 a day -- the roof covers the daylight draws; the fire
        # lights at 15:00 and burns to dawn, and the village shares it)
        if (place_of(h) == "HEARTH" and hold("WOOD") >= 1
                and (hour_now >= 15 or night)):
            commons = session.execute(
                select(parcels.Facility).join(parcels.Parcel)
                .where(parcels.Parcel.parcel_type == "COMMONS",
                       parcels.Facility.facility_type == "FIRE")
            ).scalar_one()
            if commons.fuel <= 1:
                try_act(h, "STOKE_FIRE"); return
        # the morning chores: pen the bought hens, then the laying
        if not night and (tick - 1) % 24 + 1 < 9 and hold("CHICKEN") >= 1:
            if "PEN" not in _facilities(session, h) \
                    and hold("WOOD") >= 3 and try_act(h, "MAKE_PEN"):
                return
            if try_act(h, "COLLECT_EGGS"):
                return
        if night:
            # the night's rhythm: the roof holds the small draws and
            # the seat tops the stock (a winter night wants ~4 seated
            # fire-hours), but SLEEP is the night's default -- the
            # rest clock draws 0.25 an hour around the clock and the
            # sleepless clock kills on the third tired night
            if hold("WARMTH") <= 2.0 and hold("REST") > 1.5:
                try_act(h, "WARM_BY_FIRE"); return
            if try_act(h, "SLEEP_SHELTERED"):
                return
            try_act(h, "SLEEP_BY_FIRE"); return
        hour = (tick - 1) % 24 + 1
        place = place_of(h)
        if place == "THICKET":
            # winter thicket: gathering is closed (its refusal has its
            # own test); what remains is the treeline's deadwood --
            # one trip's worth, and home before the dark (a walker
            # overnight at the thicket is the cold's easiest kill)
            if hour >= 16 or hold("WOOD") >= 7:
                walk(h, "HEARTH"); return
            if try_act(h, "CHOP_WOOD"):
                return
            walk(h, "HEARTH"); return
        if place == "RIVER":
            # the ice hole: DRINK stays winter-legal, and the walk is
            # the same one the (closed) fish errand used to ride --
            # fill the skin while the bank is under you
            if hold("SKINWATER") < 2 and try_act(h, "FILL_SKIN"):
                return
            if hold("WATER") < 3.5 and try_act(h, "DRINK"):
                return
            walk(h, "HEARTH"); return
        # at the hearth: top the stock at the seams of the day (dawn
        # and the evening fire) -- a body rides its errands and its
        # night on a full stock; then FOOD FIRST (a starving house
        # with coin dies beside a full shelf if wood outranks it)
        if (hold("WARMTH") <= 4 and place_of(h) == "HEARTH"
                and (hour_now <= 8 or hour_now >= 15)
                and try_act(h, "WARM_BY_FIRE")):
            return
        # the day's errands: WATER FIRST (thirst kills on the third
        # dry day; a cup is sixteen hours), then FOOD (strips from the
        # post when the bank runs low and the purse can pay), then the
        # treeline -- the fire must not die either
        if (hold("WATER") + hold("SKINWATER") < 1.5
                and st.get("drink", 0) < 1 and hour < 14):
            st["drink"] = 1
            walk(h, "RIVER"); return
        coin = next(a.balance for a in h.accounts if a.currency == COIN)
        # shop EARLY, every OTHER day: hunger merely growing (not
        # just an empty shelf) sends the purse to the counter while it
        # is still lit -- and the odd days belong to the treeline (a
        # counter nobody keeps goes dark, and coin beside a dark shelf
        # is a lantern with no oil; urgent hunger shops any day)
        day_no = (tick - 1) // 24 + 1
        urgent = hold("JERKY") + hold("EGGS") < 2
        if ((urgent or hold("HUNGER") > 6
                or (hold("HUNGER") > 4 and day_no % 2 == 0))
                and coin >= Decimal("6")
                and st.get("post", 0) < 1
                and (hour < 14 or role == "trader")
                and dark_mem[h.id] < day_no):
            st["post"] = 1
            st["buy"] = True
            walk(h, "POST"); return
        if (hold("WOOD") < 6 and st.get("chops", 0) < 1 and hour < 14):
            st["chops"] = st.get("chops", 0) + 1
            walk(h, "THICKET"); return
        # the idle hours at home: under the roof, warm and rested
        if try_act(h, "SLEEP_SHELTERED"):
            return
        try_act(h, "SLEEP_BY_FIRE" if hold("REST") < 5 else "WARM_BY_FIRE")

    st_salter, st_idler, st_trader = {}, {}, {}
    dark_mem = {salter.id: 0, idler.id: 0, trader.id: 0}
    idler_down_tick = None
    idler_death = None
    end_tick = 576                       # day 24 hour 24: the year's end
    while production.next_tick_number(session) <= end_tick:
        tick = production.next_tick_number(session)
        if (tick - 1) % 24 == 0:
            spawns.apply_on_round(session, (tick - 1) // 24 + 1)
            _no_wolves(session)          # the packs have their own tests
            st_salter, st_idler, st_trader = {}, {}, {}
        for h, role, st in ((salter, "salter", st_salter),
                            (idler, "idler", st_idler),
                            (trader, "trader", st_trader)):
            if h is None or h.status != EntityStatus.ACTIVE or busy(h):
                continue
            drive(h, role, st)
        last = run_tick(session); session.commit()
        if idler_down_tick is None and idler.status != EntityStatus.ACTIVE:
            idler_down_tick = tick
            # the estate settles inside the same pass -- the kill-line
            # is read off the event, not the burned ledger
            idler_death = next(
                (e for e in (last.events or [])
                 if e.get("type") == "entity_incapacitated"
                 and e.get("entity_id") == idler.id), None)
    # THE SALTER LIVES, WITH MARGIN: active at the year's end, hunger
    # far from the line (15), and food still on the shelf
    assert salter.status == EntityStatus.ACTIVE
    assert _hold(session, salter.id, "HUNGER") < Decimal("8")
    assert _hold(session, salter.id, "JERKY") > Decimal("0")
    assert _hold(session, salter.id, "EGGS") > Decimal("0")   # the pen pays
    # THE IDLER DIES INSIDE WINTER, ON THE HUNGER LINE: same floor,
    # same clothes, same fire -- no bank, and the thicket is closed
    assert idler_down_tick is not None and 433 <= idler_down_tick <= end_tick
    assert idler.status == EntityStatus.INCAPACITATED
    assert idler_death is not None
    assert idler_death["condition"] == "HUNGER"
    assert Decimal(idler_death["quantity"]) >= Decimal("15")
    # AND THE TRADE IS THE RESCUE: the coin-pursed idler bought the
    # post's shelf at the famine ask and stands at the year's end
    assert trader.status == EntityStatus.ACTIVE
    assert _hold(session, trader.id, "HUNGER") < Decimal("15")
