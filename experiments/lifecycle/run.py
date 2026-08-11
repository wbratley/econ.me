"""Run harness for the lifecycle demo (Step 6b).

Builds the staggered-age economy, loops ``run_tick()``, and prints a
per-tick report showing every demographic transition driven by
``ctx.query.age()`` -- a child coming of age (grant fires, poll-tax
admitted), a worker retiring (poll-tax vetoed, pension begins) -- with no
engine change anywhere.

Usage:
    .venv/bin/python -m experiments.lifecycle.run            # 6 ticks
    .venv/bin/python -m experiments.lifecycle.run --ticks 8
"""

import argparse
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.models import Account, Base, Tick
from econengine.tick import run_tick

from .scenario import build_economy

UNDER_AGE = "minor"      # poll-tax vetoed: below working age
RETIREE = "retiree"      # poll-tax vetoed: at/over retirement age
WORKER = "tax-paid"      # poll-tax applied (working age)


def _event_status(events, *, entity_id=None, reference=None, to_account=None):
    """Find the applied/rejected outcome of the (unique) transfer event
    matching the filters. Returns (status, reason) or (None, None)."""
    for e in events:
        if e.get("type") != "transfer":
            continue
        params = e.get("params", {})
        if reference is not None and params.get("reference") != reference:
            continue
        if entity_id is not None and e.get("entity_id") != entity_id:
            continue
        if to_account is not None and params.get("to_account_id") != to_account:
            continue
        return e.get("status"), e.get("reason")
    return None, None


def _labor_label(status, reason):
    """Turn the poll-tax outcome into a compact column entry."""
    if status == "applied":
        return WORKER
    if status == "rejected":
        return UNDER_AGE if reason and "minor" in reason else RETIREE
    return "no-tender"   # the citizen's behaviour didn't run (e.g. incapacitated)


def run(ticks: int = 6) -> None:
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        world = build_economy(session)
        session.commit()

        print()
        print("Step 6b -- age-driven policy (ctx.query.age)")
        print("instruments: AGE-GATE (validator)  PENSION (policy)  "
              "COMING-OF-AGE (policy)")
        print("cast:  Eve(born -13)  Adam(born -30)  Noah(born -63)  "
              "Sarah(born -70)")
        print()
        print("Note on the one-tick lead (this is the 5a dual-source design,")
        print("not a bug): a POLICY reads the EXECUTING tick, a VALIDATOR")
        print("reads the LAST-COMMITTED tick. So a policy-side transition")
        print("(grant / pension) and the matching validator-side transition")
        print("(tax admission / exemption) for the same age threshold fire")
        print("one tick apart -- the policy leads. Eve is granted at tick")
        print("3 (policy sees 16) but admitted to labor at tick 4 (validator")
        print("sees 16); Noah is pensioned at tick 2 but taxed one more tick.")
        print()
        hdr = f"{'tick':>4}  {'citizen':<6} {'age':>3}  {'labor':<10} " \
              f"{'pension':<8} {'grant':<6} {'balance':>10}"
        print(hdr)
        print("-" * len(hdr))

        transitions = []   # (tick, citizen, text) -- surfaced in the summary
        prev_labor = {}    # citizen_id -> previous labor label, for edge detect

        records = simulate(session, world, ticks)
        entity_by_name = {e.name: e for e, _ in world.citizens}
        for i, r in enumerate(records):
            entity = entity_by_name[r["name"]]
            labor = r["labor"]
            pension = "+P" if r["pension"] else "--"
            grant = "+G" if r["grant"] else "--"
            print(f"{r['tick']:>4}  {r['name']:<6} {r['age']:>3}  "
                  f"{labor:<10} {pension:<8} {grant:<6} "
                  f"{r['balance']:>10.2f}")
            _record_transition(
                transitions, entity, labor, pension, grant,
                prev_labor.get(entity.id), r["tick"], r["age"])
            prev_labor[entity.id] = labor
            # blank line between ticks (records are tick-major)
            nxt = records[i + 1] if i + 1 < len(records) else None
            if nxt is None or nxt["tick"] != r["tick"]:
                print()

        _print_summary(session, world, transitions)


