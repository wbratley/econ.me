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
web breathes (wolves and boars alive at day 10). The ladder opens for
the BATCH -- typically a world's first study lands day 1-2, delayed by
thin-wood mornings (>= 3 of 15 seats hold KNAPPING at day 10; run 55
had none in six days) -- and per-seat study is a lottery, so there is
no per-world study bar (it flaked: in about half of batches one world
had no study at all).
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
from econengine.models.place import Place as _Place
from econengine.models.recipe import Recipe as _Recipe
from econengine.models.script import ScriptType

from . import stone_age
from .stone_age import create_content, make_house

STARTER = stone_age._gate_pack_script(stone_age.STARTER)

HOUSES = ("Alpha", "Bravo", "Charlie")
DAYS = 10
WORLDS = 5

# --- The L2 floor-tax instrument (post-70 lever spec) ---------------------
# Run 70's census: WARM_BY_FIRE 33-40% + TRAVEL hops 29-42% of every
# house's completed work, facility_dark 13 of 20 days, and 2.4 gathers
# per round-trip -- the floor tax that leaves ~5 discretionary ticks a
# day. Those were SEAT-run numbers (agent-edited scripts); the soak's
# starter-only baseline is its own number and the lever is tuned
# against IT. Buckets (the run-census methodology, so the two are
# comparable): MOVE = travel hops; WARM = WARM_BY_FIRE + SLEEP_BY_FIRE
# + PACE (fireless pacing is warmth too); STOKE/COOK/eat are counted
# raw, not folded. Shares are computed over travel + process_completed,
# per house, then aggregated across the batch.
MOVE_IS = "travel"            # travel hops count as the tick's act
WARM_RECIPES = ("WARM_BY_FIRE", "SLEEP_BY_FIRE", "PACE")
GATHER_RECIPES = ("GATHER", "GATHER_BAG")


def _instrument_init(house_ids):
    return dict(
        recipe_counts={hid: {} for hid in house_ids},
        instant_counts={hid: {} for hid in house_ids},
        stoke_refusals=0,
        travel_hops={hid: 0 for hid in house_ids},
        trips=0,                    # away-spells (left home at all)
        gathers_per_trip=[],        # gathers in each away-spell with >=1
        trip_close_hour={},         # close-hour histogram (diagnostic)
        trip_gather_hops={},        # (gathers, hops-in-spell) histogram
        dark_events=0,
        dark_days=set(),
        dark_hours={},              # hour-of-day histogram of darks
        busy={hid: set() for hid in house_ids},   # ticks that were
        # the house's act: a travel hop, a process completion, or an
        # applied start of a duration >= 1 recipe (instants -- stokes,
        # meals -- are tick-free). IDLE = alive ticks minus busy: the
        # discretionary budget the L3 decision rule reads.
        alive_ticks={hid: 0 for hid in house_ids},
    )


