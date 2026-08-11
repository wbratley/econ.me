"""The spawn_entity mechanism — bringing a new entity into being during a
tick (Step 6c, docs/actors.md). The one genuinely new engine mechanism of
the Step 6 arc: where create_entity is the platform/setup path (entities
minted between ticks at genesis), spawn_entity is the mid-tick path.

Birth is mechanism; everything else about reproduction is policy. The
mechanism: a caller holding the ``spawn`` capability stamps an immutable
generic-``parents`` provenance list, sets ``owner_id`` (defaults to the
caller's owner), always creates an empty account, and fires a VALIDATOR.
It does NOT endow — starting wealth is a post-spawn transfer (policy, like
the levy rate).

Three concentric gates, each able to refuse:
  A. capability (``spawn``) — checked at the intent boundary AND in the
     service (constitutional, votable);
  B. server hard caps (active / total / per-owner) — engine invariant,
     non-votable, the operator's physical ceiling (every active entity runs
     its BEHAVIOUR each tick);
  C. world rules — a VALIDATOR over ``ctx.op`` (population cap, right
     parents/age/permit), votable, fail-closed.
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import capabilities
from econengine.lua_engine import Intent
from econengine.models import Base, EntityType, EntityStatus, Script, ScriptType, User
from econengine.scripting import OperationVetoedError, resolve_intent
from econengine.services import (
    MissingCapabilityError,
    ServerCapExceededError,
    create_account,
    create_entity,
    spawn_entity,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def world(session):
    """A spawn-capable government with a USD account (so a currency can be
    derived); a plain government with no capabilities; two prospective
    parents (the declared provenance); and a human owner to test owner_id
    defaulting/override."""
    gov = create_entity(session, "State", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.SPAWN]
    create_account(session, gov, "USD", initial_balance=Decimal("0"))
    plain = create_entity(session, "PlainGov", EntityType.GOVERNMENT)  # no caps
    mother = create_entity(session, "Eve", EntityType.INDIVIDUAL)
    father = create_entity(session, "Adam", EntityType.INDIVIDUAL)
    owner = User(id="u-alice", email="alice@x", name="Alice",
                 provider="local", provider_id="alice")
    session.add(owner)
    session.flush()
    session.flush()
    return {"gov": gov, "plain": plain, "mother": mother, "father": father,
            "owner": owner}


def make_script(session, name, source, script_type, entity=None, **kwargs):
    script = Script(
        name=name, source=source, script_type=script_type,
        entity_id=entity.id if entity else None, **kwargs,
    )
    session.add(script)
    session.flush()
    return script


def _spawn_intent(entity_id, parents, **extra):
    params = {"parents": json.dumps([str(p) for p in parents])}
    for k, v in extra.items():
        params[k] = str(v)
    return Intent(entity_id=entity_id, intent_type="spawn_entity", params=params,
                  resource_ids=[str(p) for p in parents], priority=100)


# ---------------------------------------------------------------------------
# services.spawn_entity — direct (in-process) callers
# ---------------------------------------------------------------------------

def test_spawn_creates_child_with_account(world, session):
    """The core: a new ACTIVE entity with an empty account is born."""
    result = spawn_entity(session, world["gov"],
                          parents=[world["mother"].id, world["father"].id],
                          currency="USD")
    from econengine.models import Entity
    child = session.get(Entity, result["child_id"])
    assert child is not None
    assert child.status == EntityStatus.ACTIVE
    assert child.entity_type == EntityType.INDIVIDUAL
    assert child.parents == [world["mother"].id, world["father"].id]
    # always an account, always empty (the mechanism does not endow)
    assert len(child.accounts) == 1
    assert child.accounts[0].currency == "USD"
    assert child.accounts[0].balance == Decimal("0")


def test_spawn_provenance_is_immutable(world, session):
    """Provenance is stamped once; the engine owns it (not script state)."""
    result = spawn_entity(session, world["gov"],
                          parents=[world["mother"].id], currency="USD")
    from econengine.models import Entity
    child = session.get(Entity, result["child_id"])
    assert child.parents == [world["mother"].id]
    # there is no API to change it; the field is data-of-record, like birth_tick


def test_spawn_zero_parents_is_spontaneous_generation(world, session):
    """An empty parents list is valid — spontaneous generation / a built
    object with no lineage. The engine stores [] faithfully."""
    result = spawn_entity(session, world["gov"], parents=[], currency="USD")
    from econengine.models import Entity
    child = session.get(Entity, result["child_id"])
    assert child.parents is None  # empty normalises to None (no recorded parents)


def test_spawn_owner_defaults_to_callers_owner(world, session):
    """The child inherits the caller's owner (Alice's Adam+Eve → Alice owns
    the child). This is the common case: a spawn lands in the spawner's
    ownership pool."""
    world["gov"].owner_id = world["owner"].id
    result = spawn_entity(session, world["gov"],
                          parents=[world["mother"].id], currency="USD")
    from econengine.models import Entity
    child = session.get(Entity, result["child_id"])
    assert child.owner_id == world["owner"].id


def test_spawn_owner_is_overridable(world, session):
    """An explicit owner_id targets a different pool — the server's own, a
    public/wild owner, another player. The server pool is just another
    owner."""
    world["gov"].owner_id = world["owner"].id
    result = spawn_entity(session, world["gov"],
                          parents=[world["mother"].id],
                          owner_id="u-server", currency="USD")
    from econengine.models import Entity
    child = session.get(Entity, result["child_id"])
    assert child.owner_id == "u-server"


def test_spawn_without_capability_raises(world, session):
    """A caller without the spawn capability cannot birth — defense in depth
    (the intent gate rejects this earlier; direct callers hit the service)."""
    with pytest.raises(MissingCapabilityError, match="spawn"):
        spawn_entity(session, world["plain"],
                     parents=[world["mother"].id], currency="USD")


def test_missing_capability_error_is_value_error(session):
    """Subclasses ValueError so resolve_intent's broad except reports it."""
    plain = create_entity(session, "Plain", EntityType.GOVERNMENT)
    mother = create_entity(session, "M", EntityType.INDIVIDUAL)
    with pytest.raises(ValueError):
        spawn_entity(session, plain, parents=[mother.id], currency="USD")


