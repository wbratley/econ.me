"""G3: automatic succession — the seat outlives the entity.

The generational game's core change (generational spec, step G3): the SEAT
is the immortal lineage, the entity is the mortal body it wears. When a
seat's entity dies, the harness does not park the seat (run 74's manual
loop: the operator ran ``succession.py`` by hand); it succeeds
AUTOMATICALLY, through the platform's own front doors:

  1. GENOME (the code channel, conservative): the lineage's last ACCEPTED
     script version MINUS every version that landed within the kill
     window (W ticks of the death — the rollback rule). The source is
     recovered from the ``scripts`` table by sha against the journal's
     accepted trail. Computed from the journal + the dead body's own
     script rows — the house's OWN record, parity-safe.
  2. DEATH CERTIFICATE (the memory channel): the structured autopsy
     (``death-cert-<slug>.json``, written by the census instruments, G1)
     rides into the successor's genome as a leading comment block the
     seat reads every round. Read if on file; absent, the genome is
     inherited cold (the code channel alone).
  3. JOIN: ``POST /join`` as the dynasty's own user — the successor
     founder takes the standard founder package (a fresh purse: assets
     die with the body, knowledge survives in the lineage).
  4. INSTALL: ``POST /entities/{id}/behaviour`` with the certificate +
     genome source (submit-time lint applies — a broken inherited genome
     is refused, not installed).
  5. REBIND: the seat's ``entity_id`` moves to the successor; the lineage's
     model authors it from the inherited genome, the journal (keyed by the
     dynasty, not the body) carries on.

The engine gains NOTHING: this is harness policy over existing
primitives (join + set_behaviour + the script lineage that already
exists). The cooldown is the price of death — the successor joins only
after N rounds have run without the house, so death still selects.

Bit-identical to the G2 manual operator (``~/econ-runs/succession.py``):
same genome algorithm, same certificate preamble, same front doors —
only the trigger changes (a dead seat, not an operator).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.request
from pathlib import Path
from typing import Callable

#: The rollback window W (ticks): a mutation that landed within W ticks
#: of the death is the prime suspect for it and is NOT inherited. 48
#: ticks = 2 days at K=24 — the census instruments' default (G1).
KILL_WINDOW_TICKS = 48

#: The succession cost: rounds the successor waits before joining. Death
#: sets the house back N rounds (the world runs without it).
DEFAULT_COOLDOWN_ROUNDS = 2

#: The season gate: successors are BORN in these seasons (uppercase
#: clock.SEASONS names). The trainer's fitness assays all start at tick
#: 1 -- spring day 1 -- so a continuous-world birth in any other season
#: is an unassayable handicap (run 74: heirs born into deep winter died
#: of the birth, not the genome). Timing is strategy, not mechanism:
#: the seat pays the wait, the world never fakes the season. An empty
#: gate ("any") births as G3 did, cooldown alone.
DEFAULT_SEASON_GATE = ("SPRING",)

#: Founder numerals by generation (join names them "Founder"; the
#: lineage's numeral is the bookkeeping).
_NUMERALS = ("II", "III", "IV", "V", "VI", "VII", "VIII")


class SuccessionError(Exception):
    """A succession the front doors refused (join 4xx, install lint
    refusal, no script to inherit). The seat parks on this — a broken
    lineage must not silently inherit a broken body."""


def _sha16(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def _slug(name: str) -> str:
    """A dynasty name as a filename shard (mirrors nim_run.slug):
    `House Ivar` -> `house-ivar`. The journal and the certificate are
    keyed by the DYNASTY, so the lineage's record survives its bodies."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------------------------------------------------------------------------
# The genome: the conservative code channel
# ---------------------------------------------------------------------------

def compute_genome(db_path, journal_path, dead_entity_id: str,
                   death_tick: int, ticks_per_round: int = 24,
                   kill_window: int = KILL_WINDOW_TICKS) -> dict:
    """The lineage's inherited genome, straight off the dead body's own
    record: its journal's accepted (round, sha) trail, the source
    recovered from the ``scripts`` table by sha, and the rollback rule —
    inherit the last accepted version MINUS every version whose install
    round falls inside the kill window ``[death - W, death]``.

    Returns ``{genome_src, genome_sha, rolled_back, n_versions,
    death_tick}``. Raises SuccessionError when there is no script to
    inherit (a body that never had a behaviour — the genome is the whole
    point, so this is a hard failure, not a cold start).
    """
    db = sqlite3.connect(str(db_path))
    try:
        versions = []
        jp = Path(journal_path)
        if jp.exists():
            for line in jp.read_text().splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("accepted") and r.get("source_sha"):
                    versions.append((r.get("round"),
                                     str(r["source_sha"])[:16]))
        # dedupe consecutive carries (KEEP rounds repeat the last sha)
        dedup, last = [], None
        for rnd, sha in versions:
            if sha != last:
                dedup.append((rnd, sha))
                last = sha

        sources = {}
        for (src,) in db.execute(
                "SELECT source FROM scripts WHERE entity_id=? "
                "ORDER BY created_at", (dead_entity_id,)):
            sources[_sha16(src)] = src

        lo = death_tick - kill_window
        keep = [v for v in dedup
                if (v[0] - 1) * ticks_per_round + 1 < lo] or dedup[:1]
        genome_sha = keep[-1][1] if keep else None
        genome_src = sources.get(genome_sha)
        if genome_src is None:            # sha trail broken: the last row
            rows = db.execute(
                "SELECT source FROM scripts WHERE entity_id=? "
                "ORDER BY created_at", (dead_entity_id,)).fetchall()
            if not rows:
                raise SuccessionError(
                    f"no script to inherit for entity {dead_entity_id}")
            genome_src = rows[-1][0]
            genome_sha = _sha16(genome_src)
        rolled = len(dedup) - len(keep)
    finally:
        db.close()
    return dict(genome_src=genome_src, genome_sha=genome_sha,
                rolled_back=rolled, n_versions=len(dedup),
                death_tick=death_tick)


