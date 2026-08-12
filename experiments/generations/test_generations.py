"""Smoke test for the generations demo (Step 6d proving experiment).

Asserts that death-by-old-age and inheritance actually drove the design
points, on structured records and live state (not stdout). This is the
machine-checkable half of the "prove the mechanism" deliverable; the
human-readable half is ``python -m experiments.generations.run``.

What it pins down:
  * ON-SCHEDULE DEATH     each founder dies at the tick its age reaches its
                         stamped ``lifespan`` (3, 4, 5); none before.
  * THE EVENT            ``entity_incapacitated`` with ``condition: "age"``
                         -- the only new signal; same shape as a starvation
                         death.
  * INHERITANCE          the "heir" estate rule + ``heir_id`` transfer each
                         founder's estate to Isaac (goods + money).
  * THE BURN FALLBACK    the heirless founder (Cain) burns -- supply shrinks.
  * WEALTH CONSOLIDATION Isaac ends with both founders' goods + money.
  * MONEY CONSERVATION   inheritance is a transfer; burning is a deletion.
  * LINEAGE              the 6c queries close the cycle: Isaac is the child
                         of Abraham and Sarah.
  * NO SCRIPTS           the world has zero Script rows -- death is an engine
                         pass, not an act. (The absence IS the assertion.)
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econengine.markets import get_holding
from econengine.models import Account, Base, Entity, EntityStatus, Script
from econengine.scripting import build_queries

from .run import simulate
from .scenario import (
    ABRAHAM_GOLD, ABRAHAM_LIFESPAN, ABRAHAM_USD,
    CAIN_BRONZE, CAIN_LIFESPAN, CAIN_USD,
    SARAH_SILVER, SARAH_LIFESPAN, SARAH_USD,
    build_economy,
)


# ---------------------------------------------------------------------------
# fixtures -- sessions stay open so tests can inspect the live world
# ---------------------------------------------------------------------------

@pytest.fixture
def sim():
    """A run world after 6 ticks (all three deaths have happened). Session
    stays open for live-state inspection."""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = Session(engine)
    world = build_economy(session)
    session.commit()
    records = simulate(session, world, 6)
    yield session, world, records
    session.close()


def _deaths_at(records, tick):
    return records[tick - 1]["deaths"]


def _holding_qty(session, entity_id, symbol):
    h = get_holding(session, entity_id, symbol)
    return h.quantity if h is not None else Decimal("0")


def _balance(session, entity_id, currency="USD"):
    acct = session.execute(
        select(Account).where(Account.entity_id == entity_id,
                              Account.currency == currency)
    ).scalars().first()
    return acct.balance if acct is not None else Decimal("0")


def _total_money(session):
    return session.execute(
        select(Account.balance).where(Account.balance != 0)
    ).scalars().all() or [Decimal("0")]


# ---------------------------------------------------------------------------
# on-schedule death (the core: lifespan -> death at age == lifespan)
# ---------------------------------------------------------------------------

def test_no_one_dies_before_their_lifespan(sim):
    _, _, records = sim
    assert _deaths_at(records, 1) == []   # everyone age 1
    assert _deaths_at(records, 2) == []   # age 2


def test_abraham_dies_at_lifespan_3(sim):
    _, world, records = sim
    (abraham,) = _deaths_at(records, 3)
    assert abraham["name"] == "Abraham"
    assert abraham["condition"] == "age"
    assert abraham["age"] == "3" and abraham["lifespan"] == "3"
    assert world.people["Abraham"].status == EntityStatus.INCAPACITATED


def test_cain_dies_at_lifespan_4(sim):
    _, world, records = sim
    (cain,) = _deaths_at(records, 4)
    assert cain["name"] == "Cain" and cain["condition"] == "age"
    assert cain["age"] == "4" and cain["lifespan"] == "4"


def test_sarah_dies_at_lifespan_5(sim):
    _, world, records = sim
    (sarah,) = _deaths_at(records, 5)
    assert sarah["name"] == "Sarah" and sarah["condition"] == "age"
    assert sarah["age"] == "5" and sarah["lifespan"] == "5"


# ---------------------------------------------------------------------------
# the event shape (same as a starvation death; "age" is the only new label)
# ---------------------------------------------------------------------------

def test_death_event_is_entity_incapacitated(sim):
    """The age pass reuses the canonical incapacity event verbatim."""
    session, world, _ = sim
    from econengine.models import Tick
    events = session.query(Tick).filter_by(number=3).one().events
    death = next(e for e in events if e["type"] == "entity_incapacitated"
                 and e["entity_id"] == world.people["Abraham"].id)
    assert death["type"] == "entity_incapacitated"
    assert death["condition"] == "age"
    assert death["quantity"] == "3"
    assert death["threshold"] == "3"
    assert death["estate_policy"] == "heir"
    assert death["recipient_id"] == world.people["Isaac"].id


# ---------------------------------------------------------------------------
# inheritance (the "heir" estate rule + heir_id)
# ---------------------------------------------------------------------------

def test_heir_inherits_abrahams_gold_and_money(sim):
    session, world, _ = sim
    isaac = world.people["Isaac"]
    # Abraham's GOLD(100) arrived at Isaac's tick-3 death. Money accumulates
    # as later founders die too -- tested precisely in the consolidation test;
    # here we confirm the GOOD transfer (Abraham's unique holding).
    assert _holding_qty(session, isaac.id, "GOLD") == ABRAHAM_GOLD
    assert _balance(session, isaac.id) >= ABRAHAM_USD


def test_heir_inherits_sarahs_silver_and_money(sim):
    session, world, _ = sim
    isaac = world.people["Isaac"]
    assert _holding_qty(session, isaac.id, "SILVER") == SARAH_SILVER
    assert _balance(session, isaac.id) == ABRAHAM_USD + SARAH_USD


def test_isaac_consolidates_generational_wealth(sim):
    """At the end Isaac holds both founders' goods and money."""
    session, world, _ = sim
    isaac = world.people["Isaac"]
    assert _holding_qty(session, isaac.id, "GOLD") == ABRAHAM_GOLD
    assert _holding_qty(session, isaac.id, "SILVER") == SARAH_SILVER
    assert _balance(session, isaac.id) == ABRAHAM_USD + SARAH_USD


