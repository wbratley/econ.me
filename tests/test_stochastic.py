"""Stochastic recipes at the engine level: branch table creation, the
completion roll, catalyst inputs, and the commit-reveal cancellation
window. The recurring assertion style is the auditor's: recompute the roll
from persisted data and check the engine credited exactly that branch."""

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import rng
from econengine.markets import adjust_holding, get_holding
from econengine.models import Base, EntityType, ProcessStatus
from econengine.production import (
    cancel_process,
    complete_processes,
    create_recipe,
    start_process,
)
from econengine.services import create_entity
from econengine.tick import run_tick


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


FORGE_BRANCHES = [
    {"weight": Decimal("0.70"),
     "outputs": {"SWORD": Decimal("1"), "FORGE": Decimal("1"), "SKILL-SMITH": Decimal("0.02")}},
    {"weight": Decimal("0.25"),
     "outputs": {"SCRAP": Decimal("1"), "FORGE": Decimal("1")}, "label": "ruined the blank"},
    {"weight": Decimal("0.05"),
     "outputs": {"SCRAP": Decimal("1")}, "label": "wrecked the forge"},
]


def make_forge_world(session, duration_ticks=1):
    """A smith with iron and a forge, and the design doc's FORGE_SWORD
    outcome table — the forge is a catalyst: consumed at start, re-emitted
    by the branches that spare it."""
    smith = create_entity(session, "Smith", EntityType.INDIVIDUAL)
    adjust_holding(session, smith, "IRON", Decimal("100"))
    adjust_holding(session, smith, "FORGE", Decimal("1"))
    recipe = create_recipe(
        session, "FORGE_SWORD",
        inputs={"IRON": Decimal("2"), "FORGE": Decimal("1")},
        outputs={},
        duration_ticks=duration_ticks,
        branches=FORGE_BRANCHES,
    )
    return smith, recipe


# --- creation and validation ---

def test_create_branched_recipe_normalizes(session):
    _, recipe = make_forge_world(session)
    assert [b.position for b in recipe.branches] == [0, 1, 2]
    assert [b.weight for b in recipe.branches] == [
        Decimal("0.7000"), Decimal("0.2500"), Decimal("0.0500")]
    assert recipe.branches[1].label == "ruined the blank"
    assert [(o.symbol, o.quantity) for o in recipe.branches[2].outputs] == [
        ("SCRAP", Decimal("1"))]
    assert recipe.outputs == []


def test_branch_validations(session):
    with pytest.raises(ValueError, match="not both"):
        create_recipe(session, "X", inputs={}, outputs={"Y": Decimal("1")}, duration_ticks=1,
                      branches=[{"weight": Decimal("1"), "outputs": {"Z": Decimal("1")}}])
    with pytest.raises(ValueError, match="positive"):
        create_recipe(session, "X", inputs={}, outputs={}, duration_ticks=1,
                      branches=[{"weight": Decimal("0"), "outputs": {"Z": Decimal("1")}}])
    with pytest.raises(ValueError, match="positive"):
        create_recipe(session, "X", inputs={}, outputs={}, duration_ticks=1,
                      branches=[{"weight": Decimal("1"), "outputs": {"Z": Decimal("-1")}}])
    with pytest.raises(ValueError, match="output, branch, unlock, or built facility"):
        create_recipe(session, "X", inputs={"A": Decimal("1")}, outputs={}, duration_ticks=1)
    # a branch with no outputs is a legitimate total-loss row
    recipe = create_recipe(
        session, "GAMBLE", inputs={"A": Decimal("1")}, outputs={}, duration_ticks=1,
        branches=[{"weight": Decimal("1"), "outputs": {"B": Decimal("1")}},
                  {"weight": Decimal("1"), "outputs": {}}])
    assert recipe.branches[1].outputs == []


# --- completion: the roll, the audit trail, the catalyst ---

def test_completion_credits_the_audited_branch(session):
    smith, recipe = make_forge_world(session)
    process = start_process(session, smith, "FORGE_SWORD")
    assert get_holding(session, smith.id, "FORGE").quantity == Decimal("0")  # at risk

    events = complete_processes(session, tick_number=2)

    # the auditor's replay: no ticks have run, so the seed is genesis
    roll = rng.outcome_roll(rng.GENESIS_HASH, process.id)
    branch_index = rng.weighted_index(roll, [b.weight for b in recipe.branches])
    assert process.outcome_roll == roll
    assert process.outcome_branch == branch_index
    assert process.status == ProcessStatus.COMPLETED

    branch = recipe.branches[branch_index]
    for output in branch.outputs:
        held = get_holding(session, smith.id, output.symbol)
        assert held is not None and held.quantity == output.quantity

    event = next(e for e in events if e["type"] == "process_completed")
    assert event["branch"] == branch_index
    assert event["roll"] == roll
    assert event["outputs"] == {o.symbol: str(o.quantity) for o in branch.outputs}
    if branch.label:
        assert event["branch_label"] == branch.label


def test_every_branch_is_reachable_and_conserves_the_catalyst(session):
    """Across many processes the table's every row comes up, and the forge
    survives exactly the branches that re-emit it."""
    smith, recipe = make_forge_world(session)
    seen = set()
    for _ in range(300):
        adjust_holding(session, smith, "IRON", Decimal("2"))
        adjust_holding(session, smith, "FORGE", Decimal("1"))
        before = {s: get_holding(session, smith.id, s).quantity for s in ("FORGE",)}
        process = start_process(session, smith, "FORGE_SWORD")
        complete_processes(session, tick_number=2)
        seen.add(process.outcome_branch)
        branch = recipe.branches[process.outcome_branch]
        forge_out = next((o.quantity for o in branch.outputs if o.symbol == "FORGE"), Decimal("0"))
        assert get_holding(session, smith.id, "FORGE").quantity == \
            before["FORGE"] - Decimal("1") + forge_out
        if seen == {0, 1, 2}:
            return
    raise AssertionError(f"only branches {sorted(seen)} came up in 300 completions")


