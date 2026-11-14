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
from .llm import strip_fences, strip_think

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
# The LLM mutator (structural, token-priced — G4's datum)
# ---------------------------------------------------------------------------

MUTATION_SYSTEM = """You are the structural mutator of an evolutionary
trainer. You receive one Lua BEHAVIOUR genome (a stone-age survival
script) and its fitness read. You return the FULL mutated Lua source
and nothing else: no prose, no fences.

The contract:
- ONE structural change: add, remove, or rewrite a block of LOGIC —
  a new exit, a re-ordered plan, a different commitment. Not constant
  tweaks: a parameter operator already covers numbers.
- Keep the ===== BLOCK <name> ===== / ===== END ===== markers intact —
  they are the genome's structure (succession edits block-level).
- Keep ctx.state (the S table) keys you did not change.
- The script must pass the install gate: strict Lua, only the
  documented ctx/std/world/pack vocabulary, honest line numbers.
- Survival is the only judge. Do not be clever; be alive."""

MUTATION_USER = """The champion genome (sha {sha}) and its assay read:

{fit}

What killed seats (if anyone died) is in "deaths"; winter (days 19+)
is the frontier — a genome that maxes autumn and dies in winter has
not solved anything. Make ONE structural change that plausibly moves
the survival read, and return the full source.

{source}"""


class LlmMutator:
    """One structural mutation per call, with its token price on the
    meter — every call is traced (usage when the provider sends it),
    summed, and archived with the candidate. This is the number G4's
    budget sizes: run 75 banked 48 candidates for zero tokens; every
    token from here on is a structural mutation's."""

    def __init__(self, model):
        self.model = model
        self.calls = 0
        self.failures = 0
        self.tokens_prompt = 0
        self.tokens_completion = 0
        if hasattr(model, "on_trace"):
            model.on_trace(self._trace)

    def _trace(self, fields: dict) -> None:
        usage = fields.get("usage") or {}
        self.tokens_prompt += int(usage.get("prompt_tokens") or 0)
        self.tokens_completion += int(usage.get("completion_tokens") or 0)

    @property
    def tokens(self) -> int:
        return self.tokens_prompt + self.tokens_completion

    def mutate(self, src: str, fit: dict | None) -> str | None:
        """One structural child, or None (the model returned something
        unusable — a failed mutation is a spent token, not a crash)."""
        import json as _json
        self.calls += 1
        try:
            raw = self.model.complete(
                MUTATION_SYSTEM,
                MUTATION_USER.format(
                    sha=_sha16(src),
                    fit=_json.dumps(fit or {}, indent=1, default=str),
                    source=src))
        except Exception:
            self.failures += 1
            return None
        out = strip_fences(strip_think(raw)).strip()
        if not out or "ctx." not in out:
            self.failures += 1
            return None
        return out


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
        self.stalled = 0          # generations without turnover: the LLM
                                   # mutator's trigger (param plateau)

    def record(self, *, src: str, mutator: str, parent_sha: str | None,
               fit: dict, champion: bool, tokens: int | None = None) -> dict:
        row = {
            "lineage": self.name, "generation": self.generation,
            "sha": _sha16(src), "parent": parent_sha, "mutator": mutator,
            "champion": champion, "fitness": fit,
        }
        if tokens is not None:
            row["tokens"] = tokens
        with self.archive_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def step(self, *, mutants: int, replicates: int, days: int,
             rng: random.Random, llm=None, llm_stall: int = 1) -> dict:
        """One generation: the champion re-assayed (from scratch — no
        incumbency) against ``mutants`` param children; the best source
        becomes the new champion. The LLM mutator fires only on a
        param plateau — ``stalled`` generations without turnover — so
        tokens are spent where the free operator has stopped moving
        the needle. Returns the generation's report row."""
        candidates = [("champion", self.champion_src)]
        for i in range(mutants):
            candidates.append(
                (f"param-{i}", mutate_params(self.champion_src, rng)))
        llm_tokens = None
        if llm is not None and self.stalled >= llm_stall:
            child = llm.mutate(self.champion_src, self.champion_fit)
            if child is not None and child != self.champion_src:
                candidates.append(("llm-0", child))
            llm_tokens = llm.tokens

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
                        champion=(src == best_src),   # the belt-holder,
                        # incumbent included: a successful title defense
                        # is a champion row too
                        tokens=llm_tokens if tag == "llm-0" else None)
        if turnover:
            self.champion_src, self.champion_sha = best_src, _sha16(best_src)
            self.stalled = 0
        else:
            self.stalled += 1
        self.champion_fit = best_fit
        self.generation += 1
        return {
            "lineage": self.name, "generation": self.generation,
            "best": best_tag, "turnover": turnover,
            "fitness": best_fit, "llm_tokens": llm_tokens,
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
    ap.add_argument("--llm", default=None, metavar="MODEL",
                    help="arm the structural mutator (seat-style model "
                         "name, e.g. deepseek:deepseek-v4-flash); fires "
                         "per lineage only after --llm-stall generations "
                         "without param turnover (the plateau)")
    ap.add_argument("--llm-stall", type=int, default=1)
    ap.add_argument("--out", type=Path, default=Path("trainer-run"))
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    llm = None
    if args.llm:
        from .replay_prompt import build_model as build_llm_model
        llm = LlmMutator(build_llm_model(args.llm, on_trace=None))
        print(f"llm mutator armed: {args.llm} "
              f"(fires after {args.llm_stall} stalled generation(s))")
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
                           rng=rng, llm=llm, llm_stall=args.llm_stall)
            winter = row["fitness"].get("winter")
            wtxt = (f" winter={winter['mean_alive_days']:.2f}"
                    f" deaths={winter['deaths']}" if winter else "")
            print(f"  {lin.name} gen {row['generation']}: best="
                  f"{row['best']} alive={row['fitness']['mean_alive_days']:.2f}"
                  f"{wtxt} turnover={row['turnover']}"
                  + (f" llm_tokens={row['llm_tokens']}"
                     if row.get("llm_tokens") is not None else ""))
            report.append(row)
        rad = radiation(lineages)
        print(f"  radiation: {rad}")
    summary = {"report": report, "radiation": radiation(lineages)}
    if llm is not None:
        summary["llm"] = {"model": args.llm, "calls": llm.calls,
                          "failures": llm.failures,
                          "tokens_prompt": llm.tokens_prompt,
                          "tokens_completion": llm.tokens_completion,
                          "tokens_total": llm.tokens}
        print(f"llm: {llm.calls} calls, {llm.failures} unusable, "
              f"{llm.tokens} tokens")
    args.out.joinpath("report.json").write_text(json.dumps(summary, indent=1))
    print(f"report: {args.out / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