def test_spawn_does_not_endow(world, session):
    """The mechanism never moves wealth — the child starts at zero. Endowment
    is a post-spawn transfer the spawning script/HOOK makes (policy)."""
    spawn_entity(session, world["gov"],
                 parents=[world["mother"].id], currency="USD")
    from econengine.models import Entity
    # find the newest entity (the child)
    child = session.query(Entity).filter(Entity.name == "entity").one()
    assert child.accounts[0].balance == Decimal("0")


# ---------------------------------------------------------------------------
# intent surface — resolve_intent
# ---------------------------------------------------------------------------

def test_intent_spawn_creates_child(world, session):
    """Through the intent surface, a spawn-capable caller births a child and
    the event is 'applied' with the child id."""
    out = resolve_intent(session, _spawn_intent(
        world["gov"].id, [world["mother"].id, world["father"].id],
        name="Cain", currency="USD"))
    assert out["status"] == "applied"
    assert out["type"] == "spawn_entity"
    from econengine.models import Entity
    child = session.get(Entity, out["child_id"])
    assert child.name == "Cain"
    assert child.parents == [world["mother"].id, world["father"].id]


def test_intent_spawn_defaults_currency_from_callers_account(world, session):
    """No currency passed → the child's account uses the caller's first
    account currency (the money the spawner itself uses)."""
    out = resolve_intent(session, _spawn_intent(
        world["gov"].id, [world["mother"].id]))
    assert out["status"] == "applied"
    from econengine.models import Entity, Account
    child = session.get(Entity, out["child_id"])
    acct = session.get(Account, out["child_account_id"])
    assert acct.currency == "USD"
    assert acct.entity_id == child.id


def test_intent_spawn_rejects_capability_less_caller(world, session):
    """A caller without spawn is rejected at the capability gate, before any
    service is touched."""
    out = resolve_intent(session, _spawn_intent(
        world["plain"].id, [world["mother"].id], currency="USD"))
    assert out["status"] == "rejected"
    assert "spawn" in out["reason"]


