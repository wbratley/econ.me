"""The fitness assay (G2b): one genome, measured, headless.

The spec amendment's core move: the soak IS a fitness harness. This
module turns it into the trainer's assay — the standardized conditions
every candidate genome is tested under, FROM SCRATCH:

  * a fresh in-memory world per replicate (fresh uuids = fresh event
    hashes = independent rolls; world-to-world variance IS the seed
    variance — the soak's "per-house day-commitment lottery");
  * every house seat in the world runs THE CANDIDATE (lineage
    isolation: a candidate is never contaminated by another
    candidate's market behavior — candidates MEET only in the
    competition layer, the continuous world);
  * the install is the REAL gate (``services.set_entity_behaviour``):
    a genome that cannot install scores zero with the refusal as its
    epitaph — selection starts at the lint gate, not after it;
  * no inheritance, no escrow (banned from the assay by the spec), no
    seat: the genome runs on its own, exactly as "tested from scratch
    each time" demands — a genome that only survives with help is a
    genome that gets selected against.

Ten days is the standardized window (the soak's): autumn fitness, the
curriculum's first tier. The winter tier is grown when champions start
maxing it — extending the window is a flag (``days=``), not a rewrite.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import services, spawns, tick as tick_mod
from econengine.models import Base, Entity
from econengine.models.entity import EntityStatus

from .stone_age import create_content, make_house
from . import stone_age

#: The standardized window (the soak's DAYS) — autumn fitness, tier one.
ASSAY_DAYS = 10

#: Seats per assay world — every one of them the candidate.
SEATS = 3


@dataclass
class SeatRow:
    """One seat's life under the assay."""
    seat: int
    installed: bool = True
    refusal: str = ""            # the lint gate's epitaph, when refused
    alive_ticks: int = 0
    death_tick: int | None = None
    death_condition: str | None = None
    shelter_by_d5: bool = False  # G0 adoption counters (the exits exist)
    skin_by_d3: bool = False
    script_errors: int = 0


@dataclass
class AssayResult:
    """One genome in one world: the seat rows plus the rollup."""
    genome_sha: str
    days: int
    seats: list[SeatRow] = field(default_factory=list)

    @property
    def mean_alive_days(self) -> float:
        if not self.seats:
            return 0.0
        return sum(s.alive_ticks for s in self.seats) / (len(self.seats) * 24.0)

    @property
    def total_errors(self) -> int:
        return sum(s.script_errors for s in self.seats)


def _sha16(source: str) -> str:
    import hashlib
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def run_assay(genome_src: str, days: int = ASSAY_DAYS) -> AssayResult:
    """One genome, one world, ``days`` days, every seat the candidate.

    The world is built exactly as the soak builds it (content pack,
    house seats, spawn cadence) with ONE difference: the behaviour
    installed on each seat is the candidate, through the real install
    gate. Refusals are scored, not raised — a genome that cannot
    install is a zero with a reason.
    """
    sha = _sha16(genome_src)
    result = AssayResult(genome_sha=sha, days=days)
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        create_content(session)
        seats = []
        for i in range(SEATS):
            house = make_house(session, f"House {i + 1}")
            house.owner_id = "u-assay"   # the harness owns its world
            session.flush()
            row = SeatRow(seat=i)
            try:
                services.set_entity_behaviour(
                    session, house, genome_src, owner_id="u-assay")
            except Exception as exc:            # the gate: lint, arity, smoke
                row.installed = False
                row.refusal = str(exc)[:300]
            seats.append((house.id, row))
            result.seats.append(row)
        session.commit()

        by_seat = {hid: row for hid, row in seats}
        for tick_no in range(1, days * 24 + 1):
            if (tick_no - 1) % 24 == 0:
                spawns.apply_on_round(session, (tick_no - 1) // 24 + 1)
            tick = tick_mod.run_tick(session)
            session.commit()
            for event in tick.events or []:
                hid = event.get("entity_id")
                if hid not in by_seat:
                    continue
                row = by_seat[hid]
                et = event.get("type")
                if et == "script_error":
                    row.script_errors += 1
                elif et == "entity_incapacitated" and row.death_tick is None:
                    row.death_tick = tick_no
                    row.death_condition = event.get("condition")
                elif et == "process_completed" and event.get("facility") == "SHELTER" \
                        and tick_no <= 5 * 24:
                    row.shelter_by_d5 = True
                elif et == "trade" and event.get("symbol") == "WATERSKIN" \
                        and tick_no <= 3 * 24:
                    row.skin_by_d3 = True
            for hid, row in by_seat.items():
                if (row.installed
                        and session.get(Entity, hid).status == EntityStatus.ACTIVE):
                    row.alive_ticks += 1
                # a refused install scores zero: the founder package's
                # natural survival is the seat's, not the genome's
    return result


def assay_genome(genome_src: str, replicates: int = 3,
                 days: int = ASSAY_DAYS) -> list[AssayResult]:
    """The genome's assay: ``replicates`` independent worlds (fresh
    uuids = independent rolls). The trainer's fitness read is the
    mean over these; the variance is the lottery, reported not hidden."""
    return [run_assay(genome_src, days=days) for _ in range(replicates)]


def fitness(results: list[AssayResult]) -> dict:
    """The selection read over a genome's replicates: mean alive days
    (the metric), the death-domain tally (what it dies of — the
    kill-chain memory), errors, and the adoption counters. Deterministic
    ordering: domains sorted by count then name."""
    seats = [s for r in results for s in r.seats]
    if not seats:
        return {"mean_alive_days": 0.0, "installed": 0,
                "deaths": {}, "script_errors": 0,
                "shelter": 0, "skin": 0}
    domains: dict[str, int] = {}
    for s in seats:
        if s.death_condition:
            domains[s.death_condition] = domains.get(s.death_condition, 0) + 1
    return {
        "mean_alive_days": sum(s.alive_ticks for s in seats)
        / (len(seats) * 24.0),
        "installed": sum(1 for s in seats if s.installed),
        "deaths": dict(sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))),
        "script_errors": sum(s.script_errors for s in seats),
        "shelter": sum(1 for s in seats if s.shelter_by_d5),
        "skin": sum(1 for s in seats if s.skin_by_d3),
    }
