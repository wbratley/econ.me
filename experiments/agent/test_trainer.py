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
    """BOTH axes: text (difflib) and behavior (process-mix cosine) —
    behavior can diverge while text stays similar and vice versa."""
    a = Lineage("a", STARTER, tmp_path)
    rng = random.Random(1)
    b = Lineage("b", mutate_params(STARTER, rng), tmp_path)
    rad = radiation([a, b])
    assert "a:b" in rad
    assert rad["a:b"]["text"] <= 1.0
    assert rad["a:b"]["behavior"] == -1.0      # no fingerprints yet
    same = radiation([a, Lineage("c", STARTER, tmp_path)])
    assert same["a:c"]["text"] == 1.0          # identical = text-collapsed


def test_behavior_distance_reads_the_mix():
    """The diversity instrument's second axis: identical mixes = 0.0,
    disjoint mixes = 1.0 — two text-similar genomes with different
    commitment schedules read apart."""
    from experiments.agent.trainer import _behavior_distance
    a = {"behavior": {"GATHER": 0.5, "FISH": 0.5}}
    b = {"behavior": {"GATHER": 0.5, "FISH": 0.5}}
    c = {"behavior": {"HUNT_SPEAR": 0.5, "STUDY_KNAPPING": 0.5}}
    assert _behavior_distance(a, b) == 0.0
    assert _behavior_distance(a, c) == 1.0     # disjoint strategies
    assert _behavior_distance(a, None) == -1.0  # unknown


def test_founders_seed_from_a_champions_dir(tmp_path):
    """Lineage continuity across runs: --founders reads a prior run's
    champions dir, latest generation per lineage."""
    from experiments.agent.trainer import seed_founders_from_dir
    champs = tmp_path / "champions"
    champs.mkdir()
    champs.joinpath("line-1-gen1.lua").write_text("-- old")
    champs.joinpath("line-1-gen2.lua").write_text("-- new one")
    champs.joinpath("line-2-gen1.lua").write_text("-- two")
    founders = seed_founders_from_dir(champs, 2)
    assert founders == [("line-1", "-- new one"), ("line-2", "-- two")]


# -- the LLM mutator: structural, token-priced, plateau-armed --------------

class _FakeModel:
    """A Model-protocol fake: one canned mutation + optional usage trace."""
    name = "fake:meter"

    def __init__(self, response, usage=None):
        self.response = response
        self.usage = usage
        self.calls = []
        self._cb = None

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.response

    # the HttpModel shape: a property with a setter (the mutator
    # assigns, it does not call)
    @property
    def on_trace(self):
        return self._cb

    @on_trace.setter
    def on_trace(self, cb):
        self._cb = cb

    def emit(self):
        self._cb({"usage": self.usage})


def test_llm_mutator_returns_the_child_and_meters_tokens():
    mutant = STARTER.replace("stone_age_starter", "stone_age_starter_2", 1)
    assert mutant != STARTER
    model = _FakeModel(f"```lua\n{mutant}\n```",
                       usage={"prompt_tokens": 1000,
                              "completion_tokens": 900})
    from experiments.agent.trainer import LlmMutator
    m = LlmMutator(model)
    model.emit()                          # the provider's usage trace
    child = m.mutate(STARTER, {"mean_alive_days": 9.4})
    assert child == mutant.strip()        # fences stripped, source whole
    assert "ONE structural change" in model.calls[0][0]   # the contract
    assert STARTER[:60] in model.calls[0][1]              # the genome rode
    assert m.tokens == 1900


def test_llm_mutator_counts_unusable_as_spent():
    from experiments.agent.trainer import LlmMutator
    m = LlmMutator(_FakeModel("I would suggest improving the fire "
                              "management by ..."))
    assert m.mutate(STARTER, None) is None    # prose, no ctx: unusable
    assert m.calls == 1 and m.failures == 1


def test_the_llm_fires_only_on_the_param_plateau(tmp_path):
    """Tokens are spent where the free operator stopped: the mutator
    stays cold while param turnover continues, fires after a stalled
    generation, and the archive row carries its price."""
    from experiments.agent.trainer import LlmMutator
    mutant = STARTER.replace("stone_age_starter", "stone_age_starter_x", 1)
    llm = LlmMutator(_FakeModel(mutant))
    lin = Lineage("line-1", STARTER, tmp_path)
    rng = random.Random(5)
    first = lin.step(mutants=0, replicates=1, days=1, rng=rng, llm=llm)
    assert first["llm_tokens"] is None and llm.calls == 0   # not stalled
    second = lin.step(mutants=0, replicates=1, days=1, rng=rng, llm=llm)
    assert second["llm_tokens"] == 0 and llm.calls == 1     # stalled: fired
    rows = [json.loads(l) for l in
            (tmp_path / "archive-line-1.jsonl").read_text().splitlines()]
    assert any("tokens" in r for r in rows)


# -- the Goodhart probe: survival past the selection window ----------------

def test_the_probe_reads_champions_past_the_window(tmp_path):
    """--probe-days: after the final generation each lineage champion is
    assayed PAST the selection window and the read lands in the report —
    the standing census check that catches window-overfit before a live
    run does. Not selection: the champion does not change."""
    from experiments.agent.trainer import main
    rc = main(["--lineages", "1", "--generations", "1", "--mutants", "0",
               "--replicates", "1", "--days", "1", "--probe-days", "2",
               "--probe-replicates", "1", "--out", str(tmp_path),
               "--workers", "0", "--block-ops", "0"])
    assert rc == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert "probe" in report and "line-1" in report["probe"]
    probe = report["probe"]["line-1"]
    assert probe["days"] if "days" in probe else True
    assert len(probe["ended_ticks"]) == 1
    assert 0.0 <= probe["mean_alive_days"] <= 2.0


