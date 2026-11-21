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
import os
import random
import re
import sys
from pathlib import Path

from experiments.world import stone_age
from experiments.world.assay import (
    ASSAY_DAYS, SEATS, AssayResult, assay_genome, assay_many, fitness)
from .llm import strip_fences, strip_think
from .blocklib import (
    Library, insert_block, parse_blocks, remove_block, swap_block)

#: Literals below this are structure (flags, indexes), not parameters.
MIN_MUTABLE = 2

#: The share of a genome's numeric literals one param mutation touches.
PARAM_TOUCH = 0.15

#: The jitter band: new = old * factor, factor ~ U[0.6, 1.6].
PARAM_LO, PARAM_HI = 0.6, 1.6

#: The turnover margin (alive-days): a child takes the belt only by
#: beating the incumbent's fresh re-assay by MORE than epsilon — run-78's
#: two selection regressions (M=3 noise dethroning better genomes) are
#: the evidence; combined with M=5 replicates the belt only moves on
#: signal, not lottery.
EPSILON_TURNOVER = 0.25

#: The window jitter band (days, per generation): the assay window
#: randomizes +/- this much every generation. Window-tuning (line-2 was
#: 4th in-window, 1st in the 36-day probe) becomes unlearnable; the
#: out-of-sample probe stays census-not-selection.
WINDOW_JITTER_DAYS = 2

#: The mutation budget's structural share (the spec's mix): ~70% param /
#: ~25% block ops / ~5% authorship drafts (the LLM path, cold unless
#: armed). Block ops are near-free; authorship drafts are 42K a call.
BLOCK_OP_SHARE = 0.25
AUTHORSHIP_SHARE = 0.05

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
            try:
                model.on_trace = self._trace   # property setter (HttpModel)
            except AttributeError:
                model.on_trace(self._trace)    # plain method (fakes)

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
# The block operator: structural children from the library
# ---------------------------------------------------------------------------

class BlockOperator:
    """Draws one structural child (insert / remove / swap) from the
    library against a genome — the composition operator's runtime. Ops
    are drawn uniform over what's POSSIBLE (library blocks not yet in
    the genome; genome blocks that are not the last one; alleles at
    shared loci); a genome with nothing possible yields None (the child
    slot falls back to param)."""

    def __init__(self, lib: Library):
        self.lib = lib

    def _moves(self, src: str) -> list[tuple[str, str]]:
        moves: list[tuple[str, str]] = []
        genome_blocks = parse_blocks(src)
        have = {b.name for b in genome_blocks}
        for key in self.lib.keys():
            if self.lib.index[key]["name"] not in have:
                moves.append(("insert", key))
        if len(genome_blocks) > 1:
            for b in genome_blocks:
                moves.append(("remove", b.name))
        for b in genome_blocks:
            alleles = [a for a in self.lib.alleles(b.name)]
            if len(alleles) >= 2:
                for a in alleles:
                    moves.append(("swap", a))
        return moves

    def child(self, src: str, rng: random.Random):
        """(tag, child_src, meta) or None. meta carries what the outcome
        journal needs: the op, the block/allele key, and whether the
        child carries a block fresh to the parent (valley-crossing
        eligibility)."""
        moves = self._moves(src)
        if not moves:
            return None
        op, key = rng.choice(moves)
        parent_blocks = {b.name for b in parse_blocks(src)}
        try:
            if op == "insert":
                blk = self.lib.get(key)
                out = insert_block(src, blk)
            elif op == "remove":
                name = self.lib.index[key]["name"] if key in self.lib.index \
                    else key
                out = remove_block(src, key)
            else:
                blk = self.lib.get(key)
                out = swap_block(src, blk)
        except ValueError:                    # unmet needs / missing block
            return None
        child_blocks = {b.name for b in parse_blocks(out)}
        fresh = bool(child_blocks - parent_blocks)
        return (f"block-{op}:{key}", out,
                {"op": op, "key": key, "fresh_insert": fresh})


