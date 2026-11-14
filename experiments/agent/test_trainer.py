"""The G2b trainer: param mutation, selection, archives, radiation."""

import json
import random

from experiments.agent.trainer import (
    Lineage, mutate_params, radiation, seed_founders)
from experiments.world import stone_age

STARTER = stone_age._gate_pack_script(stone_age.STARTER)


# -- the param mutator ----------------------------------------------------

def test_param_mutation_touches_only_code_and_only_numbers():
    rng = random.Random(11)
    src = (
        "-- buy at 4 coins, not more\n"
        "local cap = 4\n"
        "local price = \"2.99\"\n"
        "if wood < 2 then buy(3) end\n"
    )
    seen = set()
    for _ in range(40):
        out = mutate_params(src, rng)
        assert out.splitlines()[0].endswith("not more")   # comments kept
        assert '"2.99"' in out                            # strings kept
        seen.update(out.splitlines())
    # the mutable literals did move across the swarm
    moved = [l for l in seen if l.startswith("local cap = ") and l != "local cap = 4"]
    assert moved, "cap=4 (a mutable literal) never moved in 40 draws"


def test_param_mutation_never_breaks_the_install_gate():
    """The operator's contract: same tokens, different digits — the lint
    gate can refuse on semantics, never syntax."""
    from econengine import services
    from econengine.models import Base, Entity
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    rng = random.Random(3)
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        stone_age.create_content(s)
        house = stone_age.make_house(s, "House Test")
        house.owner_id = "u-assay"
        s.flush()
        for _ in range(5):
            services.set_entity_behaviour(
                s, house, mutate_params(STARTER, rng), owner_id="u-assay")
            # set_entity_behaviour raises on refusal; no raise = installed


# -- the lineage: selection and the archive --------------------------------

def test_lineage_step_selects_and_archives(tmp_path):
    lin = Lineage("line-1", STARTER, tmp_path)
    row = lin.step(mutants=1, replicates=1, days=2,
                   rng=random.Random(5))
    assert row["lineage"] == "line-1"
    assert row["generation"] == 1
    assert row["fitness"]["mean_alive_days"] > 0
    assert {r["tag"] for r in row["pool"]} == {"champion", "param-0"}
    rows = [json.loads(l) for l in
            (tmp_path / "archive-line-1.jsonl").read_text().splitlines()]
    assert len(rows) == 2                       # every candidate archived
    assert rows[0]["parent"] == rows[0]["sha"]  # champion's parent: itself
    assert sum(1 for r in rows if r["champion"]) == 1


def test_ties_keep_the_incumbent(tmp_path):
    """A param child that TIES the champion does not take the belt —
    the tiebreak is stability, not sha-churn."""
    lin = Lineage("line-1", STARTER, tmp_path)
    rng = random.Random(5)
    first = lin.step(mutants=0, replicates=1, days=1, rng=rng)
    champ_before = lin.champion_sha
    second = lin.step(mutants=1, replicates=1, days=1, rng=rng)
    # day-1 assays: everyone alive -> tie -> incumbent stays
    assert second["turnover"] is False
    assert lin.champion_sha == champ_before


# -- founders and radiation -------------------------------------------------

def test_founders_start_apart():
    founders = seed_founders(3, seed=9)
    assert [n for n, _ in founders] == ["line-1", "line-2", "line-3"]
    srcs = [s for _, s in founders]
    assert srcs[0] == STARTER                  # line-1: the unjittered starter
    assert len(set(srcs)) == 3                 # the lines start apart


def test_radiation_reads_the_pool(tmp_path):
    a = Lineage("a", STARTER, tmp_path)
    rng = random.Random(1)
    b = Lineage("b", mutate_params(STARTER, rng), tmp_path)
    rad = radiation([a, b])
    assert "a:b" in rad and rad["a:b"] <= 1.0
    same = radiation([a, Lineage("c", STARTER, tmp_path)])
    assert same["a:c"] == 1.0                  # identical = collapsed
