"""Make a run reproducible from its seed.

`ScenarioConfig.seed` only ever controlled *genesis* — who starts rich, who
owns land, which firm bids how hard. Everything stochastic during the run
was not seeded at all, and re-running an identical config gave a different
answer every time (measurably: same seed, two runs, 7 vs 2 people
incapacitated).

The cause is not a defect in the engine's randomness. `rng.outcome_roll()`
is `sha256(previous tick's event hash + ":" + process_id)`, which is exactly
right for the property the engine sets out to guarantee — an auditor holding
the persisted rows can recompute every roll and verify nobody cherry-picked
an outcome. But `Process.id` is a `uuid.uuid4()`, so the roll is drawn from
fresh OS entropy on each run. The history is replayable; the *experiment* is
not, and those are different properties. Auditability needs the ID to be
unpredictable in advance; science needs it to be fixed in advance.

Rather than change how the engine mints IDs — that would trade away the
commit-reveal the roll depends on for every real deployment, to serve a
harness — this seeds the ID source for the duration of a run. Same seed,
same IDs, same rolls, same economy, bit for bit.

Scope carefully: this patches `uuid.uuid4` process-wide, so it is a context
manager, and concurrent runs in one process would interleave draws and
corrupt each other's determinism. Runs here are sequential.
"""

import random
import uuid
from contextlib import contextmanager


@contextmanager
def deterministic_ids(seed: int):
    """Replace uuid4 with a seeded stream for the duration of the block.

    Draws 16 random bytes and stamps them as a version-4 UUID, so the values
    remain well-formed UUIDs (and stay unique within a run) while being a
    pure function of the seed.
    """
    rng = random.Random(f"econ.me:ids:{seed}")
    original = uuid.uuid4

    def seeded_uuid4() -> uuid.UUID:
        return uuid.UUID(bytes=rng.getrandbits(128).to_bytes(16, "big"), version=4)

    uuid.uuid4 = seeded_uuid4
    try:
        yield
    finally:
        uuid.uuid4 = original
