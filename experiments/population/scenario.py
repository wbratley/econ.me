"""Genesis for the population demo (Step 6c proving experiment).

A tiny world built to PROVE that ``spawn_entity`` -- the mechanism shipped
in 6c -- actually works end-to-end, with **no engine change**. This is the
platform layer exercising 6c, the way ``experiments/lifecycle`` proved 6a.
The engine mechanism is done; this composes world policy on top of it.

The world exercises every design point of 6c:

  SPAWN           a POLICY (the "midwife") fires ctx.action.spawn_entity each
                  tick -- the government holds SPAWN and is the caller; Adam
                  and Eve are the parents (caller != parents, by design).
  BIRTH LAW       a VALIDATOR composes the world's eligibility rule from
                  pure ctx.query reads: sex (a HOLDING), age (the 6a
                  keystone), and marriage (a WorldSetting DATUM). The engine
                  ships none of this; a world writes it.
  POPULATION CAP  a second VALIDATOR reads ctx.query.population() and caps
                  growth -- a votable ceiling below the server's hard cap.
  ENDOWMENT       a HOOK moves starting wealth to each newborn by TRANSFER
                  -- proving "endowment is a transfer, not mechanism".

The cast::

    Government   the caller/midwife; holds SPAWN; owns the treasury.
    Adam    birth_tick -30, MALE holding, married to Eve.
    Eve     birth_tick -28, FEMALE holding, married to Adam.
    Lilith  birth_tick -25, FEMALE holding, UNMARRIED.

Each tick the midwife attempts TWO births: a VALID one (Adam x Eve, admitted
until the cap) and an ILLICIT one (Adam x Lilith, always refused -- not
married). So both tier-C rules are observably active every tick: the
birth-law vetoes the illicit pair; the cap vetoes the valid pair once full.

Sex/gender and marriage are deliberately DATA, never engine fields:
sex is an entity-attached holding (read-only to scripts -- the invariant
that protects the body), marriage is a WorldSetting registry (a validator
cannot read another script's state, so relationship data is mirrored into
the world-readable setting store). ``birth_tick`` is overridden after
creation to simulate a world already in progress (exactly as in the
lifecycle demo) so the founding generation are adults at tick 1.

Founders carry NO accounts and NO behaviour -- they are pure genetic
lineage (holdings of sex) for the birth rule to read. The only money is the
treasury (which funds endowments) and the children's accounts. Keeping it
minimal keeps the proof focused: spawn_entity + three tier-C rules + the
lineage queries are the only things under test.
"""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from econengine import capabilities, markets, services
from econengine.models import (
    Entity, EntityType, Script, ScriptType, WorldSetting,
)

MIN_PARENT_AGE = 16
POPULATION_CAP = 7            # 4 founders -> 3 births, then capped
ENDOWMENT = Decimal("100")
TREASURY_ENDOWMENT = Decimal("10000")

_LUA_DIR = Path(__file__).parent / "lua"


def _lua(name: str) -> str:
    return (_LUA_DIR / name).read_text()


@dataclass
class Founder:
    name: str
    birth_tick: int          # overridden after creation (adults at tick 1)
    sex: str                 # "MALE" or "FEMALE" -- a holding, not a field
    married_to: str | None   # name of spouse, or None (unmarried)


@dataclass
class World:
    """Handles the run/test harness needs to observe state after each tick."""
    gov: Entity
    treasury_account_id: str
    founders: dict           # name -> Entity


_CAST = [
    Founder("Adam",   -30, "MALE",   married_to="Eve"),
    Founder("Eve",    -28, "FEMALE", married_to="Adam"),
    Founder("Lilith", -25, "FEMALE", married_to=None),   # unmarried
]


def build_economy(session: Session) -> World:
    """Government (the midwife) + three founders with sex holdings and a
    marriage registry. No founder accounts, no behaviour scripts -- the
    founders exist as lineage for the birth law to read."""
    gov = services.create_entity(session, "Government", EntityType.GOVERNMENT)
    gov.capabilities = [capabilities.SPAWN]
    treasury = services.create_account(
        session, gov, "USD", initial_balance=TREASURY_ENDOWMENT)

    founders: dict[str, Entity] = {}
    for m in _CAST:
        entity = services.create_entity(session, m.name, EntityType.INDIVIDUAL)
        # Override the genesis stamp to simulate a world already in progress.
        entity.birth_tick = m.birth_tick
        # Sex is a holding -- read-only to scripts (invariant-protected body).
        markets.adjust_holding(session, entity, m.sex, Decimal("1"))
        founders[m.name] = entity

    # The marriage registry: a pairwise WorldSetting datum. A validator
    # cannot read another script's state, so relationship data lives in the
    # world-readable setting store. Adam <-> Eve are wed; Lilith has none.
    for m in _CAST:
        if m.married_to is not None:
            me, spouse = founders[m.name].id, founders[m.married_to].id
            session.add(WorldSetting(key=f"married:{me}", value={"spouse": spouse}))

    _wire_scripts(session, gov, treasury.id, founders)
    session.flush()
    return World(gov=gov, treasury_account_id=treasury.id, founders=founders)


def _wire_scripts(session: Session, gov: Entity, treasury_id: str,
                  founders: dict[str, Entity]) -> None:
    adam, eve, lilith = founders["Adam"], founders["Eve"], founders["Lilith"]

    # The midwife: a POLICY on the government. It is the CALLER (holds SPAWN);
    # the parents are explicit params. Attempts one valid + one illicit birth
    # each tick.
    session.add(Script(
        name="midwife", script_type=ScriptType.POLICY,
        source=_lua("midwife.lua"), entity_id=gov.id, timeout_ms=500,
        state={"adam_id": adam.id, "eve_id": eve.id, "lilith_id": lilith.id},
    ))

    # Endowment: a HOOK on the government. Fires only after a SUCCESSFUL
    # spawn (vetoed births never reach a hook) and transfers starting wealth
    # to the newborn -- endowment is a transfer, not a mechanism.
    session.add(Script(
        name="endowment", script_type=ScriptType.HOOK,
        source=_lua("endowment.lua"), entity_id=gov.id, timeout_ms=300,
        state={"treasury_account_id": treasury_id, "endowment": str(ENDOWMENT)},
    ))

    # Birth law: a VALIDATOR composing sex (holding) + age (query) + marriage
    # (WorldSetting). Created BEFORE the cap so it fires first -- an
    # ineligible birth is refused before capacity is consulted.
    session.add(Script(
        name="birth-law", script_type=ScriptType.VALIDATOR,
        source=_lua("birth_law.lua"), timeout_ms=300,
        state={"min_parent_age": str(MIN_PARENT_AGE)},
    ))

    # Population cap: a VALIDATOR reading population() (the 6c query). A
    # votable ceiling below the server's non-votable hard cap.
    session.add(Script(
        name="population-cap", script_type=ScriptType.VALIDATOR,
        source=_lua("population_cap.lua"), timeout_ms=300,
        state={"population_cap": str(POPULATION_CAP)},
    ))
