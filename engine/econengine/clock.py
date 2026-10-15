"""The day/night clock (run 18's engine variable).

tick = hour, round = 24 ticks = one day. Every clock fact in the engine
derives from the tick number through THESE functions -- needs draws,
auto-issue gating, recipe darkness refusals, the script ctx and the
std.* queries all ask the same module, so there is one definition of
"night" in the world.

The window (hours 6..19 inclusive = 14 labor-hours) is engine policy,
not pack data: it is the physics of THIS engine's day, like the tick
itself. Packs declare what is daylight-gated (a recipe, a ration, a
night draw); the window is not theirs to move.
"""

from __future__ import annotations

TICKS_PER_DAY = 24
DAY_START_HOUR = 6    # inclusive: first hour of usable light
DAY_END_HOUR = 20     # exclusive: first hour of dark

# The season calendar (run 54's E1): the same doctrine as the daylight
# window, one tier up. Seasons are pure tick arithmetic over days -- the
# engine owns the four names and the default season length; a world may
# vote itself longer seasons via the WorldSetting `clock.season_days`
# (data, like the compute budget), and packs declare what is
# season-gated (a recipe, a per-season need draw). Tick 1 opens day 1 of
# SPRING. The default (6 days/season) makes a 24-day run exactly one
# year: days 19..24 are WINTER.
SEASONS = ("SPRING", "SUMMER", "AUTUMN", "WINTER")
SEASON_DAYS = 6
SEASON_DAYS_KEY = "clock.season_days"

__all__ = [
    "TICKS_PER_DAY", "DAY_START_HOUR", "DAY_END_HOUR",
    "SEASONS", "SEASON_DAYS", "SEASON_DAYS_KEY",
    "hour_of", "day_of", "is_day", "is_night", "clock_facts",
    "season_of", "season_day_of", "season_days_of",
]


def hour_of(tick_number: int) -> int:
    """Hour of day for a tick: tick 1 opens day 1 at hour 0."""
    return (tick_number - 1) % TICKS_PER_DAY


def day_of(tick_number: int) -> int:
    """Day number (1-based): ticks 1..24 are day 1."""
    return (tick_number - 1) // TICKS_PER_DAY + 1


def is_day(tick_number: int) -> bool:
    return DAY_START_HOUR <= hour_of(tick_number) < DAY_END_HOUR


def is_night(tick_number: int) -> bool:
    return not is_day(tick_number)


def season_of(tick_number: int, days_per_season: int = SEASON_DAYS) -> str:
    """Season name for a tick: tick 1 opens SPRING day 1; the year is
    SEASONS in order, `days_per_season` days each, cycling forever (a
    12-day-season world is on its second year by day 49)."""
    if days_per_season < 1:
        raise ValueError("days_per_season must be >= 1")
    index = (day_of(tick_number) - 1) // days_per_season
    return SEASONS[index % len(SEASONS)]


def season_day_of(tick_number: int, days_per_season: int = SEASON_DAYS) -> int:
    """Day within the current season, 1..days_per_season."""
    if days_per_season < 1:
        raise ValueError("days_per_season must be >= 1")
    return (day_of(tick_number) - 1) % days_per_season + 1


def season_days_of(session) -> int:
    """The world's season length: WorldSetting `clock.season_days`
    (votable data), else the engine default. Session-in only, like every
    engine op. Missing or nonsense values fall back to SEASON_DAYS --
    a broken vote must not break the calendar."""
    from .models import WorldSetting  # local: this module's arithmetic stays import-free
    setting = session.get(WorldSetting, SEASON_DAYS_KEY)
    if setting is None or not isinstance(setting.value, (int, float)) or int(setting.value) < 1:
        return SEASON_DAYS
    return int(setting.value)


def clock_facts(tick_number: int, days_per_season: int = SEASON_DAYS) -> dict:
    """The one dict every clock surface renders (script ctx, MCP
    entity_state, dashboards): plain facts, no policy text."""
    hour = hour_of(tick_number)
    day = is_day(tick_number)
    return {
        "tick": tick_number,
        "day": day_of(tick_number),
        "hour": hour,
        "is_day": day,
        "is_night": not day,
        "daylight_hours": f"{DAY_START_HOUR:02d}:00-{DAY_END_HOUR - 1:02d}:00",
        "season": season_of(tick_number, days_per_season),
        "season_day": season_day_of(tick_number, days_per_season),
        "days_per_season": days_per_season,
    }
