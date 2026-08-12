"""Run harness for the generations demo (Step 6d proving experiment).

Builds the founding world, loops ``run_tick()``, and prints a per-tick
report showing three founders die of old age on schedule -- two leaving
their estates to a shared heir (the "heir" rule), one burning heirless
(the fallback) -- driven entirely by the engine's age pass and the estate
rule, with no engine change and no Lua scripts anywhere.

Usage:
    .venv/bin/python -m experiments.generations.run            # 6 ticks
    .venv/bin/python -m experiments.generations.run --ticks 8
"""

import argparse
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine.models import Account, Base, Entity, EntityStatus, Tick
from econengine.scripting import build_queries
from econengine.tick import run_tick

from .scenario import (
    ABRAHAM_GOLD, ABRAHAM_LIFESPAN, ABRAHAM_USD,
    CAIN_BRONZE, CAIN_LIFESPAN, CAIN_USD,
    SARAH_SILVER, SARAH_LIFESPAN, SARAH_USD,
    build_economy,
)


def simulate(session: Session, world, ticks: int) -> list[dict]:
    """Run ``ticks`` ticks and return one record per tick.

    Each record carries the observables the report and tests both read: the
    active population after the tick, and the parsed death events (who died,
    of what cause/age, and where the estate went). Pure data -- no printing
    -- so the smoke test asserts on structure, not stdout.
    """
    id_to_name = {e.id: name for name, e in world.people.items()}
    records = []
    for n in range(1, ticks + 1):
        run_tick(session)
        session.commit()
        tick = session.query(Tick).filter_by(number=n).one()
        deaths = []
        for e in (tick.events or []):
            if e.get("type") != "entity_incapacitated":
                continue
            eid = e["entity_id"]
            deaths.append({
                "name": id_to_name.get(eid, "?"),
                "entity_id": eid,
                "condition": e["condition"],
                "age": e["quantity"],
                "lifespan": e["threshold"],
                "estate_policy": e["estate_policy"],
                "recipient": id_to_name.get(e["recipient_id"])
                if e.get("recipient_id") else None,
                "goods_transferred": e.get("goods_transferred"),
                "money_transferred": e.get("money_transferred"),
                "goods_burned": e.get("goods_burned"),
                "money_burned": e.get("money_burned"),
            })
        deaths.sort(key=lambda d: d["name"])
        population = session.execute(
            select(func.count()).select_from(Entity)
            .where(Entity.status == EntityStatus.ACTIVE)
        ).scalar_one()
        records.append({"tick": n, "population": population, "deaths": deaths})
    return records


def _holding(session, entity_id, symbol):
    """Qty of a holding, or Decimal 0 (Isaac starts with none)."""
    h = markets_get_holding(session, entity_id, symbol)
    return h.quantity if h is not None else Decimal("0")


def markets_get_holding(session, entity_id, symbol):
    from econengine.markets import get_holding
    return get_holding(session, entity_id, symbol)


def _balance(session, entity_id, currency="USD"):
    """Balance of the first account in a currency, or Decimal 0."""
    acct = session.execute(
        select(Account).where(Account.entity_id == entity_id,
                              Account.currency == currency)
    ).scalars().first()
    return acct.balance if acct is not None else Decimal("0")


def _desc(death):
    """A compact, human-readable description of one death for the table."""
    if death["estate_policy"] == "heir" and death["recipient"]:
        moved = []
        if Decimal(death["goods_transferred"]) > 0:
            moved.append(f"+goods {death['goods_transferred']}")
        if Decimal(death["money_transferred"]) > 0:
            moved.append(f"+USD {death['money_transferred']}")
        return f"{death['name']} dies -> {death['recipient']} inherits " + \
               ", ".join(moved)
    # burn (no heir, or no active recipient)
    burned = []
    if Decimal(death["goods_burned"]) > 0:
        burned.append(f"goods {death['goods_burned']}")
    if Decimal(death["money_burned"]) > 0:
        burned.append(f"USD {death['money_burned']}")
    what = ", ".join(burned) if burned else "nothing"
    return f"{death['name']} dies -> BURN {what}"