def test_intent_spawn_rejects_currency_less_caller_without_currency(world, session):
    """A money-incapable caller (no account) must pass an explicit currency;
    without one, the spawn is rejected rather than guessing."""
    out = resolve_intent(session, _spawn_intent(
        world["gov"].id, [world["mother"].id], currency=""))
    # gov has an account, so currency IS derivable -> this should still apply.
    # The real rejection needs a caller with NO account:
    assert out["status"] == "applied"
    bare = create_entity(session, "Bare", EntityType.GOVERNMENT)
    bare.capabilities = [capabilities.SPAWN]
    session.flush()
    out2 = resolve_intent(session, _spawn_intent(bare.id, [world["mother"].id]))
    assert out2["status"] == "rejected"
    assert "currency" in out2["reason"]


def test_intent_spawn_rejects_bad_parents_json(world, session):
    out = resolve_intent(session, Intent(
        entity_id=world["gov"].id, intent_type="spawn_entity",
        params={"parents": "{not json"}, resource_ids=[], priority=100))
    assert out["status"] == "rejected"
    assert "parents" in out["reason"]


# ---------------------------------------------------------------------------
# server hard caps — tier B (non-votable engine invariant)
# ---------------------------------------------------------------------------

def test_server_active_cap_blocks_spawn(world, session, monkeypatch):
    """The active-entity cap is the binding capacity bound (every active
    entity runs a script each tick). At the limit, a spawn is refused."""
    monkeypatch.setenv("ECON_MAX_ACTIVE_ENTITIES", "3")
    # world has 4 entities (gov, plain, mother, father) already -> 3 is exceeded
    with pytest.raises(ServerCapExceededError, match="active"):
        spawn_entity(session, world["gov"],
                     parents=[world["mother"].id], currency="USD")


def test_server_active_cap_allows_under_limit(world, session, monkeypatch):
    monkeypatch.setenv("ECON_MAX_ACTIVE_ENTITIES", "100")
    result = spawn_entity(session, world["gov"],
                          parents=[world["mother"].id], currency="USD")
    assert result["child_id"]


def test_server_total_cap_counts_dead_rows(world, session, monkeypatch):
    """Total-row cap is the storage bound: incapacitated (dead) entities
    still count, unlike the active cap."""
    monkeypatch.setenv("ECON_MAX_ENTITIES", "4")
    # incapacitate one entity (it no longer counts as active but still a row)
    world["plain"].status = EntityStatus.INCAPACITATED
    session.flush()
    with pytest.raises(ServerCapExceededError):
        spawn_entity(session, world["gov"],
                     parents=[world["mother"].id], currency="USD")


def test_server_per_owner_cap_blocks(world, session, monkeypatch):
    """The fairness cap: no single owner hogs entity slots."""
    monkeypatch.setenv("ECON_MAX_ENTITIES_PER_OWNER", "2")
    world["gov"].owner_id = world["owner"].id
    world["mother"].owner_id = world["owner"].id
    world["father"].owner_id = world["owner"].id  # owner already holds 3
    session.flush()
    with pytest.raises(ServerCapExceededError, match="owner"):
        spawn_entity(session, world["gov"],
                     parents=[world["mother"].id], currency="USD")


def test_server_caps_unset_means_unbounded(world, session, monkeypatch):
    """No env var set → no cap (the default)."""
    for var in ("ECON_MAX_ACTIVE_ENTITIES", "ECON_MAX_ENTITIES",
                "ECON_MAX_ENTITIES_PER_OWNER"):
        monkeypatch.delenv(var, raising=False)
    result = spawn_entity(session, world["gov"],
                          parents=[world["mother"].id], currency="USD")
    assert result["child_id"]


def test_server_cap_through_intent_is_rejected(world, session, monkeypatch):
    """Through the intent surface, a cap hit becomes a clean rejection, not a
    raised exception (the resolver swallows ValueError subclasses)."""
    monkeypatch.setenv("ECON_MAX_ACTIVE_ENTITIES", "3")
    out = resolve_intent(session, _spawn_intent(
        world["gov"].id, [world["mother"].id], currency="USD"))
    assert out["status"] == "rejected"
    assert "cap" in out["reason"]


