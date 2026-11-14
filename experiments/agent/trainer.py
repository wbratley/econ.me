"""G2b: the trainer — genomes are made here, proven in the assay,
raced in the continuous world (the competition layer).

The spec amendment's economics: generation is cheap, evaluation is
expensive. Every candidate costs a generation of world time to assay
regardless of who authored its mutation, so the trainer spends its
mutation budget on the most INFORMED operator per evaluation:

  * ``param`` (token-free): jitter the genome's numeric literals —
    thresholds, prices, buffers. The scaffold's structure stays; the
    constants move. Most mutations are lethal or inert; the assay
    prices them all the same, so the param operator runs WIDE (many
    candidates per generation, zero tokens).
  * ``llm`` (structural, token-priced): a model edit of the genome —
    new blocks, new exits, new logic. Rare, expensive, saved for when
    the param operator plateaus.

The loop, per lineage (K isolated lines, own mutators, NO crossover —
diversity is protected by isolation, and pairwise champion diffs in
the report measure whether the pool is actually radiating):

    founder -> [champion + mutants] -> assay each x M replicates
            -> champion = argmax(mean_alive_days, -errors, -len)

Every generation is TESTED FROM SCRATCH: the champion re-competes
against its own children under the same standardized assay — no
incumbency, no inheritance, no escrow (banned from the assay by the
spec). A champion that only survives with help is selected against.

The archive (``archive.jsonl`` per lineage) is the trainer's whole
memory: every candidate, its parent, its mutator, its fitness. A
champion that falls is not lost — the lineage's history is the gene
bank.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import random
import re
import sys
from pathlib import Path

from experiments.world import stone_age
from experiments.world.assay import (
    ASSAY_DAYS, SEATS, AssayResult, assay_genome, fitness)

#: Literals below this are structure (flags, indexes), not parameters.
MIN_MUTABLE = 2

#: The share of a genome's numeric literals one param mutation touches.
PARAM_TOUCH = 0.15

#: The jitter band: new = old * factor, factor ~ U[0.6, 1.6].
PARAM_LO, PARAM_HI = 0.6, 1.6

_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")


def _sha16(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# The param mutator (token-free)
# ---------------------------------------------------------------------------

_STRING_MASK = re.compile(r'"[^"\n]*"|\'[^\n]*\'')


def _split_code_and_rest(line: str) -> tuple[str, str]:
    """(code, rest) where rest is a trailing comment, if any. The
    mutator never touches comments — they are the genome's memory."""
    if line.lstrip().startswith("--"):
        return "", line
    # find '--' OUTSIDE string literals: mask the strings, then scan
    masked = _STRING_MASK.sub(lambda m: " " * len(m.group(0)), line)
    idx = masked.find("--")
    if idx < 0:
        return line, ""
    return line[:idx], line[idx:]


def _jitter_line_code(code: str, rng: random.Random) -> str:
    """Jitter numeric literals in one line of CODE, with string literals
    masked (prices inside quotes are data the genome quotes verbatim —
    a mutation that rewrites a quoted constant is a different mutator's
    job, and an unquoted escape)."""
    strings: list[str] = []

    def _stash(match):
        # a private-use char per stashed string: letters, not digits —
        # the number jitter must never see (and rewrite) the stash keys
        strings.append(match.group(0))
        return chr(0xE000 + len(strings) - 1)

    masked = _STRING_MASK.sub(_stash, code)
    if masked is not code and not strings:
        return code

    def _maybe(match):
        literal = match.group(1)
        value = float(literal)
        if value < MIN_MUTABLE or rng.random() > PARAM_TOUCH:
            return literal
        factor = rng.uniform(PARAM_LO, PARAM_HI)
        new = value * factor
        if "." in literal:
            new = round(new, len(literal.split(".")[1]))
        else:
            new = max(1, round(new))
        return str(new)

    masked = _NUMBER.sub(_maybe, masked)
    out = re.sub("[\ue000-\uf8ff]",
                 lambda m: strings[ord(m.group(0)) - 0xE000], masked)
    return out


def mutate_params(source: str, rng: random.Random) -> str:
    """Jitter some numeric literals (>= MIN_MUTABLE) in the code parts
    of the genome. Structure is untouchable by construction: the same
    tokens come back with different digits, so the lint gate can only
    refuse on semantics, never syntax."""
    out_lines = []
    for line in source.splitlines():
        code, rest = _split_code_and_rest(line)
        if code:
            code = _jitter_line_code(code, rng)
        out_lines.append(code + rest)
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# A lineage
# ---------------------------------------------------------------------------

