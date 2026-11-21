"""The block library + composition operator's substrate (the spec:
~/econ-runs/block-library-spec.md).

G0 gave genomes their structure — named BLOCK banners inside a
numbered section flow — and #266 put champions on disk. This module
turns that structure into an addressable substrate:

  parse_blocks    the ===== BLOCK name[.part] | provenance ===== banners
                  into Block records (source span, provenance, and the
                  slot/needs/provides metadata when present)
  diff_genomes    block-granularity diffs: added / removed / structural
                  / param-only — the extraction tooling's eye
  Library         a dir of block files + index.json: name -> alleles,
                  provenance-tracked; the shared toolkit (lineages never
                  merge, the TOOLKIT is environment — the diffusion
                  rule governs what enters it)
  compose ops     insert / remove / swap — the structural children the
                  trainer now mixes with param children

Slot classes are the spec's insertion-order contract: blocks insert
only into their class's region of the tick flow. The starter's own
section numbering maps onto them (RANKS below).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# -- markers ---------------------------------------------------------------

_START = re.compile(
    r"^(?P<indent>\s*)--\s*(?:(?P<sec>\d[\w-]*[.-])\s+)?"
    r"===== BLOCK (?P<name>[A-Za-z0-9_.-]+)"
    r"(?P<meta>.*?)=====\s*$")
_END = re.compile(r"^\s*--\s*===== END (?P<name>[A-Za-z0-9_.-]+)\s*=====\s*$")
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")

#: slot classes (the spec) -> insertion rank. The starter's sections map:
#: 0..1b survival-critical, 2..3 provisioning, 2d/4 market, later recovery.
SLOTS = ("survival", "provisioning", "market", "recovery")
SLOT_RANK = {s: i for i, s in enumerate(SLOTS)}
#: section-number prefix -> slot class (the starter's own ordering)
_SECTION_SLOT = [
    (re.compile(r"^0"), "survival"),        # eat, drink, heal
    (re.compile(r"^1"), "survival"),        # fire, sleep, shelter, wolves
    (re.compile(r"^2[dD]"), "market"),      # the counter's law, POST
    (re.compile(r"^2"), "provisioning"),    # cooking, spear, study
    (re.compile(r"^[3-9]"), "provisioning"),
]


def _parse_meta(meta: str | None, name: str, sec: str | None = None) -> dict:
    """`| provenance | slot=x | needs=a,b | provides=c` — provenance is
    free text until a key=value token; unknown keys ignored. The
    starter's banners carry section prefixes (2d = market): when no
    explicit slot= is given the prefix infers it."""
    out = {"provenance": "", "slot": "survival", "needs": [], "provides": []}
    if sec and not meta_has_slot(meta):
        for pat, slot in _SECTION_SLOT:
            if pat.match(sec):
                out["slot"] = slot
                break
    parts = [p.strip() for p in (meta or "").split("|")] if meta else []
    for p in parts:
        kv = p.split("=", 1)
        if len(kv) == 2 and kv[0].strip() in ("slot", "needs", "provides"):
            k, v = kv[0].strip(), kv[1].strip()
            if k == "slot" and v in SLOTS:
                out["slot"] = v
            elif k != "slot":
                # state keys are BARE (last_season); an S. prefix is
                # stripped so the needs check can compose S.<key>
                out[k] = [x.strip().removeprefix("S.")
                          for x in v.split(",") if x.strip()]
        elif p:
            out["provenance"] = (out["provenance"] + " | " + p).strip(" |")
    return out


def meta_has_slot(meta: str | None) -> bool:
    return bool(meta) and "slot=" in meta


@dataclass
class Block:
    name: str                      # e.g. "market.errand"
    source: str                    # the banner .. END-or-next-banner span
    provenance: str = ""
    slot: str = "survival"
    needs: list = field(default_factory=list)
    provides: list = field(default_factory=list)
    start_line: int = 0            # 1-based, in the parent genome

    def params(self) -> dict:
        """The block's numeric literals by line — the param signature
        (block-granularity diffs classify param-only vs structural)."""
        out: dict[int, list[str]] = {}
        for i, line in enumerate(self.source.splitlines()):
            code = line.split("--")[0]
            vals = _NUMBER.findall(code)
            if vals:
                out[i] = vals
        return out

    def banner(self) -> str:
        meta = f" | {self.provenance}" if self.provenance else ""
        needs = f" | needs={','.join(self.needs)}" if self.needs else ""
        provides = (f" | provides={','.join(self.provides)}"
                    if self.provides else "")
        return (f"-- ===== BLOCK {self.name} | slot={self.slot}"
                f"{needs}{provides}{meta} =====")


def _section_banner_rank(line: str) -> int | None:
    """A numbered section banner's slot rank (insertion anchors)."""
    m = re.match(r"^--\s+(\d[\w-]*)[.)]?\s", line)
    if not m:
        return None
    sec = m.group(1)
    for pat, slot in _SECTION_SLOT:
        if pat.match(sec):
            return SLOT_RANK[slot]
    return None


