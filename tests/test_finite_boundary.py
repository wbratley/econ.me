"""The intent boundary must reject non-finite amounts, not halt on them.

`Decimal("nan")` and `Decimal("inf")` construct cleanly; the poison hits
later, in a compare (`balance < amount`) or an enactment arithmetic step,
as `decimal.InvalidOperation` — which is NOT a ValueError, so it escapes
resolve_intent's rejection path and unwinds run_tick itself. These tests
pin the clean-reject behaviour at every parse site the bug hunt found:
amount_of (transfer / issue / place_order), the governance params
(create_proposal threshold/quorum), the constitution floor params, and
council member weights.
"""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.models import Base, EntityType
from econengine.lua_engine import Intent
from econengine.scripting import resolve_intent
from econengine.services import create_account, create_entity
from econengine.constitution import set_constitution
from econengine import councils


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    alice = create_entity(session, "Alice", EntityType.INDIVIDUAL)
    gov = create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True
    a = create_account(session, alice, "USD", initial_balance=Decimal("1000"))
    g = create_account(session, gov, "USD")
    return session, alice, gov, a, g


def _resolve(session, entity_id, intent_type, params):
    return resolve_intent(session, Intent(
        entity_id=entity_id, intent_type=intent_type, params=params,
        resource_ids=[],
    ))


def test_non_finite_transfer_amounts_are_clean_rejects(world):
    s, alice, gov, a, g = world
    for bad in ("nan", "inf", "-inf", "1e999", "-1e999"):
        r = _resolve(s, alice.id, "transfer", {
            "from_account_id": a.id, "to_account_id": g.id,
            "amount": bad, "reference": "",
        })
        assert r["status"] == "rejected", (bad, r)
        assert "finite" in r["reason"] or "invalid" in r["reason"], (bad, r)
    # nothing moved, nothing halted
    assert a.balance == Decimal("1000")
    assert g.balance == Decimal("0")


def test_infinite_mint_is_rejected_not_committed(world):
    s, alice, gov, a, g = world
    r = _resolve(s, gov.id, "issue_money", {
        "account_id": g.id, "amount": "inf", "reference": "",
    })
    assert r["status"] == "rejected", r
    assert g.balance == Decimal("0")


def test_non_finite_order_params_are_clean_rejects(world):
    s, alice, gov, a, g = world
    r = _resolve(s, alice.id, "place_order", {
        "symbol": "ACME", "side": "buy", "quantity": "nan",
        "limit_price": "1.00", "account_id": a.id,
    })
    assert r["status"] == "rejected", r
    r = _resolve(s, alice.id, "place_order", {
        "symbol": "ACME", "side": "buy", "quantity": "1",
        "limit_price": "inf", "account_id": a.id,
    })
    assert r["status"] == "rejected", r


def test_non_finite_governance_params_are_rejected_at_proposal_time(world):
    s, alice, gov, a, g = world
    for bad in ("nan", "inf"):
        r = _resolve(s, alice.id, "create_proposal", {
            "target_id": alice.id, "mutations": "[]",
            "weight_model": "registered", "threshold": bad, "quorum": "0",
            "title": "poison",
        })
        assert r["status"] == "rejected", (bad, r)
        r = _resolve(s, alice.id, "create_proposal", {
            "target_id": alice.id, "mutations": "[]",
            "weight_model": "registered", "threshold": "0.5", "quorum": bad,
            "title": "poison",
        })
        assert r["status"] == "rejected", (bad, r)


def test_poisoned_constitution_amendment_is_refused(world):
    s, alice, gov, a, g = world
    with pytest.raises(ValueError):
        set_constitution(s, {"supermajority_threshold": "nan"})
    with pytest.raises(ValueError):
        set_constitution(s, {"supermajority_quorum": "inf"})


def test_poisoned_council_weight_falls_back_to_one(session):
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    councils.set_register(session, "elders", {bob.id: "nan"})
    assert councils.member_weight(session, "elders", bob.id) == Decimal("1")
    assert councils.member_weight(session, "elders", "nonmember") == Decimal("0")


def test_full_precision_amounts_pass_and_land_quantized(world):
    """Lua's 0.1*3 arrives as 0.30000000000000004 — finite, so it passes
    the boundary untouched (the author's own formatting rides into the
    audit strings); persistence re-quantizes at Numeric(18,4) scale on
    read-back."""
    s, alice, gov, a, g = world
    r = _resolve(s, gov.id, "issue_money", {
        "account_id": g.id, "amount": "0.30000000000000004",
        "reference": "",
    })
    assert r["status"] == "applied", r
    s.expire_all()  # force the read-back through the column type
    assert g.balance == Decimal("0.3000")
