"""Good definitions and the two tick passes they drive: auto-issue (top-up
semantics, entity-type filter) and decay (fraction lost, dust death,
summary events)."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.goods import apply_decay, auto_issue, create_good, get_good
from econengine.markets import adjust_holding, get_holding
from econengine.models import Base, EntityType
from econengine.services import create_entity


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------

def test_create_good_normalizes_and_validates(session):
    good = create_good(session, "labor", decay_per_tick=Decimal("0.5"),
                       auto_issue_quantity=Decimal("8"),
                       auto_issue_entity_type=EntityType.INDIVIDUAL)
    assert good.symbol == "LABOR"
    assert good.decay_per_tick == Decimal("0.5000")
    assert good.auto_issue_quantity == Decimal("8.0000")
    assert get_good(session, "labor") is good  # case-insensitive lookup

    with pytest.raises(ValueError, match="decay_per_tick"):
        create_good(session, "BAD", decay_per_tick=Decimal("1.5"))
    with pytest.raises(ValueError, match="decay_per_tick"):
        create_good(session, "BAD", decay_per_tick=Decimal("-0.1"))
    with pytest.raises(ValueError, match="auto_issue_quantity"):
        create_good(session, "BAD", auto_issue_quantity=Decimal("-1"))


# ---------------------------------------------------------------------------
# Auto-issue
# ---------------------------------------------------------------------------

def test_auto_issue_tops_up_instead_of_adding(session):
    """holding = max(holding, N): partial holders are topped up, holders at
    or above the target are untouched."""
    create_good(session, "LABOR", auto_issue_quantity=Decimal("8"))
    poor = create_entity(session, "Poor", EntityType.INDIVIDUAL)
    partial = create_entity(session, "Partial", EntityType.INDIVIDUAL)
    hoarder = create_entity(session, "Hoarder", EntityType.INDIVIDUAL)
    adjust_holding(session, partial, "LABOR", Decimal("3"))
    adjust_holding(session, hoarder, "LABOR", Decimal("20"))

    events = auto_issue(session, tick_number=1)

    assert get_holding(session, poor.id, "LABOR").quantity == Decimal("8")
    assert get_holding(session, partial.id, "LABOR").quantity == Decimal("8")
    assert get_holding(session, hoarder.id, "LABOR").quantity == Decimal("20")
    assert events == [{
        "type": "auto_issue",
        "entity_id": None,
        "symbol": "LABOR",
        "issued": "13.0000",  # 8 + 5, hoarder untouched
        "recipients": 2,
    }]


def test_auto_issue_entity_type_filter(session):
    create_good(session, "LABOR", auto_issue_quantity=Decimal("8"),
                auto_issue_entity_type=EntityType.INDIVIDUAL)
    person = create_entity(session, "Person", EntityType.INDIVIDUAL)
    firm = create_entity(session, "Firm", EntityType.BUSINESS)

    auto_issue(session, tick_number=1)

    assert get_holding(session, person.id, "LABOR").quantity == Decimal("8")
    assert get_holding(session, firm.id, "LABOR") is None


def test_auto_issue_silent_when_everyone_topped_up(session):
    create_good(session, "LABOR", auto_issue_quantity=Decimal("8"))
    person = create_entity(session, "Person", EntityType.INDIVIDUAL)
    adjust_holding(session, person, "LABOR", Decimal("8"))
    assert auto_issue(session, tick_number=1) == []


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------

def test_decay_removes_fraction_and_summarizes(session):
    create_good(session, "BREAD", decay_per_tick=Decimal("0.25"))
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    b = create_entity(session, "B", EntityType.INDIVIDUAL)
    adjust_holding(session, a, "BREAD", Decimal("10"))
    adjust_holding(session, b, "BREAD", Decimal("4"))

    events = apply_decay(session, tick_number=1)

    assert get_holding(session, a.id, "BREAD").quantity == Decimal("7.5")
    assert get_holding(session, b.id, "BREAD").quantity == Decimal("3")
    assert events == [{
        "type": "decay",
        "entity_id": None,
        "symbol": "BREAD",
        "decayed": "3.5000",
        "holders": 2,
    }]


def test_decay_dust_dies(session):
    """The amount LOST is quantized half-up, so a minimum-quantum holding
    loses the whole quantum instead of surviving forever."""
    create_good(session, "MILK", decay_per_tick=Decimal("0.5"))
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    adjust_holding(session, a, "MILK", Decimal("0.0001"))

    apply_decay(session, tick_number=1)

    assert get_holding(session, a.id, "MILK").quantity == Decimal("0")


def test_decay_one_expires_fully(session):
    create_good(session, "LABOR", decay_per_tick=Decimal("1"))
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    adjust_holding(session, a, "LABOR", Decimal("8"))

    apply_decay(session, tick_number=1)

    assert get_holding(session, a.id, "LABOR").quantity == Decimal("0")


def test_decay_silent_with_no_positive_holdings(session):
    create_good(session, "BREAD", decay_per_tick=Decimal("0.5"))
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    adjust_holding(session, a, "BREAD", Decimal("0"))
    assert apply_decay(session, tick_number=1) == []


def test_decay_ignores_goods_without_decay(session):
    create_good(session, "GOLD")  # decay 0
    a = create_entity(session, "A", EntityType.INDIVIDUAL)
    adjust_holding(session, a, "GOLD", Decimal("5"))

    assert apply_decay(session, tick_number=1) == []
    assert get_holding(session, a.id, "GOLD").quantity == Decimal("5")
