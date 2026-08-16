"""Genesis for the "stone_age" content pack: survival under scarcity.

The frontier pack proved the substrate (and, across runs 3-5, that
symmetric seats with free subsistence converge on autarky: no customer
for depth, no cost to neglect). This pack inverts both levers at once --
the world is POOR and the needs are FATAL:

  * Everything edible rots (no free pantry), and the two needs -- FOOD
    and WARMTH -- are paid every tick in perishable flows.
  * Neglect kills. Each unmet need drives a cumulative condition, and
    every condition's equilibrium (grant/decay, the run5 lesson) sits
    ABOVE its incapacitation threshold: chronic neglect eventually
    seizes the entity. HUNGER (starvation), EXPOSURE (cold), DISEASE
    (raw meat) all have real teeth.
  * Survival consumes most of the LABOR budget (1 auto-issued per
    tick): bare-handed living runs a deficit the genesis buffers only
    briefly cover. The way out is CAPITAL -- spear, bag, trap make food
    cheap; fire, shelter, clothes make warmth free -- so specialization
    and trade have room to pay.

Goods: MEAT, BERRIES, WOOD, YARN, FLINT (gathered/hunted), COOKED_MEAT,
SPEAR, BAG, TRAP, CLOTHES, BED, plus the flows WARMTH/SATIETY and the
conditions HUNGER/EXPOSURE/DISEASE. Money is COIN; food, materials,
tools and spare LABOR all trade on COIN markets (no barter: the engine
clears coin-denominated order books per tick).

Balance doctrine (asserted by test_stone_age.py's policy tests):
  - a seat that does NOTHING dies inside two rounds (conditions bite);
  - the starter script survives indefinitely at a hand-to-mouth pace;
  - tooled policies accumulate tradeable surplus bare ones cannot.

The numbers below were tuned with those three policies, not by taste:
see the equilibrium notes on each condition good.
"""

from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from econengine import goods, markets, needs, parcels, production, scripting, services
from experiments.world import manifest
from econengine.models import Entity, EntityType, Parcel, Script, ScriptType

_LUA_DIR = Path(__file__).parent / "lua"

# --- Money ------------------------------------------------------------------
# The same genesis-endowment doctrine as the frontier (money is conserved
# by trade; nothing mints or burns), under a stone-appropriate name.
COIN = "COIN"
SEAT_COIN = Decimal("500")

# --- Genesis buffers --------------------------------------------------------
# What a seat starts with: a few days of food and one night's warmth. Not a
# lifestyle -- a runway. Bare survival (no tools, no capital) runs a LABOR
# deficit against FOOD+WARMTH, so these buffers only postpone the reckoning;
# capital (fire, clothes, shelter, tools) is the only way to a surplus.
BERRY_BUFFER = Decimal("8")
WARMTH_BUFFER = Decimal("15")

# --- Needs and their teeth --------------------------------------------------
# Both needs bill every tick. Each condition's death threshold sits BELOW
# its chronic-neglect equilibrium (grant/decay) -- the run5 lesson, applied
# three times deliberately:
#
#   HUNGER:   grant 1.0/tick, decay 0.05 -> equilibrium 20, dies at 15.
#             Total fast: ~tick 27 from cold; buffers push it into round 2.
#   EXPOSURE: full neglect grants 1.5/tick -> equilibrium 30, dies at 18.
#             Any single warmth source (1.0 or 0.5/tick) equilibrates at 10
#             or 20* -- see WARMTH sources below -- chronically cold but
#             alive: partial shelter is survivable misery, none is death.
#   DISEASE:  a raw-meat diet grants 0.25/tick expected -> equilibrium 5,
#             dies at 2.5. One raw meal is a scare (+1, fades in ~14
#             ticks); raw as a staple is a slow death. Cooking is cheap.
FOOD_PER_TICK = Decimal("1")
WARMTH_PER_TICK = Decimal("1.5")
EAT_RAW_DISEASE_WEIGHT = Decimal("25")   # out of 100 per raw meal

DEFAULT_TICKS = 40


def _read_lua(name: str) -> str:
    return (_LUA_DIR / name).read_text()


