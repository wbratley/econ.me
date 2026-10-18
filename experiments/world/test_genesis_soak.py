"""The run-56 genesis soak: the one-off bar for big changes.

Run 55 died totally (every house, day 6, still spring) and the
postmortem named three wounds: the wildlife starving squalorous
(COMFORT is scoped INDIVIDUAL and the beasts are INDIVIDUALs -- no
fire seat exists for a wolf, and SQUALOR's x0.85 ration refused the
whole-hour HUNT: zero hunts all run, the carrion economy died with
the packs), the study ladder never opening (a yarn-gated starter
against P1's certain-rung STUDY), and the houses gathered to mouth
against a rotting shelf. Phases 1-3 fixed each: engine need profiles
(commit 0646d3d), the beasts' biology and the starter's certainty
order (447673e + this one).

THE SOAK is the bar that pins it closed -- genesis exactly as a run
ships it (create_content + spawn cadence driven each day, the way the
platform's round path does), three houses running the shipped
STARTER, ten days headless. The soak measured the wounds while
fixing them, and its findings changed the pack twice more:

- the packs died of ERRANDS, not scarcity: at the house's thirst
  rate the cup ran dry daily and the river is an eight-hour commute
  from the deep wood (phase 1's rates fix: a predator drinks from the
  kill -- WATER at a lapping tenth);
- the packs died of the WATCH: a hungry-but-not-starving pack held
  the whole night's watch with no prey in reach (the blind bite
  refuses at an empty den) and never dens -- FATIGUE, every cadence
  wave, four nights flat (the bite is armed or it is not; everything
  else curls; and a tired pack does not walk the raid road);
- the floor's ladder never opened THREE ways: the study gate sat
  where the woodpile never wakes (the fire eats two and three logs a
  night), the hour gate closed MID-WALK (a floor that leaves at 9
  arrives at 11 and finds the rung locked), and a floor that
  re-decided its errand every tick zig-zagged scrape-bank-thicket in
  one daylight and starved on the roads (the day is COMMITTED now:
  one errand, taken from a fed morning, worked to dusk);
- the gathered shelf ROTS (two thirds of a basket by dawn): the
  belly is the pantry that cannot rot, so the floor eats the basket
  down instead of waking to a rotted larder -- and the smoke rung
  (jerky, the pantry that never rots) stands in the day plan for the
  day the meat shelf runs fat.

WHAT THE BAR IS (and is not): the world is a Monte Carlo (uuid-seeded
events-hash RNG -- the doctrine), so the soak asserts CERTAINTIES
where the mechanism is deterministic and RATES where the world is a
lottery. Certainties: no beast ever squalors (COMFORT is omitted --
there is no draw), no script errors, the packs work the game from the
first morning (>= 10 wolf hunts by day 2; run 55 had zero), the food
web breathes (wolves and boars alive at day 10), and the ladder opens
(a STUDY lands in every batch -- typically day 1-2, delayed by
thin-wood mornings; run 55 had none in six days).
The lottery: per-house ten-day survival runs ~60% (the thicket's
margin is thin by design and two bad days kill -- that margin is the
players' to beat; run 53's seats did) -- so the house bar is the
aggregate: at least 5 of 15 seats alive at day 10. Run 55's signature
was ZERO; the soak holds the line well above it.

Runtime: five worlds x 240 ticks, roughly two minutes -- this is the
one-off soak the roadmap licensed (big changes only), not a per-PR
gate.
"""

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine import spawns, tick as tick_mod
from econengine.models import Base, Entity, Holding, Script, Tick, Unlock
from econengine.models.entity import EntityStatus
from econengine.models.script import ScriptType

from . import stone_age
from .stone_age import create_content, make_house

STARTER = stone_age._gate_pack_script(stone_age.STARTER)

HOUSES = ("Alpha", "Bravo", "Charlie")
DAYS = 10
WORLDS = 5


