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
    BERRY_BUFFER, COIN, POST_FOOD, SEAT_COIN, WARMTH_BUFFER, create_content,
    make_house,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
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
        "LABOR", "BERRIES", "MEAT", "COOKED_MEAT", "JERKY", "WOOD", "YARN",
        "FLINT", "SPEAR", "AXE", "BAG", "TRAP", "CLOTHES", "BED", "PELT"}
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
        "berries", "wood", "yarn", "flint"}
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
    """HUNT_SPEAR needs a held SPEAR; TEND_FIRE/COOK_MEAT need a FIRE on
    the parcel; REST needs a SHELTER; HUDDLE needs CLOTHES. MAKE_FIRE
    builds the FIRE from scratch."""
    create_content(session)
    w = _seat(session)
    camp = _camp(session, w)
    with pytest.raises(Exception, match="SPEAR"):
        production.start_process(session, w, "HUNT_SPEAR")
    with pytest.raises(Exception, match="no free FIRE"):
        production.start_process(session, w, "TEND_FIRE", camp.id)
    with pytest.raises(Exception, match="no free FIRE"):
        production.start_process(session, w, "COOK_MEAT", camp.id)
    with pytest.raises(Exception, match="no free FIRE"):
        production.start_process(session, w, "SMOKE_MEAT", camp.id)
    with pytest.raises(Exception, match="no free SHELTER"):
        production.start_process(session, w, "REST_SHELTERED", camp.id)
    with pytest.raises(Exception, match="CLOTHES"):
        production.start_process(session, w, "HUDDLE")
    # The fire chain, built the intended way.
    markets.adjust_holding(session, w, "WOOD", Decimal("2"))
    assert _act(session, w, "MAKE_FIRE", camp.id)
    _run(session, 2)
    assert _facilities(session, w) == ["FIRE"]
    markets.adjust_holding(session, w, "WOOD", Decimal("1"))
    assert _act(session, w, "TEND_FIRE", camp.id)
    _run(session, 2)
    assert _hold(session, w.id, "WARMTH") > 0


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
        "SPEAR": 3, "AXE": 2}


def test_cook_meat_converts(session):
    create_content(session)
    w = _seat(session)
    camp = _camp(session, w)
    parcels.add_facility(session, camp, "FIRE")
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
    parcels.add_facility(session, camp, "FIRE")
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
    markets.adjust_holding(session, w, "BERRIES", Decimal("2"))
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
            production.start_process(session, w, "REST_SHELTERED",
                                     _camp(session, w).id)   # labor-free
        except Exception:
            pass
        if _hold(session, w.id, "SATIETY") < 1:      # conscious eating (run 19):
            try:                                      # gathered food is not a meal
                production.start_process(session, w, "EAT_BERRIES")
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
    assert by_sym[("sell", "BERRIES")].limit_price == Decimal("2.00")
    assert by_sym[("sell", "BERRIES")].quantity == Decimal("60")
    assert by_sym[("sell", "COOKED_MEAT")].limit_price == Decimal("3.00")
    # bids on every raw good, sized to the purse (4 each: 24 of 30 coin);
    # BERRIES itself is skipped -- the ladder is already stuffed (60 >= 20)
    for sym in ("MEAT", "WOOD", "YARN", "FLINT"):
        assert by_sym[("buy", sym)].quantity == Decimal("4")
    assert ("buy", "BERRIES") not in by_sym
    assert by_sym[("buy", "MEAT")].limit_price == Decimal("1.00")
    assert by_sym[("buy", "YARN")].limit_price == Decimal("2.00")
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
    markets.place_order(session, buyer.id, "BERRIES", "buy",
                        Decimal("2"), Decimal("2.00"),
                        next(a.id for a in buyer.accounts if a.currency == COIN))
    _run(session, 1)                    # the fill
    trades = [e for e in _events(session, "trade")]
    assert trades and Decimal(trades[-1]["quantity"]) == Decimal("2")
    assert buyer.accounts[0].balance == SEAT_COIN - Decimal("4")
    _run(session, 1)                    # the post reads its fill
    ask = _open_orders(session, post.id, "BERRIES", OrderSide.SELL)
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
    _run(session, 1)                    # the fill
    assert _hold(session, post.id, "WOOD") >= Decimal("3")
    assert next(a for a in seller.accounts if a.currency == COIN).balance \
        == SEAT_COIN + Decimal("3")
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
    assert [o.limit_price for o in ask] == [Decimal("2.00")]
    _run(session, 1)
    ask = _open_orders(session, post.id, "BERRIES", OrderSide.SELL)
    assert [o.limit_price for o in ask] == [Decimal("1.90")]   # 2.00*0.95
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
    # too thin to spread 4 each: one unit of the cheapest three fits
    # (MEAT 1.00 + WOOD 1.00 + YARN 2.00 = 4.00 <= 5.5); FLINT's 2.00
    # does not -- and NO good hoards the purse
    assert by_sym["MEAT"].quantity == Decimal("1")
    assert by_sym["WOOD"].quantity == Decimal("1")
    assert by_sym["YARN"].quantity == Decimal("1")
    assert "FLINT" not in by_sym


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
    # five wants now (the rotted larder re-opened the BERRIES appetite):
    # 12 coin against a 28-coin want -- every bid comes back, pro-rated
    assert set(buys) == {"MEAT", "WOOD", "YARN", "FLINT", "BERRIES"}
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

def test_the_map_six_places_eight_roads(session):
    """docs/spatial.md S4's stone-age map, as content rows: the hearth
    (start), thicket 1h, river and flint scrape 2h, deep forest 3h,
    post 1h down the valley (run 26's census: at four hours the post
    was a trip houses died taking -- two starved at its counter) --
    the forest and river roads stay the long ways round."""
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
    }
    hearth, post = keys["HEARTH"], keys["POST"]
    assert edges.distance_ticks(session, hearth, post) == 1
    # the valley road shortens the forest to 2h through the post
    assert edges.distance_ticks(session, hearth, keys["FOREST"]) == 2
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
    # breeding cadence: rounds 1-4 nothing; round 5 tops up toward 3;
    # round 10 stays at the cap
    assert spawns.apply_on_round(session, 4) == []
    born5 = spawns.apply_on_round(session, 5)
    assert len(born5) == 1                          # 2 alive, cap 3
    assert spawns.apply_on_round(session, 9) == []
    assert spawns.apply_on_round(session, 10) == []  # already at cap


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
    """The range (run 26's census): a denned wolf that cannot reach prey
    no longer waits to starve -- by night a hungry pack travels toward
    the hearth (the raid walk), and by day it works its way home to the
    forest (the game). The applied travel intents carry the itinerary."""
    from econengine import clock
    create_content(session)
    wolf = next(e for e in session.execute(select(Entity)).scalars()
                if e.name.startswith("Wolf Pack"))
    assert wolf.place.key == "FOREST"
    # hungry and dark: the raid walk begins (no prey named, no bite yet)
    markets.adjust_holding(session, wolf, "HUNGER", Decimal("4"))
    markets.adjust_holding(session, wolf, "SATIETY", Decimal("0"))
    while not clock.is_night(production.next_tick_number(session)):
        run_tick(session); session.commit()
    run_tick(session); session.commit()
    # through the night and past dawn: home again by day
    for _ in range(14):
        run_tick(session); session.commit()
    walks = [e["to"] for e in _events(session, "travel")
             if e["entity_id"] == wolf.id and e["status"] == "applied"]
    assert "HEARTH" in walks               # the night raid walk
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
