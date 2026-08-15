"""Ownership-gated autonomy path — a player rewrites their own entity's
BEHAVIOUR script (docs/game.md §6, Phase 1).

``set_entity_behaviour`` is the distinct counterpart to ``set_script``:
where ``set_script`` is the LEGISLATE-gated governed write surface for
polity-owned entities (legislation), ``set_entity_behaviour`` is the
ownership-gated surface for player-owned entities (autonomy). It needs no
vote and no capability, only proof that the acting user owns the entity.

Three guards:
  - ownership  — ``entity.owner_id == owner_id`` (the autonomy guard);
  - immutable  — ``is_fixed`` entities refused (the server tier, §4);
  - scope      — BEHAVIOUR only (the signature fixes it; POLICY /
                 VALIDATOR / HOOK are legislation/constitution).

And the two cross-tier invariants:
  - the money-scope invariant still binds (an autonomy script spends only
    its own entity's money);
  - both governed paths (autonomy + legislation) refuse a fixed entity.

Phase 3 adds the fourth guard:
  - lint -- submit-time strictness: the source is checked against the
    injected tiers with the install gate's standard. Vocabulary that is
    not there (the nil-call trap that zombied the first live demo's
    founder) refuses the submission; the entity keeps its behaviour.
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econengine import capabilities, services
from econengine.models import (
    Base, Entity, EntityType, Script, ScriptType, Tick, User,
)
from econengine.scripting import ScriptRejected, build_queries
from econengine.services import (
    MissingCapabilityError, OwnershipError, create_entity, set_entity_behaviour,
    set_script,
)
from econengine.tick import run_tick


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _user(session, name="owner"):
    u = User(email=f"{name}@x", name=name, provider="test", provider_id=name)
    session.add(u)
    session.flush()
    return u


def _owned_entity(session, owner, name="Owned", entity_type=EntityType.INDIVIDUAL, fixed=False):
    e = create_entity(session, name, entity_type)
    e.owner_id = owner.id
    e.is_fixed = fixed
    session.flush()
    return e


# ===========================================================================
# ownership guard
# ===========================================================================

def test_owner_can_set_their_entity_behaviour(session):
    """The happy path: the owner replaces their entity's behaviour; the new
    BEHAVIOUR script is active, bound to the entity, and runs as it."""
    owner = _user(session)
    other = _user(session, "other")
    entity = _owned_entity(session, owner)

    script, _ = set_entity_behaviour(
        session, entity, "ctx.state.tick = ctx.tick", owner_id=owner.id,
    )
    session.commit()

    assert script.script_type == ScriptType.BEHAVIOUR
    assert script.is_active is True
    assert script.entity_id == entity.id
    assert script.lineage_id == f"behaviour:{entity.id}"
    assert script.source == "ctx.state.tick = ctx.tick"
    # a different user does not own it -- the ownership guard fires
    with pytest.raises(OwnershipError):
        set_entity_behaviour(
            session, entity, "-- nope", owner_id=other.id,
        )


def test_server_owned_entity_is_not_owner_editable(session):
    """A server-owned entity (owner_id NULL) is not editable by any user --
    ownership fails by construction. (The immutable tier is refused on a
    separate axis below; this is the autonomy guard doing its job.)"""
    player = _user(session)
    npc = create_entity(session, "NPC labourer", EntityType.INDIVIDUAL)
    assert npc.owner_id is None  # server-owned
    with pytest.raises(OwnershipError):
        set_entity_behaviour(session, npc, "-- nope", owner_id=player.id)


# ===========================================================================
# immutable-tier guard (is_fixed)
# ===========================================================================

def test_fixed_entity_refused_by_autonomy(session):
    """An is_fixed entity is the immutable tier: autonomy refuses it, even
    for its owner."""
    owner = _user(session)
    npc = _owned_entity(session, owner, name="NPC", fixed=True)
    with pytest.raises(ValueError, match="fixed"):
        set_entity_behaviour(session, npc, "-- nope", owner_id=owner.id)


def test_fixed_entity_refused_by_legislation(session):
    """Both governed paths refuse the immutable tier (§4): a polity holding
    LEGISLATE may not use set_script to change a fixed entity's behaviour."""
    owner = _user(session)
    npc = _owned_entity(session, owner, name="NPC", fixed=True)
    legislator = create_entity(session, "Polity", EntityType.GOVERNMENT)
    legislator.capabilities = [capabilities.LEGISLATE]
    session.flush()
    with pytest.raises(ValueError, match="fixed"):
        set_script(
            session, legislator, ScriptType.BEHAVIOUR,
            f"behaviour:{npc.id}", "-- imposed", entity_id=npc.id,
        )


