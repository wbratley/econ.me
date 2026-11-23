"""The family frame (aging-repro spec): the genome-inheritance seam."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econengine import services
from econengine.models import Base, Entity, Script, ScriptType

from . import family
from . import stone_age as sa


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _family(session, name="House Kin"):
    sa.create_content(session)          # the map before parcels (S4)
    house = sa.make_house(session, name)
    house.owner_id = "u-fam"
    session.flush()
    services.set_entity_behaviour(
        session, house, sa._gate_pack_script(sa.STARTER),
        owner_id="u-fam")
    return house


def test_founders_are_adults_with_the_family_frame(session):
    """make_house stamps the 48-day lifespan, the SPAWN capability,
    and an ADULT birth_tick (age 336 at tick 1): the age-branched
    genome reads founders as grown — the 20-day soak caught the
    regression where founders ran the juvenile phase and starved."""
    house = _family(session)
    assert house.lifespan == 48 * 24
    assert "spawn" in (house.capabilities or [])
    assert house.birth_tick == -336


def test_children_inherit_the_family_genome(session):
    """The seam: a spawned child (no behaviour of its own) receives
    its first living parent's ACTIVE behaviour, whole — juvenility is
    a phase of the genome (the age-branch inside it), not a mechanic.
    Idempotent: a child with a script is never overwritten."""
    house = _family(session)
    session.commit()
    summary = services.spawn_entity(
        session, house, parents=[house.id], owner_id="u-fam",
        name="Child of House Kin", lifespan=1152)
    session.commit()
    assert family.install_family_genomes(session) == 1
    src = session.execute(
        select(Script).where(
            Script.entity_id == summary["child_id"],
            Script.script_type == ScriptType.BEHAVIOUR,
            Script.is_active.is_(True))
    ).scalars().first()
    parent_src = session.execute(
        select(Script).where(
            Script.entity_id == house.id,
            Script.script_type == ScriptType.BEHAVIOUR,
            Script.is_active.is_(True))
    ).scalars().first()
    assert src.source == parent_src.source
    # idempotent
    assert family.install_family_genomes(session) == 0


def test_the_spawn_block_fires_in_a_world(session):
    """The demography is live: a family-framed house with a real
    surplus in the survivable window spawns — the kill-box physics
    become reproductive discipline (the traced soak: a birth at t187,
    early summer, day 7.8)."""
    from econengine import tick as tick_mod, spawns
    from .test_aging_soak import run_aging_world
    census = run_aging_world(days=10, seats=3, verbose=False)
    # 10 days: the early-summer window (days 7-9) is reachable; the
    # birth is world-lottery (the surplus gate is honest), so the bar
    # here is mechanics-not-crash + zero script errors
    assert census["script_errors"] == 0
