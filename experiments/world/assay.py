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

#: Tier two: the winter window. At the pack's 6-day seasons winter is
#: days 19-24; a days>=19 assay enters it and every seat's winter is
#: read separately (alive ticks in-window, deaths in-window) — the
#: curriculum's second tier, grown exactly when tier one saturates
#: (run 75: every candidate maxed alive-days from generation 1 on).
WINTER_START_DAY = 19

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
    winter_alive_ticks: int = 0   # tier two: alive inside days 19..end
    winter_death: str | None = None
    # the behavioral fingerprint (the diversity instrument): what the
    # genome DOES, measured — process mix by recipe, market use,
    # movement. Text similarity and behavioral similarity are different
    # axes (behavior can diverge while text stays similar and vice
    # versa); the pool's radiation digest reads both.
    processes: dict = field(default_factory=dict)
    orders: int = 0
    trades: int = 0
    travels: int = 0


@dataclass
class AssayResult:
    """One genome in one world: the seat rows plus the rollup."""
    genome_sha: str
    days: int
    seats: list[SeatRow] = field(default_factory=list)
    ended_tick: int = 0          # the last tick run: the window, or the
                                 # tick the last counting seat died (early
                                 # termination — fitness is survival time,
                                 # a world with nobody left to measure
                                 # stops ticking)

    @property
    def mean_alive_days(self) -> float:
        if not self.seats:
            return 0.0
        return sum(s.alive_ticks for s in self.seats) / (len(self.seats) * 24.0)

    @property
    def total_errors(self) -> int:
        return sum(s.script_errors for s in self.seats)


