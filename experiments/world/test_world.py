"""Proving tests for the Phase 0 "world" content-pack experiment.

Two kinds, exactly as docs/game.md §12.7 prescribes:

  * LIVE-RUN tests build the full cast and advance real ticks, asserting the
    substrate holds together: the population survives, the ORE->IRON chain
    flows across markets, money is conserved, no script errors.

  * FOCUSED feature tests stand up the content pack WITHOUT the cast and
    exercise one Recipe feature in isolation -- tech-gating, multi-tick
    flows, construction, research/unlock, deposits, capital goods, and the
    ENTITY/WORLD scope distinction -- so every declared recipe is proven
    even though only three live in the survival run.
"""

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from econengine import markets, parcels, production, services, tech
from econengine.models import (
    Base, Entity, EntityStatus, EntityType, Holding, Script, ScriptType, Tick,
)
from econengine.tech import TechScope
from econengine.tick import run_tick

from experiments.world.scenario import (
    MONEY_SUPPLY, PROVING_UPKEEP_BUFFER, UPKEEP_BUFFER, UPKEEP_RATE,
    _behaviour, build_economy, create_content,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _hold(session, entity_id, symbol):
    qty = session.execute(
        select(Holding.quantity)
        .where(Holding.entity_id == entity_id, Holding.symbol == symbol)
    ).scalar_one_or_none()
    return qty if qty is not None else Decimal("0")


def _balance(session, entity):
    return session.get(Entity, entity.id).accounts[0].balance


def _tick_events(session, tick_number):
    tick = session.execute(
        select(Tick).where(Tick.number == tick_number)
    ).scalar_one_or_none()
    return (tick.events or []) if tick else []


def _all_events(session):
    out = []
    for tick in session.execute(select(Tick).order_by(Tick.number)).scalars():
        out.extend(tick.events or [])
    return out


def _run(session, ticks):
    for _ in range(ticks):
        run_tick(session)
        session.commit()


def _worker(session, name, usd=Decimal("200"), grain=Decimal("50"),
             iron=PROVING_UPKEEP_BUFFER, entity_type=EntityType.INDIVIDUAL):
    """A solvent entity with no script -- a clean test subject for focused
    recipe tests. INDIVIDUALs auto-issue LABOR and carry GRAIN and IRON
    buffers so FOOD and UPKEEP stay met (pass iron=0 when the test measures
    IRON itself); use a BUSINESS for tests whose OUTPUT is itself a FOOD
    satisfier (FLOUR/BREAD/GRAIN), so the consumption pass does not eat the
    thing being measured."""
    entity = services.create_entity(session, name, entity_type)
    services.create_account(session, entity, "USD", initial_balance=usd)
    if entity_type == EntityType.INDIVIDUAL:
        markets.adjust_holding(session, entity, "GRAIN", grain)
        markets.adjust_holding(session, entity, "IRON", iron)
    return entity


def _grant(session, entity, code):
    tech.grant_unlock(session, entity, tech.get_technology(session, code), 0)


# ===========================================================================
# LIVE-RUN TESTS -- the proving economy
# ===========================================================================

def test_builds_and_runs_clean(session):
    """The world builds and advances ticks with no script errors."""
    world = build_economy(session)
    session.commit()
    _run(session, 40)
    errors = [e for e in _all_events(session) if e.get("type") == "script_error"]
    assert errors == [], f"script errors: {[e.get('error') for e in errors][:3]}"


def test_population_survives(session):
    """The core robustness claim: every entity stays ACTIVE, HUNGER never
    accumulates -- survival does not depend on delicate market calibration."""
    world = build_economy(session)
    session.commit()
    _run(session, 40)
    for entity in (world.farmer, world.miner, world.smith):
        assert session.get(Entity, entity.id).status == EntityStatus.ACTIVE
        assert _hold(session, entity.id, "HUNGER") < Decimal("1")


def test_food_need_fully_met(session):
    """FOOD satisfaction is high throughout -- nobody goes hungry."""
    world = build_economy(session)
    session.commit()
    _run(session, 40)
    # Re-derive satisfaction: a HUNGER holding near zero means FOOD was met
    # (HUNGER is granted only on shortfall, scaled by it).
    for entity in (world.farmer, world.miner, world.smith):
        assert _hold(session, entity.id, "HUNGER") == Decimal("0")


def test_proving_run_inert_to_upkeep(session):
    """The proving cast's 30-IRON buffer covers all 40 ticks: DISREPAIR
    never accrues and every UPKEEP tick is met, so the sink changes
    nothing about the proving run's balanced-by-construction story."""
    world = build_economy(session)
    session.commit()
    _run(session, 40)
    for entity in (world.farmer, world.miner, world.smith):
        assert _hold(session, entity.id, "DISREPAIR") == Decimal("0")
    unmet = [e for e in _all_events(session) if e.get("type") == "need_unmet"]
    assert unmet == []


# ===========================================================================
# THE DEMAND SINK -- UPKEEP burns IRON so the ORE->IRON chain has a customer
# ===========================================================================

def test_upkeep_consumes_iron_per_tick(session):
    """UPKEEP draws exactly UPKEEP_RATE IRON per tick from every INDIVIDUAL,
    and a fully-covered entity accrues no DISREPAIR."""
    create_content(session)
    worker = _worker(session, "Smith", grain=Decimal("50"))
    start = _hold(session, worker.id, "IRON")
    _run(session, 2)
    assert start - _hold(session, worker.id, "IRON") == 2 * UPKEEP_RATE
    assert _hold(session, worker.id, "DISREPAIR") == Decimal("0")


def test_upkeep_shortfall_credits_disrepair(session):
    """No IRON at all: each tick grants 1 DISREPAIR (the HUNGER shape),
    scaled by the shortfall -- half-covered grants half. Decay lands the
    same tick (consumption, then decay), so a granted 1 reads 0.95."""
    create_content(session)
    broke = _worker(session, "Luddite", grain=Decimal("50"), iron=Decimal("0"))
    _run(session, 1)
    assert _hold(session, broke.id, "DISREPAIR") == Decimal("0.95")
    events = _tick_events(session, 1)
    assert {e["type"] for e in events if e.get("need")} == {
        "need_satisfied", "need_unmet"}   # FOOD met, UPKEEP not
    half = _worker(session, "Halfstock", grain=Decimal("50"),
                   iron=UPKEEP_RATE / 2)
    _run(session, 1)
    assert _hold(session, half.id, "DISREPAIR") == Decimal("0.475")


def test_house_seats_carry_the_upkeep_buffer(session):
    """make_house endows each symmetric seat with UPKEEP_BUFFER IRON --
    one round of grace, the iron analog of FOOD_BUFFER."""
    create_content(session)
    from experiments.world.scenario import make_house
    house = make_house(session, "House Test")
    assert _hold(session, house.id, "IRON") == UPKEEP_BUFFER


def test_industrial_chain_runs(session):
    """ORE is mined and IRON is smelted -- the multi-stage chain flows."""
    world = build_economy(session)
    session.commit()
    _run(session, 40)
    # Smith has accumulated IRON (sold none -- no buyer), proving smelting ran.
    assert _hold(session, world.smith.id, "IRON") >= Decimal("10")


def test_ore_crosses_market(session):
    """The Miner sells ORE and the Smith buys it -- production crosses a
    market (the key substrate claim)."""
    world = build_economy(session)
    session.commit()
    _run(session, 40)
    ore_trades = [
        e for e in _all_events(session)
        if e.get("type") == "trade" and e.get("market") == "ORE"
    ]
    assert ore_trades, "no ORE trades -- the ore market never cleared"


def test_food_market_clears(session):
    """The Farmer sells GRAIN and the buyers buy -- the food market clears."""
    world = build_economy(session)
    session.commit()
    _run(session, 40)
    grain_trades = [
        e for e in _all_events(session)
        if e.get("type") == "trade" and e.get("market") == "GRAIN"
    ]
    assert grain_trades, "no GRAIN trades -- the food market never cleared"


def test_money_is_conserved(session):
    """No monetary authority mints or burns during the run: the sum of all
    USD balances stays at the genesis endowment."""
    world = build_economy(session)
    session.commit()
    _run(session, 40)
    from sqlalchemy import func
    from econengine.models import Account
    total = session.execute(
        select(func.coalesce(func.sum(Account.balance), 0)).where(Account.currency == "USD")
    ).scalar_one()
    assert total == MONEY_SUPPLY


def test_deposit_depletes_and_regenerates(session):
    """The Miner draws down the ORE seam; regen keeps it from running dry."""
    from econengine.models import Parcel
    world = build_economy(session)
    session.commit()
    parcel = session.execute(
        select(Parcel).where(Parcel.owner_id == world.miner.id)
    ).scalar_one()
    _run(session, 40)
    deposit = parcels.get_deposit(session, parcel.id, "ORE")
    assert deposit is not None
    # Drawn down from the genesis 100 but regen (2/tick) kept it alive.
    assert deposit.quantity < Decimal("100")
    assert deposit.quantity > Decimal("0")


# ===========================================================================
# FOCUSED FEATURE TESTS -- the declared recipe graph, in isolation
# ===========================================================================

def test_smelting_tech_gate(session):
    """SMELT_IRON is refused without the SMELTING unlock, allowed with it."""
    create_content(session)
    worker = _worker(session, "Smelter")
    parcel = parcels.create_parcel(session, "LAND", owner=worker)
    parcels.add_facility(session, parcel, "FORGE")
    markets.adjust_holding(session, worker, "ORE", Decimal("2"))
    markets.adjust_holding(session, worker, "LABOR", Decimal("2"))
    with pytest.raises(ValueError, match="SMELTING"):
        production.start_process(session, worker, "SMELT_IRON", parcel.id)
    _grant(session, worker, "SMELTING")
    proc = production.start_process(session, worker, "SMELT_IRON", parcel.id)
    assert proc.recipe.code == "SMELT_IRON"


def test_milling_tech_gate(session):
    """MILL_FLOUR requires the MILLING unlock and a MILL facility."""
    create_content(session)
    worker = _worker(session, "Miller", entity_type=EntityType.BUSINESS)
    parcel = parcels.create_parcel(session, "LAND", owner=worker)
    parcels.add_facility(session, parcel, "MILL")
    markets.adjust_holding(session, worker, "GRAIN", Decimal("10"))
    with pytest.raises(ValueError, match="MILLING"):
        production.start_process(session, worker, "MILL_FLOUR", parcel.id)
    _grant(session, worker, "MILLING")
    production.start_process(session, worker, "MILL_FLOUR", parcel.id)
    _run(session, 3)
    assert _hold(session, worker.id, "FLOUR") >= Decimal("1")


def test_make_steel_multitick_flow(session):
    """MAKE_STEEL is a multi-tick, flow-fed recipe (per_tick LABOR): it
    completes after its duration and credits STEEL."""
    create_content(session)
    worker = _worker(session, "Steelsmith")
    _grant(session, worker, "SMELTING")
    _grant(session, worker, "STEELMAKING")
    parcel = parcels.create_parcel(session, "LAND", owner=worker)
    parcels.add_facility(session, parcel, "FORGE")
    markets.adjust_holding(session, worker, "IRON", Decimal("2"))
    markets.adjust_holding(session, worker, "COAL", Decimal("1"))
    markets.adjust_holding(session, worker, "LABOR", Decimal("5"))
    production.start_process(session, worker, "MAKE_STEEL", parcel.id)
    _run(session, 4)
    assert _hold(session, worker.id, "STEEL") >= Decimal("2")


def test_build_forge_construction(session):
    """BUILD_FORGE erects a FORGE on the bound parcel; good_requirements
    (TOOLS) are held, not consumed."""
    create_content(session)
    worker = _worker(session, "Builder")
    parcel = parcels.create_parcel(session, "LAND", owner=worker)
    markets.adjust_holding(session, worker, "STONE", Decimal("4"))
    markets.adjust_holding(session, worker, "TOOLS", Decimal("1"))
    markets.adjust_holding(session, worker, "LABOR", Decimal("5"))
    production.start_process(session, worker, "BUILD_FORGE", parcel.id)
    _run(session, 5)
    from econengine.models import Parcel
    refreshed = session.get(Parcel, parcel.id)
    assert "FORGE" in [f.facility_type for f in refreshed.facilities]
    # TOOLS was a good_requirement (held, not consumed).
    assert _hold(session, worker.id, "TOOLS") >= Decimal("1")


def test_research_grants_world_unlock(session):
    """RESEARCH_STEEL (an unlocks-only recipe) grants STEELMAKING on
    completion -- the research path, output-is-an-unlock."""
    create_content(session)
    worker = _worker(session, "Researcher")
    _grant(session, worker, "SMELTING")  # RESEARCH_STEEL requires it
    markets.adjust_holding(session, worker, "LABOR", Decimal("8"))
    production.start_process(session, worker, "RESEARCH_STEEL")
    steelmaking = tech.get_technology(session, "STEELMAKING")
    assert not tech.has_unlock(session, worker.id, steelmaking)
    _run(session, 7)
    assert tech.has_unlock(session, worker.id, steelmaking)


def test_mine_coal_depletes_deposit(session):
    """MINE_COAL draws from the parcel's COAL deposit (extraction), crediting
    COAL to the miner at completion."""
    create_content(session)
    worker = _worker(session, "CoalMiner")
    parcel = parcels.create_parcel(session, "LAND", owner=worker)
    parcels.add_deposit(
        session, parcel, "COAL", Decimal("20"),
        capacity=Decimal("20"), regen_per_tick=Decimal("0"),
    )
    markets.adjust_holding(session, worker, "LABOR", Decimal("2"))
    production.start_process(session, worker, "MINE_COAL", parcel.id)
    # The deposit is drawn immediately at start.
    assert parcels.get_deposit(session, parcel.id, "COAL").quantity == Decimal("18")
    _run(session, 3)
    assert _hold(session, worker.id, "COAL") >= Decimal("2")


def test_make_tools_capital_good(session):
    """MAKE_TOOLS consumes IRON+STEEL and produces TOOLS (a capital good).
    iron=0: UPKEEP would otherwise nibble the buffer and blur the assert."""
    create_content(session)
    worker = _worker(session, "Toolmaker", iron=Decimal("0"))
    _grant(session, worker, "SMELTING")
    _grant(session, worker, "TOOLMAKING")
    markets.adjust_holding(session, worker, "IRON", Decimal("1"))
    markets.adjust_holding(session, worker, "STEEL", Decimal("1"))
    production.start_process(session, worker, "MAKE_TOOLS")
    _run(session, 4)
    assert _hold(session, worker.id, "TOOLS") >= Decimal("1")
    # Inputs were consumed.
    assert _hold(session, worker.id, "IRON") < Decimal("1")


def test_world_vs_entity_scope(session):
    """SMELTING (WORLD) granted to one entity is held by all; FARMING
    (ENTITY) granted to one is held only by that one."""
    create_content(session)
    a = _worker(session, "A")
    b = _worker(session, "B")
    smelting = tech.get_technology(session, "SMELTING")
    farming = tech.get_technology(session, "FARMING")
    assert smelting.scope == TechScope.WORLD
    assert farming.scope == TechScope.ENTITY
    _grant(session, a, "SMELTING")
    assert tech.has_unlock(session, a.id, smelting)
    assert tech.has_unlock(session, b.id, smelting)     # world -> everyone
    _grant(session, a, "FARMING")
    assert tech.has_unlock(session, a.id, farming)
    assert not tech.has_unlock(session, b.id, farming)  # entity -> only A


def test_labor_market_clears(session):
    """A focused two-entity trade: one sells LABOR, another buys, the auction
    clears and the buyer receives it."""
    create_content(session)
    seller = _worker(session, "Seller")
    buyer = _worker(session, "Buyer")
    markets.adjust_holding(session, seller, "LABOR", Decimal("5"))
    markets.place_order(
        session, seller.id, "LABOR", "sell",
        Decimal("3"), Decimal("1"), seller.accounts[0].id,
    )
    markets.place_order(
        session, buyer.id, "LABOR", "buy",
        Decimal("3"), Decimal("5"), buyer.accounts[0].id,
    )
    events = markets.run_auctions(session, tick_number=1)
    trades = [e for e in events if e.get("type") == "trade"]
    assert trades, "no trade cleared"
    assert _hold(session, buyer.id, "LABOR") >= Decimal("1")


def test_starter_template_survives(session):
    """A LONE entity endowed with a farm and the starter template survives
    indefinitely on its own -- the defensive default works unmodified."""
    create_content(session)
    loner = services.create_entity(session, "Loner", EntityType.INDIVIDUAL)
    services.create_account(session, loner, "USD", initial_balance=Decimal("100"))
    markets.adjust_holding(session, loner, "GRAIN", Decimal("5"))
    parcel = parcels.create_parcel(session, "LAND", owner=loner)
    parcels.add_facility(session, parcel, "FARM")
    _grant(session, loner, "FARMING")
    session.add(Script(
        name="loner-behaviour",
        script_type=ScriptType.BEHAVIOUR,
        source=_behaviour("starter.lua"),
        entity_id=loner.id,
        timeout_ms=200,
        state={},
    ))
    session.flush()
    _run(session, 30)
    assert session.get(Entity, loner.id).status == EntityStatus.ACTIVE
    assert _hold(session, loner.id, "HUNGER") < Decimal("5")
