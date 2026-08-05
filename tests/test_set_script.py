"""Governed script lifecycle — enact a new version of a law (step 4a-1).

``set_script`` is the privileged write surface for POLICY / BEHAVIOUR / HOOK
scripts: where the admin API creates scripts by operator fiat, ``set_script``
is the *enactable* path a vote will drive (4a-ii). It is to scripts what
``set_fiscal_policy`` is to parameters.

Semantics are retire-old + activate-new, never in-place edit, so every
enacted law leaves a lineage of retired predecessors — auditable,
revertible, sandbox-triable. ``lineage_id`` is the stable identity; ``name``
is auto-versioned per row (``{lineage_id}#{n}``).

All the safety lives in the gating:
  - capability (``legislate``) checked at the intent boundary AND in the service;
  - VALIDATOR scripts are excluded (they are the constitution, 4b's job);
  - no validator gates ``set_script`` itself (validators are the protected thing);
  - a HOOK fires after enactment for audit.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities
from econengine.lua_engine import Intent
from econengine.models import Base, EntityType, Script, ScriptType
from econengine.scripting import build_queries, resolve_intent
from econengine.services import (
    MissingCapabilityError,
    create_entity,
    set_script,
)
from econengine.tick import run_tick


@pytest.fixture
def session():
    # check_same_thread off: ctx.query.* callbacks run on the script thread
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """A legislating government, a government with NO capabilities, and an
    individual citizen (who may not legislate directly)."""
    gov = create_entity(session, "Gov", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.LEGISLATE]
    plain = create_entity(session, "PlainGov", EntityType.GOVERNMENT)
    individual = create_entity(session, "Citizen", EntityType.INDIVIDUAL)
    session.flush()
    return {"gov": gov, "plain": plain, "individual": individual}


def make_script(session, name, source, script_type, entity=None):
    script = Script(
        name=name, source=source, script_type=script_type,
        entity_id=entity.id if entity else None,
    )
    session.add(script)
    session.flush()
    return script


# ---------------------------------------------------------------------------
# services.set_script — direct (in-process) callers
# ---------------------------------------------------------------------------

def test_set_script_enacts_active_version(world, session):
    s = set_script(session, world["gov"], ScriptType.POLICY, "tax_law", "ctx.state.x = 1")
    assert s.lineage_id == "tax_law"
    assert s.is_active is True
    assert s.name == "tax_law#1"
    assert s.source == "ctx.state.x = 1"


def test_set_script_retires_old_activates_new(world, session):
    v1 = set_script(session, world["gov"], ScriptType.POLICY, "tax_law", "ctx.state.v = 1")
    v2 = set_script(session, world["gov"], ScriptType.POLICY, "tax_law", "ctx.state.v = 2")
    assert v1.is_active is False          # retired
    assert v2.is_active is True           # new active
    assert v1.lineage_id == v2.lineage_id == "tax_law"
    assert v1.name == "tax_law#1"
    assert v2.name == "tax_law#2"
    # exactly one active version in the lineage
    actives = [s for s in session.query(Script).filter_by(lineage_id="tax_law") if s.is_active]
    assert len(actives) == 1
    assert actives[0].id == v2.id


def test_set_script_preserves_full_history(world, session):
    """The whole point of retire+activate: the legislative record survives."""
    for n in (1, 2, 3):
        set_script(session, world["gov"], ScriptType.POLICY, "law", f"ctx.state.v = {n}")
    history = session.query(Script).filter_by(lineage_id="law").order_by(Script.created_at).all()
    assert [s.name for s in history] == ["law#1", "law#2", "law#3"]
    assert [s.is_active for s in history] == [False, False, True]


def test_set_script_rejects_authority_without_capability(world, session):
    with pytest.raises(MissingCapabilityError) as exc_info:
        set_script(session, world["plain"], ScriptType.POLICY, "law", "ctx.state.x = 1")
    assert exc_info.value.entity_id == world["plain"].id
    assert exc_info.value.capability == capabilities.LEGISLATE
    assert session.query(Script).filter_by(lineage_id="law").count() == 0


def test_set_script_protects_validators(world, session):
    """Validators are the constitution; set_script must not touch them — only
    the constitutional process (4b) may. Fail closed: no row created."""
    with pytest.raises(ValueError, match="validator"):
        set_script(session, world["gov"], ScriptType.VALIDATOR, "constitution", "return true")
    assert session.query(Script).filter_by(lineage_id="constitution").count() == 0


def test_set_script_requires_lineage_id(world, session):
    with pytest.raises(ValueError, match="lineage_id"):
        set_script(session, world["gov"], ScriptType.POLICY, "", "ctx.state.x = 1")


def test_set_script_binds_behaviour_to_entity(world, session):
    s = set_script(
        session, world["gov"], ScriptType.BEHAVIOUR, "gov_behave",
        "ctx.state.ran = 'yes'", entity_id=world["gov"].id,
    )
    assert s.entity_id == world["gov"].id


# ---------------------------------------------------------------------------
# resolve_intent — the shared gate (intents API + tick + scripts)
# ---------------------------------------------------------------------------

def _set_script_intent(entity_id, script_type="policy", lineage_id="tax_law",
                       source="ctx.state.x = 1", bound_entity_id=None):
    params = {"script_type": script_type, "lineage_id": lineage_id, "source": source}
    if bound_entity_id is not None:
        params["entity_id"] = bound_entity_id
    return Intent(
        entity_id=entity_id, intent_type="set_script",
        params=params, resource_ids=[lineage_id], priority=10,
    )


def test_intent_set_script_enacts(world, session):
    out = resolve_intent(session, _set_script_intent(world["gov"].id))
    assert out["status"] == "applied", out
    assert out["lineage_id"] == "tax_law"
    s = session.get(Script, out["script_id"])
    assert s.is_active is True
    assert s.lineage_id == "tax_law"


def test_intent_set_script_rejects_missing_capability(world, session):
    out = resolve_intent(session, _set_script_intent(world["plain"].id))
    assert out["status"] == "rejected"
    assert "legislate" in out["reason"]
    assert session.query(Script).filter_by(lineage_id="tax_law").count() == 0


def test_intent_set_script_rejects_non_authority_individual(world, session):
    """A citizen may not legislate directly — only through the proposal→vote→
    enact cycle (4a-ii). Direct legislation is capability-gated away."""
    out = resolve_intent(session, _set_script_intent(world["individual"].id))
    assert out["status"] == "rejected"
    assert "legislate" in out["reason"]


def test_intent_set_script_rejects_unknown_script_type(world, session):
    out = resolve_intent(session, _set_script_intent(world["gov"].id, script_type="nonsense"))
    assert out["status"] == "rejected"
    assert "script_type" in out["reason"]


def test_intent_set_script_rejects_validator(world, session):
    out = resolve_intent(session, _set_script_intent(
        world["gov"].id, script_type="validator", lineage_id="constitution",
    ))
    assert out["status"] == "rejected"
    assert "validator" in out["reason"].lower()
    assert session.query(Script).filter_by(lineage_id="constitution").count() == 0


def test_intent_set_script_rejects_missing_lineage(world, session):
    out = resolve_intent(session, _set_script_intent(world["gov"].id, lineage_id=""))
    assert out["status"] == "rejected"
    assert "lineage_id" in out["reason"]


def test_intent_set_script_two_enactments_retire_and_activate(world, session):
    o1 = resolve_intent(session, _set_script_intent(world["gov"].id, source="ctx.state.a = 1"))
    o2 = resolve_intent(session, _set_script_intent(world["gov"].id, source="ctx.state.a = 2"))
    assert o1["status"] == "applied" and o2["status"] == "applied"
    assert session.get(Script, o1["script_id"]).is_active is False
    assert session.get(Script, o2["script_id"]).is_active is True


# ---------------------------------------------------------------------------
# ctx.query.active_script / script_history — the read side
# ---------------------------------------------------------------------------

def test_query_active_script_returns_live_source(world, session):
    set_script(session, world["gov"], ScriptType.POLICY, "law", "ctx.state.v = 7")
    q = build_queries(session)
    active = q["active_script"]("law")
    assert active["source"] == "ctx.state.v = 7"
    assert active["lineage_id"] == "law"


def test_query_active_script_follows_enactments(world, session):
    set_script(session, world["gov"], ScriptType.POLICY, "law", "ctx.state.v = 1")
    set_script(session, world["gov"], ScriptType.POLICY, "law", "ctx.state.v = 2")
    q = build_queries(session)
    assert q["active_script"]("law")["source"] == "ctx.state.v = 2"


def test_query_active_script_none_for_unknown_lineage(world, session):
    assert build_queries(session)["active_script"]("nope") is None


def test_query_script_history_orders_oldest_first(world, session):
    for n in (1, 2, 3):
        set_script(session, world["gov"], ScriptType.POLICY, "law", f"ctx.state.v = {n}")
    hist = build_queries(session)["script_history"]("law")
    assert [h["name"] for h in hist] == ["law#1", "law#2", "law#3"]
    assert [h["is_active"] for h in hist] == [False, False, True]


# ---------------------------------------------------------------------------
# HOOK fires for audit (validators deliberately do NOT — see below)
# ---------------------------------------------------------------------------

def test_hook_observes_set_script_op(world, session):
    """A HOOK sees ctx.op for set_script — lineage, script_id, retired_id —
    so audit/policy records every enactment."""
    audit = make_script(session, "audit", """
    if ctx.op.type == 'set_script' then
      ctx.state.lineage = ctx.op.lineage_id
      ctx.state.script_id = ctx.op.script_id
      ctx.state.retired = ctx.op.retired_script_id
    end
    """, ScriptType.HOOK)
    out = resolve_intent(session, _set_script_intent(world["gov"].id, source="ctx.state.x = 1"))
    assert audit.state["lineage"] == "tax_law"
    assert audit.state["script_id"] == out["script_id"]
    assert audit.state.get("retired") is None   # first enactment, nothing retired (Lua nil = absent)


def test_hook_sees_retired_id_on_second_enactment(world, session):
    audit = make_script(session, "audit", """
    if ctx.op.type == 'set_script' then ctx.state.retired = ctx.op.retired_script_id end
    """, ScriptType.HOOK)
    o1 = resolve_intent(session, _set_script_intent(world["gov"].id, source="ctx.state.a = 1"))
    resolve_intent(session, _set_script_intent(world["gov"].id, source="ctx.state.a = 2"))
    assert audit.state["retired"] == o1["script_id"]


# ---------------------------------------------------------------------------
# Validators do NOT gate set_script — the legislature cannot be locked out
# ---------------------------------------------------------------------------

def test_validator_cannot_veto_set_script(world, session):
    """Validators are never consulted for set_script: they cannot lock the
    legislature out, and only a constitution (4b) may restrain lawmaking.
    A validator that would deny every set_script is simply ignored."""
    make_script(session, "blocker", """
    if ctx.op.type == 'set_script' then return {allow=false, reason='no'} end
    """, ScriptType.VALIDATOR)
    out = resolve_intent(session, _set_script_intent(world["gov"].id))
    assert out["status"] == "applied"          # validator not consulted


# ---------------------------------------------------------------------------
# Integration: an enacted law goes live on the next tick
# ---------------------------------------------------------------------------

def test_enacted_law_runs_next_tick(world, session):
    """The lifecycle produces a live, active script: enact a BEHAVIOUR bound
    to the government, run a tick, and it executes."""
    out = resolve_intent(session, _set_script_intent(
        world["gov"].id, script_type="behaviour", lineage_id="gov_behave",
        source="ctx.state.ran = 'yes'", bound_entity_id=world["gov"].id,
    ))
    assert out["status"] == "applied"
    run_tick(session)
    active = session.get(Script, out["script_id"])
    assert active.state.get("ran") == "yes"


def test_legislating_script_enacts_new_law(world, session):
    """A legislating authority's BEHAVIOUR script drives lawmaking via
    ctx.action.set_script — the script-level entry to the governed lifecycle."""
    make_script(
        session, "enactor",
        f"ctx.action.set_script('behaviour', 'spawned', "
        f"'ctx.state.born = true', '{world['gov'].id}')",
        ScriptType.BEHAVIOUR, entity=world["gov"],
    )
    run_tick(session)
    spawned = [s for s in session.query(Script).filter_by(lineage_id="spawned") if s.is_active]
    assert len(spawned) == 1
    assert spawned[0].entity_id == world["gov"].id