def run_winter_birth(genome_src: str, *, endowment: dict | None = None,
                     at_day: int = 22, days_after: int = 6) -> AssayResult:
    """The winter-birth assay (the deconfound instrument, run-80 read
    #3): the world runs WITHOUT candidate houses until deep winter,
    then the candidate is BORN (make_house + genome + optional
    endowment) — the live succession's exact shape (founder seat +
    escrow delivery). Measures how many days a genome + capital shape
    survives a winter birth; the arms isolate the lever (naked seat /
    +wood buffer / +axe / +axe+wood). Zero tokens, trainer-side."""
    from decimal import Decimal as _D
    from .stone_age import make_house
    sha = _sha16(genome_src)
    result = AssayResult(genome_sha=sha, days=days_after)
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        create_content(session)
        birth = (at_day - 1) * 24 + 1
        seats = []
        for tick_no in range(1, (at_day - 1) * 24 + days_after * 24 + 1):
            if (tick_no - 1) % 24 == 0:
                spawns.apply_on_round(session, (tick_no - 1) // 24 + 1)
            if tick_no == birth:
                for i in range(SEATS):
                    house = make_house(session, f"House {i + 1}")
                    house.owner_id = "u-assay"
                    session.flush()
                    row = SeatRow(seat=i)
                    try:
                        services.set_entity_behaviour(
                            session, house, genome_src, owner_id="u-assay")
                    except Exception as exc:
                        row.installed = False
                        row.refusal = str(exc)[:300]
                    from econengine.markets import adjust_holding
                    for sym, qty in (endowment or {}).items():
                        adjust_holding(session, house, sym, _D(str(qty)))
                    seats.append((house.id, row))
                    result.seats.append(row)
                session.commit()
            tick = tick_mod.run_tick(session)
            session.commit()
            born = tick_no >= birth
            counting = False
            for hid, row in seats:
                alive = (row.installed
                         and session.get(Entity, hid).status == EntityStatus.ACTIVE)
                if born and alive:
                    counting = True
                    row.alive_ticks += 1
                if born and not alive and row.death_tick is None:
                    for e in tick.events or []:
                        if (e.get("entity_id") == hid
                                and e.get("type") == "entity_incapacitated"):
                            row.death_tick = tick_no
                            row.death_condition = e.get("condition")
            if born and not counting:
                result.ended_tick = tick_no
                break
    return result


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
        winter_lo = (WINTER_START_DAY - 1) * 24 + 1
        ended = 0
        for tick_no in range(1, days * 24 + 1):
            if (tick_no - 1) % 24 == 0:
                spawns.apply_on_round(session, (tick_no - 1) // 24 + 1)
            tick = tick_mod.run_tick(session)
            session.commit()
            ended = tick_no
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
                    if tick_no >= winter_lo:
                        row.winter_death = event.get("condition")
                elif et == "process_completed":
                    code = str(event.get("recipe") or "?")
                    row.processes[code] = row.processes.get(code, 0) + 1
                    if event.get("facility") == "SHELTER" \
                            and tick_no <= 5 * 24:
                        row.shelter_by_d5 = True
                elif et == "trade":
                    row.trades += 1
                    if event.get("symbol") == "WATERSKIN" \
                            and tick_no <= 3 * 24:
                        row.skin_by_d3 = True
                elif et == "place_order":
                    row.orders += 1
                elif et == "travel":
                    row.travels += 1
            in_winter = tick_no >= winter_lo
            counting_alive = False
            for hid, row in by_seat.items():
                alive = (row.installed
                         and session.get(Entity, hid).status == EntityStatus.ACTIVE)
                if alive:
                    counting_alive = True
                    row.alive_ticks += 1
                    if in_winter:
                        row.winter_alive_ticks += 1
                # a refused install scores zero: the founder package's
                # natural survival is the seat's, not the genome's
            if not counting_alive:
                # early termination: every counting seat is dead, and
                # fitness is survival time — the world has nothing left
                # to measure. (Order-of-operations note: this runs BEFORE
                # multiprocess scaling because it also shrinks the
                # variance of per-world cost, which makes worker
                # balancing trivial.)
                break
    result.ended_tick = ended
    return result


def assay_genome(genome_src: str, replicates: int = 3,
                 days: int = ASSAY_DAYS) -> list[AssayResult]:
    """The genome's assay: ``replicates`` independent worlds (fresh
    uuids = independent rolls). The trainer's fitness read is the
    mean over these; the variance is the lottery, reported not hidden."""
    return [run_assay(genome_src, days=days) for _ in range(replicates)]


def _assay_job(job: tuple) -> AssayResult:
    """A picklable one-world job for the process pool: (src, replicate,
    days). Each world is its own in-memory SQLite — no server, no shared
    state, near-linear to core count."""
    src, _replicate, days = job
    return run_assay(src, days=days)


def assay_many(jobs: list[tuple], workers: int = 0) -> list[AssayResult]:
    """Run (src, replicate, days) jobs across a process pool, preserving
    job order in the results (callers zip candidates to their
    replicates deterministically). workers=0 -> sequential (debug)."""
    if workers <= 1 or len(jobs) <= 1:
        return [_assay_job(j) for j in jobs]
    import concurrent.futures as cf
    with cf.ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_assay_job, jobs))


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
    # the fingerprint's process mix, merged across seats and normalized
    # to shares (the pool digest's behavior axis; fire-hours approximated
    # by fire-recipe share — the mix carries what the genome DOES)
    mix: dict[str, int] = {}
    for s in seats:
        for code, n in s.processes.items():
            mix[code] = mix.get(code, 0) + n
    mix_total = sum(mix.values()) or 1
    out = {
        "mean_alive_days": sum(s.alive_ticks for s in seats)
        / (len(seats) * 24.0),
        "installed": sum(1 for s in seats if s.installed),
        "deaths": dict(sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))),
        "script_errors": sum(s.script_errors for s in seats),
        "shelter": sum(1 for s in seats if s.shelter_by_d5),
        "skin": sum(1 for s in seats if s.skin_by_d3),
        "trades": sum(s.trades for s in seats),
        "orders": sum(s.orders for s in seats),
        "travels": sum(s.travels for s in seats),
        "behavior": {k: round(v / mix_total, 4)
                     for k, v in sorted(mix.items())},
    }
    # tier two, when the window reached winter: the winter read rides
    # along (selection stays the full-window metric; the winter read is
    # the report's frontier signal — and the next tier's selection once
    # the long window saturates in turn)
    if any(s.winter_alive_ticks or s.winter_death for s in seats) \
            or results and results[0].days >= WINTER_START_DAY:
        winter_days = max(0, results[0].days - WINTER_START_DAY + 1) \
            if results else 0
        wdomains: dict[str, int] = {}
        for s in seats:
            if s.winter_death:
                wdomains[s.winter_death] = wdomains.get(s.winter_death, 0) + 1
        out["winter"] = {
            "days": winter_days,
            "mean_alive_days": sum(s.winter_alive_ticks for s in seats)
            / (len(seats) * 24.0),
            "deaths": dict(sorted(wdomains.items(),
                                  key=lambda kv: (-kv[1], kv[0]))),
        }
    return out