# ---------------------------------------------------------------------------
# A lineage
# ---------------------------------------------------------------------------

class Lineage:
    """One isolated line: a founder, a champion, an archive on disk."""

    def __init__(self, name: str, founder_src: str, out_dir: Path):
        self.name = name
        self.out_dir = out_dir
        self.archive_path = out_dir / f"archive-{name}.jsonl"
        self.blocks_path = out_dir / f"blocks-{name}.jsonl"
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
             rng: random.Random, llm=None, llm_stall: int = 1,
             workers: int = 0, epsilon: float = EPSILON_TURNOVER,
             jitter: int = WINDOW_JITTER_DAYS, block_op=None,
             block_share: float = BLOCK_OP_SHARE,
             heir_days: int = 6) -> dict:
        """One generation: the champion re-assayed (from scratch — no
        incumbency) against ``mutants`` param children; the best source
        becomes the new champion — but the belt moves only on signal: a
        child must beat the incumbent's fresh re-assay by MORE than
        epsilon (run-78's M=3 noise regressions are the evidence). The
        window jitters +/- a few days per generation (all candidates
        share the generation's window — selection stays fair within it;
        window-tuning becomes unlearnable across generations). The LLM
        mutator fires only on a param plateau. Returns the report row."""
        if jitter:
            days = max(1, days + rng.randint(-jitter, jitter))
        # the mutation budget's mix (the spec): param ~70% / block ops
        # ~25% / authorship ~5% (the LLM path, cold unless armed AND
        # stalled). Each child slot draws its operator; failed block ops
        # fall back to param children (the mix is a budget, not a quota).
        candidates = [("champion", self.champion_src)]
        block_children: list[dict] = []
        llm_tokens = None
        for i in range(mutants):
            r = rng.random()
            if block_op is not None and r < block_share / (1 - AUTHORSHIP_SHARE):
                made = block_op.child(self.champion_src, rng)
                if made is not None:
                    tag, src_child, meta = made
                    candidates.append((tag, src_child))
                    block_children.append(
                        dict(meta, tag=tag, sha=_sha16(src_child)))
                    continue
            candidates.append((f"param-{i}",
                               mutate_params(self.champion_src, rng)))
        # the authorship draft (the mix's ~5%): fires only when armed AND
        # the free operators have stalled — tokens land on the plateau,
        # one draft per generation max
        if llm is not None and self.stalled >= llm_stall:
            child = llm.mutate(self.champion_src, self.champion_fit)
            if child is not None and child != self.champion_src:
                candidates.append(("llm-0", child))
            llm_tokens = llm.tokens

        # the generation's assays as one flat job list: embarrassingly
        # parallel in-memory worlds (each its own SQLite; no shared
        # state), order-preserving so candidates zip to their
        # replicates deterministically
        jobs = [(src, r, days)
                for _tag, src in candidates for r in range(replicates)]
        flat = assay_many(jobs, workers=workers)
        rows, i = [], 0
        for tag, src in candidates:
            chunk = flat[i:i + replicates]
            i += replicates
            rows.append((tag, src, fitness(chunk)))
        # Deterministic selection with the epsilon margin: the
        # incumbent keeps the belt unless a child beats its fresh
        # re-assay by MORE than epsilon (signal, not lottery — the
        # champion re-proved itself in this very assay). Among eligible
        # children: alive days, then fewer errors, then sha.
        # Block-aware epsilon (valley crossing): a FRESH INSERT arrives
        # untuned (library-default constants) — within-epsilon of the
        # parent it takes the belt over a param-only child, so param
        # mutation gets a generation or two to tune the new gene before
        # the margin rule can strangle it. Everything else still needs
        # parent + epsilon.
        incumbent = next(f for t, _s, f in rows if t == "champion")
        fresh_tags = {bc["tag"] for bc in block_children
                      if bc.get("fresh_insert")}
        children = []
        for t, s_, f in rows:
            if t == "champion":
                continue
            bar = incumbent["mean_alive_days"] - epsilon \
                if t in fresh_tags else incumbent["mean_alive_days"] + epsilon
            if f["mean_alive_days"] > bar:
                children.append((t, s_, f))
        if children:
            def key(item):
                t, _s, f = item
                fresh = 0 if t in fresh_tags else 1   # fresh genes first
                return (-f["mean_alive_days"], fresh,
                        f["script_errors"], _sha16(_s))
            best_tag, best_src, best_fit = sorted(children, key=key)[0]
        else:
            best_tag, best_src, best_fit = "champion", self.champion_src, \
                incumbent
        turnover = best_src != self.champion_src
        # archive every candidate; the winner flagged
        for tag, src, fit in rows:
            self.record(src=src, mutator=tag.split(":")[0].split("-")[0],
                        parent_sha=self.champion_sha, fit=fit,
                        champion=(src == best_src),   # the belt-holder,
                        # incumbent included: a successful title defense
                        # is a champion row too
                        tokens=llm_tokens if tag.startswith("llm") else None)

        # the block outcome journal: every structural child's immediate
        # delta (feeds the diffusion rule later) + the champion's block
        # trajectory (presence + score per generation — the only way to
        # tell 'bad block' from 'good block, never got tuned')
        if block_children or True:
            with self.blocks_path.open("a") as fh:
                by_tag = {t: f for t, _s, f in rows}
                for bc in block_children:
                    fit = by_tag.get(bc["tag"])
                    if fit is None:
                        continue
                    fh.write(json.dumps({
                        "kind": "op", "generation": self.generation + 1,
                        **bc, "parent_score":
                            round(incumbent["mean_alive_days"], 3),
                        "child_score":
                            round(fit["mean_alive_days"], 3),
                        "delta": round(
                            fit["mean_alive_days"]
                            - incumbent["mean_alive_days"], 3),
                        "took_belt": bc["sha"] == _sha16(best_src),
                    }) + "\n")
                fh.write(json.dumps({
                    "kind": "champion", "generation": self.generation + 1,
                    "sha": _sha16(best_src),
                    "score": round(best_fit["mean_alive_days"], 3),
                    "blocks": sorted(
                        b.name for b in parse_blocks(best_src)),
                }) + "\n")
        if turnover:
            self.champion_src, self.champion_sha = best_src, _sha16(best_src)
            self.stalled = 0
        else:
            self.stalled += 1
        self.champion_fit = best_fit
        self.generation += 1
        # the champion's source lands on disk every generation: the
        # archive carries shas + fitness, but the probe (and any future
        # re-assay of a past champion) needs the genome itself
        champs = self.out_dir / "champions"
        champs.mkdir(exist_ok=True)
        champs.joinpath(f"{self.name}-gen{self.generation}.lua").write_text(
            best_src)
        return {
            "lineage": self.name, "generation": self.generation,
            "best": best_tag, "turnover": turnover, "window": days,
            "fitness": best_fit, "llm_tokens": llm_tokens,
            "pool": [{"tag": t, "alive": round(f["mean_alive_days"], 2)}
                     for t, _, f in rows],
        }