def simulate(session: Session, world, ticks: int) -> list[dict]:
    """Run ``ticks`` ticks and return one record per (tick, citizen).

    Each record carries the observables the report and the tests both read:
    age, the labor outcome (WORKER / UNDER_AGE / RETIREE), whether pension
    and grant applied this tick, and the account balance. Pure data -- no
    printing -- so the smoke test asserts on structure, not stdout.
    """
    records = []
    for tick_number in range(1, ticks + 1):
        run_tick(session)
        session.commit()
        tick = session.query(Tick).filter_by(number=tick_number).one()
        events = tick.events or []
        for entity, acct_id in world.citizens:
            age = tick_number - entity.birth_tick
            acct = session.get(Account, acct_id)
            labor_st, labor_reason = _event_status(
                events, entity_id=entity.id, reference="poll-tax")
            pension_st, _ = _event_status(
                events, to_account=acct_id, reference="pension")
            grant_st, _ = _event_status(
                events, to_account=acct_id, reference="grant")
            records.append({
                "tick": tick_number,
                "name": entity.name,
                "entity_id": entity.id,
                "age": age,
                "labor": _labor_label(labor_st, labor_reason),
                "pension": pension_st == "applied",
                "grant": grant_st == "applied",
                "balance": acct.balance,
            })
    return records


def _record_transition(out, entity, labor, pension, grant, prev_labor,
                        tick, age):
    """Detect genuine IN-RUN lifecycle edges (initial states are not edges).

    A transition is reported only when the previous tick's state DIFFERS,
    so Sarah (who starts retired) and Adam (who starts working) contribute
    nothing -- only Eve's coming-of-age and Noah's retirement show up.
    """
    if grant == "+G":
        out.append((tick, entity.name,
                    f"coming-of-age GRANT (policy, executing tick: age {age})"))
    # Pension beginning while still taxed = the one-tick overlap (policy
    # leads). Only counts if the citizen was a worker last tick.
    if pension == "+P" and labor == WORKER and prev_labor == WORKER:
        out.append((tick, entity.name,
                    f"pension BEGINS (policy sees age {age}); still taxed "
                    f"this tick (validator lags)"))
    # Tax exemption landing = the validator catching up = retirement done.
    if labor == RETIREE and prev_labor == WORKER:
        out.append((tick, entity.name,
                    f"retirement complete (validator now sees age {age}: "
                    f"tax exempt)"))
    # Labor admission landing = the validator catching up = coming of age
    # (on the enforcement side; the grant already fired last tick).
    if labor == WORKER and prev_labor == UNDER_AGE:
        out.append((tick, entity.name,
                    f"labor ADMITTED (validator now sees age {age})"))


def _print_summary(session: Session, world, transitions) -> None:
    treasury = session.get(Account, world.treasury_account_id)
    print("=" * 64)
    print("final balances")
    for entity, acct_id in world.citizens:
        acct = session.get(Account, acct_id)
        age = "(predates tracking)" if entity.birth_tick is None else ""
        print(f"  {entity.name:<8} {acct.balance:>10.2f}  "
              f"birth_tick={entity.birth_tick} {age}")
    print(f"  {'treasury':<8} {treasury.balance:>10.2f}")
    print()
    print("transitions (the lifecycle, driven by ctx.query.age()):")
    if transitions:
        for tick, name, text in transitions:
            print(f"  tick {tick}  {name:<6}  {text}")
    else:
        print("  (none this run)")
    print()
    print("No engine change. A single primitive -- ctx.query.age() -- drove a")
    print("pension, a coming-of-age grant, and an age-gated tax. The one-tick")
    print("lead between policy-side and validator-side transitions is the 5a")
    print("dual-source design: policies act on the executing tick, validators")
    print("on the last-committed tick. Both saw age() -- and both gated on it")
    print("exactly -- which is the point.")
    print()


def main() -> None:
    p = argparse.ArgumentParser(description="Step 6b lifecycle demo")
    p.add_argument("--ticks", type=int, default=6,
                   help="ticks to run (default 6 -- covers both transitions)")
    args = p.parse_args()
    run(ticks=args.ticks)


if __name__ == "__main__":
    main()
