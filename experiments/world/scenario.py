"""Genesis for the "world" content-pack experiment (Phase 0, game.md §12).

Builds a richer single-region economy directly on econengine (no HTTP):
a multi-stage industrial chain (ORE -> IRON, gated by a tech tree) running
across markets beside a food chain (GRAIN), with needs, deposits, and
facilities. **No engine change** -- this is data + Lua, the substrate every
later phase of the game is built on.

THE CAST (three specialist INDIVIDUALs; survival is robust by design):

  Farmer   runs starter.lua: FARM_GRAIN (1 LABOR -> 4 GRAIN) on a FARM,
           self-feeds and sells the surplus -- the food market's supply side.
  Miner    runs miner.lua:   MINE_ORE (1 LABOR + a drawn seam -> 2 ORE),
           sells ORE to the Smith -- extraction / deposits.
  Smith    runs smith.lua:   SMELT_IRON (2 ORE + 1 LABOR -> 2 IRON), gated by
           the SMELTING tech and a FORGE, buying ORE across the market --
           the tech-gated, multi-stage node that proves the substrate.

Per tick the Miner produces exactly the 2 ORE the Smith consumes, and the
Farmer's surplus covers both buyers' food: the chain is balanced by
construction, so the proving run survives without the heroic calibration a
self-organising uniform population would demand (the lesson of
experiments/inequality). Money is a genesis endowment, conserved by trade.

The full recipe graph (MILL_FLOUR, BAKE_BREAD, MINE_COAL, QUARRY_STONE,
MAKE_STEEL, MAKE_TOOLS, BUILD_FORGE, RESEARCH_STEEL) is DECLARED here so the
content pack is complete, and exercised in ISOLATION by the focused feature
tests rather than entangling the survival run.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from econengine import goods, markets, needs, parcels, production, services, tech
from econengine.models import Entity, EntityType, Script, ScriptType
from econengine.tech import TechScope

_LUA_DIR = Path(__file__).parent / "lua"

# --- Money -----------------------------------------------------------------
# A genesis endowment; no monetary authority is needed because nothing mints
# or burns during the run (trades and transfers conserve it). Asserted.
FARMER_USD = Decimal("500")
MINER_USD = Decimal("500")
SMITH_USD = Decimal("1000")
MONEY_SUPPLY = FARMER_USD + MINER_USD + SMITH_USD

# --- The survival buffer ---------------------------------------------------
# FARM_GRAIN is duration-1, so a Farmer has no home-grown food on tick 1;
# every entity carries a few GRAIN so the first tick's consumption pass does
# not trip HUNGER before trade begins. Bootstrap insurance, not a crutch:
# survival past the buffer depends on the food market clearing (it does --
# food is cheap to produce: the Farmer's LABOR is auto-issued and free).
FOOD_BUFFER = Decimal("5")

# --- The ORE seam ----------------------------------------------------------
# The Miner draws 2 ORE/tick against regen of 2: a single mine is exactly
# sustainable, and a second on the same parcel would not be (the inequality
# pattern for land quality biting). Capacity 100 is plenty for a proving run.
ORE_QUANTITY = Decimal("100")
ORE_CAPACITY = Decimal("100")
ORE_REGEN = Decimal("2")

# --- Run shape -------------------------------------------------------------
DEFAULT_TICKS = 40


def _read_lua(name: str) -> str:
    return (_LUA_DIR / name).read_text()


def _behaviour(name: str) -> str:
    """Behaviour source is prelude + role script (the sandbox has no
    `require`, so shared helpers are prepended)."""
    return _read_lua("prelude.lua") + "\n" + _read_lua(name)


@dataclass
class World:
    """Handles the run/test harness needs to observe state after each tick."""
    farmer: Entity
    miner: Entity
    smith: Entity
    farmer_account_id: str
    miner_account_id: str
    smith_account_id: str
    individuals: list[Entity] = field(default_factory=list)
    clerk: Entity | None = None   # the polity (server-owned), Phase 2b


def create_content(session: Session) -> None:
    """Goods, tech, recipes, needs, markets -- the world's "physics".

    Separable from ``build_economy`` so the focused feature tests can stand
    up the content pack without the cast, then exercise one recipe in
    isolation. Idempotent-enough for a single genesis call (does not guard
    against double-create).
    """
    _create_goods(session)
    _create_tech(session)
    _create_recipes(session)
    _create_needs(session)
    _create_markets(session)


def build_economy(session: Session) -> World:
    """Content pack + the three-entity cast + their behaviour scripts."""
    create_content(session)

    farmer = _make_farmer(session)
    miner = _make_miner(session)
    smith = _make_smith(session)
    clerk = make_clerk(session)

    individuals = [farmer, miner, smith]
    _wire_scripts(session, farmer, miner, smith)

    session.flush()
    return World(
        farmer=farmer,
        miner=miner,
        smith=smith,
        farmer_account_id=farmer.accounts[0].id,
        miner_account_id=miner.accounts[0].id,
        smith_account_id=smith.accounts[0].id,
        individuals=individuals,
        clerk=clerk,
    )


# ---------------------------------------------------------------------------
# The clerk -- governance windows land on the content pack (Phase 2b, §14.4)
# ---------------------------------------------------------------------------

def make_clerk(session: Session) -> Entity:
    """The server-owned polity: the government proposals target.

    It holds the enactment capability for both tiers (LEGISLATE,
    AMEND_CONSTITUTION) plus the operating capability its ordinary law
    exercises (SET_FISCAL_POLICY) -- operator fiat at content time:
    capabilities arrive only by grant, and the operator is the genesis
    grantor; from then on they are ordinary grantable data (§8). Its
    POLICY script (clerk.lua) reads `round.state` and, on window rounds,
    enacts the docket via the ordinary `enact` intent. Enactment is the
    clerk's job: *when laws pass* is policy, *how* is mechanism, and no
    engine surface was added to make either possible.

    Server-owned (owner_id stays None): no player may rewrite the polity
    by the autonomy path, and it is not `is_fixed` -- the polity's own
    governed surfaces (set_script via proposal->enact) remain law, as for
    any government.
    """
    from econengine import capabilities as _capabilities

    clerk = services.create_entity(session, "Assembly", EntityType.GOVERNMENT)
    clerk.capabilities = [
        _capabilities.LEGISLATE,
        _capabilities.AMEND_CONSTITUTION,
        _capabilities.SET_FISCAL_POLICY,
    ]
    session.add(Script(
        name=f"clerk-policy-{clerk.id}",
        description="Governance-window clerk: sweep the docket on window close",
        script_type=ScriptType.POLICY,
        source=_read_lua("clerk.lua"),
        entity_id=clerk.id,
        timeout_ms=100,
        state={},
    ))
    return clerk


# ---------------------------------------------------------------------------
# Content pack
# ---------------------------------------------------------------------------

def _create_goods(session: Session) -> None:
    # LABOR is auto-issued to every INDIVIDUAL each tick (the inequality
    # pattern). Partial decay lets a market-bought unit survive long enough
    # for the buyer's next script to use it.
    goods.create_good(
        session, "LABOR",
        decay_per_tick=Decimal("0.5"),
        auto_issue_quantity=Decimal("1"),
        auto_issue_entity_type=EntityType.INDIVIDUAL,
    )
    # Food chain (all perishable; all satisfy FOOD).
    goods.create_good(session, "GRAIN", decay_per_tick=Decimal("0.2"))
    goods.create_good(session, "FLOUR", decay_per_tick=Decimal("0.2"))
    goods.create_good(session, "BREAD", decay_per_tick=Decimal("0.15"))
    # Industrial minerals (from deposits) -- do not perish.
    goods.create_good(session, "ORE")
    goods.create_good(session, "COAL")
    goods.create_good(session, "STONE")
    # Industrial intermediates and capital -- do not perish.
    goods.create_good(session, "IRON")
    goods.create_good(session, "STEEL")
    goods.create_good(session, "TOOLS")
    # HUNGER: the deprivation counter. Proportional decay against a constant
    # grant converges to grant/decay, so the incapacitation threshold must sit
    # below that equilibrium or chronic hunger never bites. Grant is 1 per
    # hungry tick, decay 0.05 -> equilibrium 20 at permanent famine, below the
    # threshold of 30: a real famine kills, an intermittent miss does not.
    goods.create_good(
        session, "HUNGER",
        incapacitates_at=Decimal("30"),
        decay_per_tick=Decimal("0.05"),
    )


def _create_tech(session: Session) -> None:
    # Food skills are per-person (ENTITY scope): each farmer/miller/baker
    # learns them for themselves.
    tech.create_technology(session, "FARMING", scope=TechScope.ENTITY)
    tech.create_technology(session, "MILLING", scope=TechScope.ENTITY)
    tech.create_technology(session, "BAKING", scope=TechScope.ENTITY)
    # Smelting is "known physics" (WORLD scope): once anyone knows it,
    # everyone does. STEELMAKING builds on it.
    tech.create_technology(session, "SMELTING", scope=TechScope.WORLD)
    tech.create_technology(
        session, "STEELMAKING", prerequisites=["SMELTING"], scope=TechScope.WORLD,
    )
    # Toolmaking is a per-person skill that presupposes the world knows how
    # to smelt.
    tech.create_technology(
        session, "TOOLMAKING", prerequisites=["SMELTING"], scope=TechScope.ENTITY,
    )


def _create_recipes(session: Session) -> None:
    D = Decimal

    # --- The LIVE chain (exercised by the proving run) ---------------------
    # Farmer's food engine. Yields 4: eat 1, sell the surplus to both buyers.
    production.create_recipe(
        session, "FARM_GRAIN",
        inputs={"LABOR": D("1")}, outputs={"GRAIN": D("4")},
        duration_ticks=1, requires_facility="FARM", requires=["FARMING"],
    )
    # Miner's extraction. 1 LABOR draws 2 ORE from the seam -> 2 ORE.
    production.create_recipe(
        session, "MINE_ORE",
        inputs={"LABOR": D("1")}, outputs={"ORE": D("2")},
        duration_ticks=1, deposit_inputs={"ORE": D("2")},
    )
    # Smith's tech-gated smelt. Consumes exactly what the Miner produces.
    production.create_recipe(
        session, "SMELT_IRON",
        inputs={"ORE": D("2"), "LABOR": D("1")}, outputs={"IRON": D("2")},
        duration_ticks=1, requires_facility="FORGE", requires=["SMELTING"],
    )

    # --- DECLARED recipes (exercised by the focused feature tests) ---------
    # Food value-add chain.
    production.create_recipe(
        session, "MILL_FLOUR",
        inputs={"GRAIN": D("2")}, outputs={"FLOUR": D("2")},
        duration_ticks=1, requires_facility="MILL", requires=["MILLING"],
    )
    production.create_recipe(
        session, "BAKE_BREAD",
        inputs={"FLOUR": D("1")}, outputs={"BREAD": D("2")},
        duration_ticks=1, requires_facility="BAKERY", requires=["BAKING"],
    )
    # Further extraction (coal for steel, stone for construction).
    production.create_recipe(
        session, "MINE_COAL",
        inputs={"LABOR": D("1")}, outputs={"COAL": D("2")},
        duration_ticks=1, deposit_inputs={"COAL": D("2")},
    )
    production.create_recipe(
        session, "QUARRY_STONE",
        inputs={"LABOR": D("1")}, outputs={"STONE": D("4")},
        duration_ticks=1, deposit_inputs={"STONE": D("4")},
    )
    # Multi-tick, flow-fed steel (per_tick LABOR paid each of 2 ticks).
    production.create_recipe(
        session, "MAKE_STEEL",
        inputs={"IRON": D("2"), "COAL": D("1")}, outputs={"STEEL": D("2")},
        per_tick_inputs={"LABOR": D("1")},
        duration_ticks=2, requires_facility="FORGE", requires=["STEELMAKING"],
    )
    # Capital-good output.
    production.create_recipe(
        session, "MAKE_TOOLS",
        inputs={"IRON": D("1"), "STEEL": D("1")}, outputs={"TOOLS": D("1")},
        duration_ticks=2, requires=["TOOLMAKING"],
    )
    # Construction: erects a FORGE on the bound parcel. good_requirements
    # shows the hold-not-consume feature (you must HOLD a TOOLS, kept after).
    production.create_recipe(
        session, "BUILD_FORGE",
        inputs={"STONE": D("4")}, outputs={},
        good_requirements={"TOOLS": D("1")},
        per_tick_inputs={"LABOR": D("1")},
        duration_ticks=3, builds_facility="FORGE",
    )
    # Research: the output is an unlock, not goods. Prereq SMELTING; grants
    # STEELMAKING (world-scope -> first discovery unlocks for everyone).
    production.create_recipe(
        session, "RESEARCH_STEEL",
        inputs={}, outputs={},
        per_tick_inputs={"LABOR": D("1")},
        duration_ticks=5, requires=["SMELTING"], unlocks=["STEELMAKING"],
    )


def _create_needs(session: Session) -> None:
    # FOOD: 1 unit/tick, met by any of GRAIN/FLOUR/BREAD. Unmet -> HUNGER.
    needs.create_need(
        session, "FOOD", Decimal("1"), ["GRAIN", "FLOUR", "BREAD"],
        entity_type=EntityType.INDIVIDUAL, priority=0,
        condition_symbol="HUNGER", condition_quantity=Decimal("1"),
    )


def _create_markets(session: Session) -> None:
    for symbol in ("LABOR", "GRAIN", "FLOUR", "BREAD", "ORE", "COAL",
                    "STONE", "IRON", "STEEL", "TOOLS"):
        markets.create_market(session, symbol, "USD")


# ---------------------------------------------------------------------------
# Cast
# ---------------------------------------------------------------------------

def _make_farmer(session: Session) -> Entity:
    farmer = services.create_entity(session, "Farmer", EntityType.INDIVIDUAL)
    services.create_account(session, farmer, "USD", initial_balance=FARMER_USD)
    markets.adjust_holding(session, farmer, "GRAIN", FOOD_BUFFER)
    # A FARM on an owned parcel, and the skill to use it.
    parcel = parcels.create_parcel(session, "LAND", name="Farmer's Field", owner=farmer)
    parcels.add_facility(session, parcel, "FARM")
    tech.grant_unlock(session, farmer, tech.get_technology(session, "FARMING"), 0)
    return farmer


def _make_miner(session: Session) -> Entity:
    miner = services.create_entity(session, "Miner", EntityType.INDIVIDUAL)
    services.create_account(session, miner, "USD", initial_balance=MINER_USD)
    markets.adjust_holding(session, miner, "GRAIN", FOOD_BUFFER)
    # A parcel with a regenerating ORE seam (no facility -- the deposit binds).
    parcel = parcels.create_parcel(session, "LAND", name="Miner's Claim", owner=miner)
    parcels.add_deposit(
        session, parcel, "ORE", ORE_QUANTITY,
        capacity=ORE_CAPACITY, regen_per_tick=ORE_REGEN,
    )
    return miner


def _make_smith(session: Session) -> Entity:
    smith = services.create_entity(session, "Smith", EntityType.INDIVIDUAL)
    services.create_account(session, smith, "USD", initial_balance=SMITH_USD)
    markets.adjust_holding(session, smith, "GRAIN", FOOD_BUFFER)
    # A FORGE on an owned parcel.
    parcel = parcels.create_parcel(session, "LAND", name="Smith's Forge", owner=smith)
    parcels.add_facility(session, parcel, "FORGE")
    # SMELTING is world-scope: granting it (via any entity) unlocks it for the
    # whole world. Only the Smith owns a FORGE, so only the Smith smelts.
    tech.grant_unlock(session, smith, tech.get_technology(session, "SMELTING"), 0)
    return smith


def _wire_scripts(session: Session, farmer: Entity, miner: Entity, smith: Entity) -> None:
    session.add(Script(
        name=f"farmer-behaviour-{farmer.id}",
        script_type=ScriptType.BEHAVIOUR,
        source=_behaviour("starter.lua"),
        entity_id=farmer.id,
        timeout_ms=200,
        state={},
    ))
    session.add(Script(
        name=f"miner-behaviour-{miner.id}",
        script_type=ScriptType.BEHAVIOUR,
        source=_behaviour("miner.lua"),
        entity_id=miner.id,
        timeout_ms=200,
        state={},
    ))
    session.add(Script(
        name=f"smith-behaviour-{smith.id}",
        script_type=ScriptType.BEHAVIOUR,
        source=_behaviour("smith.lua"),
        entity_id=smith.id,
        timeout_ms=200,
        state={},
    ))