# ---------------------------------------------------------------------------
# The radiation metric (diversity across lineages)
# ---------------------------------------------------------------------------

def _behavior_distance(fit_a: dict | None, fit_b: dict | None) -> float:
    """1 - cosine over the two process-mix vectors (union of keys).
    Behavior can diverge while text stays similar (two param-twins
    running different commitment schedules) and vice versa; the digest
    reads both axes. 0.0 = behaviorally identical."""
    a = (fit_a or {}).get("behavior") or {}
    b = (fit_b or {}).get("behavior") or {}
    if not a or not b:
        return -1.0                       # unknown: no fingerprint yet
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return round(1.0 - dot / (na * nb), 3)


def radiation(lineages: list[Lineage]) -> dict:
    """The pool's diversity digest, BOTH axes: pairwise difflib ratios
    between champion SOURCES (1.0 = text-collapsed) and pairwise
    behavioral distances between their fingerprints (0.0 =
    behaviorally identical replicates; high = actually different
    strategies). Param tuning converges on both; the block library is
    where behavioral diversity is expected to enter."""
    out: dict = {}
    for i in range(len(lineages)):
        for j in range(i + 1, len(lineages)):
            a, b = lineages[i], lineages[j]
            ratio = difflib.SequenceMatcher(
                None, a.champion_src, b.champion_src).ratio()
            out[f"{a.name}:{b.name}"] = {
                "text": round(ratio, 3),
                "behavior": _behavior_distance(a.champion_fit,
                                               b.champion_fit),
            }
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