def test_dead_founders_holdings_zeroed(sim):
    session, world, _ = sim
    for name, symbol in (("Abraham", "GOLD"), ("Sarah", "SILVER"),
                         ("Cain", "BRONZE")):
        assert _holding_qty(session, world.people[name].id, symbol) == Decimal("0")


# ---------------------------------------------------------------------------
# the burn fallback (no heir_id -> burn; supply shrinks)
# ---------------------------------------------------------------------------

def test_heirless_cain_burns(sim):
    """Cain has no heir_id; under the 'heir' rule the estate falls back to
    burn. His wealth is gone, not inherited."""
    _, world, records = sim
    (cain,) = _deaths_at(records, 4)
    assert cain["estate_policy"] == "burn"
    assert cain["recipient"] is None
    assert Decimal(cain["money_burned"]) == CAIN_USD
    assert Decimal(cain["goods_burned"]) == CAIN_BRONZE


def test_burn_shrinks_the_money_supply(sim):
    """Burning deletes money (supply shrinks); inheriting conserves it.
    Started 900; Abraham+Sarah's 800 transferred to Isaac, Cain's 100 burned."""
    session, _, _ = sim
    total = sum(_total_money(session))
    assert total == ABRAHAM_USD + SARAH_USD   # 800; Cain's 100 is gone


def test_inheritance_is_a_transfer_not_issuance(sim):
    """No money was created: Isaac's balance == exactly what the two
    founders had. The heir rule moves existing money, never issues."""
    session, world, _ = sim
    isaac = world.people["Isaac"]
    assert _balance(session, isaac.id) == ABRAHAM_USD + SARAH_USD


