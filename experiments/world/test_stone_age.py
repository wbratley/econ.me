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

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econengine import markets, parcels, production, services
from econengine.models import (
    Base, Entity, EntityStatus, EntityType, Holding, Script, ScriptType, Tick,
)
from econengine.tick import run_tick

from experiments.world import stone_age
from experiments.world.stone_age import (
    BERRY_BUFFER, COIN, SEAT_COIN, WARMTH_BUFFER, create_content, make_house,
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


# ===========================================================================
# FOCUSED FEATURE TESTS
# ===========================================================================

def test_content_and_coin_markets(session):
    create_content(session)
    rows = list(session.execute(select(markets.Market)).scalars())
    assert {m.symbol for m in rows} == {
        "LABOR", "BERRIES", "MEAT", "COOKED_MEAT", "WOOD", "YARN", "FLINT",
        "SPEAR", "BAG", "TRAP", "CLOTHES", "BED"}
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
    w = _seat(session)
    for _ in range(24):
        run_tick(session); session.commit()
        _act(session, w, "GATHER")
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
    bare, bagged = _seat(session, "Bare"), _seat(session, "Bagged")
    # Two BAGs: one stays reserved by the running gather between ticks
    # (scripts act in-tick after completions and need only one; the
    # between-tick harness needs a spare).
    markets.adjust_holding(session, bagged, "BAG", Decimal("2"))
    for _ in range(60):
        run_tick(session); session.commit()
        _act(session, bare, "GATHER")
        _act(session, bagged, "GATHER_BAG")

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
    markets.adjust_holding(session, hunter, "SPEAR", Decimal("3"))  # see BAG note; duration-2 hunts can hold two
    for _ in range(100):
        run_tick(session); session.commit()
        _act(session, bare, "HUNT")
        _act(session, hunter, "HUNT_SPEAR")

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

def test_neglect_kills(session):
    """Doing nothing is fatal: the buffers run out and the conditions --
    EXPOSURE first, then HUNGER -- reach their thresholds. Death lands
    inside two rounds (20-tick rounds): after tick 18, before tick 40."""
    create_content(session)
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
    """The graded ladder: REST under a SHELTER covers 1.0 of the 1.5
    WARMTH need -- a chronic 0.5/tick gap that equilibrates at 10, under
    the 18 threshold. Cold, uncomfortable, alive (REST is labor-free, so
    the whole LABOR budget still goes to gathering food)."""
    create_content(session)
    w = _seat(session, "Sheltered")
    parcels.add_facility(session, _camp(session, w), "SHELTER")
    for _ in range(60):
        run_tick(session); session.commit()
        try:
            production.start_process(session, w, "REST_SHELTERED",
                                     _camp(session, w).id)   # labor-free
        except Exception:
            pass
        _act(session, w, "GATHER")
    assert session.get(Entity, w.id).status == EntityStatus.ACTIVE
    assert _hold(session, w.id, "EXPOSURE") < Decimal("15")


def test_starter_survives(session):
    """The inherited script keeps a seat alive indefinitely at a
    hand-to-mouth pace: no incapacity, no script errors, both conditions
    well under their thresholds."""
    create_content(session)
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
    # Spares for the between-tick reservation window (see the bag test).
    markets.adjust_holding(session, digger, "BAG", Decimal("3"))
    for _ in range(200):
        run_tick(session); session.commit()
        _act(session, digger, "GATHER_BAG")
        _act(session, bare, "GATHER")

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
    """The manual WorldSetting carries the whole action space: the
    recipes, the death conditions, the ladder."""
    from econengine.models import WorldSetting

    create_content(session)
    row = session.get(WorldSetting, stone_age.MANUAL_KEY)
    assert row is not None
    text = row.value["text"]
    flat = " ".join(text.split()).lower()   # needles must survive line wraps
    for needle in ("GATHER_BAG", "HUNT_SPEAR", "MAKE_SHELTER", "EAT_RAW",
                   "dies at 15", "dies at 2.5", "SPEAR", "BAG", "the ladder",
                   "PRIVACY"):
        assert needle.lower() in flat, needle


def test_pack_sets_rival_privacy(session):
    """create_content turns on world.private_holdings: scripts see only
    their own pantry in this world."""
    from econengine.models import WorldSetting
    from econengine.scripting import PRIVATE_HOLDINGS_KEY

    create_content(session)
    row = session.get(WorldSetting, PRIVATE_HOLDINGS_KEY)
    assert row is not None and row.value