def _gate_pack_script(name: str) -> str:
    source = _read_lua(name)
    problems = scripting.validate_script_source(
        source, {"world": _read_lua("world_lib.lua"), "pack": _read_lua("pack.lua")})
    if problems:
        raise scripting.LibraryRejected(problems)
    return source


STARTER = "stone_age_starter.lua"


def create_content(session: Session) -> None:
    """Goods, recipes, needs, markets -- the stone-age "physics"."""
    _create_goods(session)
    _create_recipes(session)
    _create_needs(session)
    _create_markets(session)
    manifest.verify_manifest()
    scripting.pin_std_version(session)
    scripting.set_world_lib(session, _read_lua("world_lib.lua"))
    scripting.set_pack_lib(session, _read_lua("pack.lua"))


def _create_goods(session: Session) -> None:
    # The action ration: one auto-issued LABOR per INDIVIDUAL per tick.
    goods.create_good(
        session, "LABOR",
        decay_per_tick=Decimal("0.5"),
        auto_issue_quantity=Decimal("1"),
        auto_issue_entity_type=EntityType.INDIVIDUAL,
    )
    # Food -- everything edible rots. MEAT fastest (raw), COOKED slightly
    # slower, BERRIES in between: preserving food is a real problem, and
    # selling surplus before it rots is the trader's edge.
    goods.create_good(session, "MEAT", decay_per_tick=Decimal("0.30"))
    goods.create_good(session, "BERRIES", decay_per_tick=Decimal("0.25"))
    goods.create_good(session, "COOKED_MEAT", decay_per_tick=Decimal("0.25"))
    # Gathered materials -- durable.
    goods.create_good(session, "WOOD")
    goods.create_good(session, "YARN")
    goods.create_good(session, "FLINT")
    # Capital -- durable, never consumed by use (traps are the exception:
    # a consumed input of HUNT_TRAPS, the one-shot technology).
    goods.create_good(session, "SPEAR")
    goods.create_good(session, "BAG")
    goods.create_good(session, "TRAP")
    goods.create_good(session, "CLOTHES")
    goods.create_good(session, "BED")
    # Flows. WARMTH fades (0.2/tick): a tended fire warms you for a few
    # ticks, not forever. SATIETY is instant (1.0 decay): eat-now food
    # from the desperate EAT_RAW path, consumed the same tick.
    goods.create_good(session, "WARMTH", decay_per_tick=Decimal("0.2"))
    goods.create_good(session, "SATIETY", decay_per_tick=Decimal("1"))
    # Conditions. See the equilibrium notes in the module docstring.
    goods.create_good(
        session, "HUNGER", decay_per_tick=Decimal("0.05"),
        incapacitates_at=Decimal("15"),
    )
    goods.create_good(
        session, "EXPOSURE", decay_per_tick=Decimal("0.05"),
        incapacitates_at=Decimal("18"),
    )
    goods.create_good(
        session, "DISEASE", decay_per_tick=Decimal("0.05"),
        incapacitates_at=Decimal("2.5"),
    )


