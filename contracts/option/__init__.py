"""Options reference contract (Step 5d).

An exchange (CCP) matches a buyer (holder of a right) and a writer (the
obligated party). The buyer pays a premium; the writer posts margin. Settlement
pays the buyer only if the option is in the money — otherwise the writer's
margin returns whole. The deficiency case (payout exceeds margin) reuses the
futures ``seize``→``to_entity`` pattern. See ``README.md``.
"""
from contracts.option.option import (
    DEFAULT_MAINTENANCE,
    Exchange,
    buyer_value,
    open_exchange,
    open_option,
    position,
    position_status,
    settle,
    total_open_interest,
    writer_credit,
)

__all__ = [
    "DEFAULT_MAINTENANCE",
    "Exchange",
    "buyer_value",
    "open_exchange",
    "open_option",
    "position",
    "position_status",
    "settle",
    "total_open_interest",
    "writer_credit",
]