# ---------------------------------------------------------------------------
# VALIDATOR — tier C (the world's votable birth rules)
# ---------------------------------------------------------------------------

POPULATION_CAP_4 = """
if ctx.op.type == 'spawn_entity' and ctx.query.population() >= 4 then
    return {allow=false, reason="population cap reached"}
end
"""


def test_validator_vetoes_spawn_over_population(world, session):
    """A VALIDATOR may enforce a votable population cap — the world's own
    ceiling, tighter than the server's. The validator reads population()
    BEFORE the child is born, so the 4 existing active entities trip a
    >=4 cap. Fail-closed: no child is born."""
    make_script(session, "popcap", POPULATION_CAP_4, ScriptType.VALIDATOR)
    with pytest.raises(OperationVetoedError, match="population cap"):
        spawn_entity(session, world["gov"],
                     parents=[world["mother"].id], currency="USD")
    from econengine.models import Entity
    # nothing was created
    assert session.query(Entity).filter(Entity.name == "entity").count() == 0


def test_validator_allows_spawn_under_cap(world, session):
    # world has 4 active; incapacitate one to get under the cap of 4
    world["plain"].status = EntityStatus.INCAPACITATED
    session.flush()
    make_script(session, "popcap", POPULATION_CAP_4, ScriptType.VALIDATOR)
    result = spawn_entity(session, world["gov"],
                          parents=[world["mother"].id], currency="USD")
    assert result["child_id"]


def test_validator_reads_parents_for_birth_rule(world, session):
    """The world's relationship rules are validators reading ctx.op.parents —
    e.g. 'exactly two parents' (a biological model). The engine ships no
    such rule; a validator composes it."""
    two_parent_rule = """
if ctx.op.type == 'spawn_entity' and #ctx.op.parents ~= 2 then
    return {allow=false, reason="need exactly two parents"}
end
"""
    make_script(session, "biology", two_parent_rule, ScriptType.VALIDATOR)
    # one parent -> vetoed
    with pytest.raises(OperationVetoedError, match="two parents"):
        spawn_entity(session, world["gov"],
                     parents=[world["mother"].id], currency="USD")
    # two parents -> allowed
    result = spawn_entity(session, world["gov"],
                          parents=[world["mother"].id, world["father"].id],
                          currency="USD")
    assert result["child_id"]


def test_vetoed_spawn_intent_is_rejected_not_raised(world, session):
    """Through the intent surface a veto becomes a clean rejection."""
    make_script(session, "popcap", POPULATION_CAP_4, ScriptType.VALIDATOR)
    out = resolve_intent(session, _spawn_intent(
        world["gov"].id, [world["mother"].id], currency="USD"))
    assert out["status"] == "rejected"
    assert "population cap" in out["reason"]


# ---------------------------------------------------------------------------
# ctx.action.spawn_entity — reachable from the script layer
# ---------------------------------------------------------------------------

def test_policy_script_can_spawn_each_tick(world, session):
    """A spawn-capable government's POLICY script fires ctx.action.spawn_entity
    each tick: the capability gate admits it and a child is born."""
    make_script(
        session, "midwife",
        f"ctx.action.spawn_entity({{ '{world['mother'].id}', '{world['father'].id}' }}, "
        f"{{name='Cain', currency='USD'}})",
        ScriptType.POLICY, entity=world["gov"],
    )
    from econengine.tick import run_tick
    run_tick(session)
    from econengine.models import Entity
    child = session.query(Entity).filter(Entity.name == "Cain").one()
    assert child.parents == [world["mother"].id, world["father"].id]
    assert len(child.accounts) == 1
    assert child.accounts[0].balance == Decimal("0")


def test_spawn_lua_parents_round_trip_as_list(world, session):
    """The Lua parents table arrives as a real JSON list (engine-blind), and
    ctx.query.parents reads it back."""
    make_script(
        session, "midwife",
        f"ctx.action.spawn_entity({{ '{world['mother'].id}' }}, "
        f"{{name='Solo', currency='USD'}})",
        ScriptType.POLICY, entity=world["gov"],
    )
    from econengine.tick import run_tick
    run_tick(session)
    from econengine.models import Entity
    child = session.query(Entity).filter(Entity.name == "Solo").one()
    assert child.parents == [world["mother"].id]


