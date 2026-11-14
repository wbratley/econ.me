"""The fitness assay (G2b): a genome, measured, headless, from scratch."""

import pytest

from experiments.world import stone_age
from experiments.world.assay import (
    ASSAY_DAYS, SEATS, assay_genome, fitness, run_assay)

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