def _create_recipes(session: Session) -> None:
    D = Decimal

    # --- Subsistence: gather and hunt --------------------------------------
    # One gather = one loot-table roll of ONE resource (you find what you
    # find): 45% 3 BERRIES, 25% 2 WOOD, 15% 1 YARN, 15% 1 FLINT. Expected
    # food value 1.35/tick against a need of 1.0 -- bare subsistence spends
    # ~3/4 of the LABOR budget on food. The BAG doubles the table.
    production.create_recipe(
        session, "GATHER", inputs={"LABOR": D("1")}, outputs={}, duration_ticks=1,
        branches=[
            {"weight": D("45"), "outputs": {"BERRIES": D("3")}, "label": "berries"},
            {"weight": D("25"), "outputs": {"WOOD": D("2")}, "label": "wood"},
            {"weight": D("15"), "outputs": {"YARN": D("1")}, "label": "yarn"},
            {"weight": D("15"), "outputs": {"FLINT": D("1")}, "label": "flint"},
        ],
    )
    production.create_recipe(
        session, "GATHER_BAG", inputs={"LABOR": D("1")}, outputs={}, duration_ticks=1,
        good_requirements={"BAG": D("1")},
        branches=[
            {"weight": D("45"), "outputs": {"BERRIES": D("6")}, "label": "berries"},
            {"weight": D("25"), "outputs": {"WOOD": D("4")}, "label": "wood"},
            {"weight": D("15"), "outputs": {"YARN": D("2")}, "label": "yarn"},
            {"weight": D("15"), "outputs": {"FLINT": D("2")}, "label": "flint"},
        ],
    )
    # Hunting: slow (2 ticks), risky bare-handed (55% total loss), better
    # with a SPEAR held (never consumed) and best with TRAPs (consumed --
    # the supply chain: wood+yarn per hunt).
    production.create_recipe(
        session, "HUNT", inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        branches=[
            {"weight": D("55"), "outputs": {}, "label": "nothing"},
            {"weight": D("35"), "outputs": {"MEAT": D("2")}, "label": "small"},
            {"weight": D("10"), "outputs": {"MEAT": D("4")}, "label": "big"},
        ],
    )
    production.create_recipe(
        session, "HUNT_SPEAR", inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        good_requirements={"SPEAR": D("1")},
        branches=[
            {"weight": D("25"), "outputs": {}, "label": "nothing"},
            {"weight": D("55"), "outputs": {"MEAT": D("3")}, "label": "small"},
            {"weight": D("20"), "outputs": {"MEAT": D("6")}, "label": "big"},
        ],
    )
    production.create_recipe(
        session, "HUNT_TRAPS", inputs={"LABOR": D("1"), "TRAP": D("1")},
        outputs={}, duration_ticks=3,
        branches=[
            {"weight": D("15"), "outputs": {}, "label": "nothing"},
            {"weight": D("60"), "outputs": {"MEAT": D("4")}, "label": "small"},
            {"weight": D("25"), "outputs": {"MEAT": D("8")}, "label": "big"},
        ],
    )

    # --- The fire chain: warmth and cooking --------------------------------
    # MAKE_FIRE erects the FIRE facility on a parcel (one-time); TEND_FIRE
    # burns wood into a warmth stock (~4 ticks of cover per log); COOK_MEAT
    # needs the fire. Fire is the bootstrap technology: cheap, immediate,
    # and superseded for warmth (not for cooking) by clothes + shelter.
    production.create_recipe(
        session, "MAKE_FIRE", inputs={"LABOR": D("1"), "WOOD": D("2")},
        outputs={}, duration_ticks=1, builds_facility="FIRE",
    )
    production.create_recipe(
        session, "TEND_FIRE", inputs={"LABOR": D("1"), "WOOD": D("1")},
        outputs={"WARMTH": D("8")}, duration_ticks=1, requires_facility="FIRE",
    )
    production.create_recipe(
        session, "COOK_MEAT", inputs={"LABOR": D("1"), "MEAT": D("2")},
        outputs={"COOKED_MEAT": D("2")}, duration_ticks=1,
        requires_facility="FIRE",
    )
    # Eating raw: free (no LABOR -- desperation does not wait), instant
    # (duration 0: SATIETY lands before this tick's consumption pass), and
    # a 25% chance of DISEASE. The alternative to cooking, priced in risk.
    production.create_recipe(
        session, "EAT_RAW", inputs={"MEAT": D("1")}, outputs={}, duration_ticks=0,
        branches=[
            {"weight": D("75"), "outputs": {"SATIETY": D("1")}, "label": "fine"},
            {"weight": EAT_RAW_DISEASE_WEIGHT,
             "outputs": {"SATIETY": D("1"), "DISEASE": D("1")}, "label": "sick"},
        ],
    )

    # --- Shelter and clothing: warmth as capital ---------------------------
    # SHELTER + CLOTHES together cover the whole WARMTH need forever, for
    # free (their recipes carry no LABOR: capital pays once, then drips).
    # Either alone leaves a chronic 0.5/tick gap -- survivable (equilibrium
    # 10 < 18) but miserable: the graded ladder from misery to comfort.
    production.create_recipe(
        session, "MAKE_SHELTER", inputs={"LABOR": D("1"), "WOOD": D("4"),
                                          "YARN": D("2")},
        outputs={}, duration_ticks=3, builds_facility="SHELTER",
    )
    production.create_recipe(
        session, "REST_SHELTERED", inputs={},
        outputs={"WARMTH": D("1")}, duration_ticks=1, requires_facility="SHELTER",
    )
    production.create_recipe(
        session, "MAKE_CLOTHES", inputs={"LABOR": D("1"), "YARN": D("3"),
                                          "FLINT": D("1")},
        outputs={"CLOTHES": D("1")}, duration_ticks=2,
    )
    production.create_recipe(
        session, "HUDDLE", inputs={},
        outputs={"WARMTH": D("0.5")}, duration_ticks=1,
        good_requirements={"CLOTHES": D("1")},
    )

    # --- Tools ---------------------------------------------------------------
    # SPEAR (held, never worn) and BAG (held) upgrade hunt and gather.
    # TRAP is ammunition. BED is declared for the future REST mechanics --
    # craftable and tradeable now, mechanically idle (the expansion hook).
    production.create_recipe(
        session, "MAKE_SPEAR", inputs={"LABOR": D("1"), "FLINT": D("1"),
                                        "WOOD": D("2"), "YARN": D("1")},
        outputs={"SPEAR": D("1")}, duration_ticks=1,
    )
    production.create_recipe(
        session, "MAKE_BAG", inputs={"LABOR": D("1"), "YARN": D("2"),
                                      "WOOD": D("1")},
        outputs={"BAG": D("1")}, duration_ticks=1,
    )
    production.create_recipe(
        session, "MAKE_TRAP", inputs={"LABOR": D("1"), "WOOD": D("2"),
                                       "YARN": D("1")},
        outputs={"TRAP": D("1")}, duration_ticks=1,
    )
    production.create_recipe(
        session, "MAKE_BED", inputs={"LABOR": D("1"), "WOOD": D("2"),
                                      "YARN": D("3")},
        outputs={"BED": D("1")}, duration_ticks=2,
    )