def parse_blocks(source: str) -> list[Block]:
    """Every BLOCK banner in the genome, with its source span. A block
    closes at its END marker; banner-only blocks (no END — the starter's
    inline style) close at the next banner or numbered section."""
    lines = source.splitlines()
    blocks: list[Block] = []
    open_i: int | None = None
    open_block: Block | None = None
    for i, line in enumerate(lines):
        m = _START.match(line)
        if m:
            if open_block is not None:      # banner-only block closes here
                open_block.source = "\n".join(lines[open_i:i])
                blocks.append(open_block)
            meta = _parse_meta(m.group("meta"), m.group("name"),
                               m.group("sec")) or {
                "provenance": "", "slot": "survival",
                "needs": [], "provides": []}
            open_i, open_block = i, Block(
                name=m.group("name"), source="", start_line=i + 1, **meta)
            continue
        e = _END.match(line)
        if e and open_block is not None and e.group("name") == open_block.name:
            open_block.source = "\n".join(lines[open_i:i + 1])
            blocks.append(open_block)
            open_block, open_i = None, None
            continue
        if open_block is not None and _section_banner_rank(line) is not None:
            # a numbered section banner closes a banner-only block
            open_block.source = "\n".join(lines[open_i:i])
            blocks.append(open_block)
            open_block, open_i = None, None
    if open_block is not None:
        open_block.source = "\n".join(lines[open_i:])
        blocks.append(open_block)
    return blocks


def _param_signature(block: Block) -> list:
    return [v for _, vals in sorted(block.params().items()) for v in vals]


def _structural_signature(block: Block) -> str:
    """The block's code with numeric literals masked: what is structur-
    ally there, independent of tuning."""
    out = []
    for line in block.source.splitlines():
        code = line.split("--")[0].strip()
        if code:
            out.append(_NUMBER.sub("#", code))
    return "\n".join(out)


def diff_genomes(a: str, b: str) -> dict:
    """Block-granularity diff a -> b: added / removed block names, and
    per shared block whether the change is param-only (identical
    structure, different literals) or structural. The extraction
    tooling's eye: structural divergence between a champion and its
    siblings is where a candidate block lives."""
    ba = {blk.name: blk for blk in parse_blocks(a)}
    bb = {blk.name: blk for blk in parse_blocks(b)}
    out = {
        "added": sorted(set(bb) - set(ba)),
        "removed": sorted(set(ba) - set(bb)),
        "param_only": [],
        "structural": [],
        "param_diffs": {},
    }
    for name in sorted(set(ba) & set(bb)):
        sa, sb = _structural_signature(ba[name]), _structural_signature(bb[name])
        if sa != sb:
            out["structural"].append(name)
        pa, pb = _param_signature(ba[name]), _param_signature(bb[name])
        if pa != pb:
            out["param_only"].append(name)
            out["param_diffs"][name] = {"a": pa, "b": pb}
    out["param_only"] = [n for n in out["param_only"]
                         if n not in out["structural"]]
    return out


