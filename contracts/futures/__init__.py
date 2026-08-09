"""Futures + margin reference contract (Step 5d).

An exchange (CCP) matches longs/shorts, holds margin, marks to market from a
signal price, and settles — seizing goods from a defaulter to make the winner
whole. Validates ``seize`` as a margin call and the signal convention (5c).
See ``README.md``.
"""
from contracts.futures.futures import (
    DEFAULT_MAINTENANCE,
    Exchange,
    long_credit,
    open_exchange,
    open_future,
    position,
    position_status,
    settle,
    short_credit,
    total_open_interest,
)

__all__ = [
    "DEFAULT_MAINTENANCE",
    "Exchange",
    "long_credit",
    "open_exchange",
    "open_future",
    "position",
    "position_status",
    "settle",
    "short_credit",
    "total_open_interest",
]