def test_single_branch_is_certain(session):
    entity = create_entity(session, "E", EntityType.INDIVIDUAL)
    create_recipe(session, "SURE", inputs={}, outputs={}, duration_ticks=0,
                  branches=[{"weight": Decimal("3"), "outputs": {"X": Decimal("2")}}])
    process = start_process(session, entity, "SURE")
    assert process.outcome_branch == 0
    assert get_holding(session, entity.id, "X").quantity == Decimal("2")


def test_duration_zero_rolls_at_start(session):
    """A duration-0 stochastic recipe completes at start, seeded by the
    latest persisted tick (genesis on a fresh economy) — there is no
    cancellation window to protect."""
    entity = create_entity(session, "E", EntityType.INDIVIDUAL)
    create_recipe(session, "PAN", inputs={}, outputs={}, duration_ticks=0,
                  branches=[{"weight": Decimal("1"), "outputs": {"GOLD": Decimal("1")}},
                            {"weight": Decimal("1"), "outputs": {}}])
    process = start_process(session, entity, "PAN")
    assert process.status == ProcessStatus.COMPLETED
    assert process.outcome_roll == rng.outcome_roll(rng.GENESIS_HASH, process.id)


def test_plain_recipes_keep_no_audit_trail(session):
    entity = create_entity(session, "E", EntityType.INDIVIDUAL)
    create_recipe(session, "PLAIN", inputs={}, outputs={"X": Decimal("1")}, duration_ticks=0)
    process = start_process(session, entity, "PLAIN")
    assert process.outcome_branch is None and process.outcome_roll is None


# --- the commit-reveal cancellation window ---

def test_cancel_window_closes_once_seed_is_committed(session):
    """A process due at tick N is cancellable until tick N-1 has run; after
    that the seed of its outcome roll is persisted and cancellation must
    refuse — otherwise a roller could compute the roll and cherry-pick."""
    smith, _ = make_forge_world(session, duration_ticks=2)
    process = start_process(session, smith, "FORGE_SWORD")  # completes tick 3

    run_tick(session)  # tick 1: window still open (completes 3 > next tick 2)
    adjust_holding(session, smith, "FORGE", Decimal("1"))  # the first one is in use
    still_open = start_process(session, smith, "FORGE_SWORD")  # completes tick 4
    tick2 = run_tick(session)  # seed of tick 3's rolls is now committed
    with pytest.raises(ValueError, match="window has closed"):
        cancel_process(session, process.id, smith.id)
    # the later process (due tick 4) is still inside its window
    cancel_process(session, still_open.id, smith.id)
    assert still_open.status == ProcessStatus.CANCELLED

    run_tick(session)  # tick 3 completes it, seeded by tick 2's hash
    assert process.status == ProcessStatus.COMPLETED
    assert process.outcome_roll == rng.outcome_roll(tick2.events_hash, process.id)


# --- through the tick engine: persistence and full replay ---

def test_tick_persists_events_hash(session):
    tick = run_tick(session)
    session.expire(tick)  # force a reload of the JSON column
    assert tick.events_hash == rng.hash_events(list(tick.events))
    assert len(tick.events_hash) == 64


def test_full_replay_from_persisted_rows(session):
    """The audit story end to end: an outsider holding only the DB rows
    (tick N-1's events, the process id, the recipe's branch table) must be
    able to reproduce the outcome bit for bit."""
    smith, recipe = make_forge_world(session, duration_ticks=1)
    process = start_process(session, smith, "FORGE_SWORD")  # completes tick 2

    tick1 = run_tick(session)
    tick2 = run_tick(session)

    session.expire(tick1)
    seed = rng.hash_events(list(tick1.events))  # recomputed from stored events
    assert seed == tick1.events_hash
    roll = rng.outcome_roll(seed, process.id)
    index = rng.weighted_index(roll, [b.weight for b in recipe.branches])
    assert process.outcome_roll == roll
    assert process.outcome_branch == index

    event = next(e for e in tick2.events if e["type"] == "process_completed")
    assert event["branch"] == index and event["roll"] == roll
    branch = recipe.branches[index]
    for output in branch.outputs:
        assert get_holding(session, smith.id, output.symbol).quantity == output.quantity


def test_intent_pass_cancel_is_the_last_opportunity(session):
    """During tick N-1 itself a script may still cancel a process due at
    tick N — the seed (tick N-1's hash) is not determined until after the
    intent pass, which is exactly the design's commit-reveal boundary."""
    from econengine.models import Script, ScriptType
    smith, _ = make_forge_world(session, duration_ticks=1)
    process = start_process(session, smith, "FORGE_SWORD")  # completes tick 2
    run_tick(session)  # tick 1 — window open during the run

    adjust_holding(session, smith, "FORGE", Decimal("1"))  # the first may be scrap
    process2 = start_process(session, smith, "FORGE_SWORD")  # completes tick 3
    session.add(Script(name="abort", source=f"ctx.action.cancel_process('{process2.id}')",
                       script_type=ScriptType.BEHAVIOUR, entity_id=smith.id))
    session.flush()
    tick2 = run_tick(session)  # intent pass of the tick before process2 completes
    event = next(e for e in tick2.events if e["type"] == "cancel_process")
    assert event["status"] == "applied"
    assert process2.status == ProcessStatus.CANCELLED
    assert process.status == ProcessStatus.COMPLETED  # due processes still complete
