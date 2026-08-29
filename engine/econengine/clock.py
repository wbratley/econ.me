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

__all__ = [
    "TICKS_PER_DAY", "DAY_START_HOUR", "DAY_END_HOUR",
    "hour_of", "day_of", "is_day", "is_night", "clock_facts",
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


def clock_facts(tick_number: int) -> dict:
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
    }