def _soak_world(days=DAYS):
    """One genesis world, run headless, measured. Returns a dict of
    counts; the caller asserts bars against them."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        create_content(session)
        house_ids = []
        for name in HOUSES:
            seat = make_house(session, name)
            session.add(Script(
                name=f"starter-behaviour-{seat.id}",
                script_type=ScriptType.BEHAVIOUR,
                source=STARTER,
                entity_id=seat.id,
                timeout_ms=200,
                state={},
            ))
            house_ids.append(seat.id)
        session.commit()

        out = dict(
            wolf_hunts_day2=0,          # the food web works from dawn one
            studies=[],                 # ticks where a STUDY_KNAPPING applied
            squalor_beast_days=0,       # any beast holding SQUALOR, any day
            script_errors=0,
            deaths=[],                  # (tick, name, condition)
        )
        for tick_no in range(1, days * 24 + 1):
            # The platform drives the cadence on round boundaries; a
            # day is a round here (round == day in the genesis run's
            # configuration: ECON_TICKS_PER_ROUND=24).
            if (tick_no - 1) % 24 == 0:
                spawns.apply_on_round(session, (tick_no - 1) // 24 + 1)
            tick = tick_mod.run_tick(session)
            session.commit()

            for event in tick.events or []:
                if event.get("type") == "script_error":
                    out["script_errors"] += 1
                recipe = (event.get("params") or {}).get("recipe")
                if (event.get("type") == "start_process"
                        and event.get("status") == "applied"
                        and recipe == "HUNT" and tick_no <= 48):
                    hunter = session.get(Entity, event.get("entity_id"))
                    if hunter and hunter.name and "Wolf" in hunter.name:
                        out["wolf_hunts_day2"] += 1
                if (event.get("type") == "start_process"
                        and event.get("status") == "applied"
                        and recipe == "STUDY_KNAPPING"):
                    out["studies"].append(tick_no)
                if event.get("type") == "entity_incapacitated":
                    gone = session.get(Entity, event.get("entity_id"))
                    out["deaths"].append(
                        (tick_no, gone.name if gone else "?",
                         event.get("condition")))

            # The squalor bar is checked DAILY (holdings move with the
            # estate on death): any beast, any day, any SQUALOR at all.
            if tick_no % 24 == 0:
                beasts = [
                    row[0] for row in session.execute(
                        select(Entity.id, Entity.name)).fetchall()
                    if row[1] and ("Wolf" in row[1] or "Boar" in row[1])]
                if beasts:
                    out["squalor_beast_days"] += session.execute(
                        select(func.count()).select_from(Holding).where(
                            Holding.symbol == "SQUALOR",
                            Holding.quantity > 0,
                            Holding.entity_id.in_(beasts))).scalar()

        def alive(prefix):
            return sum(
                1 for ent in session.execute(select(Entity)).scalars()
                if ent.name and prefix in ent.name
                and ent.status == EntityStatus.ACTIVE)

        out["houses_alive"] = sum(
            1 for hid in house_ids
            if session.get(Entity, hid).status == EntityStatus.ACTIVE)
        out["wolves_alive"] = alive("Wolf")
        out["boars_alive"] = alive("Boar")
        out["knapping_unlocks"] = session.execute(
            select(func.count()).select_from(Unlock).where(
                Unlock.entity_id.in_(house_ids))).scalar()
        out["spears_held"] = session.execute(
            select(func.count()).select_from(Holding).where(
                Holding.symbol == "SPEAR", Holding.quantity > 0,
                Holding.entity_id.in_(house_ids))).scalar()
        return out


def test_the_genesis_soak_pins_run_55_closed():
    """Five genesis worlds, ten days, the shipped starter and wildlife.

    Certainties (mechanism-determined, every world): the beasts never
    squalor, no script errors, the packs hunt from the first morning,
    and the food web breathes at day 10. The ladder and the seats are
    lotteries with wide margins: the ladder OPENS in every world
    (typically day 1-2, delayed by thin-wood mornings -- the commons
    fire eats six logs a day and the study gate will not walk a
    woodless floor), and the seats survive at ~60% (the thicket's
    margin is thin by design and two bad days kill -- that margin is
    the players' to beat; run 53's seats did) -- so those bars are
    aggregates: at least 4 unlocks across fifteen seats, at least 5
    seats alive at day 10, at least 3 worlds keeping a living house.
    Run 55's signature was zero everything; the soak holds the line
    well above it. The lottery (statistical, documented): at least 5 of 15
    houses alive at day 10 -- run 55's signature was zero houses and
    zero hunts; this holds the line the postmortem drew.
    """
    worlds = [_soak_world() for _ in range(WORLDS)]

    # --- certainties: every world, no exceptions -------------------
    for i, w in enumerate(worlds):
        assert w["squalor_beast_days"] == 0, (
            f"world {i}: a beast squalored {w['squalor_beast_days']} "
            f"beast-days -- the comfort omission is not holding")
        assert w["script_errors"] == 0, (
            f"world {i}: {w['script_errors']} script errors")
        assert w["wolf_hunts_day2"] >= 10, (
            f"world {i}: only {w['wolf_hunts_day2']} wolf hunts by day 2 "
            f"(run 55's signature was zero -- the food web must work)")
        assert w["wolves_alive"] >= 1, (
            f"world {i}: no wolf pack alive at day {DAYS}: {w['deaths']}")
        assert w["boars_alive"] >= 1, (
            f"world {i}: no boar alive at day {DAYS}: {w['deaths']}")

    # --- the lottery: aggregate survival across all fifteen seats ---
    # The study morning competes with the commons fire's wood diet
    # (six logs a day, communally) and the gather table's luck --
    # which correlates per world (one events-hash stream). Typical
    # worlds open the ladder day 1-2; a thin-wood world can delay a
    # house for days. The run-55 closure is that the ladder OPENS,
    # in every world, and unlocks accumulate -- not that every
    # morning is a school morning.
    for i, w in enumerate(worlds):
        assert w["studies"], (
            f"world {i}: the study ladder never opened at all in "
            f"{DAYS} days -- run 55's signature, back from the dead")
    unlocks = sum(w["knapping_unlocks"] for w in worlds)
    assert unlocks >= 4, (
        f"only {unlocks}/15 seats hold KNAPPING at day {DAYS} -- the "
        f"certain rung is not being taken")
    alive = sum(w["houses_alive"] for w in worlds)
    assert alive >= 5, (
        f"only {alive}/15 houses alive at day {DAYS} across {WORLDS} "
        f"worlds -- the starter's survival margin collapsed "
        f"(deaths: {[w['deaths'] for w in worlds]})")

    # The dynasty's capital line: most worlds keep a living house.
    worlds_with_a_house = sum(1 for w in worlds if w["houses_alive"] >= 1)
    assert worlds_with_a_house >= 3, (
        f"only {worlds_with_a_house}/{WORLDS} worlds kept a living house")
