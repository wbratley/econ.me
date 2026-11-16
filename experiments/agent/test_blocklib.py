"""The block library substrate: parse, diff, library, compose ops."""

import pytest

from experiments.agent.blocklib import (
    Block, Library, diff_genomes, insert_block, needs_met, parse_blocks,
    remove_block, swap_block)
from experiments.agent.seed_library import seed
from experiments.world import stone_age

STARTER = stone_age._gate_pack_script(stone_age.STARTER)


# -- parsing ---------------------------------------------------------------

def test_parses_the_g0_blocks_from_the_starter():
    names = [b.name for b in parse_blocks(STARTER)]
    assert {"shelter", "market", "market.errand", "shelter.build"} <= set(names)
    market = next(b for b in parse_blocks(STARTER) if b.name == "market")
    assert "G0" in market.provenance
    assert market.slot == "market"            # the 2d section prefix infers it


def test_slot_metadata_and_section_inference():
    blk = parse_blocks(
        "-- ===== BLOCK x | authored:test | slot=recovery =====\n"
        "if true then end\n"
        "-- ===== END x =====\n")[0]
    assert blk.slot == "recovery" and blk.provenance == "authored:test"
    # needs/provides parse; a section prefix infers slot absent explicit meta
    blk2 = parse_blocks(
        "-- 1a. ===== BLOCK y | needs=a,S.b | provides=S.c =====\n"
        "-- ===== END y =====\n")[0]
    assert blk2.slot == "survival" and blk2.needs == ["a", "b"]
    assert blk2.provides == ["c"]


# -- block-granularity diffs -------------------------------------------------

def test_diff_classifies_param_only_vs_structural():
    # an IN-BLOCK literal (the shelter block's gate), not a bare-section one
    shelter = next(b for b in parse_blocks(STARTER) if b.name == "shelter")
    lit = sorted(shelter.params().items())[0]
    line = shelter.source.splitlines()[lit[0]]
    tuned = STARTER.replace(line, line.replace(lit[1][0], "99", 1), 1)
    assert tuned != STARTER
    d = diff_genomes(STARTER, tuned)
    assert not d["added"] and not d["removed"] and not d["structural"]
    assert d["param_only"]

    lib = seed(__import__("pathlib").Path("/tmp/lib-test-blocks"))
    struct = insert_block(STARTER, lib.get("winter.ration"))
    d2 = diff_genomes(STARTER, struct)
    assert d2["added"] == ["winter.ration"]


# -- the library -------------------------------------------------------------

def test_library_round_trip_and_alleles(tmp_path):
    lib = seed(tmp_path / "lib")
    assert "market" in lib.keys() and "winter.ration" in lib.keys()
    allele = Block(name="market", source=lib.get("market").source,
                   provenance="extracted:line-2@run78", slot="market")
    lib.add(allele, allele="line2")
    assert lib.alleles("market") == ["market", "market-line2"]
    # index persists
    lib2 = Library(tmp_path / "lib")
    assert lib2.alleles("market") == ["market", "market-line2"]
    assert lib2.get("market-line2").provenance == "extracted:line-2@run78"


# -- the compose ops -----------------------------------------------------------

def test_insert_needs_check_refuses_unmet():
    needy = Block(name="x", source="-- x", needs=["never_seen_key"])
    with pytest.raises(ValueError):
        insert_block(STARTER, needy)
    assert not needs_met(STARTER, needy)
    selffed = Block(name="x", source="S.never_seen_key = 1",
                    needs=["never_seen_key"])
    assert needs_met(STARTER, selffed)


def test_remove_and_swap_round_trip(tmp_path):
    lib = seed(tmp_path / "lib")
    assert "market.errand" in [b.name for b in parse_blocks(STARTER)]
    slim = remove_block(STARTER, "market.errand")
    assert "market.errand" not in [b.name for b in parse_blocks(slim)]
    assert len(slim) < len(STARTER)
    with pytest.raises(ValueError):            # not the last block's problem,
        remove_block(slim, "no-such-block")    # but a missing one raises
    allele = Block(name="market", source=lib.get("market").source,
                   provenance="extracted:t", slot="market")
    swapped = swap_block(STARTER, allele)
    names = [b.name for b in parse_blocks(swapped)]
    assert "market" in names and "market.errand" in names


def test_the_composed_genomes_pass_the_real_gate(tmp_path):
    """The operator's contract with the assay: every composed child is a
    real installable genome — insert (all three winter blocks), remove,
    and swap must all clear the lint gate, not just parse."""
    from econengine import services
    from econengine.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    lib = seed(tmp_path / "lib")
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        stone_age.create_content(s)
        children = {
            "insert.stockpile": insert_block(STARTER, lib.get("winter.stockpile")),
            "insert.ration": insert_block(STARTER, lib.get("winter.ration")),
            "insert.restart": insert_block(STARTER, lib.get("winter.spring_restart")),
            "remove.errand": remove_block(STARTER, "market.errand"),
        }
        for i, (tag, src) in enumerate(children.items()):
            house = stone_age.make_house(s, f"H{i}")
            house.owner_id = "u-assay"
            s.flush()
            services.set_entity_behaviour(s, house, src, owner_id="u-assay")
