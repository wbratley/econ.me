"""
Auditable randomness — the event-hash RNG for stochastic recipes.

Determinism is an engine invariant: ticks must stay replayable and
verifiable, yet a roller must not be able to cherry-pick outcomes (inputs
are consumed at start, but cancellation exists). The tick structure supplies
a commit-reveal: a process completing at tick N can last be cancelled during
tick N-1's intent pass, and the hash of tick N-1's full event list is not
determined until after that pass. So

    outcome_roll = sha256(events_hash(tick N-1) ":" process_id)

is unknowable at the final cancellation opportunity, reproducible by any
auditor from persisted rows afterwards, and needs no oracle — the entropy is
the economy itself.

Everything here is pure integer/Decimal arithmetic on hex digests: no
floats, no platform-dependent rounding, so any auditor reproduces rolls
bit-for-bit from the stored tick events.
"""

import hashlib
import json
from decimal import Decimal

# seed for rolls before the first tick has ever run (an empty economy has no
# events to draw entropy from yet)
GENESIS_HASH = hashlib.sha256(b"econ.me:genesis").hexdigest()

_ROLL_BITS = 256  # sha256 output size; rolls are uniform in [0, 2**256)


def hash_events(events: list) -> str:
    """sha256 of the canonical JSON encoding of a tick's event list.

    Canonical means sorted keys and no whitespace, so the hash survives the
    round-trip through the JSON column — an auditor recomputes it from the
    stored events alone. Events are JSON-native by construction (quantities
    are already strings); anything else is a bug worth failing loudly on.
    """
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def outcome_roll(prev_events_hash: str, process_id: str) -> str:
    """The completion roll for one process: hex sha256 digest."""
    return hashlib.sha256(f"{prev_events_hash}:{process_id}".encode()).hexdigest()


def weighted_index(roll_hex: str, weights: list[Decimal]) -> int:
    """Map a roll onto a weight table: the index whose cumulative weight
    span covers the roll. Weights are the declared odds (any positive
    Decimals; they need not sum to 1). Exact integer arithmetic: weights are
    scaled to their smallest common unit and the roll's 256-bit integer is
    reduced onto [0, total) without floating point."""
    if not weights:
        raise ValueError("empty weight table")
    if any(w <= 0 for w in weights):
        raise ValueError("weights must be positive")
    # scale every weight to an integer (weights are Numeric(18,4), so
    # exponent >= -4; use the largest scale present)
    scale = max(-w.as_tuple().exponent for w in weights)
    units = [int(w.scaleb(scale)) for w in weights]
    total = sum(units)
    pick = int(roll_hex, 16) * total >> _ROLL_BITS  # uniform in [0, total)
    cumulative = 0
    for index, unit in enumerate(units):
        cumulative += unit
        if pick < cumulative:
            return index
    raise AssertionError("unreachable: pick < total by construction")
