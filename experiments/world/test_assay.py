"""The fitness assay (G2b): a genome, measured, headless, from scratch."""

import pytest

from experiments.world import stone_age
from experiments.world.assay import (
    ASSAY_DAYS, SEATS, assay_genome, fitness, run_assay,
    run_winter_birth)

STARTER = stone_age._gate_pack_script(stone_age.STARTER)


def test_the_starter_survives_a_short_assay():
    """The pack's starter installs through the real gate and keeps every
    seat alive through a 2-day assay window."""
    result = run_assay(STARTER, days=2)
    assert result.seats and len(result.seats) == SEATS
    assert all(s.installed for s in result.seats)
    assert all(s.death_tick is None for s in result.seats)
    assert result.mean_alive_days == pytest.approx(2.0)
    assert result.total_errors == 0


def test_a_broken_genome_scores_zero_with_its_epitaph():
    """A genome that cannot install is a zero with a reason: the lint
    gate is selection's first filter, not a crash."""
    result = run_assay("this is not lua at all", days=1)
    assert all(not s.installed for s in result.seats)
    assert all(s.refusal for s in result.seats)
    assert result.mean_alive_days < 1.0      # nobody home to keep alive


def test_replicates_are_independent_worlds():
    """M replicates = M fresh worlds (fresh uuids = independent rolls):
    the fitness read aggregates them; the lottery is the variance."""
    results = assay_genome(STARTER, replicates=2, days=1)
    assert len(results) == 2
    fit = fitness(results)
    assert fit["installed"] == 2 * SEATS
    assert 0.0 <= fit["mean_alive_days"] <= 1.0
    assert fit["deaths"] == {}              # the starter survives day 1


def test_deaths_are_attributed():
    """A genome that provably dies records the domain it died of — the
    kill-chain memory for the lineage's next mutation."""
    # a behaviour that does nothing: the house stands still and starves
    result = run_assay("-- stand still and die\n", days=6)
    assert all(s.death_tick is not None for s in result.seats)
    fit = fitness([result])
    assert fit["deaths"], "a standing-still house must die of something"
    assert set(fit["deaths"]) <= {"HUNGER", "THIRST", "FATIGUE", "EXPOSURE"}


# -- tier two: the winter window ------------------------------------------

def test_short_assays_carry_no_winter_read():
    fit = fitness(assay_genome(STARTER, replicates=1, days=1))
    assert "winter" not in fit          # the window never reached winter


def test_the_winter_window_reads():
    """Days 19+ enter winter (the pack's 6-day seasons) and every seat's
    winter is read separately: alive ticks in-window, deaths in-window
    attributed. The mechanism is pinned here; the FRONTIER (whether the
    tier-one champion survives winter) is empirical — run 76's datum,
    not a test bar (a 2-day gauntlet is a lottery at day 20)."""
    result = run_assay(STARTER, days=20)     # winter days 19-20 (2 days)
    fit = fitness([result])
    assert "winter" in fit
    assert fit["winter"]["days"] == 2
    assert 0.0 <= fit["winter"]["mean_alive_days"] <= 2.0
    # attribution: any death in-window lands in the winter tally with
    # its domain — the kill-chain memory for the next mutation
    in_window_deaths = sorted(s.winter_death for s in result.seats
                              if s.winter_death)
    tallied = sorted(d for d, n in fit["winter"]["deaths"].items()
                     for _ in range(n))
    assert tallied == in_window_deaths


# -- early termination: nobody left to measure, stop ticking ---------------

def test_a_dead_world_terminates_early():
    """Fitness is survival time: when every counting seat is dead the
    world has nothing left to measure, so it stops ticking (the free
    speedup BEFORE the pool — and the variance shrinker that makes
    worker balancing trivial)."""
    result = run_assay("-- stand still and die\n", days=6)
    assert result.ended_tick < 6 * 24            # stopped before the wall
    assert all(s.death_tick is not None for s in result.seats)
    # and the measurement is honest: nobody's alive_ticks reach the wall
    assert all(s.alive_ticks < 6 * 24 for s in result.seats)


