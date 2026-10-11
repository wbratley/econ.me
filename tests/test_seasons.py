"""The season calendar (run 54 E1).

One definition of "winter", one tier above the one definition of
"night": pure tick arithmetic in clock.py, a votable length
(clock.season_days), a recipe gate (requires_season) refused with the
season named, and per-season need draws that resolve most-specific-first
(season-night > season > global-night > base).
"""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine import clock
from econengine.markets import adjust_holding
from econengine.models import Base, EntityType, Tick
from econengine.needs import create_need, required_quantity, run_consumption
from econengine.production import create_recipe, start_process
from econengine.services import create_entity
from econengine.tick import set_season_days


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# --- the calendar (pure arithmetic) ---

def test_tick_one_is_spring_day_one():
    assert clock.season_of(1) == "SPRING"
    assert clock.season_day_of(1) == 1


def test_default_season_boundaries():
    # 6-day seasons: days 1-6 SPRING, 7-12 SUMMER, 13-18 AUTUMN, 19-24 WINTER
    assert clock.season_of(24 * 6) == "SPRING"      # day 6, last spring tick
    assert clock.season_of(24 * 6 + 1) == "SUMMER"  # day 7
    assert clock.season_of(24 * 12) == "SUMMER"     # day 12
    assert clock.season_of(24 * 12 + 1) == "AUTUMN"  # day 13
    assert clock.season_of(24 * 18) == "AUTUMN"     # day 18
    assert clock.season_of(24 * 18 + 1) == "WINTER"  # day 19: a 24-day run's winter
    assert clock.season_of(24 * 24) == "WINTER"     # day 24
    assert clock.season_of(24 * 24 + 1) == "SPRING"  # the next year


def test_season_day_cycles_within_season():
    assert clock.season_day_of(24 * 6) == 6     # spring day 6
    assert clock.season_day_of(24 * 6 + 1) == 1  # summer day 1
    assert clock.season_day_of(24 * 18 + 3) == 1  # winter day 1 (hour 2)


def test_voted_season_length():
    # 12-day seasons: days 1-12 SPRING, 13-24 SUMMER -- no winter in a
    # 24-day run at all.
    assert clock.season_of(24 * 6, 12) == "SPRING"
    assert clock.season_of(24 * 6 + 1, 12) == "SPRING"   # day 7
    assert clock.season_of(24 * 12, 12) == "SPRING"      # day 12
    assert clock.season_of(24 * 12 + 1, 12) == "SUMMER"  # day 13
    assert clock.season_of(24 * 13, 12) == "SUMMER"


def test_season_math_rejects_broken_length():
    with pytest.raises(ValueError):
        clock.season_of(1, 0)
    with pytest.raises(ValueError):
        clock.season_day_of(1, -3)


def test_clock_facts_grows_the_season():
    facts = clock.clock_facts(24 * 18 + 1, 6)  # day 19, hour 0
    assert facts["season"] == "WINTER"
    assert facts["season_day"] == 1
    assert facts["days_per_season"] == 6
    # the legacy keys are untouched
    assert facts["tick"] == 24 * 18 + 1
    assert facts["day"] == 19
    assert facts["hour"] == 0
    assert facts["is_night"] is True
    # default length = the engine's 6
    assert clock.clock_facts(1)["season"] == "SPRING"


# --- the votable knob ---

def test_season_days_setting(session):
    # no setting: the engine default
    assert clock.season_days_of(session) == clock.SEASON_DAYS
    set_season_days(session, 12)
    assert clock.season_days_of(session) == 12
    with pytest.raises(ValueError):
        set_season_days(session, 0)
    set_season_days(session, None)  # unset = back to default
    assert clock.season_days_of(session) == clock.SEASON_DAYS


def test_broken_setting_value_falls_back(session):
    from econengine.models import WorldSetting
    session.add(WorldSetting(key=clock.SEASON_DAYS_KEY, value="soon"))
    session.flush()
    assert clock.season_days_of(session) == clock.SEASON_DAYS


# --- the recipe gate ---


def _advance_to(session, tick_number):
    """Pin next_tick_number by inserting the tick rows before it
    (start_process reads the clock from the tick table)."""
    current = session.execute(select(func.count()).select_from(Tick)).scalar() or 0
    for n in range(current + 1, tick_number):
        session.add(Tick(number=n))
    session.flush()


def test_recipe_allowed_in_season_refused_outside(session):
    entity = create_entity(session, "Frost", EntityType.INDIVIDUAL)
    create_recipe(
        session, "ice_fish",
        inputs={}, outputs={"FISH": Decimal("1")},
        duration_ticks=1,
        requires_season=["WINTER"],
    )
    # next tick 432 = day 18 hour 23: still AUTUMN, refused with the season named
    _advance_to(session, 432)
    with pytest.raises(
        ValueError,
        match=r"wrong season for ICE_FISH \(AUTUMN.*it runs in WINTER\)",
    ):
        start_process(session, entity, "ICE_FISH")
    # one tick later it is day 19: WINTER, allowed
    _advance_to(session, 433)
    process = start_process(session, entity, "ICE_FISH")
    assert process.recipe.code == "ICE_FISH"