def _instrument_tick(inst, tick_no, tick, house_ids, instant_recipes=frozenset()):
    """Fold one tick's events into the instrument.

    Instant (duration-0) recipes -- stokes, meals, drinks -- complete
    inline at start and never emit process_completed (verified against
    production.py: the remaining==0 branch calls _complete inside
    start_process). They are tick-free, so they stay OUT of the share
    denominators (a stoke is not an hour); they are counted as raw
    diagnostics (instants), including stoke refusals -- the capacity
    wall (fuel 3 +4 > cap 6) is a first-class lever signal. Busy vs
    idle uses `instant_recipes` (duration_ticks == 0 codes, fetched
    once by the caller) so instant starts do not mark their tick busy.
    """
    for event in tick.events or []:
        et = event.get("type")
        if et == "facility_dark":
            inst["dark_events"] += 1
            inst["dark_days"].add((tick_no - 1) // 24 + 1)
            hh = (tick_no - 1) % 24
            inst["dark_hours"][hh] = inst["dark_hours"].get(hh, 0) + 1
        if et == "travel" and event.get("entity_id") in house_ids:
            inst["travel_hops"][event["entity_id"]] += 1
            inst["busy"][event["entity_id"]].add(tick_no)
        if et == "process_completed" and event.get("entity_id") in house_ids:
            counts = inst["recipe_counts"][event["entity_id"]]
            counts[event.get("recipe")] = counts.get(event.get("recipe"), 0) + 1
            inst["busy"][event["entity_id"]].add(tick_no)
        if et == "start_process" and event.get("entity_id") in house_ids:
            recipe = (event.get("params") or {}).get("recipe")
            if event.get("status") == "applied":
                counts = inst["instant_counts"][event["entity_id"]]
                counts[recipe] = counts.get(recipe, 0) + 1
                if recipe not in instant_recipes:
                    inst["busy"][event["entity_id"]].add(tick_no)
            elif recipe == "STOKE_FIRE":
                inst["stoke_refusals"] += 1


def _instrument_finish(inst, spell_state):
    """Close any still-open away-spell at end of run."""
    for hid, st in spell_state.items():
        if st["away"] and st["gathers"] >= 1:
            inst["gathers_per_trip"].append(st["gathers"])


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
            shelter_by_d3=set(),        # G0: houses that completed the roof
            shelter_by_d5=set(),        # G0: the honest window (d3 is the
                                        # study->post->roof chain's edge)
            skin_by_d3=set(),           # G0: houses that bought a skin
        )
        # L2: places are read per tick to reconstruct away-spells (the
        # gather trip shape); place ids -> keys once, the map is static.
        place_keys = {
            row[0]: row[1] for row in session.execute(
                select(_Place.id, _Place.key)).fetchall()}
        instant_recipes = frozenset(
            row[0] for row in session.execute(
                select(_Recipe.code).where(
                    _Recipe.duration_ticks == 0)).fetchall())
        home_key = next((k for k in place_keys.values() if k == "HEARTH"),
                        "HEARTH")
        inst = _instrument_init(house_ids)
        spell = {hid: dict(away=False, gathers=0) for hid in house_ids}
        for tick_no in range(1, days * 24 + 1):
            # The platform drives the cadence on round boundaries; a
            # day is a round here (round == day in the genesis run's
            # configuration: ECON_TICKS_PER_ROUND=24).
            if (tick_no - 1) % 24 == 0:
                spawns.apply_on_round(session, (tick_no - 1) // 24 + 1)
            tick = tick_mod.run_tick(session)
            session.commit()
            _instrument_tick(inst, tick_no, tick, house_ids, instant_recipes)
            # the away-spell tracker reads END-of-tick places: a spell
            # opens the first tick the house stands away, closes the
            # tick it is home again; gathers land in the open spell.
            for hid in house_ids:
                ent = session.get(Entity, hid)
                if ent.status == EntityStatus.ACTIVE:
                    inst["alive_ticks"][hid] += 1
                pkey = place_keys.get(ent.location_place_id)
                st = spell[hid]
                if pkey != home_key:
                    if not st["away"]:
                        st["away"], st["gathers"] = True, 0
                        st["hops"] = 0
                        st["start"] = tick_no
                        inst["trips"] += 1
                    if (tick.events or []) and any(
                            e.get("type") == "process_completed"
                            and e.get("entity_id") == hid
                            and e.get("recipe") in GATHER_RECIPES
                            for e in tick.events):
                        st["gathers"] += 1
                    if (tick.events or []) and any(
                            e.get("type") == "travel"
                            and e.get("entity_id") == hid
                            for e in tick.events):
                        st["hops"] = st.get("hops", 0) + 1
                elif st["away"]:
                    st["away"] = False
                    hh = (tick_no - 1) % 24
                    inst["trip_close_hour"][hh] = \
                        inst["trip_close_hour"].get(hh, 0) + 1
                    key = (st.get("gathers", 0), st.get("hops", 0))
                    inst["trip_gather_hops"][key] = \
                        inst["trip_gather_hops"].get(key, 0) + 1
                    if st["gathers"] >= 1:
                        inst["gathers_per_trip"].append(st["gathers"])

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
                if (event.get("type") == "process_completed"
                        and event.get("recipe") == "MAKE_SHELTER"):
                    if tick_no <= 72:
                        out["shelter_by_d3"].add(event.get("entity_id"))
                    if tick_no <= 120:
                        out["shelter_by_d5"].add(event.get("entity_id"))
                if (event.get("type") == "trade"
                        and event.get("side") == "buy"
                        and event.get("market") == "WATERSKIN"
                        and tick_no <= 72):
                    out["skin_by_d3"].add(event.get("entity_id"))
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
        out["instrument"] = dict(
            inst, dark_days=len(inst["dark_days"]))
        return out


def test_the_genesis_soak_pins_run_55_closed():
    """Five genesis worlds, ten days, the shipped starter and wildlife.

    Certainties (mechanism-determined, every world): the beasts never
    squalor, no script errors, the packs hunt from the first morning,
    and the food web breathes at day 10. The ladder and the seats are
    lotteries with wide margins: the ladder OPENS for the batch
    (typically a world's first study lands day 1-2, a thin-wood
    morning delays a house for days -- the commons fire eats six logs
    a day and the study gate will not walk a woodless floor), and the
    seats survive at ~60% (the thicket's margin is thin by design and
    two bad days kill -- that margin is the players' to beat; run 53's
    seats did) -- so those bars are aggregates: at least 4 unlocks
    across fifteen seats, at least 5 seats alive at day 10, at least
    2 worlds keeping a living house. There is deliberately NO per-world
    study bar: a world's study is a per-seat lottery (a fed morning,
    a banked stoke, the gather roll), and a per-world "at least one"
    bar was tried and flaked -- in about half of five-world batches
    one world had no study at all, with the old and the new gather
    table alike (A/B: 7 and 10 seats studied pre, 8 and 13 post). The
    rung itself is pinned at certainty in test_stone_age (the study
    grants the rank; re-study is idempotent); this bar says only that
    the shipped world REACHES it.
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
    # house for days. The run-55 closure is that the ladder OPENS
    # for the batch and unlocks accumulate -- not that every morning
    # is a school morning, and not that every world opens (a per-world
    # bar flakes on one house's morning; see the docstring).
    # KNAPPING is granted only by STUDY_KNAPPING, so the unlock count
    # IS the count of seats whose ladder opened -- the batch bar.
    studies = sum(len(w["studies"]) for w in worlds)
    unlocks = sum(w["knapping_unlocks"] for w in worlds)
    assert unlocks >= 4, (
        f"only {unlocks}/15 seats hold KNAPPING at day {DAYS} "
        f"({studies} study starts) -- the certain rung is not being "
        f"taken; run 55's signature was zero")
    alive = sum(w["houses_alive"] for w in worlds)
    # Bundle A re-baseline (measured, not guessed: the herb roll spends
    # ~5% of gather hours on non-food and the floor's margin is steep --
    # 110 isolated worlds put per-house survival at ~50-58% against the
    # ~60% pre-bundle (and the study at ~43% of seats); bars move
    # 5->4 alive, 3->2 worlds-with-house, 4->3 unlocks, keeping the
    # run-55 line well above zero at the new rates (~8% batch flake,
    # the soak's historical rate). The census (run
    # A) is the tuning pass the doc names)
    #
    # The resume ledger re-baseline (a4e82ee, measured: 15 worlds + 4
    # gated batches of 5): 30/45 seats alive (67%, batch min 7 of 5
    # worlds), 27/45 seats holding KNAPPING (60%, batch min 5), 17/20
    # worlds kept a living house (85%, batch min 4/5) -- and studies
    # equal unlocks exactly, because a started one-tick rung always
    # finishes. The starter's ladder day no longer burns itself waiting
    # on a per-tick draw its own survival loop eats. Bars sit one step
    # under the observed batch minima (6/4/3): a drift back to the
    # bundle-A floor (~3.7 alive, ~2.1 unlocks per 5-world batch) now
    # FAILS instead of passing silently, without flaking on the tail.
    assert alive >= 6, (
        f"only {alive}/15 houses alive at day {DAYS} across {WORLDS} "
        f"worlds -- the starter's survival margin collapsed "
        f"(deaths: {[w['deaths'] for w in worlds]})")

    # --- the L2 floor-tax bars (run 70 -> post-70 lever spec) --------
    # Aggregate-ONLY (a per-world share flakes on the same per-seat
    # lottery as the study bar). Baseline (pre-lever, 5 worlds, the
    # shipped run-69 starter): MOVE 36-37%, gath/trip 1.8-1.9. The
    # lever's measured landing: MOVE 25-26%, gath/trip 3.2-4.3 across
    # four gated batches. Bars sit one step past the baseline with
    # headroom under the landing zone (the headroom doctrine): 31%
    # catches a regression to the un-batched shape, 2.5 catches the
    # one-gather commute, and neither flakes on batch variance. The
    # gath/trip ceiling is structural, not tuned: the carry law caps a
    # bare back at 20 and the fattest bare roll is six, so a trip from
    # an empty back tops out near four rolls -- and a walk-out bar
    # tight enough to force four-plus (load <= 5) starves the floor
    # (measured: alive 2/15 -- the larder rots faster than the back
    # drains; batching past ~3 rolls banks rot, not food). The
    # facility_dark story is the same shape: stoke timing is optimal
    # now (bank at fuel <= 2 -- the zero-waste moment under the +4
    # stoke and cap 6; the watch is a home duty, no mid-trip chase),
    # and the remaining darks are wood-supply-bound (0.6 logs per
    # gather roll against a fire that eats six a day) -- reported as a
    # diagnostic in measure(), not barred.
    hops = sum(sum(w["instrument"]["travel_hops"].values()) for w in worlds)
    done = sum(sum(c for per in w["instrument"]["recipe_counts"].values()
                   for c in per.values()) for w in worlds)
    move_share = hops / max(hops + done, 1)
    assert move_share <= 0.31, (
        f"aggregate MOVE share {100 * move_share:.0f}% across {WORLDS} "
        f"worlds -- the batched-trip starter regressed past the bar "
        f"(pre-lever baseline 36-37%, landing zone 25-26%)")
    all_trips = [g for w in worlds
                 for g in w["instrument"]["gathers_per_trip"]]
    gpt = sum(all_trips) / max(len(all_trips), 1)
    assert gpt >= 2.5, (
        f"aggregate gathers per round-trip {gpt:.1f} across {WORLDS} "
        f"worlds ({len(all_trips)} trips) -- the commute tax is back "
        f"(pre-lever baseline 1.8-1.9, landing zone 3.2-4.3)")

    # The dynasty's capital line: most worlds keep a living house.
    worlds_with_a_house = sum(1 for w in worlds if w["houses_alive"] >= 1)
    assert worlds_with_a_house >= 3, (
        f"only {worlds_with_a_house}/{WORLDS} worlds kept a living house -- "
        f"the survival margin collapsed")

    # --- the G0 scaffold bars (generational spec; run 73's audit) ------
    # The floor now OWNS the survival exits (skin, cords, the roof) as
    # block-marked scaffold -- measured by ADOPTION, aggregate-only
    # (the day-commitment ladder is a per-house lottery: a poor house
    # spends every daylight on food and the tool run waits -- the
    # Malthusian point, not a defect). Two gated samples: skin_d3
    # 13/15 and 8/15; shelter_d3 4/15 and 4/15 with the honest window
    # at day 5 (the chain is study d1 -> post d2 -> roof d3+, and the
    # roof's dawn-wood gate slips a day in poor draws): shelter_d5
    # 10/15 and 4/15. Bars sit one step under the observed minima.
    skin_d3 = sum(len(w["skin_by_d3"]) for w in worlds)
    assert skin_d3 >= 5, (
        f"only {skin_d3}/15 houses held a waterskin by day 3 -- the "
        f"tool run stopped reaching the counter (observed 8-13)")
    shelter_d5 = sum(len(w["shelter_by_d5"]) for w in worlds)
    assert shelter_d5 >= 3, (
        f"only {shelter_d5}/15 houses raised a roof by day 5 -- the "
        f"cord-to-roof conversion broke (observed 4-10)")
    # WARM share: the spec guessed the scaffold would push it under
    # 45%; measured, it does NOT (46-49% both samples -- the roof
    # survives DARK nights, it does not replace lit-fire hours, which
    # the bucket already counts as sleep). The bar is the regression
    # guard, not the thesis: the fire watch stays the day's spine
    # until a later lever attacks the time budget itself.
    warm = sum(sum(per.get(r, 0)
                   for per in w["instrument"]["recipe_counts"].values()
                   for r in WARM_RECIPES)
               for w in worlds)
    warm_share = warm / max(hops + done, 1)
    assert warm_share <= 0.52, (
        f"aggregate WARM share {100 * warm_share:.0f}% -- the fire "
        f"watch regressed past the guard (landing zone 41-49%)")


def measure(n=WORLDS, days=DAYS):
    """L2 floor-tax report: run n soak worlds headless and print the
    starter's process-share baseline (not collected by pytest -- a
    measurement entry point for the lever's before/after table):

        .venv/bin/python -c \
          "from experiments.world.test_genesis_soak import measure; measure()"

    Buckets per the module doc: MOVE = travel hops, WARM = the three
    warmth recipes, shares over travel + process_completed -- the same
    methodology as the run-69/70 seat census, so starter numbers and
    seat numbers are directly comparable.
    """
    worlds = [_soak_world(days) for _ in range(n)]
    print(f"\n=== L2 floor-tax baseline: {n} worlds x {days} days, "
          f"shipped starter ===")
    tot = dict(hops=0, done=0, warm=0, gather=0, trips=0, dark=0,
               dark_days=0, alive=0, seats=0)
    all_trips = []
    for i, w in enumerate(worlds):
        inst = w["instrument"]
        hops = sum(inst["travel_hops"].values())
        counts = {}
        for per_house in inst["recipe_counts"].values():
            for sym, c in per_house.items():
                counts[sym] = counts.get(sym, 0) + c
        instants = {}
        for per_house in inst["instant_counts"].values():
            for sym, c in per_house.items():
                instants[sym] = instants.get(sym, 0) + c
        done = sum(counts.values())
        warm = sum(counts.get(r, 0) for r in WARM_RECIPES)
        gath = sum(counts.get(r, 0) for r in GATHER_RECIPES)
        trips = inst["gathers_per_trip"]
        all_trips.extend(trips)
        gh = dict(sorted(inst["trip_gather_hops"].items()))
        print(f"world {i}: alive={w['houses_alive']}/3 "
              f"hops={hops} done={done} "
              f"MOVE={100 * hops / max(hops + done, 1):.0f}% "
              f"WARM={100 * warm / max(hops + done, 1):.0f}% "
              f"GATHER={100 * gath / max(hops + done, 1):.0f}% "
              f"trips={inst['trips']} gath/trip="
              f"{sum(trips) / max(len(trips), 1):.1f} "
              f"dark={inst['dark_events']}ev/{inst['dark_days']}d "
              f"stokes={instants.get('STOKE_FIRE', 0)}"
              f"(+{inst['stoke_refusals']}ref) "
              f"eats={instants.get('EAT_BERRIES', 0) + instants.get('EAT_APPLES', 0)} "
              f"shelter_d3={len(w['shelter_by_d3'])}"
              f"/d5={len(w['shelter_by_d5'])} skin_d3={len(w['skin_by_d3'])}")
        alive_t = sum(inst["alive_ticks"].values())
        busy_t = sum(len(v) for v in inst["busy"].values())
        idle_t = max(alive_t - busy_t, 0)
        print(f"         IDLE={100 * idle_t / max(alive_t, 1):.0f}% "
              f"of {alive_t} alive house-ticks "
              f"(~{idle_t / 10 / 3:.1f} idle h/house/day)")
        print(f"         close-hour={dict(sorted(inst['trip_close_hour'].items()))}")
        print(f"         (gathers,hops)={gh}")
        print(f"         dark-hours={dict(sorted(inst['dark_hours'].items()))}")
        tot["hops"] += hops; tot["done"] += done; tot["warm"] += warm
        tot["gather"] += gath; tot["trips"] += inst["trips"]
        tot["dark"] += inst["dark_events"]
        tot["dark_days"] += inst["dark_days"]
        tot["alive"] += w["houses_alive"]; tot["seats"] += 3
    shel = sum(len(w["shelter_by_d3"]) for w in worlds)
    shel5 = sum(len(w["shelter_by_d5"]) for w in worlds)
    skin = sum(len(w["skin_by_d3"]) for w in worlds)
    t = tot["hops"] + tot["done"]
    print(f"AGGREGATE: alive={tot['alive']}/{tot['seats']} "
          f"shelter_d3={shel}/d5={shel5}/{tot['seats']} "
          f"skin_d3={skin}/{tot['seats']} "
          f"MOVE={100 * tot['hops'] / max(t, 1):.0f}% "
          f"WARM={100 * tot['warm'] / max(t, 1):.0f}% "
          f"GATHER={100 * tot['gather'] / max(t, 1):.0f}% "
          f"gath/trip={sum(all_trips) / max(len(all_trips), 1):.1f} "
          f"(n={len(all_trips)}) dark={tot['dark']}ev/"
          f"{tot['dark_days']}d")
    return worlds