# ---------------------------------------------------------------------------
# The library: the shared toolkit (provenance-tracked, diffusion-governed)
# ---------------------------------------------------------------------------

class Library:
    """A directory of block files + index.json. Alleles: several files
    may serve the same locus (name), differing in params/variant; the
    operator's `swap` trades one allele for another. Provenance is
    mandatory (seed / authored:<x> / extracted:<lineage>@<run>-gen<N>)
    — the diffusion rule reads it: an extracted block enters only after
    its lineage proved it one tier up."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        if self.index_path.exists():
            self.index = json.loads(self.index_path.read_text())
        else:
            self.index = {}

    def _save(self) -> None:
        self.index_path.write_text(json.dumps(self.index, indent=1))

    def add(self, block: Block, *, allele: str = "") -> str:
        """Add a block (an allele of its locus). File name:
        <name>[-<allele>].lua; the index records slot/needs/provides/
        provenance/params."""
        key = f"{block.name}{('-' + allele) if allele else ''}"
        self.root.joinpath(f"{key}.lua").write_text(block.source)
        self.index[key] = {
            "name": block.name, "allele": allele, "slot": block.slot,
            "needs": block.needs, "provides": block.provides,
            "provenance": block.provenance,
        }
        self._save()
        return key

    def alleles(self, name: str) -> list[str]:
        return sorted(k for k, v in self.index.items()
                      if v["name"] == name)

    def get(self, key: str) -> Block:
        v = self.index[key]
        return Block(
            name=v["name"], source=self.root.joinpath(f"{key}.lua").read_text(),
            provenance=v["provenance"], slot=v["slot"],
            needs=v["needs"], provides=v["provides"])

    def keys(self) -> list[str]:
        return sorted(self.index)

    def fingerprint(self) -> str:
        """A stable content hash of the whole library — sorted (key,
        provenance, slot, source) pairs. The version a run used: pinned
        at launch, printed, archived, and carried in the report (the
        pack-manifest doctrine, one level up: the library is the
        accumulated gene pool, and 'which library produced this
        genome' must be answerable without archaeology)."""
        import hashlib
        h = hashlib.sha256()
        for key in sorted(self.index):
            v = self.index[key]
            src = self.root.joinpath(f"{key}.lua").read_text()
            h.update(key.encode())
            h.update(str(v.get("provenance", "")).encode())
            h.update(str(v.get("slot", "")).encode())
            h.update(src.encode())
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# The composition ops (the trainer's structural children)
# ---------------------------------------------------------------------------

def _slot_rank_of_section(lines: list[str], i: int) -> int | None:
    return _section_banner_rank(lines[i])


def needs_met(genome_src: str, block: Block) -> bool:
    """The cheap static check: every needs= key (a ctx.state key or a
    library dependency) must appear in the host genome or be provided
    by the block itself. Unmet inserts are refused before the assay
    spends worlds on them."""
    hay = genome_src + "\n" + block.source
    return all((f"S.{k}" in hay) or (k in block.provides)
               for k in block.needs)


def insert_block(genome_src: str, block: Block) -> str:
    """Insert a block into its slot's region: after the last line of
    the last block/section of a lower-or-equal rank, i.e. immediately
    before the first anchor (BLOCK banner or numbered section) of a
    HIGHER rank; none found -> the genome's end. The needs check gates
    the call, and so does presence (ValueError on unmet needs OR a
    block of the same name already standing — run 79's follow-up:
    swaps accumulated duplicate spans, shelter x3 in line-3's
    champion; insertion is idempotent-refusing, and remove takes ALL
    spans of a name, so compose ops can never stack)."""
    if not needs_met(genome_src, block):
        raise ValueError(f"insert {block.name}: unmet needs {block.needs}")
    if any(b.name == block.name for b in parse_blocks(genome_src)):
        raise ValueError(f"insert {block.name}: block already present")
    lines = genome_src.splitlines()
    rank = SLOT_RANK[block.slot]
    insert_at = len(lines)               # default: the end
    for i, line in enumerate(lines):
        r = _slot_rank_of_section(lines, i)
        if r is not None and r > rank:
            insert_at = i
            break
        m = _START.match(line)
        if m:
            meta = _parse_meta(m.group("meta"), m.group("name"),
                               m.group("sec")) or {
                "provenance": "", "slot": "survival",
                "needs": [], "provides": []}
            if SLOT_RANK[meta["slot"]] > rank:
                insert_at = i
                break
    # the body: the block's source WITHOUT its own banners (library
    # files and parsed spans both carry them; banner-inside-banner
    # double-parses — the run-79 duplicate-span bug's other half)
    body_lines = _strip_banners(block.source, block.name)
    body = [block.banner()] + body_lines + [
        f"-- ===== END {block.name} ====="]
    return "\n".join(lines[:insert_at] + body + lines[insert_at:]) + "\n"


def _strip_banners(source: str, name: str) -> list[str]:
    """A block's source minus its own START/END banners."""
    lines = source.splitlines()
    if lines:
        m = _START.match(lines[0])
        if m and m.group("name") == name:
            lines = lines[1:]
    if lines:
        e = _END.match(lines[-1])
        if e and e.group("name") == name:
            lines = lines[:-1]
    return lines


def remove_block(genome_src: str, name: str) -> str:
    """Delete a block — ALL spans of it (run 79's duplicate-span
    follow-up: swaps could leave an older span standing beside the
    new one, bloating the genome). Deletion stays a first-class
    operator (dead weight is a fitness cost — run-73's 48K scripts)."""
    lines = genome_src.splitlines()
    out, i, removed = [], 0, 0
    while i < len(lines):
        m = _START.match(lines[i])
        if m and m.group("name") == name:
            removed += 1
            i = _span_of(lines, i, name)   # ALL spans: loop revisits
            continue
        out.append(lines[i])
        i += 1
    if not removed:
        raise ValueError(f"remove: no block {name}")
    return "\n".join(out) + "\n"


def _span_of(lines: list[str], i: int, name: str) -> int:
    """The line AFTER the span starting at lines[i] (a block banner at
    i): its END, the next banner, the next section, or EOF."""
    for k in range(i + 1, len(lines)):
        e = _END.match(lines[k])
        if e and e.group("name") == name:
            return k + 1
        if _START.match(lines[k]) or _section_banner_rank(lines[k]) is not None:
            return k
    return len(lines)


def dedupe_blocks(genome_src: str) -> str:
    """Keep only the LAST span of each duplicated block name (the most
    recent allele), dropping older duplicates — the cleanup for genomes
    the pre-fix compose ops stacked (run 79's champions: shelter x3,
    spring_restart x2). Round-trips clean genomes unchanged."""
    lines = genome_src.splitlines()
    spans: dict[str, list[int]] = {}
    for b in parse_blocks(genome_src):
        spans.setdefault(b.name, []).append(b.start_line - 1)
    drop: set[int] = set()
    for name, starts in spans.items():
        drop.update(starts[:-1])            # all but the last
    if not drop:
        return genome_src
    out, i = [], 0
    while i < len(lines):
        if i in drop:
            m = _START.match(lines[i])
            i = _span_of(lines, i, m.group("name"))
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) + "\n"


def swap_block(genome_src: str, block: Block) -> str:
    """Replace a block with a library ALLELE of the same locus (same
    structure family, different params/variant) — how param divergence
    gets reused without text merging. (The pre-fix bug lived here: the
    removal's result was discarded and the allele inserted beside the
    span it was supposed to replace — run 79's duplicate spans.)"""
    stripped = remove_block(genome_src, block.name)  # raises if absent
    return insert_block(stripped, block)
