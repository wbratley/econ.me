"""ctx.query.world_setting — the signal/observation primitive (Step 5c).

A generic read for any world-level votable datum. It is the layer behind
fiscal_policy() and constitution() (those are named specialisations) and
the Fork-A signal channel: an oracle (platform/admin) writes a
WorldSetting each tick; contract scripts read it here instead of each
keeping their own copy. The engine affordance is the READ — writing is
platform/oracle-layer, like every other ctx.query (all read-only).
"""
import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.models import Base, EntityType, Script, ScriptType, WorldSetting
from econengine.services import create_account, create_entity
from econengine.tick import run_tick


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
    g = create_account(session, gov, "USD", initial_balance=Decimal("1000"))
    return session, alice, gov, a, g


def make_script(session, name, source, script_type, entity=None, **kwargs):
    script = Script(
        name=name, source=source, script_type=script_type,
        entity_id=entity.id if entity else None, **kwargs,
    )
    session.add(script)
    session.flush()
    return script


def set_setting(session, key, value):
    """Stand in for the platform oracle that posts signals between ticks."""
    session.add(WorldSetting(key=key, value=value))
    session.flush()


def test_world_setting_reads_existing(world):
    """The basic read: a stored dict comes back as a Lua table."""
    session, alice, gov, a, g = world
    set_setting(session, "signal:wheat", {"price": "50", "currency": "USD"})
    script = make_script(
        session, "reader",
        "ctx.state.price = ctx.query.world_setting('signal:wheat').price",
        ScriptType.POLICY, entity=gov,
    )
    run_tick(session)
    assert script.state["price"] == "50"


def test_world_setting_returns_nil_for_unset(world):
    """An unset key reads as nil -- a dark feed, not an error."""
    session, alice, gov, a, g = world
    script = make_script(
        session, "reader",
        "local s = ctx.query.world_setting('signal:missing')\n"
        "ctx.state.seen = (s == nil)",
        ScriptType.POLICY, entity=gov,
    )
    run_tick(session)
    assert script.state["seen"] is True


def test_signal_drives_action(world):
    """The Fork-A pattern end to end: an oracle posts a price signal; a
    consumer POLICY reacts to it (levies a surcharge when wheat is dear)."""
    session, alice, gov, a, g = world
    set_setting(session, "signal:wheat", {"price": "55"})
    script = make_script(
        session, "reactor",
        "local feed = ctx.query.world_setting('signal:wheat')\n"
        "local dear = feed ~= nil and tonumber(feed.price) > 50\n"
        "ctx.state.surcharge = dear and 'yes' or 'no'",
        ScriptType.POLICY, entity=gov,
    )
    run_tick(session)
    assert script.state["surcharge"] == "yes"   # price 55 > 50

    # Oracle lowers the price next tick; the consumer re-reads live.
    session.query(WorldSetting).filter_by(key="signal:wheat").update(
        {"value": {"price": "40"}}
    )
    run_tick(session)
    assert script.state["surcharge"] == "no"


def test_world_setting_accessible_in_validator(world):
    """VALIDATOR scripts (the _op_ctx path) get ctx.query too -- the bond's
    monetization cap relies on this to read a data-driven ceiling."""
    session, alice, gov, a, g = world
    set_setting(session, "monetary:issue_cap", {"cap": "0"})
    validator = make_script(
        session, "hard-money",
        "if ctx.op.type ~= 'issue_money' then return true end\n"
        "local s = ctx.query.world_setting('monetary:issue_cap')\n"
        "local cap = (s ~= nil and s.cap) or '0'\n"
        "if tonumber(ctx.op.amount) > tonumber(cap) then\n"
        "  return {allow=false, reason='over cap ' .. cap}\n"
        "end\n"
        "return true",
        ScriptType.VALIDATOR,
    )
    issuer = make_script(
        session, "issuer",
        f"ctx.action.issue_money('{g.id}', '100', 'print')",
        ScriptType.POLICY, entity=gov,
    )

    run_tick(session)
    assert g.balance == Decimal("1000")   # vetoed: cap is 0, no money created

    # Oracle lifts the cap; the same validator now allows issuance.
    session.query(WorldSetting).filter_by(key="monetary:issue_cap").update(
        {"value": {"cap": "200"}}
    )
    run_tick(session)
    assert g.balance == Decimal("1100")   # 100 created, under the 200 cap
