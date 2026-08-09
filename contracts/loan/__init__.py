"""Secured loan reference contract (Step 5d).

A lender lends base money, takes collateral, and on default enforces with
``levy`` (take cash by force) and ``seize`` (take collateral by force) — the
enforcement spine of private debt. See ``README.md``.
"""
from contracts.loan.loan import (
    DEFAULT_RATE,
    Loan,
    create_loan,
    enforce,
    is_in_default,
    loan_due,
    loan_status,
    open_lender,
    repay,
    total_outstanding,
)

__all__ = [
    "DEFAULT_RATE",
    "Loan",
    "create_loan",
    "enforce",
    "is_in_default",
    "loan_due",
    "loan_status",
    "open_lender",
    "repay",
    "total_outstanding",
]
