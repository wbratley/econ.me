"""Seed the block library: the G0 scaffold blocks (the always-available
toolkit) + authored winter blocks + the first extracted artifact (run-78
line-2's param profile, as an allele — the spec's step-1 finding).

Usage:
  python -m experiments.agent.seed_library <library-dir>

Idempotent: re-running refreshes sources and index (provenance kept).
"""
import sys
from pathlib import Path

from .blocklib import Block, Library, parse_blocks
from experiments.world import stone_age

#: the G0 scaffold's blocks, by name -> provenance
_SEED_PROVENANCE = {
    "shelter": "G0 scaffold (seed)",
    "market": "G0 scaffold (seed)",
    "market.errand": "G0 scaffold (seed)",
    "shelter.build": "G0 scaffold (seed)",
}

_WINTER_DIR = Path(__file__).parent / "library"


def seed(library_dir: Path) -> Library:
    lib = Library(library_dir)
    starter = stone_age._gate_pack_script(stone_age.STARTER)
    for blk in parse_blocks(starter):
        if blk.name in _SEED_PROVENANCE:
            blk.provenance = _SEED_PROVENANCE[blk.name]
            lib.add(blk)
    for lua in sorted(_WINTER_DIR.glob("winter-*.lua")):
        for blk in parse_blocks(lua.read_text()):
            if blk.provenance.startswith("authored:"):
                lib.add(blk)
    return lib


def seed_line2_allele(library_dir: Path, champion: Path) -> Library:
    """The spec's first library artifact: run-78 line-2's blocks as an
    ALLELE of each shared locus (swap-level reuse of its param profile
    -- the deep optimum is 100% param-only per the step-1 diff)."""
    lib = seed(library_dir)
    for blk in parse_blocks(champion.read_text()):
        if blk.name in _SEED_PROVENANCE:
            allele = Block(
                name=blk.name, source=blk.source,
                provenance="extracted:line-2@run78-gen4 (allele)",
                slot=blk.slot, needs=blk.needs, provides=blk.provides)
            lib.add(allele, allele="line2")
    return lib


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "block-library")
    lib = seed(out)
    print(f"seeded {out}: {lib.keys()}")