# ---------------------------------------------------------------------------
# population + status
# ---------------------------------------------------------------------------

def test_population_declines_as_founders_die(sim):
    """5 (gov + 4) -> 4 (Abraham) -> 3 (Cain) -> 2 (Sarah); holds at 2."""
    _, _, records = sim
    assert [r["population"] for r in records] == [5, 5, 4, 3, 2, 2]


def test_isaac_survives_and_founders_are_dead(sim):
    session, world, _ = sim
    assert world.people["Isaac"].status == EntityStatus.ACTIVE
    assert world.gov.status == EntityStatus.ACTIVE
    for name in ("Abraham", "Sarah", "Cain"):
        assert world.people[name].status == EntityStatus.INCAPACITATED


def test_immortal_isaac_never_dies():
    """Run well past the founders' lifespans: Isaac (NULL lifespan) never
    dies of old age."""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = Session(engine)
    world = build_economy(session)
    session.commit()
    simulate(session, world, 20)
    assert world.people["Isaac"].status == EntityStatus.ACTIVE
    session.close()


# ---------------------------------------------------------------------------
# observability: ctx.query.lifespan (the one new read)
# ---------------------------------------------------------------------------

def test_lifespan_query_reads_each_entity(sim):
    session, world, _ = sim
    q = build_queries(session, 6)
    assert q["lifespan"](world.people["Abraham"].id) == ABRAHAM_LIFESPAN
    assert q["lifespan"](world.people["Sarah"].id) == SARAH_LIFESPAN
    assert q["lifespan"](world.people["Cain"].id) == CAIN_LIFESPAN
    assert q["lifespan"](world.people["Isaac"].id) is None   # immortal
    assert q["lifespan"](world.gov.id) is None               # immortal


def test_age_query_matches_ctx_tick(sim):
    """age() is consistent with the tick a POLICY would see as ctx.tick."""
    session, world, _ = sim
    q = build_queries(session, 6)
    assert q["age"](world.people["Isaac"].id) == 6   # born tick 0


# ---------------------------------------------------------------------------
# lineage closes the cycle 6c opened
# ---------------------------------------------------------------------------

def test_isaac_is_the_child_of_abraham_and_sarah(sim):
    session, world, _ = sim
    abraham, sarah, isaac = (world.people["Abraham"], world.people["Sarah"],
                             world.people["Isaac"])
    q = build_queries(session, 6)
    assert set(q["parents"](isaac.id)) == {abraham.id, sarah.id}
    assert isaac.id in q["children"](abraham.id)
    assert isaac.id in q["children"](sarah.id)


def test_provenance_is_immutable_data(sim):
    """Isaac's parents were stamped at genesis and read back exactly."""
    session, world, _ = sim
    isaac = world.people["Isaac"]
    abraham, sarah = world.people["Abraham"], world.people["Sarah"]
    assert isaac.parents == [abraham.id, sarah.id]


# ---------------------------------------------------------------------------
# the absence IS the assertion: no scripts (death is a pass, not an act)
# ---------------------------------------------------------------------------

def test_the_world_has_no_scripts(sim):
    """6d adds no Lua action. This world has zero Script rows -- death is an
    invariant engine pass, unlike 6c's spawn which needed a midwife POLICY."""
    session, _, _ = sim
    assert session.query(Script).count() == 0


def test_heir_id_is_per_entity_provenance(sim):
    """Abraham and Sarah both designated Isaac; Cain designated no one.
    heir_id is stamped at genesis (immutable provenance), not scribbleable."""
    session, world, _ = sim
    isaac = world.people["Isaac"]
    assert world.people["Abraham"].heir_id == isaac.id
    assert world.people["Sarah"].heir_id == isaac.id
    assert world.people["Cain"].heir_id is None
