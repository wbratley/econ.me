"""Repo-wide pytest fixtures (applies to tests/, experiments/, contracts/)."""
import itertools
import uuid

import pytest


@pytest.fixture(autouse=True)
def deterministic_uuids():
    """Pin the uuid4 stream so every test world is bit-identical across runs.

    Entity ids and process ids are ``uuid.uuid4()``, and they feed the
    commit-reveal RNG (``econengine.rng``: ``outcome_roll = sha256(previous
    tick's events : process_id)``, ``GENESIS_HASH`` at tick 1). With a fresh
    random id per run, every stochastic test (wolf attacks, hen rolls, fish,
    loot) draws a fresh roll stream -- the world is deterministic within a run
    and a coin flip across runs, which is how the flaky-test class
    (``test_starter_floor_survives_the_wolves`` and kin) gets born. The
    per-test counter keeps every id unique while making the whole world
    reproducible from run to run.

    Restored by hand rather than via the ``monkeypatch`` fixture: this is
    autouse and root-scoped, and requesting ``monkeypatch`` here would force
    it to instantiate (and tear down) before file-level autouse fixtures that
    also use it -- e.g. ``test_auth_token_ttl`` reloads its module in
    teardown and needs the env back before that.
    """
    counter = itertools.count()
    original = uuid.uuid4
    uuid.uuid4 = lambda: uuid.uuid5(
        uuid.NAMESPACE_URL, f"econ.me/test/{next(counter)}")
    try:
        yield
    finally:
        uuid.uuid4 = original
