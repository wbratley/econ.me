"""Genesis for the lifecycle demo (Step 6b).

A tiny economy built to PROVE that ``ctx.query.age()`` drives real policy,
end-to-end, with no engine change (this is the platform layer exercising 6a,
the way ``contracts/bond`` proved 5a-5c). Three age-driven instruments, all
reading the same primitive:

  PENSION         a POLICY pays every senior a stipend each tick.
  COMING-OF-AGE   a POLICY fires a one-time grant the tick a citizen first
                  reaches working age (recorded so it never repeats).
  AGE-GATE        a VALIDATOR vetoes the poll-tax for minors and retirees --
                  only working-age citizens are taxed as workers.

The population has STAGGERED ``birth_tick`` values so every demographic
stage AND both transitions (coming of age, retirement) land inside a short
run, rather than spreading the demo over 70 ticks::

    Eve    birth_tick -13  -> age 14 at tick 1; comes of age at tick 3 (16)
    Adam   birth_tick -30  -> age 30; prime worker throughout
    Noah   birth_tick -63  -> age 64 at tick 1; retires at tick 2 (65)
    Sarah  birth_tick -70  -> age 70; retired throughout

``birth_tick`` is normally stamped by ``services.create_entity`` to the
latest committed tick (0 at genesis). We OVERRIDE it afterwards to simulate
a world that has already been running -- a legitimate scenario setup, and
exactly how a real long-running world would look if you joined it late.
``age = ctx.tick - birth_tick`` then behaves precisely as it would in that
world. (Negative birth ticks are just integers; age stays a non-negative
count of ticks-since-birth for every living entity.)
"""

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from econengine import services
from econengine.models import Entity, EntityType, Script, ScriptType

MIN_WORK_AGE = 16
RETIRE_AGE = 65
POLL_TAX = Decimal("5")
PENSION = Decimal("8")
GRANT = Decimal("50")          # one-time coming-of-age endowment
TREASURY_ENDOWMENT = Decimal("10000")
CITIZEN_ENDOWMENT = Decimal("1000")

_LUA_DIR = __import__("pathlib").Path(__file__).parent / "lua"


def _lua(name: str) -> str:
    return (_LUA_DIR / name).read_text()


@dataclass
class Citizen:
    name: str
    birth_tick: int            # overridden after creation (see module docstring)


@dataclass
class World:
    """Handles the scenario needs to read state after each tick."""
    gov: Entity
    treasury_account_id: str
    citizens: list[tuple[Entity, str]] = field(default_factory=list)  # (entity, account_id)


# The four demographic archetypes. See module docstring for the timing.
_CAST = [
    Citizen("Eve", -13),    # child -> comes of age at tick 3
    Citizen("Adam", -30),   # prime worker
    Citizen("Noah", -63),   # worker -> retires at tick 2
    Citizen("Sarah", -70),  # senior
]


def build_economy(session: Session) -> World:
    """Government + four citizens of staggered age. No goods, no markets, no
    production -- the lifecycle demo is purely about money flows (transfers)
    gated by age, which keeps it focused on the affordance 6a added."""
    gov = services.create_entity(session, "Government", EntityType.GOVERNMENT)
    treasury = services.create_account(session, gov, "USD",
                                       initial_balance=TREASURY_ENDOWMENT)

    citizens = []
    for c in _CAST:
        entity = services.create_entity(session, c.name, EntityType.INDIVIDUAL)
        # Override the genesis stamp to simulate a world already in progress.
        entity.birth_tick = c.birth_tick
        account = services.create_account(session, entity, "USD",
                                          initial_balance=CITIZEN_ENDOWMENT)
        citizens.append((entity, account.id))

    _wire_scripts(session, gov, treasury.id, citizens)
    session.flush()
    return World(gov=gov, treasury_account_id=treasury.id, citizens=citizens)


def _wire_scripts(session: Session, gov: Entity, treasury_id: str,
                  citizens: list[tuple[Entity, str]]) -> None:
    citizen_state = {
        "treasury_account_id": treasury_id,
        "poll_tax": str(POLL_TAX),
    }
    for entity, _acct in citizens:
        session.add(Script(
            name=f"citizen-{entity.id}",
            script_type=ScriptType.BEHAVIOUR,
            source=_lua("citizen.lua"),
            entity_id=entity.id,
            timeout_ms=200,
            state=dict(citizen_state),
        ))

    # The welfare state: pension + coming-of-age grant. Bound to the
    # government so it acts from the treasury account (ownership invariant).
    session.add(Script(
        name="welfare",
        script_type=ScriptType.POLICY,
        source=_lua("welfare.lua"),
        entity_id=gov.id,
        timeout_ms=500,
        state={
            "treasury_account_id": treasury_id,
            "pension": str(PENSION),
            "grant": str(GRANT),
            "min_work_age": str(MIN_WORK_AGE),
            "retire_age": str(RETIRE_AGE),
            # entity_id + account_id per citizen, so the policy can both
            # query age (needs the entity) and pay (needs the account).
            "citizens": [{"entity": e.id, "account": a} for e, a in citizens],
            # Set of entity_ids already granted -- grows by nested mutation
            # (ctx.state.came_of_age[id] = true), captured on read-back.
            # Pre-seeded with everyone ALREADY of age at tick 1, so the grant
            # means exactly "came of age DURING this run" (Eve) rather than
            # "enrolled on the welfare system's first tick" (the adults, who
            # came of age before observation began).
            "came_of_age": {
                e.id: True
                for e, _ in citizens
                if e.birth_tick is not None
                and (1 - e.birth_tick) >= MIN_WORK_AGE
            },
        },
    ))

    # The labor law: a VALIDATOR that vetoes the poll-tax outside the
    # working-age band. Fires on every transfer; passes through anything that
    # isn't a poll-tax (pension and grant use their own references).
    session.add(Script(
        name="age-gate",
        script_type=ScriptType.VALIDATOR,
        source=_lua("age_gate.lua"),
        timeout_ms=200,
        state={
            "min_work_age": str(MIN_WORK_AGE),
            "retire_age": str(RETIRE_AGE),
        },
    ))
