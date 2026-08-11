"""Run harness for the population demo (Step 6c proving experiment).

Builds the founding world, loops ``run_tick()``, and prints a per-tick
report showing a population grow to its votable cap and stop -- with an
illicit birth refused every tick by the birth law -- driven entirely by
``spawn_entity`` and the 6c queries, with no engine change anywhere.

Usage:
    .venv/bin/python -m experiments.population.run            # 6 ticks
    .venv/bin/python -m experiments.population.run --ticks 9
"""

import argparse
import json
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine.models import Account, Base, Entity, EntityStatus, Tick
from econengine.scripting import build_queries
from econengine.tick import run_tick

from .scenario import (
    ENDOWMENT, POPULATION_CAP, TREASURY_ENDOWMENT, build_economy,
)

BORN = "born"        # the valid Adam x Eve birth was admitted
CAPPED = "capped"    # the valid birth was vetoed by the population cap
UNWED = "unwed"      # the illicit birth was vetoed by the birth law


def _spawn_by_parents(events, wanted: frozenset):
    """Find the spawn_entity event whose declared parents match `wanted`
    (as a set). Robust to ordering -- we identify births by lineage, not by
    the generated name."""
    for e in events:
        if e.get("type") != "spawn_entity":
            continue
        try:
            have = frozenset(json.loads(e.get("params", {}).get("parents", "[]")))
        except (TypeError, ValueError):
            continue
        if have == wanted:
            return e
    return None


def _birth_label(event) -> str:
    if event is None:
        return "-"
    if event.get("status") == "applied":
        return BORN
    reason = event.get("reason", "")
    # Map the (verbose) veto reason to a compact, semantic column label.
    if "population cap" in reason:
        return CAPPED
    if "married" in reason:
        return UNWED
    if "two parents" in reason:
        return "1-parent"
    if "male and one female" in reason:
        return "same-sex"
    if "of age" in reason:
        return "underage"
    return "veto"


def simulate(session: Session, world, ticks: int) -> list[dict]:
    """Run ``ticks`` ticks and return one record per tick.

    Each record carries the observables the report and tests both read: the
    active population after the tick, and the outcome of the two spawn
    attempts (valid Adam x Eve; illicit Adam x Lilith). Pure data -- no
    printing -- so the smoke test asserts on structure, not stdout.
    """
    adam = world.founders["Adam"].id
    eve = world.founders["Eve"].id
    lilith = world.founders["Lilith"].id
    valid_key = frozenset({adam, eve})
    illicit_key = frozenset({adam, lilith})

    records = []
    for n in range(1, ticks + 1):
        run_tick(session)
        session.commit()
        tick = session.query(Tick).filter_by(number=n).one()
        events = tick.events or []
        birth = _spawn_by_parents(events, valid_key)
        illicit = _spawn_by_parents(events, illicit_key)
        population = session.execute(
            select(func.count()).select_from(Entity)
            .where(Entity.status == EntityStatus.ACTIVE)
        ).scalar_one()
        records.append({
            "tick": n,
            "population": population,
            "birth_status": birth.get("status") if birth else None,
            "birth_child_id": birth.get("child_id") if birth else None,
            "birth_name": birth.get("params", {}).get("name") if birth else None,
            "birth_reason": birth.get("reason") if birth else None,
            "birth_label": _birth_label(birth),
            "illicit_status": illicit.get("status") if illicit else None,
            "illicit_reason": illicit.get("reason") if illicit else None,
            "illicit_label": _birth_label(illicit),
        })
    return records


def run(ticks: int = 6) -> None:
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        world = build_economy(session)
        session.commit()

        print()
        print("Step 6c -- spawn_entity, proven (population grows to its cap)")
        print("rules:  BIRTH-LAW (validator)  POPULATION-CAP (validator)  "
              "ENDOWMENT (hook)")
        print("cast:   Government(midwife)  Adam(m, married Eve)  "
              "Eve(f, married Adam)  Lilith(f, unwed)")
        print()
        print("Each tick the midwife attempts a VALID birth (Adam x Eve) and")
        print("an ILLICIT one (Adam x Lilith, not married). Both tier-C rules")
        print("are observably active: the birth-law vetoes the illicit pair")
        print("every tick; the cap vetoes the valid pair once the world is")
        print(f"full (cap {POPULATION_CAP}).")
        print()
        hdr = (f"{'tick':>4}  {'pop':>3}  {'Adam x Eve':<22}  "
               f"{'Adam x Lilith':<22}")
        print(hdr)
        print("-" * len(hdr))

        records = simulate(session, world, ticks)
        for r in records:
            valid = r["birth_label"]
            if valid == BORN:
                valid_col = f"+ {r['birth_name']:<16}"
            else:
                valid_col = f"x {valid:<17}"
            illicit_col = f"x {r['illicit_label']:<17}" if r["illicit_label"] != "-" else "-"
            print(f"{r['tick']:>4}  {r['population']:>3}  {valid_col:<22}  "
                  f"{illicit_col:<22}")
        print()

        _print_summary(session, world)


def _print_summary(session: Session, world) -> None:
    treasury = session.get(Account, world.treasury_account_id)
    adam, eve = world.founders["Adam"], world.founders["Eve"]
    q = build_queries(session, session.query(Tick.number)
                      .order_by(Tick.number.desc()).limit(1).scalar() or 0)

    # All living children of the founding couple.
    children = [c for c in session.query(Entity).all()
                if c.parents and adam.id in c.parents and eve.id in c.parents]
    children.sort(key=lambda c: c.birth_tick or 0)

    print("=" * 64)
    print("the next generation (children of Adam x Eve)")
    if children:
        for c in children:
            acct = c.accounts[0] if c.accounts else None
            bal = f"{acct.balance:>7.2f}" if acct else "   no $"
            print(f"  {c.name:<10} birth_tick={c.birth_tick:<3} {bal}   "
                  f"parents=[{', '.join(q['parents'](c.id))}]")
    else:
        print("  (none survived the cap)")
    print()
    print(f"  treasury      {treasury.balance:>7.2f}")
    print(f"  population    {q['population']()}  "
          f"(cap {POPULATION_CAP}; {len(children)} children born)")
    print()
    print("lineage check (the 6c queries):")
    print(f"  children(Adam) = {len(q['children'](adam.id))}")
    print(f"  children(Eve)  = {len(q['children'](eve.id))}")
    print(f"  parents(firstborn) = {q['parents'](children[0].id) if children else '-'}")
    print()
    print("No engine change. A single mechanism -- spawn_entity -- brought")
    print("entities into being mid-tick; three tier-C rules (sex-holding +")
    print("age + marriage, a population cap, and an endowment transfer)")
    print("composed world policy entirely from ctx.query reads. Each child's")
    print("birth_tick is the tick it was born (the executing tick, not the")
    print("prior one), so age never disagrees with ctx.tick.")
    print()


def main() -> None:
    p = argparse.ArgumentParser(description="Step 6c population demo")
    p.add_argument("--ticks", type=int, default=6,
                   help="ticks to run (default 6 -- reaches the cap)")
    args = p.parse_args()
    run(ticks=args.ticks)


if __name__ == "__main__":
    main()