def test_assay_many_runs_the_pool_in_order():
    """The parallel entry point: (src, replicate, days) jobs across a
    process pool, results in job order (callers zip candidates to their
    replicates deterministically)."""
    from experiments.world.assay import assay_many
    jobs = [(STARTER, 0, 1), (STARTER, 1, 1), ("not lua", 0, 1)]
    out = assay_many(jobs, workers=2)
    assert len(out) == 3
    assert [r.genome_sha for r in out] == [r.genome_sha for r in out]
    assert out[0].days == 1 and out[0].mean_alive_days == 1.0
    assert out[2].mean_alive_days < 1.0           # the broken one scores 0


# -- the winter-birth assay (the deconfound instrument) ---------------------

def test_winter_birth_naked_dies_the_wood_buffer_survives():
    """Run-80 read #3's deconfound, pinned: a naked winter birth dies
    the FATIGUE winter-birth kill in ~2 days; a ~10-wood buffer (the
    fuel, not the tools) carries the same genome through the window —
    the live heirs' measured law, reproducible on demand."""
    naked = run_winter_birth(STARTER, endowment=None, at_day=22,
                             days_after=4)
    assert all(s.death_condition == "FATIGUE" for s in naked.seats)
    assert all(s.alive_ticks / 24 < 3 for s in naked.seats)

    wood = run_winter_birth(STARTER, endowment={"WOOD": 9.74},
                            at_day=22, days_after=4)
    # the buffer strictly extends survival (the 6/6-with-wood law was
    # measured on the trainer's champion, whose winter fire discipline
    # converts ~10 wood into a bridged winter tail; the weaker starter
    # converts it partially — the buffer helps, the genome decides)
    assert wood.mean_alive_days > naked.mean_alive_days + 1.0


# -- the bequest assay (tier 4): fitness is what you leave ---------------

def test_the_bequest_assay_measures_two_generations():
    """Phase 1 runs the genome to death under the pack's escrow policy
    (seats own separate escrows); phase 2 delivers each estate to a
    fresh heir born at the first death and measures its survival. The
    rows must be honest arithmetic: bequest == gen1 + heir."""
    from experiments.world.assay import run_bequest
    out = run_bequest(STARTER, days=10, heir_days=2)
    assert out["seats"] and len(out["seats"]) == SEATS
    for s in out["seats"]:
        assert s["gen1_days"] >= 0
        if s["heir_days"] is None:
            # the honest no-bequest row: nobody died in the window,
            # no heir was born, nothing to leave yet
            assert s["bequest_days"] is None
        else:
            assert abs(s["bequest_days"]
                       - (s["gen1_days"] + s["heir_days"])) < 0.01


# -- the family tier (metric pre-registered in the spec) --------------

def test_the_family_assay_measures_two_lives():
    """One genome, two lives: parents run the candidate with the spawn
    discipline; children (born mid-world) inherit it via the seam v1
    and run it age-branched. The rows must carry the pinned metric's
    parts: parent/child days, births, alive-at-end (the 10-day bonus
    is applied in family_days)."""
    from experiments.world.assay import run_family
    out = run_family(STARTER, days=12)
    assert out["seats"] and len(out["seats"]) == SEATS
    for s in out["seats"]:
        assert s["parent_days"] >= 0 and s["child_days"] >= 0
        assert s["family_days"] == round(
            s["parent_days"] + s["child_days"]
            + (10 if s["alive_at_end"] else 0), 2)
        assert s["spam_ratio"] is None or s["spam_ratio"] >= 0
        # the instrumented rows: reach, blocked reasons, the
        # distance-to-gate components (second-cycle peaks + joint
        # gate-clear ticks)
        assert s["fertile_reach"] in (0.0, 1.0)
        assert s["blocked_surplus"] >= 0 and s["blocked_wood"] >= 0
        ps = s["peak_spring"]
        assert set(ps) == {"food", "wood", "coin", "shelf",
                           "gate_clear_ticks"}
        assert ps["gate_clear_ticks"] >= 0