def seed_founders_from_dir(champions_dir: Path,
                           k: int) -> list[tuple[str, str]]:
    """Lineage continuity across runs: founders = a prior run's
    champions dir, latest generation per lineage (lineage names match:
    line-1..line-K). The tier ladder climbs on the SAME lineages — tier
    3 selects among tier 2's survivors, not among fresh starter kicks."""
    founders = []
    for i in range(1, k + 1):
        name = f"line-{i}"
        gens = sorted(champions_dir.glob(f"{name}-gen*.lua"),
                      key=lambda p: p.stat().st_mtime)
        if not gens:
            raise SystemExit(
                f"--founders: no {name}-gen*.lua in {champions_dir}")
        founders.append((name, gens[-1].read_text()))
    return founders


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lineages", type=int, default=4)
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--mutants", type=int, default=3,
                    help="param children per lineage per generation")
    ap.add_argument("--replicates", type=int, default=5,
                    help="assay replicates per candidate (M=5: run-78's "
                         "M=3 selection regressions)")
    ap.add_argument("--epsilon", type=float, default=EPSILON_TURNOVER,
                    help="turnover margin in alive-days")
    ap.add_argument("--no-jitter", action="store_true",
                    help="disable per-generation window jitter")
    ap.add_argument("--library", type=Path, default=None, metavar="DIR",
                    help="the block library dir — the gene pool. Pinned "
                         "and fingerprinted at launch (the pack-manifest "
                         "doctrine): a run reports WHICH library version "
                         "produced its genomes")
    ap.add_argument("--seed-library", action="store_true",
                    help="explicitly CREATE the library at --library "
                         "(fail-loudly doctrine: without this flag a "
                         "missing library dir is an error, never a "
                         "silent fresh seed — the run-85 near-miss)")
    ap.add_argument("--block-ops", type=float, default=BLOCK_OP_SHARE,
                    help="structural children's share of the mutation "
                         "budget (param gets the rest)")
    ap.add_argument("--allele-from", type=Path, default=None,
                    metavar="CHAMPION.lua",
                    help="extract a champion's blocks as library alleles "
                         "(provenance-carried) — the diffusion rule's "
                         "manual arm, run-79's line-2 path")
    ap.add_argument("--days", type=int, default=ASSAY_DAYS)
    ap.add_argument("--seed", type=int, default=74)
    ap.add_argument("--founders", type=Path, default=None, metavar="DIR",
                    help="lineage continuity: seed from a prior run's "
                         "champions/ dir (latest gen per lineage) instead "
                         "of fresh starter kicks — the tier ladder climbs "
                         "on the same lines")
    ap.add_argument("--llm", default=None, metavar="MODEL",
                    help="arm the structural mutator (seat-style model "
                         "name, e.g. deepseek:deepseek-v4-flash); fires "
                         "per lineage only after --llm-stall generations "
                         "without param turnover (the plateau)")
    ap.add_argument("--llm-stall", type=int, default=1)
    ap.add_argument("--workers", type=int,
                    default=min(8, (os.cpu_count() or 1)),
                    help="assay process pool size (early termination runs "
                         "first: a world with no counting seat alive stops "
                         "ticking, which also shrinks per-world cost "
                         "variance and makes pool balancing trivial)")
    ap.add_argument("--probe-days", type=int, default=0, metavar="N",
                    help="the Goodhart check: after the final generation, "
                         "assay each lineage champion PAST the selection "
                         "window (N days) — a frontier that aces the "
                         "window and falls over past it is overfit to the "
                         "window, and this reads it in the digest before a "
                         "live run does. Not selection: a census probe.")
    ap.add_argument("--probe-replicates", type=int, default=2)
    ap.add_argument("--bequest-select", action="store_true",
                    help="DEPRECATED (spec §DOCTRINE, post run-85): "
                         "bequest_days is plumbing diagnostics, not the "
                         "goal — structurally forced, not strategically "
                         "emergent (the streetlight effect). The flag "
                         "now refuses; selection stays survival + "
                         "surplus (tier-3). The bequest assay, flow "
                         "columns, and fuel_share remain census "
                         "instruments via --bequest-probe")
    ap.add_argument("--bequest-probe", type=int, default=0, metavar="DAYS",
                    help="tier-4 census-not-selection (stage 1): each "
                         "generation, every lineage champion runs the "
                         "two-generation bequest assay (heir window "
                         "DAYS) — rows in the report; selection stays "
                         "tier-3. Watches (pre-registered): spread, "
                         "gen1:heir gaming ratio, STOCKPILE adoption + "
                         "fuel share")
    ap.add_argument("--out", type=Path, default=Path("trainer-run"))
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    if args.bequest_select:
        raise SystemExit(
            "--bequest-select is demoted (spec §DOCTRINE): bequest is "
            "plumbing diagnostics, not the selection objective. "
            "Survival + surplus (tier-3) is the active assay; use "
            "--bequest-probe for the diagnostic rows.")
    block_op = None
    library_sha = None
    if args.block_ops > 0:
        from .seed_library import seed, seed_alleles_from
        lib_dir = args.library or (args.out / "library")
        if not (lib_dir / "index.json").exists():
            if not args.seed_library:
                raise SystemExit(
                    f"library {lib_dir} does not exist — refusing to "
                    f"silently seed fresh (which library produced "
                    f"this genome must never be archaeology). Pass "
                    f"--seed-library to create it, or point --library "
                    f"at the versioned home")
            seed(lib_dir)
            if args.allele_from:
                seed_alleles_from(lib_dir, args.allele_from)
            print(f"library: SEEDED {lib_dir} (explicit --seed-library)")
        lib = Library(lib_dir)
        library_sha = lib.fingerprint()
        print(f"library: pinned {lib_dir} fingerprint={library_sha} "
              f"({len(lib.keys())} blocks)")
        block_op = BlockOperator(lib)
    llm = None
    if args.llm:
        from .replay_prompt import build_model as build_llm_model
        llm = LlmMutator(build_llm_model(args.llm, on_trace=None))
        print(f"llm mutator armed: {args.llm} "
              f"(fires after {args.llm_stall} stalled generation(s))")
    if args.founders:
        founders = seed_founders_from_dir(args.founders, args.lineages)
        print(f"founders: {args.founders} (continuity: "
              + ", ".join(f"{n}:{_sha16(s)[:8]}" for n, s in founders)
              + ")")
    else:
        founders = seed_founders(args.lineages, args.seed)
    lineages = [Lineage(name, src, args.out)
                for name, src in founders]
    print(f"trainer: {len(lineages)} lineages x {args.generations} "
          f"generations, {args.mutants}+1 candidates each, "
          f"{args.replicates} replicate worlds x {args.days} days")

    report = []
    bequest_rows: list[dict] = []
    for g in range(args.generations):
        for lin in lineages:
            row = lin.step(mutants=args.mutants,
                           replicates=args.replicates, days=args.days,
                           rng=rng, llm=llm, llm_stall=args.llm_stall,
                           workers=args.workers, epsilon=args.epsilon,
                           jitter=0 if args.no_jitter
                           else WINDOW_JITTER_DAYS,
                           block_op=block_op,
                           block_share=args.block_ops,
                           heir_days=args.bequest_probe or 6)
            winter = row["fitness"].get("winter")
            wtxt = (f" winter={winter['mean_alive_days']:.2f}"
                    f" deaths={winter['deaths']}" if winter else "")
            print(f"  {lin.name} gen {row['generation']}: best="
                  f"{row['best']} alive={row['fitness']['mean_alive_days']:.2f}"
                  f"{wtxt} win={row['window']} turnover={row['turnover']}"
                  + (f" llm_tokens={row['llm_tokens']}"
                     if row.get("llm_tokens") is not None else ""))
            report.append(row)
        rad = radiation(lineages)
        print(f"  radiation: {rad}")
        if args.bequest_probe:
            from experiments.world.assay import run_bequest
            for lin in lineages:
                row = run_bequest(lin.champion_src, days=args.days,
                                  heir_days=args.bequest_probe)
                seats = [x for x in row["seats"]
                         if x["heir_days"] is not None]
                if seats:
                    b = sum(x["bequest_days"] for x in seats) / len(seats)
                    g1 = sum(x["gen1_days"] for x in seats) / len(seats)
                    hd = sum(x["heir_days"] for x in seats) / len(seats)
                    fs = sum(x["fuel_share"] for x in seats) / len(seats)
                    st = sum(1 for x in seats if x["stockpile"])
                else:
                    b = g1 = hd = fs = 0.0
                    st = 0
                bequest_rows.append({
                    "lineage": lin.name, "generation": lin.generation,
                    "bequest_days": round(b, 2), "gen1_days": round(g1, 2),
                    "heir_days": round(hd, 2),
                    "gen1_heir_ratio": round(g1 / hd, 2) if hd else None,
                    "fuel_share": round(fs, 3), "stockpile_seats": st,
                })
                last = bequest_rows[-1]
                print(f"  bequest[{lin.name}] gen{lin.generation}: "
                      f"bequest={last['bequest_days']:.2f} "
                      f"(gen1 {last['gen1_days']:.2f} + heir "
                      f"{last['heir_days']:.2f}) fuel_share="
                      f"{last['fuel_share']} stockpile={st}/"
                      f"{len(seats) or 1}")
    summary = {"report": report, "radiation": radiation(lineages)}
    if library_sha is not None:
        summary["library_sha"] = library_sha
    if args.bequest_probe:
        summary["bequest"] = bequest_rows
    if block_op is not None:
        adoption: dict[str, list[str]] = {}
        for lin in lineages:
            for b in parse_blocks(lin.champion_src):
                adoption.setdefault(b.name, []).append(lin.name)
        summary["adoption"] = {
            k: v for k, v in sorted(adoption.items()) if len(v) >= 1}
        shared = {k: v for k, v in summary["adoption"].items()
                  if len(v) >= 2}
        print(f"adoption: {len(summary['adoption'])} blocks in champions, "
              f"{len(shared)} in >=2 lineages: {shared}")
    if args.probe_days:
        print(f"probe: champions at {args.probe_days} days "
              f"({args.probe_days - args.days} past the window)")
        probe = {}
        for lin in lineages:
            results = assay_many(
                [(lin.champion_src, r, args.probe_days)
                 for r in range(args.probe_replicates)],
                workers=args.workers)
            fit = fitness(results)
            probe[lin.name] = {"sha": lin.champion_sha, **fit,
                               "ended_ticks": [r.ended_tick
                                               for r in results]}
            w = fit.get("winter") or {}
            print(f"  {lin.name}: alive={fit['mean_alive_days']:.2f}/"
                  f"{args.probe_days} winter={w.get('mean_alive_days', -1):.2f}"
                  f" deaths={fit['deaths']} ended={probe[lin.name]['ended_ticks']}")
        summary["probe"] = probe
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