# ---------------------------------------------------------------------------
# The certificate: the memory channel
# ---------------------------------------------------------------------------

def certificate_preamble(cert: dict | None, genome: dict) -> str:
    """The successor's inheritance preamble: the parent's death, the
    vitals and the shelf, as Lua comments riding every prompt (the seat
    sees its own source every round). Parity-safe: it is the house's
    OWN death + public prices. ``cert`` None -> inherited cold."""
    lines = ["-- ================== INHERITED MEMORY =================="]
    if cert:
        d = cert["death"]
        lines += [
            f"-- Your parent ({cert['house']}, {cert.get('model') or '?'}) "
            f"died on day {d['day']} hour {d['hour']} of {d['condition']}"
            f" @{d['quantity']} -- domain: {d['domain']}.",
            "-- The last rounds before the end (conditions, purse):",
        ]
        for v in cert.get("vitals", [])[-3:]:
            conds = ", ".join(
                f"{k}={val}" for k, val in v["conditions"].items()
                if str(val) not in ("0", "0.0000")) or "all clear"
            lines.append(f"--   round {v['round']}: {conds}; "
                         f"coin {v['coin']}, wood {v['wood']}, "
                         f"jerky {v['jerky']}")
        shelf = cert.get("shelf") or {}
        quoted = ", ".join(
            f"{sym} ask {row.get('ask')}" for sym, row in shelf.items()
            if row.get("ask")) or "shelf bare"
        lines.append(f"-- The post at the end: {quoted}.")
        lines += [
            "-- The kill-chain rule already applied: the last"
            f" {genome['rolled_back']} edit(s) your parent landed before"
            " dying were",
            "-- NOT inherited (they are the prime suspects). What follows"
            " is the genome that",
            "-- kept the line alive longest. Mutate carefully; die"
            " differently.",
        ]
    else:
        lines.append("-- (no certificate on file: inherit the genome cold)")
    lines.append("-- ======================================================")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The Succession: the automatic operator
# ---------------------------------------------------------------------------

