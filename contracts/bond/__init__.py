"""Government bond — a fungible, traded claim (Step 5d, first deliverable).

A bond is a ``Holding`` row (Fork A) whose terms live in the issuer's
servicing-script ``state``. ``gov_bond.lua`` honours coupons and redemption
each tick from ``ctx.tick``; :mod:`contracts.bond.bond` issues and retires.
"""
