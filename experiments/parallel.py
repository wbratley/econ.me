"""Run independent scenarios across CPU cores.

Sweeps (the variant matrix, the seed sweep) are embarrassingly parallel: no
run reads another's state, and each `run_scenario()` builds its own engine and
session. They were serial only because `determinism.deterministic_ids` swaps
`uuid.uuid4` for a seeded stream *process-wide* -- two runs sharing an
interpreter would interleave draws off one another's generator and corrupt
both. That rules out threads permanently; it does not rule out processes,
which get their own `uuid4` and their own seeded stream.

So: processes, one scenario each. Results come back in submission order, not
completion order, so a sweep's output does not depend on which worker
happened to finish first.

Per-run progress bars are suppressed under parallelism -- N workers writing
carriage returns to one stderr produces noise, not a progress bar. Callers
get a completion line per finished run instead.
"""

import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from .inequality.scenario import ScenarioConfig


@dataclass
class Job:
    """One scenario run. `label` identifies it in progress output; `kwargs`
    are passed through to run_scenario()."""
    label: str
    config: ScenarioConfig
    ticks: int
    kwargs: dict[str, Any]


def _run_job(job: Job) -> dict:
    # Imported inside the worker: the parent may have been forked mid-run, and
    # this keeps the worker's engine/session construction entirely its own.
    from .inequality.run import run_scenario

    return run_scenario(job.config, job.ticks, progress=False, **job.kwargs)


def default_workers(n_jobs: int) -> int:
    return max(1, min(os.cpu_count() or 1, n_jobs))


def run_jobs(
    jobs: list[Job],
    workers: int | None = None,
    on_result: Callable[[Job, dict], None] | None = None,
) -> list[dict]:
    """Run every job and return results in submission order.

    workers=1 runs in-process, which keeps stack traces intact and the
    debugger usable; anything higher forks a pool.
    """
    workers = workers if workers is not None else default_workers(len(jobs))

    if workers == 1:
        results = []
        for job in jobs:
            result = _run_job(job)
            results.append(result)
            if on_result:
                on_result(job, result)
        return results

    print(f"running {len(jobs)} scenarios across {workers} processes",
          file=sys.stderr, flush=True)

    results: list[dict | None] = [None] * len(jobs)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_job, job): i for i, job in enumerate(jobs)}
        done = 0
        # as_completed reports finishes as they land; each result is still
        # stored at its submission index, so the returned order is stable.
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
            done += 1
            print(f"  [{done}/{len(jobs)}] {jobs[index].label} done",
                  file=sys.stderr, flush=True)

    return [r for r in results if r is not None]
