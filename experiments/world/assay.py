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

from sqlalchemy import create_engine, select
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


def _genome_has_block(src: str, name: str) -> bool:
    try:
        from experiments.agent.blocklib import parse_blocks
        return any(b.name == name for b in parse_blocks(src))
    except Exception:
        return False


def run_bequest(genome_src: str, *, days: int = 24,
                heir_days: int = 6) -> dict:
    """The bequest assay (tier 4, the fuel law's curriculum): fitness
    is what you LEAVE. Phase 1 runs the genome to its death under the
    pack's escrow policy — each seat its OWN owner, so estates stay
    separate. Phase 2 delivers each escrow to a fresh heir born at the
    first seat's death-season (the live succession's shape) and
    measures the heir's survival. Returns per-seat
    {gen1_days, heir_days, bequest_days} — the generational thesis's
    metric, assay-side, zero tokens."""
    from decimal import Decimal as _D
    from econengine.models import EstateEscrow
    from .stone_age import make_house
    from econengine.markets import adjust_holding

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    out = {"genome_sha": _sha16(genome_src), "seats": []}
    with Session(engine) as session:
        create_content(session)
        owners, rows = [], []
        for i in range(SEATS):
            house = make_house(session, f"House {i + 1}")
            house.owner_id = f"u-assay-{i}"    # separate escrows
            session.flush()
            ok = True
            try:
                services.set_entity_behaviour(
                    session, house, genome_src, owner_id=house.owner_id)
            except Exception:
                ok = False
            owners.append((house.id, house.owner_id, ok))
        session.commit()

        # phase 1 + 2 fused: per-line succession. The world ticks
        # once; the tick a gen-1 seat dies, its line's heir is BORN
        # (make_house + genome + escrow delivery — the live rule: each
        # heir at its own line's funeral, never overlapping a living
        # parent), and the heir's window closes heir_days after its
        # birth. Early exit: every line resolved (gen-1 dead AND its
        # heir dead), or the horizon.
        heirs: dict[str, dict] = {}
        flow = {owner: {"wood_in": _D("0"), "stokes": 0} for _h, owner, _ok in owners}

        def _birth_heir(owner, tick_no):
            heir = make_house(session, f"Heir {owner[-1]}")
            heir.owner_id = owner
            session.flush()
            installed = True
            try:
                services.set_entity_behaviour(
                    session, heir, genome_src, owner_id=owner)
            except Exception:
                installed = False
            summary = services.deliver_escrow(session, heir)
            session.commit()
            heirs[owner] = {"id": heir.id, "installed": installed,
                            "born": tick_no, "summary": summary,
                            "died": None}

        horizon = days * 24 + heir_days * 24
        last_tick = 0
        for tick_no in range(1, horizon + 1):
            if (tick_no - 1) % 24 == 0:
                spawns.apply_on_round(session, (tick_no - 1) // 24 + 1)
            tick = tick_mod.run_tick(session)
            session.commit()
            last_tick = tick_no
            for e in tick.events or []:
                if e.get("type") != "process_completed":
                    continue
                nm2 = db_name(session, e.get("entity_id")) if False else None
                for _h, owner, _ok in owners:
                    pass
            # wood flow: outputs into the estate's owners' hands
            for e in tick.events or []:
                if e.get("type") == "process_completed":
                    eid = e.get("entity_id")
                    owner_of = {h: o for h, o, _k in owners}.get(eid)
                    if owner_of is None:
                        continue
                    for sym, qty in (e.get("outputs") or {}).items():
                        if sym == "WOOD":
                            flow[owner_of]["wood_in"] += _D(str(qty))
                    if e.get("recipe") == "STOKE_FIRE":
                        flow[owner_of]["stokes"] += 1
                elif e.get("type") == "trade":
                    eid = e.get("entity_id")
                    owner_of = {h: o for h, o, _k in owners}.get(eid)
                    if owner_of is not None and e.get("symbol") == "WOOD" \
                            and e.get("side") == "buy":
                        flow[owner_of]["wood_in"] += _D(str(e.get("quantity", "0")))
            resolved = True
            for hid, owner, ok in owners:
                ent = session.get(Entity, hid)
                if ok and ent.status == EntityStatus.ACTIVE:
                    resolved = False            # a gen-1 still living
                elif ok and ent.incapacitated_tick == tick_no:
                    _birth_heir(owner, tick_no)  # the funeral birth
            for owner, row in heirs.items():
                if row["died"] is None:
                    ent2 = session.get(Entity, row["id"])
                    if row["installed"] and ent2.status \
                            == EntityStatus.ACTIVE \
                            and tick_no < row["born"] + heir_days * 24:
                        resolved = False        # an heir in its window
                    elif ent2.incapacitated_tick:
                        row["died"] = ent2.incapacitated_tick
            if resolved and len(heirs) == sum(
                    1 for _hid, _o, ok in owners if ok):
                break

        for hid, owner, ok in owners:
            ent1 = session.get(Entity, hid)
            g1 = (ent1.incapacitated_tick or min(last_tick, days * 24)) / 24 \
                if ok else 0.0
            row = heirs.get(owner)
            if row is None:
                out["seats"].append({
                    "gen1_days": round(g1, 2), "heir_days": None,
                    "bequest_days": None, "fuel_share": 0.0,
                    "stockpile": _genome_has_block(
                        genome_src, "winter.stockpile")})
                continue
            summary = row["summary"]
            goods = (summary or {}).get("goods_delivered") or {}
            fuel = _D(str(goods.get("WOOD", "0")))
            goods_total = sum((_D(str(v)) for v in goods.values()), _D("0"))
            if row["died"] is not None:
                h = (row["died"] - row["born"]) / 24
            else:
                h = (min(last_tick, row["born"] + heir_days * 24)
                     - row["born"]) / 24
            # the gather-vs-bank fork's diagnostic: wood IN (gathered,
            # chopped, bought), wood BURNED (stokes), wood BANKED (the
            # estate's WOOD at delivery). Flat fuel-share + a present
            # reserve = defaults too small (param patch); flat + no
            # reserve = no withholding rule (winter.stockpile.v2 needs
            # a bank line — structural).
            st_wd = _D("0.6") * flow[owner]["stokes"]  # ~0.6 wood/stoke
            out["seats"].append({
                "gen1_days": round(g1, 2), "heir_days": round(h, 2),
                "bequest_days": round(g1 + h, 2),
                "fuel_share": round(float(fuel / goods_total), 3)
                if goods_total > 0 else 0.0,
                "wood_in": round(float(flow[owner]["wood_in"]), 1),
                "stokes": flow[owner]["stokes"],
                "wood_burned_est": round(float(st_wd), 1),
                "wood_banked": round(float(fuel), 1),
                "stockpile": _genome_has_block(
                    genome_src, "winter.stockpile"),
            })
    return out


def run_family(genome_src: str, *, days: int = 40,
               seats: int = SEATS) -> dict:
    """The family tier (aging-repro spec, metric pre-registered and
    pinned): one genome, two lives at once. Each seat is a PARENT
    running the candidate genome with the family frame's spawn
    discipline; children born mid-world inherit it (family.
    install_family_genomes, the seam v1, post-tick) and run the same
    source age-branched. The lineage's fitness:

        family_days = (lineage alive at window end ? 10 : 0)
                      + sum(alive-days across members)

    Members = every entity under the seat's owner, all generations,
    in-window. Guards (journaled before the first family run): the
    parent floor (anti-r-selection: a parent that burns itself to
    breed is ineligible -- the trainer applies it), and the spam
    ratio (child_days : parent_days, the standing census watch).
    Returns per-seat rows + the rollup; zero tokens."""
    from . import family as _fam
    from econengine.models import (
        Account, Entity, EntityStatus, FacilityHolding, Holding, Parcel)
    sha = _sha16(genome_src)
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    out = {"genome_sha": sha, "days": days, "seats": []}
    with Session(engine) as session:
        create_content(session)
        parents = []
        for i in range(seats):
            house = make_house(session, f"House {i + 1}")
            house.owner_id = f"u-fam-{i}"
            session.flush()
            ok = True
            try:
                services.set_entity_behaviour(
                    session, house, genome_src, owner_id=house.owner_id)
            except Exception:
                ok = False
            parents.append((house.id, house.owner_id, ok))
        session.commit()

        member_birth = {}      # entity id -> birth tick (parents = 0)
        blocked = {"surplus": 0, "wood": 0}   # the spawn-gate row, live
        peak_spring = [dict(food=0.0, wood=0.0, coin=0.0, shelf=0.0,
                             gate_clear_ticks=0)
                       for _ in parents]      # distance-to-gate row
        from econengine.clock import season_of, season_day_of
        for tick_no in range(1, days * 24 + 1):
            if (tick_no - 1) % 24 == 0:
                spawns.apply_on_round(session, (tick_no - 1) // 24 + 1)
            tick = tick_mod.run_tick(session)
            _fam.install_family_genomes(session)
            session.commit()
            for e in tick.events or []:
                if e.get("type") == "spawn_entity":
                    member_birth[e.get("child_id")] = tick_no
                elif e.get("type") == "say" \
                        and "spawn blocked" in str(e.get("text", "")):
                    key = ("surplus" if "surplus" in e.get("text", "")
                           else "wood")
                    blocked[key] += 1
            # the distance-to-gate instrument (second-cycle fertile
            # window only: tick 577+ = day 25+; spring-1 founders are
            # full-buffered and would dominate the peaks)
            season = season_of(tick_no, 6)
            fertile2 = tick_no >= 24 * 24 + 1 and (
                season == "SPRING"
                or (season == "SUMMER" and season_day_of(tick_no, 6) <= 3))
            if fertile2:
                for i, (pid, owner, ok) in enumerate(parents):
                    if not ok:
                        continue
                    ent_p = session.get(Entity, pid)
                    if ent_p is None \
                            or ent_p.status != EntityStatus.ACTIVE:
                        continue
                    hold_p = {r.symbol: float(r.quantity) for r in
                              session.execute(select(Holding).where(
                                  Holding.entity_id == pid)).scalars()}
                    food_p = (hold_p.get("BERRIES", 0)
                              + hold_p.get("APPLES", 0)
                              + hold_p.get("COOKED_MEAT", 0)
                              + hold_p.get("JERKY", 0))
                    wood_p = hold_p.get("WOOD", 0)
                    coin_p = sum(float(a.balance) for a in
                                 session.execute(select(Account).where(
                                     Account.entity_id == pid)).scalars())
                    shelf_p = 0.0
                    for parcel_p in session.execute(
                            select(Parcel).where(
                                Parcel.owner_id == pid)).scalars():
                        for fh in session.execute(
                                select(FacilityHolding).where(
                                    FacilityHolding.facility_id
                                    == parcel_p.id,
                                    FacilityHolding.owner_id == "*",
                                )).scalars():
                            if fh.symbol in ("JERKY", "COOKED_MEAT",
                                             "BERRIES", "APPLES", "WOOD"):
                                shelf_p += float(fh.quantity)
                    ps = peak_spring[i]
                    ps["food"] = max(ps["food"], food_p)
                    ps["wood"] = max(ps["wood"], wood_p)
                    ps["coin"] = max(ps["coin"], coin_p)
                    ps["shelf"] = max(ps["shelf"], shelf_p)
                    if (food_p + shelf_p >= 5 and wood_p >= 3
                            and hold_p.get("SATIETY", 0) > 3
                            and hold_p.get("WARMTH", 0) > 3):
                        ps["gate_clear_ticks"] += 1
            # the distance-to-gate read: at every tick of the FAMILY
            # window (the SECOND cycle's fertile season, ticks 577+ --
            # spring-1 founders are full-buffered and would dominate
            # the peaks), each seat's holdings vs the spawn gate's own
            # bars. Components (goods, coin, the family shelf) show
            # WHAT the lineage holds; gate_clear_ticks counts the
            # instants ALL the economic gates pass jointly (surplus 5
            # incl shelf, wood 3, satiety, warmth) -- the honest
            # distance to a birth.
            season = season_of(tick_no, 6)
            fertile = (tick_no >= 24 * 24 + 1) and (
                season == "SPRING"
                or (season == "SUMMER" and season_day_of(tick_no, 6) <= 3))
            if fertile:
                for i, (pid, owner, ok) in enumerate(parents):
                    if not ok:
                        continue
                    ent = session.get(Entity, pid)
                    if ent.status != EntityStatus.ACTIVE:
                        continue
                    hold = {r.symbol: float(r.quantity) for r in session.execute(
                        select(Holding).where(
                            Holding.entity_id == pid)).scalars()}
                    food = (hold.get("BERRIES", 0) + hold.get("APPLES", 0)
                            + hold.get("COOKED_MEAT", 0) + hold.get("JERKY", 0))
                    wood = hold.get("WOOD", 0)
                    coin = sum(float(a.balance) for a in session.execute(
                        select(Account).where(
                            Account.entity_id == pid)).scalars())
                    # the family shelf: the camp parcel's shared drop rows
                    shelf = 0.0
                    for parcel in session.execute(
                            select(Parcel).where(
                                Parcel.owner_id == pid)).scalars():
                        for fh in session.execute(
                                select(FacilityHolding).where(
                                    FacilityHolding.facility_id == parcel.id,
                                    FacilityHolding.owner_id == "*",
                                )).scalars():
                            if fh.symbol in ("JERKY", "COOKED_MEAT",
                                             "BERRIES", "APPLES", "WOOD"):
                                shelf += float(fh.quantity)
                    ps = peak_spring[i]
                    ps["food"] = max(ps["food"], food)
                    ps["wood"] = max(ps["wood"], wood)
                    ps["coin"] = max(ps["coin"], coin)
                    ps["shelf"] = max(ps["shelf"], shelf)
                    if (food + shelf >= 5 and wood >= 3
                            and hold.get("SATIETY", 0) > 3
                            and hold.get("WARMTH", 0) > 3):
                        ps["gate_clear_ticks"] = \
                            ps.get("gate_clear_ticks", 0) + 1
        # measure: alive-days per member + alive-at-end per lineage
        rows = []
        for pid, owner, ok in parents:
            parent_days = 0.0
            alive_end = False
            child_days = 0.0
            births = 0
            if ok:
                ent = session.get(Entity, pid)
                if ent.incapacitated_tick:
                    parent_days = ent.incapacitated_tick / 24
                else:
                    parent_days = days
                    alive_end = True
            members = session.execute(
                select(Entity).where(Entity.owner_id == owner)
            ).scalars().all()
            for m in members:
                if m.id == pid:
                    continue
                births += 1
                if m.status == EntityStatus.ACTIVE:
                    child_days += (days * 24 - member_birth.get(m.id, 0)) / 24
                    alive_end = True
                elif m.incapacitated_tick:
                    child_days += (m.incapacitated_tick
                                   - member_birth.get(m.id, 0)) / 24
            family_days = (10 if alive_end else 0) + parent_days + child_days
            rows.append({
                "parent_days": round(parent_days, 2),
                "child_days": round(child_days, 2),
                "births": births,
                "alive_at_end": alive_end,
                "family_days": round(family_days, 2),
                "spam_ratio": round(child_days / parent_days, 2)
                if parent_days > 0 and child_days > 0 else None,
                "fertile_reach": 1.0 if (ok and parent_days >= 25) else 0.0,
                "blocked_surplus": blocked["surplus"],
                "blocked_wood": blocked["wood"],
                "peak_spring": peak_spring[i],
            })
        out["seats"] = rows
    return out


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
    # the climb's standing watch: fertile-window reach -- the share of
    # seats alive into the second cycle's spring (tick 577+ = day 25,
    # the fixed season clock). The family's activation energy: report
    # per generation; reliable reach = bar-2's prerequisite met by
    # selection rather than hope.
    out = {
        "mean_alive_days": sum(s.alive_ticks for s in seats)
        / (len(seats) * 24.0),
        "fertile_reach": (sum(1 for s in seats
                              if s.alive_ticks >= 24 * 24 + 1)
                          / len(seats)) if seats else 0.0,
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
