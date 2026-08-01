"""Capability model and intent gating (docs/actors.md step 1).

Capabilities are the privilege layer above ownership: they answer "which
entities may do the things that are NOT pure self-directed action" —
create money today; compel a transfer (tax) and set policy later. The
single check site is `Entity.has_capability`; `resolve_intent` consults
`INTENT_CAPABILITIES` at the same boundary where ownership is enforced.
"""
from decimal import Decimal

from econengine import capabilities
from econengine.lua_engine import Intent
from econengine.models import Base, EntityType
from econengine.scripting import resolve_intent
from econengine.services import (
    NotMonetaryAuthorityError,
    create_account,
    create_entity,
    issue_money,
)


def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


import pytest


@pytest.fixture
def session():
    return next(_session())


@pytest.fixture
def world(session):
    """An individual (no caps), a capability-granted bank, and a legacy
    monetary-authority government, each with a USD account."""
    ind = create_entity(session, "Individual", EntityType.INDIVIDUAL)
    bank = create_entity(session, "CapBank", EntityType.BANK)
    bank.capabilities = [capabilities.MONETARY_AUTHORITY]
    gov = create_entity(session, "Gov", EntityType.GOVERNMENT)
    gov.is_monetary_authority = True  # legacy path
    ia = create_account(session, ind, "USD", Decimal("0"))
    ba = create_account(session, bank, "USD", Decimal("0"))
    ga = create_account(session, gov, "USD", Decimal("0"))
    session.flush()
    return {"ind": ind, "bank": bank, "gov": gov,
            "ia": ia, "ba": ba, "ga": ga}


# --- has_capability model logic ---

def test_default_entity_has_no_capabilities(world):
    assert world["ind"].has_capability(capabilities.MONETARY_AUTHORITY) is False
    assert world["ind"].has_capability(capabilities.LEVY) is False


def test_capability_in_list_grants_it(world):
    assert world["bank"].has_capability(capabilities.MONETARY_AUTHORITY) is True
    # but only the ones granted
    assert world["bank"].has_capability(capabilities.LEVY) is False


def test_legacy_monetary_flag_implies_capability(world):
    """An entity created with is_monetary_authority=True (the old world)
    must keep working: the flag implies the monetary capability."""
    assert world["gov"].is_monetary_authority is True
    assert world["gov"].has_capability(capabilities.MONETARY_AUTHORITY) is True
    # and only the monetary one — the flag grants nothing else
    assert world["gov"].has_capability(capabilities.SET_FISCAL_POLICY) is False


# --- registry ---

def test_registry_gates_only_privileged_intents():
    # privileged: money creation and compelled transfer (levy)
    assert capabilities.required_for("issue_money") == capabilities.MONETARY_AUTHORITY
    assert capabilities.required_for("retire_money") == capabilities.MONETARY_AUTHORITY
    assert capabilities.required_for("levy") == capabilities.LEVY
    assert capabilities.required_for("set_fiscal_policy") == capabilities.SET_FISCAL_POLICY
    # ordinary self-directed action requires no capability — only ownership
    assert capabilities.required_for("transfer") is None
    assert capabilities.required_for("place_order") is None
    assert capabilities.required_for("start_process") is None
    # not-yet-built actions are declared but not wired
    assert "seize" not in capabilities.INTENT_CAPABILITIES


# --- resolve_intent capability gate ---

def _issue_intent(entity_id, account_id):
    return Intent(
        entity_id=entity_id,
        intent_type="issue_money",
        params={"account_id": account_id, "amount": "500", "reference": "issuance"},
        resource_ids=[],
        priority=10,
    )


def test_intent_rejects_entity_without_capability(world, session):
    out = resolve_intent(session, _issue_intent(world["ind"].id, world["ia"].id))
    assert out["status"] == "rejected"
    assert "monetary_authority" in out["reason"]
    # money was not created
    assert world["ia"].balance == Decimal("0")


def test_intent_allows_capability_granted_entity(world, session):
    out = resolve_intent(session, _issue_intent(world["bank"].id, world["ba"].id))
    assert out["status"] == "applied", out
    assert world["ba"].balance == Decimal("500")


def test_intent_allows_legacy_monetary_authority(world, session):
    """Backward compatibility: a world built before capabilities, where the
    government simply has is_monetary_authority=True, can still issue."""
    out = resolve_intent(session, _issue_intent(world["gov"].id, world["ga"].id))
    assert out["status"] == "applied", out
    assert world["ga"].balance == Decimal("500")


def test_ordinary_intent_unaffected_by_capabilities(world, session):
    """A transfer (no capability required) is gated only by ownership and
    balance — capabilities never enter the picture."""
    # fund the individual so the transfer can clear
    world["ia"].balance = Decimal("1000")
    out = resolve_intent(session, Intent(
        entity_id=world["ind"].id,
        intent_type="transfer",
        params={"from_account_id": world["ia"].id, "to_account_id": world["ba"].id,
                "amount": "100", "reference": "gift"},
        resource_ids=[],
        priority=10,
    ))
    assert out["status"] == "applied", out


# --- services-layer defense in depth (direct callers) ---

def test_service_rejects_entity_without_capability(world, session):
    try:
        issue_money(session, world["ia"], Decimal("100"), "ref")
        assert False, "expected NotMonetaryAuthorityError"
    except NotMonetaryAuthorityError:
        pass


def test_service_allows_capability_granted_entity(world, session):
    issue_money(session, world["ba"], Decimal("100"), "ref")
    assert world["ba"].balance == Decimal("100")
