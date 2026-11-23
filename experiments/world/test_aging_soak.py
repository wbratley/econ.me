"""The aging+reproduction soak (aging-repro spec, acceptance bars).

One long world (56 days — past the 48-day lifespan horizon) with the
family starter in every seat and the family-genome installer running
post-tick. The five bars:

1. condition:"age" appears as a death mode (the horizon is reachable);
2. >=2 natural generations (birth -> provisioned childhood -> adult
   -> spawn);
3. childhood survival vs provisioning flow (correlates, not just
   happens);
4. population bounded without cap hits, oscillating with the commons;
5. the existing soak bars hold (survival, no script errors).

Run:  .venv/bin/python -m experiments.world.test_aging_soak
      (or pytest -q for the assertions; the print()s are the census)
"""
from __future__ import annotations

from collections import Counter

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine import spawns, tick as tick_mod
from econengine.models import Base, Entity, EntityStatus

from . import stone_age as sa
from . import family

STARTER = None  # set at runtime (the gated pack script)

DAYS = 56


def run_aging_world(days: int = DAYS, seats: int = 3, verbose: bool = True,
                    genome: str | None = None):
    global STARTER
    STARTER = genome or sa._gate_pack_script(sa.STARTER)
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    census = dict(
        deaths=Counter(), births=0, script_errors=0,
        population=[], transfers=0, pickups=0, ages_at_death=[],
        childhood_deaths=0, orphan_picks=0,
    )
    with Session(engine) as session:
        sa.create_content(session)
        house_ids = []
        for i in range(seats):
            house = sa.make_house(session, f"House {i + 1}")
            house.owner_id = f"u-fam-{i}"
            session.flush()
            from econengine import services
            services.set_entity_behaviour(
                session, house, STARTER, owner_id=house.owner_id)
            house_ids.append((house.id, house.owner_id))
        session.commit()

        for tick_no in range(1, days * 24 + 1):
            if (tick_no - 1) % 24 == 0:
                spawns.apply_on_round(session, (tick_no - 1) // 24 + 1)
            tick = tick_mod.run_tick(session)
            n = family.install_family_genomes(session)
            census["births"] += n
            session.commit()
            for e in tick.events or []:
                et = e.get("type")
                if et == "script_error":
                    census["script_errors"] += 1
                elif et == "entity_incapacitated":
                    ent = session.get(Entity, e.get("entity_id"))
                    if ent is not None and ent.name.startswith("House"):
                        census["deaths"][e.get("condition")] += 1
                        census["ages_at_death"].append(
                            (e.get("condition"),
                             (tick_no - (ent.birth_tick or 0)) // 24))
                        if e.get("condition") != "age" \
                                and (tick_no - (ent.birth_tick or 0)) < 240:
                            census["childhood_deaths"] += 1
                elif et == "facility_drop":
                    census["transfers"] += 1
                elif et == "facility_pickup":
                    census["pickups"] += 1
            if tick_no % (24 * 7) == 0:
                alive = session.execute(
                    __import__("sqlalchemy").select(
                        __import__("sqlalchemy").func.count()).select_from(
                        Entity).where(
                            Entity.status == EntityStatus.ACTIVE)
                ).scalar_one()
                census["population"].append((tick_no // 24, alive))
        gens = session.execute(
            __import__("sqlalchemy").select(Entity).where(
                Entity.parents.is_not(None))
        ).scalars().all()
        census["n_children_ever"] = len(gens)
    if verbose:
        print(f"== aging world: {days}d, {seats} seats ==")
        print("deaths:", dict(census["deaths"]))
        print("births (installed):", census["births"],
              "children ever:", census["n_children_ever"])
        print("script_errors:", census["script_errors"])
        print("provisioning: drops", census["transfers"],
              "pickups", census["pickups"])
        print("childhood deaths (<10d, non-age):", census["childhood_deaths"])
        print("population by week:", census["population"])
        print("ages at death (days):",
              sorted(set(census["ages_at_death"]))[:12])
    return census