def test_recipe_season_gate_reads_the_voted_length(session):
    entity = create_entity(session, "Villager", EntityType.INDIVIDUAL)
    create_recipe(
        session, "mow_meadow",
        inputs={}, outputs={"HAY": Decimal("1")},
        duration_ticks=1,
        requires_season=["SPRING", "SUMMER"],
    )
    set_season_days(session, 12)
    # next tick 433 = day 19: WINTER at 6-day seasons, but SUMMER day 7
    # at the voted 12 -- the same calendar day, a different year.
    _advance_to(session, 433)
    process = start_process(session, entity, "MOW_MEADOW")
    assert process.recipe.code == "MOW_MEADOW"


def test_create_recipe_validates_season_names(session):
    with pytest.raises(ValueError, match="unknown seasons"):
        create_recipe(
            session, "monsoon_farm",
            inputs={}, outputs={"RICE": Decimal("1")},
            duration_ticks=1,
            requires_season=["WET"],
        )
    # None = every season, all of history
    plain = create_recipe(
        session, "plain_work",
        inputs={}, outputs={"STONE": Decimal("1")},
        duration_ticks=1,
    )
    assert plain.requires_season is None


# --- the need draws ---

def test_season_draws_resolve_most_specific_first(session):
    entity = create_entity(session, "Cold", EntityType.INDIVIDUAL)
    adjust_holding(session, entity, "WARMTH", Decimal("100"))
    need = create_need(
        session, "warmth_test",
        quantity_per_tick=Decimal("1"),
        satisfiers=["WARMTH"],
        night_quantity_per_tick=Decimal("3"),
        season_quantity_per_tick={"WINTER": Decimal("2")},
        season_night_quantity_per_tick={"WINTER": Decimal("4")},
    )
    # spring day (tick 7 = day 1 hour 6): base
    assert required_quantity(need, 7) == Decimal("1")
    # spring night (tick 2 = day 1 hour 1): global night
    assert required_quantity(need, 2) == Decimal("3")
    # winter day (tick 24*18+7 = day 19 hour 6): season
    assert required_quantity(need, 24 * 18 + 7) == Decimal("2")
    # winter night (tick 24*18+2 = day 19 hour 1): season-night
    assert required_quantity(need, 24 * 18 + 2) == Decimal("4")
    # and run_consumption draws the same numbers
    spring_day = run_consumption(session, tick_number=7)[0]
    winter_night = run_consumption(session, tick_number=24 * 18 + 2)[0]
    assert spring_day["required"] == "1.0000"
    assert winter_night["required"] == "4.0000"
    assert winter_night["type"] == "need_satisfied"


def test_season_draw_with_voted_length(session):
    entity = create_entity(session, "Long", EntityType.INDIVIDUAL)
    adjust_holding(session, entity, "WARMTH", Decimal("100"))
    create_need(
        session, "warmth_long",
        quantity_per_tick=Decimal("1"),
        satisfiers=["WARMTH"],
        season_quantity_per_tick={"SUMMER": Decimal("5")},
    )
    # day 7 = tick 145: SPRING at 6-day seasons, but SUMMER at 12... no:
    # 12-day seasons put day 7 in SPRING; day 13 (tick 289) is SUMMER.
    assert run_consumption(session, tick_number=145, days_per_season=12)[0][
        "required"] == "1.0000"
    assert run_consumption(session, tick_number=289, days_per_season=12)[0][
        "required"] == "5.0000"


def test_create_need_validates_season_maps(session):
    with pytest.raises(ValueError, match="unknown seasons"):
        create_need(
            session, "bad_season",
            quantity_per_tick=Decimal("1"), satisfiers=["X"],
            season_quantity_per_tick={"MONSOON": Decimal("2")},
        )
    with pytest.raises(ValueError, match="positive"):
        create_need(
            session, "bad_qty",
            quantity_per_tick=Decimal("1"), satisfiers=["X"],
            season_quantity_per_tick={"WINTER": Decimal("0")},
        )


def test_legacy_need_never_varies_by_season(session):
    entity = create_entity(session, "Plain", EntityType.INDIVIDUAL)
    adjust_holding(session, entity, "FOOD", Decimal("100"))
    create_need(
        session, "food_plain",
        quantity_per_tick=Decimal("0.5"),
        satisfiers=["FOOD"],
    )
    for tick in (1, 2, 24 * 18 + 1, 24 * 24, 24 * 30):
        assert run_consumption(session, tick_number=tick)[0]["required"] == "0.5000"