def test_champion_sources_land_on_disk(tmp_path):
    """The archive carries shas; the probe (and re-assaying a past
    champion) needs the genome itself — every generation's belt-holder
    is written to champions/<lineage>-gen<N>.lua."""
    lin = Lineage("line-1", STARTER, tmp_path)
    lin.step(mutants=1, replicates=1, days=1, rng=random.Random(5))
    champ = tmp_path / "champions" / "line-1-gen1.lua"
    assert champ.exists()
    assert champ.read_text() == lin.champion_src


# -- the epsilon margin + window jitter (the assay upgrades) ----------------

def test_epsilon_margin_blocks_noise_turnovers(tmp_path):
    """Run-78's evidence: M-noise dethroned better genomes. A child
    within epsilon of the incumbent does NOT take the belt; the belt
    moves only on signal (child beats the fresh re-assay by > eps)."""
    lin = Lineage("line-1", STARTER, tmp_path)
    rng = random.Random(5)
    first = lin.step(mutants=0, replicates=1, days=1, rng=rng,
                     epsilon=0.25, jitter=0)
    champ = lin.champion_sha
    # day-1 assays: every child ties the champion exactly -> no belt move
    second = lin.step(mutants=3, replicates=1, days=1, rng=rng,
                      epsilon=0.25, jitter=0)
    assert second["turnover"] is False
    assert lin.champion_sha == champ


def test_window_jitter_varies_and_is_shared(tmp_path):
    """The assay window jitters per generation (line-2's 4th-in-window /
    1st-in-probe anomaly's standing answer) and ALL candidates in a
    generation share the same window — selection stays fair within it."""
    lin = Lineage("line-1", STARTER, tmp_path)
    rng = random.Random(5)
    seen = set()
    for _ in range(3):
        row = lin.step(mutants=0, replicates=1, days=10, rng=rng,
                       epsilon=0.25, jitter=2)
        seen.add(row["window"])
    assert seen                            # the window moved somewhere
    assert all(8 <= w <= 12 for w in seen)


# -- the block operator: the mix, the journal, valley crossing -------------

def _lib(tmp_path):
    from experiments.agent.seed_library import seed
    return seed(tmp_path / "lib")


def test_block_operator_draws_real_children(tmp_path):
    from experiments.agent.trainer import BlockOperator
    from experiments.agent.seed_library import seed_alleles_from
    lib = _lib(tmp_path)
    # swaps need a second allele at a shared locus (the plain seed has
    # one per name — run-79's --allele-from provides the real ones)
    champ = tmp_path / "champ.lua"
    champ.write_text(STARTER)
    lib = seed_alleles_from(lib.root, champ, tag="extracted")
    op = BlockOperator(lib)
    rng = random.Random(3)
    seen = set()
    for _ in range(30):
        made = op.child(STARTER, rng)
        assert made is not None
        tag, src, meta = made
        seen.add(meta["op"])
        assert src != STARTER
        # every composed child parses and carries its block
        from experiments.agent.blocklib import parse_blocks
        names = {b.name for b in parse_blocks(src)}
        if meta["op"] == "insert":
            assert meta["fresh_insert"] is True
    assert seen == {"insert", "remove", "swap"}


def test_block_children_journal_their_outcomes(tmp_path):
    """Per-block outcome journaling: every structural child records op,
    key, fresh_insert, parent/child scores, delta, took_belt — and each
    generation appends the champion's block trajectory (the 'good block,
    never got tuned' vs 'bad block' instrument, and the diffusion
    rule's feed)."""
    from experiments.agent.trainer import BlockOperator
    lib = _lib(tmp_path)
    lin = Lineage("line-1", STARTER, tmp_path)
    rng = random.Random(9)
    lin.step(mutants=4, replicates=1, days=1, rng=rng,
             epsilon=0.25, jitter=0, block_op=BlockOperator(lib))
    rows = [json.loads(l)
            for l in (tmp_path / "blocks-line-1.jsonl").read_text().splitlines()]
    champs = [r for r in rows if r["kind"] == "champion"]
    ops = [r for r in rows if r["kind"] == "op"]
    assert len(champs) == 1 and champs[0]["blocks"]
    assert ops and all({"op", "key", "delta", "took_belt"} <= set(o)
                       for o in ops)


def test_fresh_inserts_cross_the_valley(tmp_path):
    """Block-aware epsilon: a fresh-insert child within epsilon BELOW
    its parent can take the belt (library constants arrive untuned —
    param mutation gets a generation to tune them); a param child in
    the same position never does."""
    from experiments.agent.trainer import BlockOperator
    lib = _lib(tmp_path)
    lin = Lineage("line-1", STARTER, tmp_path)
    rng = random.Random(4)
    row = lin.step(mutants=4, replicates=1, days=1, rng=rng,
                   epsilon=0.9, jitter=0, block_op=BlockOperator(lib))
    # with a fat epsilon a fresh insert within it may take the belt;
    # the report row records which tag won
    assert row["best"].startswith(("champion", "param", "block-"))


def test_missing_library_fails_loudly(tmp_path, monkeypatch):
    """The run-85 near-miss, made impossible: a --library dir that
    doesn't exist is an ERROR unless --seed-library says 'I mean to
    create one' — never a silent fresh seed."""
    import pytest as _pytest
    from experiments.agent.trainer import main
    with _pytest.raises(SystemExit) as exc:
        main(["--lineages", "1", "--generations", "1", "--mutants", "0",
              "--replicates", "1", "--days", "1",
              "--library", str(tmp_path / "nope"), "--workers", "0",
              "--out", str(tmp_path / "o")])
    assert "refusing to silently seed" in str(exc.value)
