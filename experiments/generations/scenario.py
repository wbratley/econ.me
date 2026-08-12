"""Genesis for the generations demo (Step 6d proving experiment).

A tiny world built to PROVE death-by-old-age and inheritance work
end-to-end, with **no engine change and no Lua scripts**. Where 6c's
``experiments/population`` proved birth (which *needs* a POLICY to fire
``ctx.action.spawn_entity``), this experiment proves death -- and death is
the opposite face: an invariant engine pass that needs no script at all.
That absence is the point of 6d ("mortality is an engine pass, like the
condition pass it extends; no new capability, no new Lua action").

What this world exercises, purely from genesis data + the engine pass:

  LIFESPAN     each founder carries a per-entity ``lifespan`` (stamped at
               creation, immutable -- the layer-2 floor). At
               ``age >= lifespan`` the incapacity pass deactivates them.
  DEATH EVENT  the SAME ``entity_incapacitated`` event a starvation death
               fires, with ``condition: "age"`` -- the only new signal.
  INHERITANCE  the votable ``heir`` estate rule + a per-entity ``heir_id``
               transfer the estate to the next generation. No heir
               designated -> burns (supply shrinks), the default fallback.
  LINEAGE      the heir's ``parents`` (6c's provenance) make the handoff
               genealogically meaningful -- closing the cycle 6c opened:
               birth -> aging -> death -> inheritance.
  OBSERVABILITY ``ctx.query.lifespan()`` is the one new read -- proven here
                (and in the report) via ``build_queries``, the same way
                ``experiments/population`` proved ``age()``/``population()``.

The cast (all pre-seeded at genesis -- the setup path, exactly as
population's founders pre-seeded ``birth_tick`` and marriage)::

    Government   immortal institution; holds no wealth (the estate rule is
                 votable WorldSetting data, not an account). Estate = "heir".
    Abraham      lifespan 3.  GOLD 100  + USD 500.   heir = Isaac.
    Sarah        lifespan 5.  SILVER 50 + USD 300.   heir = Isaac.
    Cain         lifespan 4.  BRONZE 20 + USD 100.   no heir -> burns.
    Isaac        immortal.    nothing at birth.       parents = [Abraham, Sarah].

Timeline over 6 ticks (everyone born at tick 0, so age == tick):

    tick 3   Abraham (age 3/3) dies -> Isaac inherits GOLD 100 + USD 500.
    tick 4   Cain    (age 4/4) dies -> BURNS BRONZE 20 + USD 100 (no heir).
    tick 5   Sarah   (age 5/5) dies -> Isaac inherits SILVER 50 + USD 300.

At the end Isaac -- the immortal child of Abraham and Sarah -- holds the
consolidated wealth of two founders; Cain's wealth is gone (burned). Money
supply: 900 -> 800 (100 burned with the heirless Cain). No issuance
anywhere: inheritance is a transfer, burning is a deletion.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from econengine import markets, services
from econengine.conditions import set_estate_rule
from econengine.models import Entity, EntityType


# Lifespans (in ticks). NULL/None = immortal -- the default, opt-in.
ABRAHAM_LIFESPAN = 3
SARAH_LIFESPAN = 5
CAIN_LIFESPAN = 4

# Founding wealth (goods are just holdings -- no Good row required, as in
# the population demo's sex holdings).
ABRAHAM_GOLD = Decimal("100")
ABRAHAM_USD = Decimal("500")
SARAH_SILVER = Decimal("50")
SARAH_USD = Decimal("300")
CAIN_BRONZE = Decimal("20")
CAIN_USD = Decimal("100")


@dataclass
class Person:
    """A cast member pre-seeded at genesis."""
    name: str
    lifespan: Optional[int]          # None = immortal
    holdings: dict[str, Decimal] = field(default_factory=dict)
    usd: Decimal = Decimal("0")
    heir: Optional[str] = None       # name of heir, or None (burn)
    parents: tuple[str, ...] = ()    # names of parents (lineage)


@dataclass
class World:
    """Handles the run/test harness needs to observe state after each tick."""
    gov: Entity
    people: dict                     # name -> Entity


_CAST = [
    Person("Abraham", ABRAHAM_LIFESPAN, {"GOLD": ABRAHAM_GOLD}, ABRAHAM_USD,
           heir="Isaac"),
    Person("Sarah", SARAH_LIFESPAN, {"SILVER": SARAH_SILVER}, SARAH_USD,
           heir="Isaac"),
    Person("Cain", CAIN_LIFESPAN, {"BRONZE": CAIN_BRONZE}, CAIN_USD,
           heir=None),
    # The immortal heir: parents link him to the founding couple (the bridge
    # to 6c), but he holds nothing and has no account until he inherits one.
    Person("Isaac", None, {}, Decimal("0"), parents=("Abraham", "Sarah")),
]


def build_economy(session: Session) -> World:
    """The Government (which sets the estate rule) + four people: three
    mortal founders with wealth and (for two) a designated heir, and one
    immortal heir whose parents close the lineage. No scripts, no accounts
    on the Government, no capabilities -- death is a pass, not an act."""
    gov = services.create_entity(session, "Government", EntityType.GOVERNMENT)
    # The estate rule is votable WorldSetting data. In a full world a
    # governance vote would set it; here it is set directly (the same way
    # population set marriage). "heir" transfers to heir_id; a missing
    # heir_id falls back to burn (Cain demonstrates this).
    set_estate_rule(session, "heir")

    # Pass 1: create everyone at tick 0 (so age == tick), stamp lifespan.
    people: dict[str, Entity] = {}
    for m in _CAST:
        entity = services.create_entity(session, m.name, EntityType.INDIVIDUAL)
        entity.birth_tick = 0
        entity.lifespan = m.lifespan  # None = immortal (Isaac, the Government)
        for symbol, qty in m.holdings.items():
            markets.adjust_holding(session, entity, symbol, qty)
        if m.usd > 0:
            services.create_account(session, entity, "USD", initial_balance=m.usd)
        people[m.name] = entity

    # Pass 2: wire provenance -- heir_id and parents reference other entities,
    # so they are resolved after all exist. These are immutable once stamped
    # (the same provenance pattern as birth_tick / parents / lifespan).
    for m in _CAST:
        entity = people[m.name]
        if m.heir is not None:
            entity.heir_id = people[m.heir].id
        if m.parents:
            entity.parents = [people[p].id for p in m.parents]

    session.flush()
    return World(gov=gov, people=people)
