"""The event-hash RNG: canonical hashing, roll derivation, and exact
weighted branch selection."""

import json
from decimal import Decimal
from fractions import Fraction

import pytest

from econengine.rng import GENESIS_HASH, hash_events, outcome_roll, weighted_index


def test_hash_events_is_canonical():
    """Key order must not matter, and the hash must survive the round trip
    through a JSON column — an auditor recomputes it from stored events."""
    events = [{"type": "trade", "price": "10.00", "entity_id": "e1"}]
    reordered = [{"entity_id": "e1", "price": "10.00", "type": "trade"}]
    assert hash_events(events) == hash_events(reordered)
    round_tripped = json.loads(json.dumps(events))
    assert hash_events(round_tripped) == hash_events(events)
    assert hash_events([]) != hash_events(events)
    assert len(hash_events([])) == 64


def test_outcome_roll_derivation():
    roll = outcome_roll(GENESIS_HASH, "process-1")
    assert roll == outcome_roll(GENESIS_HASH, "process-1")  # reproducible
    assert roll != outcome_roll(GENESIS_HASH, "process-2")  # per-process
    assert roll != outcome_roll(hash_events([]), "process-1")  # per-seed
    assert len(roll) == 64


def test_weighted_index_extremes():
    weights = [Decimal("0.70"), Decimal("0.25"), Decimal("0.05")]
    assert weighted_index("0" * 64, weights) == 0
    assert weighted_index("f" * 64, weights) == len(weights) - 1


def test_weighted_index_matches_exact_fractions():
    """The shift-based selection must agree with exact rational arithmetic
    for arbitrary real digests."""
    weights = [Decimal("0.70"), Decimal("0.25"), Decimal("0.05")]
    total = sum(weights)
    for i in range(50):
        roll = outcome_roll(GENESIS_HASH, f"p{i}")
        fraction = Fraction(int(roll, 16), 2 ** 256)
        cumulative, expected = Decimal("0"), None
        for index, w in enumerate(weights):
            cumulative += w
            if fraction < Fraction(cumulative) / Fraction(total):
                expected = index
                break
        assert weighted_index(roll, weights) == expected


def test_weighted_index_unnormalized_weights():
    """Weights are relative odds; scaling them must not change selection."""
    a = [Decimal("0.70"), Decimal("0.25"), Decimal("0.05")]
    b = [Decimal("70"), Decimal("25"), Decimal("5")]
    for i in range(20):
        roll = outcome_roll(GENESIS_HASH, f"p{i}")
        assert weighted_index(roll, a) == weighted_index(roll, b)


def test_weighted_index_validations():
    with pytest.raises(ValueError, match="empty"):
        weighted_index("0" * 64, [])
    with pytest.raises(ValueError, match="positive"):
        weighted_index("0" * 64, [Decimal("1"), Decimal("0")])