def test_legislation_still_works_on_non_fixed_entity(session):
    """The fixed refusal is surgical: legislation on an ordinary
    (non-fixed) entity is unaffected."""
    legislator = create_entity(session, "Polity", EntityType.GOVERNMENT)
    legislator.capabilities = [capabilities.LEGISLATE]
    target = create_entity(session, "Public corp", EntityType.BUSINESS)
    session.flush()
    assert target.is_fixed is False
    s = set_script(
        session, legislator, ScriptType.BEHAVIOUR,
        "behaviour:public", "-- legislated", entity_id=target.id,
    )
    assert s.is_active is True


# ===========================================================================
# scope + retire/activate semantics
# ===========================================================================

def test_only_behaviour_is_produced(session):
    """The autonomy path can only ever yield a BEHAVIOUR script -- POLICY /
    VALIDATOR / HOOK are out of scope by signature, not by guard."""
    owner = _user(session)
    entity = _owned_entity(session, owner)
    script, _ = set_entity_behaviour(session, entity, "-- x", owner_id=owner.id)
    assert script.script_type == ScriptType.BEHAVIOUR


def test_replace_retires_prior_behaviour_and_versions(session):
    """Successive autonomy edits retire the predecessor and version within
    one entity-scoped lineage; the entity ends with exactly one active
    BEHAVIOUR."""
    owner = _user(session)
    entity = _owned_entity(session, owner)

    first, _ = set_entity_behaviour(session, entity, "-- v1", owner_id=owner.id)
    second, _ = set_entity_behaviour(session, entity, "-- v2", owner_id=owner.id)
    third, _ = set_entity_behaviour(session, entity, "-- v3", owner_id=owner.id)
    session.flush()

    assert first.is_active is False
    assert second.is_active is False
    assert third.is_active is True
    assert first.name == f"behaviour:{entity.id}#1"
    assert second.name == f"behaviour:{entity.id}#2"
    assert third.name == f"behaviour:{entity.id}#3"
    assert all(s.lineage_id == f"behaviour:{entity.id}" for s in (first, second, third))

    active = session.execute(
        select(Script).where(
            Script.entity_id == entity.id,
            Script.script_type == ScriptType.BEHAVIOUR,
            Script.is_active.is_(True),
        )
    ).scalars().all()
    assert len(active) == 1
    assert active[0].id == third.id


def test_replace_retires_behaviour_set_by_legislation(session):
    """Autonomy owns the whole behaviour surface: a prior legislated
    BEHAVIOUR is retired when the owner sets their own -- "my entity's
    behaviour" is singular."""
    owner = _user(session)
    entity = _owned_entity(session, owner)
    legislator = create_entity(session, "Polity", EntityType.GOVERNMENT)
    legislator.capabilities = [capabilities.LEGISLATE]
    session.flush()

    imposed = set_script(
        session, legislator, ScriptType.BEHAVIOUR, "behaviour:imposed",
        "-- imposed", entity_id=entity.id,
    )
    assert imposed.is_active is True

    own, _ = set_entity_behaviour(session, entity, "-- mine", owner_id=owner.id)
    session.flush()
    assert imposed.is_active is False
    assert own.is_active is True


def test_empty_source_refused(session):
    owner = _user(session)
    entity = _owned_entity(session, owner)
    with pytest.raises(ValueError, match="source"):
        set_entity_behaviour(session, entity, "   ", owner_id=owner.id)


# ===========================================================================
# end-to-end: the new behaviour actually runs next tick
# ===========================================================================

def test_new_behaviour_runs_as_entity_next_tick(session):
    """The whole point: the owner sets a behaviour and it runs as the entity
    on the next tick, affecting only that entity's own assets."""
    owner = _user(session)
    entity = _owned_entity(session, owner)
    services.create_account(session, entity, "USD", 100)
    session.commit()

    # A behaviour that records the tick it ran on into state.
    set_entity_behaviour(
        session, entity,
        "ctx.state['ran_on'] = ctx.tick",
        owner_id=owner.id,
    )
    session.commit()

    run_tick(session)
    session.commit()

    active = session.execute(
        select(Script).where(
            Script.entity_id == entity.id,
            Script.script_type == ScriptType.BEHAVIOUR,
            Script.is_active.is_(True),
        )
    ).scalar_one()
    assert active.state.get("ran_on") == 1  # ran on tick 1