def _create_needs(session: Session) -> None:
    # FOOD: berries, cooked meat, or desperate SATIETY. Unmet -> HUNGER.
    needs.create_need(
        session, "FOOD", FOOD_PER_TICK, ["BERRIES", "COOKED_MEAT", "SATIETY"],
        entity_type=EntityType.INDIVIDUAL, priority=0,
        condition_symbol="HUNGER", condition_quantity=Decimal("1"),
    )
    # WARMTH: a flow good, stocked by fire/shelter/clothes. Unmet ->
    # EXPOSURE. 1.5/tick so that clothes (0.5) + shelter (1.0) exactly
    # cover it: full capital coverage is achievable but requires BOTH.
    needs.create_need(
        session, "WARMTH", WARMTH_PER_TICK, ["WARMTH"],
        entity_type=EntityType.INDIVIDUAL, priority=1,
        condition_symbol="EXPOSURE", condition_quantity=Decimal("1"),
    )


def _create_markets(session: Session) -> None:
    for symbol in ("LABOR", "BERRIES", "MEAT", "COOKED_MEAT", "WOOD", "YARN",
                   "FLINT", "SPEAR", "BAG", "TRAP", "CLOTHES", "BED"):
        markets.create_market(session, symbol, COIN)


# ---------------------------------------------------------------------------
# The agent seat
# ---------------------------------------------------------------------------

def make_house(session: Session, name: str = "House") -> Entity:
    """One symmetric stone-age seat: coins, a day of berries, a night of
    warmth, and a bare CAMP parcel -- no fire, no shelter, no tools, no
    unlocks. Everything beyond the body is the player's to build."""
    house = services.create_entity(session, name, EntityType.INDIVIDUAL)
    services.create_account(session, house, COIN, initial_balance=SEAT_COIN)
    markets.adjust_holding(session, house, "BERRIES", BERRY_BUFFER)
    markets.adjust_holding(session, house, "WARMTH", WARMTH_BUFFER)
    parcels.create_parcel(session, "LAND", name=f"{name}'s Camp", owner=house)
    return house
