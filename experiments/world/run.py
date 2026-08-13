"""Run harness: build the world, loop run_tick(), snapshot metrics, report.

Builds the Phase 0 content-pack economy directly on econengine (no HTTP) and
advances it tick by tick, printing a per-tick digest and a final summary.
It exists to eyeball the substrate (does the chain flow? does everyone eat?)
and to drive ad-hoc runs; the assertions live in test_world.py.

    .venv/bin/python -m experiments.world.run --ticks 40
"""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from econengine.models import Account, Entity, EntityStatus, Holding, Tick
from econengine.tick import run_tick

from .scenario import DEFAULT_TICKS, MONEY_SUPPLY, build_economy


def _balance(session: Session, entity: Entity) -> Decimal:
    return session.execute(
        select(func.coalesce(Account.balance, 0))
        .where(Account.entity_id == entity.id, Account.currency == "USD")
    ).scalar_one()


def _holding(session: Session, entity_id: str, symbol: str) -> Decimal:
    qty = session.execute(
        select(Holding.quantity)
        .where(Holding.entity_id == entity_id, Holding.symbol == symbol)
    ).scalar_one_or_none()
    return qty if qty is not None else Decimal("0")


def _trades_in(session: Session, tick_number: int) -> int:
    tick = session.execute(
        select(Tick).where(Tick.number == tick_number)
    ).scalar_one_or_none()
    if tick is None:
        return 0
    return sum(1 for e in (tick.events or []) if e.get("type") == "trade")


def _script_errors(session: Session) -> list[str]:
    errs = []
    for tick in session.execute(select(Tick).order_by(Tick.number)).scalars():
        for e in (tick.events or []):
            if e.get("type") == "script_error":
                errs.append(f"tick {tick.number}: {e.get('error')}")
    return errs


def run_scenario(ticks: int = DEFAULT_TICKS, db_path: str = ":memory:") -> dict:
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    from econengine.models import Base
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        world = build_economy(session)
        session.commit()

        snapshots = []
        for tick_number in range(1, ticks + 1):
            run_tick(session)
            session.commit()
            snapshots.append({
                "tick": tick_number,
                "farmer_usd": float(_balance(session, world.farmer)),
                "miner_usd": float(_balance(session, world.miner)),
                "smith_usd": float(_balance(session, world.smith)),
                "farmer_grain": float(_holding(session, world.farmer.id, "GRAIN")),
                "miner_grain": float(_holding(session, world.miner.id, "GRAIN")),
                "smith_grain": float(_holding(session, world.smith.id, "GRAIN")),
                "smith_iron": float(_holding(session, world.smith.id, "IRON")),
                "miner_ore": float(_holding(session, world.miner.id, "ORE")),
                "trades": _trades_in(session, tick_number),
                "active": session.execute(
                    select(func.count()).select_from(Entity)
                    .where(Entity.status == EntityStatus.ACTIVE)
                ).scalar_one(),
            })

        total_supply = float(session.execute(
            select(func.coalesce(func.sum(Account.balance), 0))
            .where(Account.currency == "USD")
        ).scalar_one())
        errors = _script_errors(session)

    return {
        "ticks": ticks,
        "snapshots": snapshots,
        "final": snapshots[-1] if snapshots else {},
        "money_supply": total_supply,
        "expected_money_supply": float(MONEY_SUPPLY),
        "money_conserved": abs(total_supply - float(MONEY_SUPPLY)) < 1e-6,
        "script_errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Phase 0 world experiment")
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument("--db", type=str, default=":memory:")
    parser.add_argument("--out", type=str, default=None,
                        help="write the result JSON here if given")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress the per-tick digest")
    args = parser.parse_args()

    result = run_scenario(ticks=args.ticks, db_path=args.db)

    if not args.quiet:
        print(f"{'tick':>4} {'farmer':>8} {'miner':>8} {'smith':>8} "
              f"{'grain(F/M/S)':>20} {'ore':>6} {'iron':>6} {'trades':>6} {'alive':>5}")
        for s in result["snapshots"]:
            print(f"{s['tick']:>4} "
                  f"{s['farmer_usd']:>8.1f} {s['miner_usd']:>8.1f} {s['smith_usd']:>8.1f} "
                  f"{s['farmer_grain']:>6.1f}/{s['miner_grain']:>5.1f}/{s['smith_grain']:>5.1f} "
                  f"{s['miner_ore']:>6.1f} {s['smith_iron']:>6.1f} "
                  f"{s['trades']:>6} {s['active']:>5}")

    f = result["final"]
    print(f"\nfinal @ t{result['ticks']}: "
          f"alive={f.get('active')} smith_iron={f.get('smith_iron', 0):.1f} "
          f"trades(last)={f.get('trades')}")
    print(f"money conserved: {result['money_conserved']} "
          f"({result['money_supply']:.2f} == {result['expected_money_supply']:.2f})")
    if result["script_errors"]:
        print(f"SCRIPT ERRORS ({len(result['script_errors'])}):")
        for e in result["script_errors"][:10]:
            print(f"  {e}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
