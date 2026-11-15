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
               "--workers", "0"])
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
