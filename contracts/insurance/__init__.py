"""Insurance reference contract (Step 5d).

An insurer collects premiums into a risk pool and pays a death benefit to a
beneficiary when a trigger event (``entity_incapacitated``) fires for a
policyholder — read from ``ctx.events``, the engine affordance no earlier
contract exercises. Validates ``ctx.events`` as a trigger source. See
``README.md``.
"""
from contracts.insurance.insurance import (
    DEFAULT_TRIGGER,
    Insurer,
    is_paid,
    is_triggered,
    open_insurer,
    policy,
    risk_pool_balance,
    total_coverage,
    underwrite,
)

__all__ = [
    "DEFAULT_TRIGGER",
    "Insurer",
    "is_paid",
    "is_triggered",
    "open_insurer",
    "policy",
    "risk_pool_balance",
    "total_coverage",
    "underwrite",
]