def run(ticks: int = 6) -> None:
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        world = build_economy(session)
        session.commit()

        print()
        print("Step 6d -- death-by-old-age, proven (founders die, heirs inherit)")
        print("rule:   ESTATE = 'heir' (votable data; missing heir_id -> burn)")
        print("cast:   Abraham(life 3)  Sarah(life 5)  Cain(life 4, no heir)  "
              "Isaac(immortal, child of Abraham+Sarah)")
        print()
        print("No scripts. Death is an invariant engine pass -- the opposite")
        print("face of 6c's spawn: birth is an act (needs a POLICY), death is")
        print("an inevitability (needs none). The estate rule + heir_id do the")
        print("rest. Everyone is born at tick 0, so age == tick.")
        print()
        hdr = f"{'tick':>4}  {'pop':>3}  {'events':<54}"
        print(hdr)
        print("-" * len(hdr))

        records = simulate(session, world, ticks)
        for r in records:
            if r["deaths"]:
                ev = "; ".join(_desc(d) for d in r["deaths"])
            else:
                ev = "-"
            print(f"{r['tick']:>4}  {r['population']:>3}  {ev:<54}")
        print()

        _print_summary(session, world)


def _print_summary(session: Session, world) -> None:
    isaac = world.people["Isaac"]
    abraham = world.people["Abraham"]
    sarah = world.people["Sarah"]
    cain = world.people["Cain"]
    last_tick = session.query(Tick.number).order_by(
        Tick.number.desc()).limit(1).scalar() or 0
    q = build_queries(session, last_tick)

    print("=" * 64)
    print("the generational handoff (Isaac, the immortal heir)")
    gold = _holding(session, isaac.id, "GOLD")
    silver = _holding(session, isaac.id, "SILVER")
    usd = _balance(session, isaac.id)
    print(f"  Isaac    GOLD {gold}  SILVER {silver}  USD {usd}")
    print(f"           parents = {q['parents'](isaac.id)}")
    print(f"           age {q['age'](isaac.id)}, lifespan {q['lifespan'](isaac.id)}"
          " (immortal)")
    print()
    print("  founders (all dead):")
    for name, e in (("Abraham", abraham), ("Sarah", sarah), ("Cain", cain)):
        print(f"    {name:<8} status={e.status.value:<13} "
              f"lifespan {e.lifespan}  heir_id={e.heir_id or '-'}")
    print()
    # Money: Abraham 500 + Sarah 300 -> Isaac. Cain 100 burned.
    total_started = ABRAHAM_USD + SARAH_USD + CAIN_USD
    print(f"  money supply: {total_started} -> {usd}  "
          f"({CAIN_USD} burned with the heirless Cain)")
    print(f"  population {q['population']()} "
          f"(Government + Isaac; the three founders are dead)")
    print()
    print("lineage check (the 6c queries close the cycle):")
    print(f"  children(Abraham) = {q['children'](abraham.id)}")
    print(f"  children(Sarah)   = {q['children'](sarah.id)}")
    print(f"  parents(Isaac)    = {q['parents'](isaac.id)}")
    print()
    print("No engine change. No Lua scripts. The engine's age pass (Step 6d)")
    print("ended each founder at age == lifespan and fired the SAME")
    print("entity_incapacitated event a starvation death fires -- condition")
    print("'age' is the only new label. The votable estate rule ('heir') and the")
    print("per-entity heir_id (provenance, stamped at birth) composed the")
    print("inheritance. ctx.query.lifespan() is the single new read. Birth")
    print("(6c) opened the demographic cycle; death (6d) closed it.")
    print()


def main() -> None:
    p = argparse.ArgumentParser(description="Step 6d generations demo")
    p.add_argument("--ticks", type=int, default=6,
                   help="ticks to run (default 6 -- all three deaths)")
    args = p.parse_args()
    run(ticks=args.ticks)


if __name__ == "__main__":
    main()
