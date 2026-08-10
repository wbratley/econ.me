"""ctx.query.age -- the time-since-birth primitive (Step 6a).

Age is the one entity attribute that is NOT a holding: it is monotonic and
tick-derived (``age = ctx.tick - birth_tick``), so it is computed, never
stored-and-mutated. The engine stamps ``birth_tick`` once at creation; this
query computes the derived value against the same tick the calling script
already sees as ``ctx.tick`` (executing tick for POLICY/BEHAVIOUR, latest
committed for VALIDATOR/HOOK), so age and ctx.tick never disagree. nil
means untracked -- the entity predates age-tracking, or does not exist.

Unforgeable the way holdings are: a script cannot change its birth tick any
more than it can write ``hunger = 0``. This is the keystone that unblocks
age-driven policy (6b: pensions, coming-of-age, age-gating) and, later, the
demographic lifecycle (6c: spawn).
"""
import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.models import Base, EntityType, Script, ScriptType
from econengine.services import create_account, create_entity
from econengine.tick import run_tick
from econengine.scripting import build_queries


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


# --- birth_tick is stamped at creation --------------------------------------

def test_birth_tick_is_zero_before_tick_one(world):
    """An entity spawned at genesis (before any tick) is born at tick 0."""
    session, alice, *_ = world
    assert alice.birth_tick == 0


def test_entity_created_between_ticks_stamps_latest(world):
    """An entity created after a tick has run is born at that tick, so its
    age is measured from when it actually appeared, not from genesis."""
    session, alice, *_ = world
    run_tick(session)                       # commits tick 1
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    assert bob.birth_tick == 1              # born at the last-completed tick


# --- age grows across ticks -------------------------------------------------

def test_age_grows_across_ticks(world):
    """A POLICY reads its own age each tick; it advances one per tick."""
    session, alice, gov, a, g = world
    script = make_script(
        session, "age-self",
        "ctx.state.age = ctx.query.age(ctx.entity.id)",
        ScriptType.POLICY, entity=alice,
    )
    run_tick(session)                       # tick 1: age 1 - 0
    assert script.state["age"] == 1
    run_tick(session)                       # tick 2: age 2
    run_tick(session)                       # tick 3: age 3
    assert script.state["age"] == 3


def test_age_of_another_entity(world):
    """The pension/citizenship use case: a POLICY queries a DIFFERENT
    entity's age. Alice is born at 0, Bob at tick 1; at tick 2 they read
    2 and 1 respectively."""
    session, alice, gov, a, g = world
    run_tick(session)                       # tick 1
    bob = create_entity(session, "Bob", EntityType.INDIVIDUAL)
    script = make_script(
        session, "registrar",
        f"ctx.state.alice = ctx.query.age('{alice.id}')\n"
        f"ctx.state.bob = ctx.query.age('{bob.id}')",
        ScriptType.POLICY, entity=gov,      # the government reads its citizens
    )
    run_tick(session)                       # tick 2
    assert script.state["alice"] == 2       # born tick 0
    assert script.state["bob"] == 1         # born tick 1


# --- unforgeable and fail-safe ---------------------------------------------

def test_age_nil_for_untracked_entity(world):
    """An entity whose birth_tick is NULL (predates tracking) reads nil --
    a dark read, not an error. A fail-closed age gate treats nil as
    'eligibility cannot be certified'."""
    session, alice, *_ = world
    alice.birth_tick = None                 # simulate a pre-tracking entity
    session.flush()
    script = make_script(
        session, "reader",
        "ctx.state.seen = (ctx.query.age(ctx.entity.id) == nil)",
        ScriptType.POLICY, entity=alice,
    )
    run_tick(session)
    assert script.state["seen"] is True


def test_age_nil_for_nonexistent_entity(world):
    """A bad id reads nil rather than raising."""
    session, *_ = world
    q = build_queries(session, 5)
    assert q["age"]("does-not-exist") is None


# --- ctx.entity.age convenience --------------------------------------------

def test_ctx_entity_age_convenience(world):
    """A BEHAVIOUR script reads its own age inline as ctx.entity.age."""
    session, alice, *_ = world
    run_tick(session)
    run_tick(session)                       # tick 2: age 2
    script = make_script(
        session, "self-aware",
        "ctx.state.age = ctx.entity.age",
        ScriptType.BEHAVIOUR, entity=alice,
    )
    run_tick(session)                       # tick 3: age 3
    assert script.state["age"] == 3


# --- dual-source: age matches the tick the script already sees --------------

def test_age_uses_caller_tick_not_global(world):
    """build_queries(session, N) computes against the tick the caller
    threads in (the executing tick for POLICY/BEHAVIOUR); the bare
    build_queries(session) form falls back to the latest committed tick
    (what VALIDATOR/HOOK see). The two agree only when N == latest."""
    session, alice, *_ = world
    run_tick(session)                       # latest committed tick is 1
    alice.birth_tick = 0
    session.flush()
    q_executing = build_queries(session, 5)      # a POLICY at tick 5
    q_fallback = build_queries(session)          # a VALIDATOR (latest = 1)
    assert q_executing["age"](alice.id) == 5
    assert q_fallback["age"](alice.id) == 1


def test_age_in_validator_uses_latest_committed(world):
    """A VALIDATOR fires mid-op, before the current tick commits, so it
    reads the LAST-COMPLETED tick -- the dual-source boundary. A POLICY
    that acts during tick 4 sees ctx.tick=4 (executing); the validator its
    op triggers sees latest=3. An age gate at '< 3' therefore vetoes
    through tick 3 (age 2) and first allows at tick 4 (age 3)."""
    session, alice, gov, a, g = world
    # Gate money issuance on ALICE's age (a global read -- the gate need not
    # be about the acting entity). Validators do not persist state, so we
    # observe the age they saw via the veto decision (balance change).
    validator = make_script(
        session, "age-of-majority",
        "if ctx.op.type ~= 'issue_money' then return true end\n"
        f"local age = ctx.query.age('{alice.id}')\n"
        "-- nil (untracked) fails closed: cannot certify eligibility\n"
        "if age == nil then\n"
        "  return {allow=false, reason='age unknown'}\n"
        "end\n"
        "return age >= 3\n",
        ScriptType.VALIDATOR,
    )
    issuer = make_script(
        session, "issuer",
        f"ctx.action.issue_money('{g.id}', '100', 'age-probe')",
        ScriptType.POLICY, entity=gov,
    )

    run_tick(session)                       # tick 1: validator sees age 0 -> veto
    assert g.balance == Decimal("1000")
    run_tick(session)                       # tick 2: age 1 -> veto
    assert g.balance == Decimal("1000")
    run_tick(session)                       # tick 3: age 2 -> veto
    assert g.balance == Decimal("1000")
    run_tick(session)                       # tick 4: age 3 -> allow
    assert g.balance == Decimal("1100")    # 100 issued
