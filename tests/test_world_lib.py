"""The per-world script library (docs/scripting.md section 3, Phase 1).

`scripting.world_lib` WorldSetting -> `get_world_libraries()` -> injected as
the read-only `world` namespace into every script run: BEHAVIOUR/POLICY via
run_tick, VALIDATOR/HOOK via the op dispatch, and the platform's dry-run
endpoint. Engine `std` is unconditional and needs no wiring. The demo world
(experiments/world) is the reference consumer -- its survival tests are the
content-level acceptance gate; these are the engine-level ones.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import scripting
from econengine.models import Base, Entity, EntityType, Script, ScriptType
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_world_lib_unset_by_default(session):
    assert scripting.get_world_lib(session) is None
    assert scripting.get_world_libraries(session) is None


def test_set_get_clear_roundtrip(session):
    src = "return { helper = function() return 1 end }"
    assert scripting.set_world_lib(session, src) == src
    assert scripting.get_world_lib(session) == src
    assert scripting.get_world_libraries(session) == {"world": src}

    scripting.set_world_lib(session, None)
    assert scripting.get_world_lib(session) is None
    assert scripting.get_world_libraries(session) is None


def test_blank_source_is_treated_as_unset(session):
    assert scripting.set_world_lib(session, "   ") is None
    assert scripting.get_world_lib(session) is None


def test_run_tick_injects_world_lib_into_behaviour_scripts(session):
    scripting.set_world_lib(session, "local t = {} function t.tag() return 'injected' end return t")
    entity = Entity(name="E", entity_type=EntityType.INDIVIDUAL)
    session.add(entity)
    session.flush()
    session.add(Script(
        name="e-behaviour",
        script_type=ScriptType.BEHAVIOUR,
        source="ctx.state.seen = world.tag(); ctx.state.std_ok = std.amount_str(1.5)",
        entity_id=entity.id,
        timeout_ms=200,
        state={},
    ))
    session.commit()

    run_tick(session)
    session.commit()

    script = session.query(Script).filter_by(name="e-behaviour").one()
    assert script.state == {"seen": "injected", "std_ok": "1.5000"}


def test_validator_sees_world_lib(session):
    # The op-path dispatch (fire_validators) injects the same tiers: a
    # validator written against world vocabulary must not fail on a nil
    # global. Exercised through a real money operation it vets.
    from decimal import Decimal

    from econengine import services

    scripting.set_world_lib(session, "local t = {} function t.ceiling() return 10000 end return t")
    alice = services.create_entity(session, "Alice", EntityType.INDIVIDUAL)
    bob = services.create_entity(session, "Bob", EntityType.INDIVIDUAL)
    a = services.create_account(session, alice, "USD", initial_balance=Decimal("100000"))
    b = services.create_account(session, bob, "USD", initial_balance=Decimal("0"))
    session.add(Script(
        name="cap-validator",
        script_type=ScriptType.VALIDATOR,
        source=(
            "if ctx.op.type == 'transfer' and tonumber(ctx.op.amount) "
            "> world.ceiling() then return {allow=false, reason='over cap'} end"
        ),
        timeout_ms=200,
        state={},
    ))
    session.flush()

    # Within the cap: the operation goes through (the validator used world.*).
    services.transfer(session, a, b, Decimal("50"), "ok")
    # Over the cap: vetoed BY the world-lib ceiling.
    with pytest.raises(scripting.OperationVetoedError, match="over cap"):
        services.transfer(session, a, b, Decimal("50000"), "no")