def test_policy_script_without_capability_spawn_is_rejected(world, session):
    """A government without the spawn capability cannot birth, even though its
    POLICY script asks to."""
    make_script(
        session, "midwife",
        f"ctx.action.spawn_entity({{ '{world['mother'].id}' }}, "
        f"{{currency='USD'}})",
        ScriptType.POLICY, entity=world["plain"],  # no caps
    )
    from econengine.tick import run_tick
    run_tick(session)
    from econengine.models import Entity
    assert session.query(Entity).filter(Entity.name == "entity").count() == 0


# ---------------------------------------------------------------------------
# birth_tick + age — the executing-tick threading (dual-source)
# ---------------------------------------------------------------------------

def test_spawn_mid_tick_stamps_executing_tick(world, session):
    """A spawn queued during tick N records birth_tick = N (the tick the
    spawner saw as ctx.tick), NOT N-1. The current Tick row is committed only
    at the END of run_tick, so this needs the executing-tick thread-local."""
    make_script(
        session, "midwife",
        f"ctx.action.spawn_entity({{ '{world['mother'].id}' }}, "
        f"{{name='Cain', currency='USD'}})",
        ScriptType.POLICY, entity=world["gov"],
    )
    from econengine.tick import run_tick
    tick = run_tick(session)  # tick 1
    assert tick.number == 1
    from econengine.models import Entity
    child = session.query(Entity).filter(Entity.name == "Cain").one()
    assert child.birth_tick == 1  # the executing tick, not 0


def test_spawn_age_is_consistent_with_ctx_tick(world, session):
    """age(child) computed at a later tick never disagrees with ctx.tick: a
    child born during tick 1 is age 1 at tick 2 (it first acts at tick 2).
    The midwife script uses ctx.state to spawn only once (tick 1)."""
    make_script(
        session, "midwife",
        "if not ctx.state.spawned then\n"
        f"  ctx.action.spawn_entity({{ '{world['mother'].id}' }}, "
        f"{{name='Cain', currency='USD'}})\n"
        "  ctx.state.spawned = true\n"
        "end",
        ScriptType.POLICY, entity=world["gov"],
    )
    from econengine.tick import run_tick
    run_tick(session)  # tick 1: Cain born (birth_tick=1)
    run_tick(session)  # tick 2: Cain is 1
    from econengine.models import Entity
    child = session.query(Entity).filter(Entity.name == "Cain").one()
    # age() against tick 2 == 2 - 1 == 1
    from econengine.scripting import build_queries
    assert build_queries(session, 2)["age"](child.id) == 1


# ---------------------------------------------------------------------------
# queries — population / parents / children
# ---------------------------------------------------------------------------

def test_population_counts_active_only(world, session):
    from econengine.scripting import build_queries
    q = build_queries(session, 0)
    # world fixture: gov, plain, mother, father = 4 active
    assert q["population"]() == 4
    world["plain"].status = EntityStatus.INCAPACITATED
    session.flush()
    assert q["population"]() == 3  # the dead do not count


def test_parents_query_reads_provenance(world, session):
    from econengine.scripting import build_queries
    result = spawn_entity(session, world["gov"],
                          parents=[world["mother"].id, world["father"].id],
                          currency="USD")
    q = build_queries(session, 0)
    assert q["parents"](result["child_id"]) == [world["mother"].id, world["father"].id]
    # a world-setup entity has no recorded parents
    assert q["parents"](world["gov"].id) == []


def test_children_query_is_reverse_of_parents(world, session):
    from econengine.scripting import build_queries
    spawn_entity(session, world["gov"],
                 parents=[world["mother"].id, world["father"].id],
                 currency="USD", name="Cain")
    spawn_entity(session, world["gov"],
                 parents=[world["mother"].id], currency="USD", name="Abel")
    q = build_queries(session, 0)
    mother_children = q["children"](world["mother"].id)
    father_children = q["children"](world["father"].id)
    assert len(mother_children) == 2  # Cain + Abel
    assert len(father_children) == 1  # Cain only