def test_money_scope_invariant_still_binds(session):
    """Autonomy changes who writes the script, never what it can reach: an
    autonomy script can only move money out of its own entity's accounts
    (the tick.py money-scope invariant). A behaviour trying to spend a
    *stranger's* account is rejected."""
    from econengine.lua_engine import Intent
    from econengine.scripting import resolve_intent

    owner = _user(session)
    entity = _owned_entity(session, owner)
    stranger = create_entity(session, "Stranger", EntityType.INDIVIDUAL)
    own_acct = services.create_account(session, entity, "USD", 100)
    stranger_acct = services.create_account(session, stranger, "USD", 100)
    sink = services.create_account(session, entity, "USD2", 0)
    session.commit()

    # A behaviour that queues an intent to drain the STRANGER's account.
    set_entity_behaviour(
        session, entity,
        "ctx.action.transfer(ctx.state.stranger, ctx.state.sink, '50', '')",
        owner_id=owner.id,
    )
    # Inject the stranger's account id into the script's visible state.
    active = session.execute(
        select(Script).where(Script.entity_id == entity.id, Script.is_active.is_(True))
    ).scalar_one()
    active.state = {"stranger": stranger_acct.id, "sink": sink.id}
    session.commit()

    run_tick(session)
    session.commit()

    # The intent was rejected -- the stranger keeps its money.
    tick = session.execute(select(Tick).where(Tick.number == 1)).scalar_one()
    rejection = next(
        e for e in tick.events
        if e.get("type") == "transfer" and e.get("status") == "rejected"
    )
    assert "own source account" in rejection["reason"]
    assert stranger_acct.balance == 100


# ---------------------------------------------------------------------------
# Phase 3: submit-time strictness -- the lint guard
# ---------------------------------------------------------------------------

def test_lint_refuses_nil_call_trap_before_any_mutation(session):
    """The zombie trap, refused at submit: the entity KEEPS its current
    behaviour (nothing retired, nothing stored) and the player gets the
    finding in hand."""
    owner = _user(session)
    entity = _owned_entity(session, owner)
    good, _ = set_entity_behaviour(session, entity, "-- healthy", owner_id=owner.id)
    session.flush()

    with pytest.raises(ScriptRejected) as exc:
        set_entity_behaviour(session, entity,
                             "local fills = settle_last_orders()",
                             owner_id=owner.id)
    assert any("settle_last_orders" in p for p in exc.value.problems)

    # Nothing was mutated by the refusal: same lineage length, the healthy
    # script still the one active behaviour.
    scripts = session.query(Script).filter_by(entity_id=entity.id).all()
    assert len(scripts) == 1 and scripts[0].id == good.id and scripts[0].is_active


def test_lint_refuses_syntax_error(session):
    owner = _user(session)
    entity = _owned_entity(session, owner)
    with pytest.raises(ScriptRejected, match="syntax:"):
        set_entity_behaviour(session, entity, "local t = {", owner_id=owner.id)


def test_lint_accepts_state_dependent_script_with_warning(session):
    """A script that errors only on the synthetic ctx (empty state) is
    accepted; the warning rides back with it."""
    owner = _user(session)
    entity = _owned_entity(session, owner)
    script, warnings = set_entity_behaviour(
        session, entity, "ctx.state.hunger = ctx.state.hunger + 1",
        owner_id=owner.id)
    assert script.is_active
    assert len(warnings) == 1 and warnings[0].startswith("smoke-run:")


def test_lint_checks_against_installed_tiers(session):
    """The lint's vocabulary is the tick's vocabulary: with a world lib
    installed, its helpers are legal; without it, the same source is
    refused."""
    from econengine import scripting

    owner = _user(session)
    entity = _owned_entity(session, owner)
    src = "local fills = world.settle_last_orders()"

    with pytest.raises(ScriptRejected):
        set_entity_behaviour(session, entity, src, owner_id=owner.id)

    scripting.set_world_lib(
        session, "local w = {} function w.settle_last_orders() return {} end return w")
    script, warnings = set_entity_behaviour(session, entity, src, owner_id=owner.id)
    assert script.is_active and warnings == []