class Lineage:
    """One isolated line: a founder, a champion, an archive on disk."""

    def __init__(self, name: str, founder_src: str, out_dir: Path):
        self.name = name
        self.out_dir = out_dir
        self.archive_path = out_dir / f"archive-{name}.jsonl"
        self.champion_src = founder_src
        self.champion_sha = _sha16(founder_src)
        self.champion_fit: dict | None = None
        self.generation = 0

    def record(self, *, src: str, mutator: str, parent_sha: str | None,
               fit: dict, champion: bool) -> dict:
        row = {
            "lineage": self.name, "generation": self.generation,
            "sha": _sha16(src), "parent": parent_sha, "mutator": mutator,
            "champion": champion, "fitness": fit,
        }
        with self.archive_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def step(self, *, mutants: int, replicates: int, days: int,
             rng: random.Random, llm=None) -> dict:
        """One generation: the champion re-assayed (from scratch — no
        incumbency) against ``mutants`` param children; the best source
        becomes the new champion. Returns the generation's report row."""
        candidates = [("champion", self.champion_src)]
        for i in range(mutants):
            candidates.append(
                (f"param-{i}", mutate_params(self.champion_src, rng)))
        if llm is not None:
            children = llm(self.name, self.champion_src,
                           self.champion_fit, replicates)
            for i, src in enumerate(children):
                candidates.append((f"llm-{i}", src))

        rows = []
        for tag, src in candidates:
            results = assay_genome(src, replicates=replicates, days=days)
            rows.append((tag, src, fitness(results)))
        # deterministic selection: alive days, then fewer errors, then
        # the INCUMBENT (ties keep the champion: the tiebreak is stability,
        # not churn — the champion already re-proved itself from scratch
        # in this very assay), then sha
        def key(item):
            tag, src, fit = item
            return (-fit["mean_alive_days"], fit["script_errors"],
                    0 if tag == "champion" else 1, _sha16(src))
        ranked = sorted(rows, key=key)
        best_tag, best_src, best_fit = ranked[0]
        turnover = best_src != self.champion_src
        # archive every candidate; the winner flagged
        for tag, src, fit in rows:
            self.record(src=src, mutator=tag.split("-")[0],
                        parent_sha=self.champion_sha, fit=fit,
                        champion=(src == best_src))   # the belt-holder,
                        # incumbent included: a successful title defense
                        # is a champion row too
        if turnover:
            self.champion_src, self.champion_sha = best_src, _sha16(best_src)
        self.champion_fit = best_fit
        self.generation += 1
        return {
            "lineage": self.name, "generation": self.generation,
            "best": best_tag, "turnover": turnover,
            "fitness": best_fit,
            "pool": [{"tag": t, "alive": round(f["mean_alive_days"], 2)}
                     for t, _, f in ranked],
        }


# ---------------------------------------------------------------------------
# The radiation metric (diversity across lineages)
# ---------------------------------------------------------------------------

def radiation(lineages: list[Lineage]) -> dict:
    """Pairwise difflib ratios between lineage champions: 1.0 = the pool
    has collapsed to one genome; low pairwise ratios = the pool is
    actually radiating. The report's diversity read."""
    out = {}
    for i in range(len(lineages)):
        for j in range(i + 1, len(lineages)):
            a, b = lineages[i], lineages[j]
            ratio = difflib.SequenceMatcher(
                None, a.champion_src, b.champion_src).ratio()
            out[f"{a.name}:{b.name}"] = round(ratio, 3)
    return out


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def seed_founders(k: int, seed: int) -> list[tuple[str, str]]:
    """K founders: the pack's starter genome, each lineage's copy given
    one distinct param kick (so the lines START apart — radiation is
    seeded, not waited for)."""
    starter = stone_age._gate_pack_script(stone_age.STARTER)
    rng = random.Random(seed)
    founders = [("line-1", starter)]
    for i in range(2, k + 1):
        founders.append((f"line-{i}", mutate_params(starter, rng)))
    return founders[:k]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lineages", type=int, default=4)
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--mutants", type=int, default=3,
                    help="param children per lineage per generation")
    ap.add_argument("--replicates", type=int, default=3)
    ap.add_argument("--days", type=int, default=ASSAY_DAYS)
    ap.add_argument("--seed", type=int, default=74)
    ap.add_argument("--out", type=Path, default=Path("trainer-run"))
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    lineages = [Lineage(name, src, args.out)
                for name, src in seed_founders(args.lineages, args.seed)]
    print(f"trainer: {len(lineages)} lineages x {args.generations} "
          f"generations, {args.mutants}+1 candidates each, "
          f"{args.replicates} replicate worlds x {args.days} days")

    report = []
    for g in range(args.generations):
        for lin in lineages:
            row = lin.step(mutants=args.mutants,
                           replicates=args.replicates, days=args.days,
                           rng=rng)
            print(f"  {lin.name} gen {row['generation']}: best="
                  f"{row['best']} alive={row['fitness']['mean_alive_days']:.2f}"
                  f" turnover={row['turnover']}")
            report.append(row)
        rad = radiation(lineages)
        print(f"  radiation: {rad}")
    args.out.joinpath("report.json").write_text(json.dumps(
        {"report": report, "radiation": radiation(lineages)}, indent=1))
    print(f"report: {args.out / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
