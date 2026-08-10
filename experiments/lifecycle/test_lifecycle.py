"""Smoke test for the lifecycle demo (Step 6b).

Asserts that ctx.query.age() actually drove the three instruments and both
lifecycle transitions, on structured records (not stdout). This is the
machine-checkable half of the "prove the affordance" deliverable; the
human-readable half is ``python -m experiments.lifecycle.run``.

What it pins down:
  * AGE-GATE    Eve (minor) and Sarah (retiree) never pay the poll-tax;
               Adam and Noah pay it while working-age.
  * PENSION     Noah and Sarah collect every tick from retirement on; Eve
               and Adam never do.
  * COMING-OF-AGE  Eve gets the grant exactly once, at tick 3 (age 16).
                  Nobody else ever does (the adults came of age before
                  observation began).
  * DUAL-SOURCE LEAD  the policy-side transition fires one tick BEFORE the
                  validator-side one for the same threshold: Eve is granted
                  at tick 3 but admitted to labor at tick 4; Noah is
                  pensioned at tick 2 but tax-exempt only at tick 3.
"""
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from econengine.models import Base, Account

from .run import simulate, WORKER, UNDER_AGE, RETIREE
from .scenario import build_economy, TREASURY_ENDOWMENT


def _run(ticks: int = 6):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        world = build_economy(session)
        session.commit()
        return world, simulate(session, world, ticks)


def _by(records, name):
    return [r for r in records if r["name"] == name]


def test_age_gate_admits_only_working_age():
    _, records = _run()
    eve, adam, noah, sarah = (_by(records, n) for n in ("Eve", "Adam", "Noah", "Sarah"))

    # Eve: minor (under 16) until the validator catches up at tick 4.
    assert [r["labor"] for r in eve] == [UNDER_AGE, UNDER_AGE, UNDER_AGE,
                                         WORKER, WORKER, WORKER]
    # Adam: prime worker throughout.
    assert all(r["labor"] == WORKER for r in adam)
    # Noah: worker through tick 2, retiree (validator catches up) from tick 3.
    assert [r["labor"] for r in noah] == [WORKER, WORKER,
                                          RETIREE, RETIREE, RETIREE, RETIREE]
    # Sarah: retiree throughout.
    assert all(r["labor"] == RETIREE for r in sarah)


def test_pension_paid_to_seniors_only():
    _, records = _run()
    eve, adam, noah, sarah = (_by(records, n) for n in ("Eve", "Adam", "Noah", "Sarah"))

    assert not any(r["pension"] for r in eve)      # never of age to retire
    assert not any(r["pension"] for r in adam)     # working throughout
    # Noah: pension begins tick 2 (policy sees age 65) and continues.
    assert [r["pension"] for r in noah] == [False, True, True, True, True, True]
    # Sarah: pension every tick.
    assert all(r["pension"] for r in sarah)


def test_coming_of_age_grant_fires_once_for_eve_only():
    _, records = _run()
    grants = [(r["tick"], r["name"], r["age"]) for r in records if r["grant"]]
    assert grants == [(3, "Eve", 16)]              # exactly once, age 16
    # Nobody else is ever granted (the adults came of age before tick 1 and
    # are pre-seeded in came_of_age).
    assert not any(r["grant"] for r in _by(records, "Adam"))
    assert not any(r["grant"] for r in _by(records, "Noah"))
    assert not any(r["grant"] for r in _by(records, "Sarah"))


def test_dual_source_lead_policy_precedes_validator():
    """The headline subtlety: a POLICY (executing tick) and a VALIDATOR
    (last-committed tick) gating the same threshold fire one tick apart,
    the policy leading. Eve's grant lands a tick before her labor is
    admitted; Noah's pension lands a tick before his tax exemption."""
    _, records = _run()
    eve, noah = _by(records, "Eve"), _by(records, "Noah")

    eve_grant_tick = next(r["tick"] for r in eve if r["grant"])
    eve_labor_tick = next(r["tick"] for r in eve if r["labor"] == WORKER)
    assert eve_labor_tick == eve_grant_tick + 1   # validator lags by one

    noah_pension_tick = next(r["tick"] for r in noah if r["pension"])
    noah_exempt_tick = next(r["tick"] for r in noah if r["labor"] == RETIREE)
    assert noah_exempt_tick == noah_pension_tick + 1


def test_balances_account_for_every_flow():
    """The citizens' net change decomposes into the three flows at known
    rates, which independently confirms each instrument fired the right
    number of times (no session needed -- pure arithmetic on records)."""
    world, records = _run()
    from .scenario import PENSION, GRANT, POLL_TAX, CITIZEN_ENDOWMENT
    final_citizen_total = sum(r["balance"] for r in records if r["tick"] == 6)
    n_citizens = len(world.citizens)
    citizens_delta = final_citizen_total - CITIZEN_ENDOWMENT * n_citizens
    # Grant: Eve once. Pension: Noah from tick 2 (5 ticks) + Sarah all 6.
    # Poll-tax: Adam all 6 + Noah ticks 1-2 (2) + Eve ticks 4-6 (3) -- the
    # age-gate admits Eve only once the lagging validator sees her at 16.
    grants_paid = GRANT
    pensions_paid = PENSION * (5 + 6)
    tax_collected = POLL_TAX * (6 + 2 + 3)
    assert citizens_delta == grants_paid + pensions_paid - tax_collected


def test_money_is_conserved_closed_system():
    """Transfers only -- no issuance. The treasury's loss equals the
    citizens' gain, so total money is invariant. Read directly off the
    session so the assertion is exact."""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        world = build_economy(session)
        session.commit()
        simulate(session, world, ticks=6)
        treasury = session.get(Account, world.treasury_account_id)
        citizens_total = sum(
            session.get(Account, a).balance for _, a in world.citizens
        )
        from .scenario import TREASURY_ENDOWMENT, CITIZEN_ENDOWMENT
        assert treasury.balance + citizens_total == (
            TREASURY_ENDOWMENT + CITIZEN_ENDOWMENT * len(world.citizens)
        )