def _urlopen_post(base: str) -> Callable[[str, str, dict | None],
                                          tuple[int, dict]]:
    """The production front-door client: one Bearer POST, the status and
    the decoded body (an error body decoded too — the refusal is the
    signal, not an exception)."""

    def post(path: str, token: str, body: dict | None = None
             ) -> tuple[int, dict]:
        req = urllib.request.Request(
            base.rstrip("/") + path, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    return post


class Succession:
    """The automatic succession for one run's world: genome + certificate
    + join + install + name, through the platform's front doors. Built
    by the harness (nim_run) with the run's server base, world.db path,
    and out dir; called by a seat whose body just died.

    `post` is injectable (tests drive it with a TestClient; production
    is the urllib client). The genome and the name read/write the world
    db DIRECTLY — the harness owns the world it built, and the G2 manual
    operator did exactly this; the join and the install are the REST
    front doors (the engine's own primitives).
    """

    def __init__(self, base: str, db_path, out_dir,
                 ticks_per_round: int = 24,
                 kill_window: int = KILL_WINDOW_TICKS,
                 cooldown_rounds: int = DEFAULT_COOLDOWN_ROUNDS,
                 season_gate=DEFAULT_SEASON_GATE,
                 post: Callable[[str, str, dict | None],
                                tuple[int, dict]] | None = None):
        self.base = base
        self.db_path = Path(db_path)
        self.out_dir = Path(out_dir)
        self.ticks_per_round = ticks_per_round
        self.kill_window = kill_window
        self.cooldown_rounds = cooldown_rounds
        self.season_gate = tuple(
            str(x).upper() for x in (season_gate or ()))
        self.post = post or _urlopen_post(base)

    # -- the birth season ----------------------------------------------

    def current_season(self) -> str | None:
        """The world's season now, read straight off the world db (the
        harness owns the world it built): the latest tick against the
        world's own `clock.season_days` (votable data, engine default
        when unset). None when no ticks have run (the world is new --
        tick 1 is spring day 1, so callers treat None as gate-open)."""
        from econengine.clock import season_of, SEASON_DAYS
        db = sqlite3.connect(str(self.db_path))
        try:
            tick = db.execute(
                "SELECT MAX(number) FROM ticks").fetchone()[0]
            row = db.execute(
                "SELECT value FROM world_settings "
                "WHERE key='clock.season_days'").fetchone()
        finally:
            db.close()
        if not tick:
            return None
        days = SEASON_DAYS
        if row and isinstance(row[0], (int, float)) and int(row[0]) >= 1:
            days = int(row[0])
        return season_of(int(tick), days)

    def birth_ok(self) -> bool:
        """May a successor be born now? The gate is open when the world
        is unstarted (nothing to time against) or the season is gated
        in."""
        if not self.season_gate:
            return True
        season = self.current_season()
        return season is None or season in self.season_gate

    # -- the lineage's record ------------------------------------------

    def journal_path(self, house: str) -> Path:
        return self.out_dir / f"journal-{_slug(house)}.jsonl"

    def certificate_path(self, house: str) -> Path:
        return self.out_dir / f"death-cert-{_slug(house)}.json"

    def read_certificate(self, house: str) -> dict | None:
        p = self.certificate_path(house)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            return None          # a torn cert inherits cold, not broken

    def _body_status(self, entity_id: str) -> tuple[str | None, int | None]:
        db = sqlite3.connect(str(self.db_path))
        try:
            row = db.execute(
                "SELECT status, incapacitated_tick FROM entities "
                "WHERE id=?", (entity_id,)).fetchone()
        finally:
            db.close()
        if not row:
            return None, None
        return row[0], row[1]

    def _next_numeral(self, user_id: str) -> str:
        """The successor's numeral, by how many bodies the user owns
        BEFORE this one is born: the dead soma stay in the table, so the
        count is the generation (one corpse -> the line's \"II\"). Must
        be called before the join, or the successor counts itself."""
        db = sqlite3.connect(str(self.db_path))
        try:
            n = db.execute(
                "SELECT COUNT(*) FROM entities WHERE owner_id=?",
                (user_id,)).fetchone()[0]
        finally:
            db.close()
        if n - 1 < len(_NUMERALS):
            return _NUMERALS[max(0, n - 1)]
        return str(n)

    # -- the succession ------------------------------------------------

    def succeed(self, d) -> str:
        """Succeed ``d`` (a Dynasty whose ``entity_id`` is the DEAD body):
        genome + certificate -> join -> install -> name. Returns the
        successor's entity id (the caller rebinding ``d.entity_id`` and
        its loop's ``entity_id`` to it). Raises SuccessionError when the
        front doors refuse."""
        status, death_tick = self._body_status(d.entity_id)
        if status is None:
            raise SuccessionError(
                f"no entity {d.entity_id} for {d.name} to succeed")
        if status == "ACTIVE":
            return d.entity_id      # it's not dead; nothing to do
        if death_tick is None:
            death_tick = 0          # a non-conditional death: no window

        genome = compute_genome(
            self.db_path, self.journal_path(d.name), d.entity_id,
            death_tick, ticks_per_round=self.ticks_per_round,
            kill_window=self.kill_window)
        cert = self.read_certificate(d.name)
        source = (certificate_preamble(cert, genome) + "\n\n"
                  + genome["genome_src"])

        # the numeral is the generation: how many bodies the user owns
        # BEFORE this successor is born (the corpses stay in the table)
        # — computed before the join, or the successor counts itself
        numeral = self._next_numeral(d.user_id)

        code, joined = self.post("/join", d.token)
        if code != 201:
            raise SuccessionError(
                f"join refused ({code}): {str(joined)[:200]}")
        succ_id = (joined.get("entity") or {}).get("id") \
            or joined.get("entity_id")
        if not succ_id:
            raise SuccessionError(f"join returned no entity: {joined}")
        succ_name = f"{d.name} {numeral}"

        db = sqlite3.connect(str(self.db_path))
        try:
            db.execute("UPDATE entities SET name=? WHERE id=?",
                       (succ_name, succ_id))
            db.commit()
        finally:
            db.close()

        code, res = self.post(
            f"/entities/{succ_id}/behaviour", d.token,
            {"source": source,
             "description": (f"G3 succession: {d.name} -> {succ_name} "
                             f"(genome {genome['genome_sha']}, "
                             f"rolled {genome['rolled_back']})")})
        if code != 201:
            raise SuccessionError(
                f"install refused ({code}): {str(res)[:200]}")
        return succ_id
