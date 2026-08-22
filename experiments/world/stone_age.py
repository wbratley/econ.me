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
conditions HUNGER/EXPOSURE/DISEASE. Money is COIN — found, not endowed:
seats start with walking money, the bagged gather mints the rest, and
food, materials, tools and spare LABOR all trade on COIN markets (no
barter: the engine clears coin-denominated order books per tick). The
world ships WORLD NOTES (world.manual) -- the authored strategy and
seams -- riding under the GENERATED catalog (the agent loop renders
every action, cost, odd and threshold from the installed content), and
runs rival-private: scripts and prompts see only their own pantry and
purse (world.private_holdings).

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
from econengine.models import (
    Entity, EntityType, Parcel, Script, ScriptType, WorldSetting,
)

_LUA_DIR = Path(__file__).parent / "lua"

# --- Money ------------------------------------------------------------------
# NOT the frontier's genesis doctrine: money is found. Seats carry walking
# money (SEAT_COIN), and the bagged gather's shiny-stone branch mints the
# rest (production._credit_output credits any banked symbol to the
# account) — the supply grows with digging, ~0.075 COIN/tick per bagged
# house.
COIN = "COIN"
#: A seat's starting stake. Deliberately tiny: coin in this world is
#: FOUND, not endowed (a gather branch can mint it), so 10 is walking
#: money -- the supply grows as people dig.
SEAT_COIN = Decimal("10")
#: Branch weight of the shiny-stone roll on both gather tables. ~5% of
#: gathers find a COIN: over a 200-tick run a house that gathers ~2/3 of
#: its ticks mints ~6-7 -- money supply creeps from 30 toward ~50.
COIN_WEIGHT = Decimal("5")
#: WorldSetting key carrying the pack's authored NOTES (strategy and
#: seams); the agent loop folds them into the system prompt under the
#: generated catalog -- tables derive, meaning stays authored.
MANUAL_KEY = "world.manual"

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


MANUAL = """\
WORLD NOTES -- the strategy and the seams. The numbers live in the
catalog above: every good, need, recipe (costs, odds, durations, gates)
and death threshold, derived from this world's physics. What follows is
what the numbers cannot spell.

== SCARCITY ==
LABOR is the ration: one recipe with LABOR in its cost per tick. You
choose what each tick is for; unspent labor is nearly worthless. A fed
entity slowly heals its conditions (~0.95/tick); conditions fade 5%/tick
on their own too, but thresholds are thresholds -- the catalog says
where each one kills.

== THE LADDER (rough order; a gather averages ~0.75 of a needed food) ==
1. FIRE first (2 WOOD + a tick): cooking + warmth. Do not sleep fireless.
2. BAG (3 YARN-ish, one tick): doubles EVERY future gather, finds COIN.
3. SPEAR (flint+yarn): meat surplus -> COOKED_MEAT stock -> sell MEAT.
4. SHELTER + CLOTHES (7 WOOD + 7 YARN): the 1.5 warmth need becomes
   FREE. Every TEND_FIRE tick you stop paying is a gather you gained.
5. TRAPs: convert surplus WOOD+YARN into the best hunt table.
A tooled house gathers ~2.5 food per LABOR against a 1.0 need -- the
surplus is what markets are for. The starter script never builds ANY of
this: it is the floor you inherit, not the ceiling.

== THE TRADING POST ==
A BUSINESS entity, THE TRADING POST, stands in every market with its own
COIN. It SELLS safe food (BERRIES, COOKED_MEAT while they last, and
JERKY -- salted meat that never rots, so the shop always has food) and
it BUYS raw goods (MEAT, WOOD, YARN, FLINT, BERRIES) for COIN: your
surplus sold to the post becomes COIN, and COIN becomes food when your
own gathering fails. Its prices haggle: each sale raises its ask 5%,
each purchase it fills lowers its bid 5%, and 3 quiet ticks move prices
the other way (ask -5%, bid +3%). Its bids are small (4 units, and
never more than its COIN covers) and it stops bidding for a good it
holds 20 of.

== SPEECH ==
ctx.action.say("text") speaks AS your entity: one utterance per tick,
up to 256 characters, delivered to every house. Every house's next
prompt carries what it heard (WHAT YOU SAW), and every behaviour
script hears it in ctx.events next tick -- a rival can quote you, act
on you, or ignore you. Speech is free and unverifiable: a claimed
price is not a standing offer; only the order book is real. Use it to
coordinate, to signal, to bluff -- and remember others may bluff you.

== THE ORDER BOOK ==
std.best_ask(symbol) and std.best_bid(symbol) read the best resting
price on each side of any market -- the ask is what you must pay to buy
NOW, the bid is what you beat to sell NOW. Read both before you quote:
buying blind above the ask or selling below the bid is coin left on the
table (std.market_price is only the LAST trade -- history, not the shelf
in front of you). Re-quoting a price level you already occupy REPLACES
your resting order there: your script runs every tick, so re-asserting
the same quote is maintenance, not stacking -- ladder different prices
if you want depth.

== PRIVACY ==
You see your own holdings, needs, and accounts. You CANNOT see any
other entity's holdings or money: ctx.query.holding on another entity
returns nil, ctx.query.holders is empty. Market prices are public; the
only way to know a rival's wealth is to trade with them.
"""


TRADING_POST = "trading_post.lua"
POST_COIN = Decimal("30")       # a small purse: price discovery, not a
                                 # bottomless buyer -- houses earn coin
                                 # by selling to it, and the coin supply
                                 # stays what seats minted
POST_FOOD = {"BERRIES": Decimal("60"), "COOKED_MEAT": Decimal("20"),
             "JERKY": Decimal("30")}   # the salted shelf: JERKY never
                                    # rots, so late-arriving coin always
                                    # has something to buy (run 4: OSS
                                    # died holding 17 COIN beside an
                                    # empty, rotted larder)


def spawn_trading_post(session: Session) -> Entity:
    """The market maker: a BUSINESS (no needs -- it cannot starve or
    freeze, and it draws no LABOR) with a purse of COIN, a larder of
    safe food, and the haggling behaviour. It is the standing
    counterparty every run lacked: a bid for surplus, an ask for food,
    and a public price for both."""
    post = services.create_entity(session, "Trading Post", EntityType.BUSINESS)
    services.create_account(session, post, COIN, initial_balance=POST_COIN)
    for sym, qty in POST_FOOD.items():
        markets.adjust_holding(session, post, sym, qty)
    session.add(Script(
        name=f"trading-post-{post.id}",
        script_type=ScriptType.BEHAVIOUR,
        source=_gate_pack_script(TRADING_POST),
        entity_id=post.id,
        timeout_ms=200,
        state={},
    ))
    return post


def create_content(session: Session, verify: bool = True) -> None:
    """Goods, recipes, needs, markets -- the stone-age "physics".

    verify=False is the manifest's counting pass (regen): measurement,
    not installation -- the shipped pins are stale by definition while
    the author is regenerating them."""
    _create_goods(session)
    _create_recipes(session)
    _create_needs(session)
    _create_markets(session)
    spawn_trading_post(session)
    if verify:
        manifest.verify_manifest()
    scripting.pin_std_version(session)
    scripting.set_world_lib(session, _read_lua("world_lib.lua"))
    scripting.set_pack_lib(session, _read_lua("pack.lua"))
    # Rival privacy: scripts and agents see only their own holdings
    # (build_queries scopes ctx.query.holding / .holders; the agent loop
    # drops the leaderboard's money column separately, multi.py).
    session.add(WorldSetting(key=scripting.PRIVATE_HOLDINGS_KEY,
                             value={"enabled": True}))
    # The legible manual (below): tech tree, conditions, effects.
    session.add(WorldSetting(key=MANUAL_KEY, value={"text": MANUAL}))
    # 15.4: everything above was installed by this pack -- say so on
    # every row, so the catalog attributes content and a later install
    # attempt on a claimed key is refused with the owner's name.
    manifest.stamp_pack(session)


def _create_goods(session: Session) -> None:
    # The action ration: one auto-issued LABOR per INDIVIDUAL per tick.
    goods.create_good(
        session, "LABOR", name="Labor",
        description="One unit of action per tick, auto-issued to every "
                    "individual. Unspent labor fades fast: use it or lose it.",
        decay_per_tick=Decimal("0.5"),
        auto_issue_quantity=Decimal("1"),
        auto_issue_entity_type=EntityType.INDIVIDUAL,
    )
    # Food -- everything edible rots. MEAT fastest (raw), COOKED slightly
    # slower, BERRIES in between: preserving food is a real problem, and
    # selling surplus before it rots is the trader's edge.
    goods.create_good(session, "MEAT", name="Raw Meat",
                      description="Raw flesh from the hunt. Rots fast; cook it "
                                  "at a fire or eat it raw at your own risk.",
                      decay_per_tick=Decimal("0.30"))
    goods.create_good(session, "BERRIES", name="Berries",
                      description="Foraged food; the staple of the early game "
                                  "and the market's most-traded good.",
                      decay_per_tick=Decimal("0.25"))
    goods.create_good(session, "COOKED_MEAT", name="Cooked Meat",
                      description="Fire-cooked meat: keeps a little better than "
                                  "raw and feeds you without disease risk.",
                      decay_per_tick=Decimal("0.25"))
    # ...except JERKY: salted meat keeps forever. Only the Trading Post
    # stocks it -- the shop shelf that is never bare (run 4's timing
    # gap: agents arrive coin-poor early and coin-rich late, so the
    # late coin needs something to buy that rot did not eat).
    goods.create_good(session, "JERKY", name="Jerky",
                      description="Salted meat that never rots. Stocked only by "
                                  "the Trading Post — the shelf that is never bare.")
    # Gathered materials -- durable.
    goods.create_good(session, "WOOD", name="Wood",
                      description="Gathered timber: the fuel of fires and the "
                                  "frame of spears, traps, and shelters.")
    goods.create_good(session, "YARN", name="Yarn",
                      description="Gathered cord; binds spearheads, weaves "
                                  "clothes and beds.")
    goods.create_good(session, "FLINT", name="Flint",
                      description="Sharp stone for spearheads and cutting tools.")
    # Capital -- durable, never consumed by use (traps are the exception:
    # a consumed input of HUNT_TRAPS, the one-shot technology).
    goods.create_good(session, "SPEAR", name="Spear",
                      description="Held while hunting, never consumed: turns a "
                                  "desperate hunt into a living.")
    goods.create_good(session, "BAG", name="Bag",
                      description="Held while gathering, never consumed: "
                                  "doubles the day's find.")
    goods.create_good(session, "TRAP", name="Trap",
                      description="One-shot hunting ammunition — consumed by "
                                  "the traps hunt, the best odds craft can buy.")
    goods.create_good(session, "CLOTHES", name="Clothes",
                      description="Worn warmth: with a shelter, covers the whole "
                                  "WARMTH need forever, free.")
    goods.create_good(session, "BED", name="Bed",
                      description="Craftable comfort, mechanically idle — "
                                  "the expansion hook.")
    # Flows. WARMTH fades (0.2/tick): a tended fire warms you for a few
    # ticks, not forever. SATIETY is instant (1.0 decay): eat-now food
    # from the desperate EAT_RAW path, consumed the same tick.
    goods.create_good(session, "WARMTH", name="Warmth",
                      description="A flow, not a stock to hoard: made by fires, "
                                  "shelter and clothes, fades fast. The WARMTH "
                                  "need drinks it every tick.",
                      decay_per_tick=Decimal("0.2"))
    goods.create_good(session, "SATIETY", name="Satiety",
                      description="Instant food from eating raw meat: lands and "
                                  "is consumed the same tick.",
                      decay_per_tick=Decimal("1"))
    # Conditions. See the equilibrium notes in the module docstring.
    goods.create_good(
        session, "HUNGER", name="Hunger",
        description="The memory of missed meals.",
        decay_per_tick=Decimal("0.05"),
        incapacitates_at=Decimal("15"),
    )
    goods.create_good(
        session, "EXPOSURE", name="Exposure",
        description="The memory of cold nights.",
        decay_per_tick=Decimal("0.05"),
        incapacitates_at=Decimal("18"),
    )
    goods.create_good(
        session, "DISEASE", name="Disease",
        description="Raw meat's price: one bad meal can be a death sentence "
                    "at this threshold.",
        decay_per_tick=Decimal("0.05"),
        incapacitates_at=Decimal("2.5"),
    )


def _create_recipes(session: Session) -> None:
    D = Decimal

    # --- Subsistence: gather and hunt --------------------------------------
    # One gather = one loot-table roll of ONE resource (you find what you
    # find): 45% 3 BERRIES, 25% 2 WOOD, 15% 1 YARN, 15% 1 FLINT... and on
    # the doubled BAG table a ~5% branch of 1 COIN -- shiny stones, minted
    # by the ground itself (production credits a banked symbol to the
    # account, production._credit_output). The bare table finds none:
    # scarcity first, then the supply grows with better tools.
    # Expected food value 1.35/tick against a need of 1.0 -- bare
    # subsistence spends ~3/4 of the LABOR budget on food.
    production.create_recipe(
        session, "GATHER", name="Gather",
        description="One loot-table roll of a single resource: you find what "
                    "you find. Bare-handed subsistence.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=1,
        branches=[
            {"weight": D("45"), "outputs": {"BERRIES": D("3")}, "label": "berries"},
            {"weight": D("25"), "outputs": {"WOOD": D("2")}, "label": "wood"},
            {"weight": D("15"), "outputs": {"YARN": D("1")}, "label": "yarn"},
            {"weight": D("15"), "outputs": {"FLINT": D("1")}, "label": "flint"},
        ],
    )
    production.create_recipe(
        session, "GATHER_BAG", name="Gather with a Bag",
        description="The doubled table — and a small chance the ground itself "
                    "has minted a coin.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=1,
        good_requirements={"BAG": D("1")},
        branches=[
            {"weight": D("40"), "outputs": {"BERRIES": D("6")}, "label": "berries"},
            {"weight": D("22"), "outputs": {"WOOD": D("4")}, "label": "wood"},
            {"weight": D("13"), "outputs": {"YARN": D("2")}, "label": "yarn"},
            {"weight": D("13"), "outputs": {"FLINT": D("2")}, "label": "flint"},
            {"weight": COIN_WEIGHT, "outputs": {COIN: D("1")}, "label": "shiny"},
            {"weight": D("7"), "outputs": {}, "label": "nothing"},
        ],
    )
    # Hunting: slow (2 ticks), risky bare-handed (55% total loss), better
    # with a SPEAR held (never consumed) and best with TRAPs (consumed --
    # the supply chain: wood+yarn per hunt).
    production.create_recipe(
        session, "HUNT", name="Hunt",
        description="Slow, and bare-handed: mostly nothing, sometimes dinner.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        branches=[
            {"weight": D("55"), "outputs": {}, "label": "nothing"},
            {"weight": D("35"), "outputs": {"MEAT": D("2")}, "label": "small"},
            {"weight": D("10"), "outputs": {"MEAT": D("4")}, "label": "big"},
        ],
    )
    production.create_recipe(
        session, "HUNT_SPEAR", name="Hunt with a Spear",
        description="A held spear (never consumed) turns a desperate hunt "
                    "into a living.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        good_requirements={"SPEAR": D("1")},
        branches=[
            {"weight": D("25"), "outputs": {}, "label": "nothing"},
            {"weight": D("55"), "outputs": {"MEAT": D("3")}, "label": "small"},
            {"weight": D("20"), "outputs": {"MEAT": D("6")}, "label": "big"},
        ],
    )
    production.create_recipe(
        session, "HUNT_TRAPS", name="Hunt with Traps",
        description="The best odds craft can buy, at the price of a consumed "
                    "trap per hunt.",
        inputs={"LABOR": D("1"), "TRAP": D("1")},
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
        session, "MAKE_FIRE", name="Make Fire",
        description="Erects the fire on your camp: the bootstrap technology — "
                    "cheap, immediate, warm, and it cooks.",
        inputs={"LABOR": D("1"), "WOOD": D("2")},
        outputs={}, duration_ticks=1, builds_facility="FIRE",
    )
    production.create_recipe(
        session, "TEND_FIRE", name="Tend Fire",
        description="Burns a log into a warmth stock: a tended fire covers "
                    "you for a few ticks, not forever.",
        inputs={"LABOR": D("1"), "WOOD": D("1")},
        outputs={"WARMTH": D("8")}, duration_ticks=1, requires_facility="FIRE",
    )
    production.create_recipe(
        session, "COOK_MEAT", name="Cook Meat",
        description="Fire-cooked meat: no disease, keeps a little better.",
        inputs={"LABOR": D("1"), "MEAT": D("2")},
        outputs={"COOKED_MEAT": D("2")}, duration_ticks=1,
        requires_facility="FIRE",
    )
    # Eating raw: free (no LABOR -- desperation does not wait), instant
    # (duration 0: SATIETY lands before this tick's consumption pass), and
    # a 25% chance of DISEASE. The alternative to cooking, priced in risk.
    production.create_recipe(
        session, "EAT_RAW", name="Eat Raw Meat",
        description="Desperation does not wait: free, instant — and a "
                    "one-in-four chance of disease.",
        inputs={"MEAT": D("1")}, outputs={}, duration_ticks=0,
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
        session, "MAKE_SHELTER", name="Build Shelter",
        description="Warmth as capital: pays once, then drips forever — "
                    "with clothes, the whole WARMTH need covered free.",
        inputs={"LABOR": D("1"), "WOOD": D("4"),
                                          "YARN": D("2")},
        outputs={}, duration_ticks=3, builds_facility="SHELTER",
    )
    production.create_recipe(
        session, "REST_SHELTERED", name="Rest Sheltered",
        description="Sleep warm under your own roof — no labor required.",
        inputs={},
        outputs={"WARMTH": D("1")}, duration_ticks=1, requires_facility="SHELTER",
    )
    production.create_recipe(
        session, "MAKE_CLOTHES", name="Make Clothes",
        description="Worn warmth: with a shelter, the whole WARMTH need "
                    "covered free.",
        inputs={"LABOR": D("1"), "YARN": D("3"),
                                          "FLINT": D("1")},
        outputs={"CLOTHES": D("1")}, duration_ticks=2,
    )
    production.create_recipe(
        session, "HUDDLE", name="Huddle",
        description="Clothed against the cold: half a warmth without a fire.",
        inputs={},
        outputs={"WARMTH": D("0.5")}, duration_ticks=1,
        good_requirements={"CLOTHES": D("1")},
    )

    # --- Tools ---------------------------------------------------------------
    # SPEAR (held, never worn) and BAG (held) upgrade hunt and gather.
    # TRAP is ammunition. BED is declared for the future REST mechanics --
    # craftable and tradeable now, mechanically idle (the expansion hook).
    production.create_recipe(
        session, "MAKE_SPEAR", name="Make Spear",
        description="The hunter's upgrade: held, never worn.",
        inputs={"LABOR": D("1"), "FLINT": D("1"),
                                        "WOOD": D("2"), "YARN": D("1")},
        outputs={"SPEAR": D("1")}, duration_ticks=1,
    )
    production.create_recipe(
        session, "MAKE_BAG", name="Make Bag",
        description="The gatherer's upgrade: held, never worn.",
        inputs={"LABOR": D("1"), "YARN": D("2"),
                                      "WOOD": D("1")},
        outputs={"BAG": D("1")}, duration_ticks=1,
    )
    production.create_recipe(
        session, "MAKE_TRAP", name="Make Trap",
        description="Hunting ammunition: consumed by the traps hunt.",
        inputs={"LABOR": D("1"), "WOOD": D("2"),
                                       "YARN": D("1")},
        outputs={"TRAP": D("1")}, duration_ticks=1,
    )
    production.create_recipe(
        session, "MAKE_BED", name="Make Bed",
        description="Craftable comfort, mechanically idle — the expansion hook.",
        inputs={"LABOR": D("1"), "WOOD": D("2"),
                                      "YARN": D("3")},
        outputs={"BED": D("1")}, duration_ticks=2,
    )


def _create_needs(session: Session) -> None:
    # FOOD: berries, cooked meat, or desperate SATIETY. Unmet -> HUNGER.
    needs.create_need(
        session, "FOOD", FOOD_PER_TICK, ["BERRIES", "COOKED_MEAT", "JERKY", "SATIETY"],
        name="Food",
        description="One meal a tick, drawn in order: berries, then cooked "
                    "meat, then jerky, then raw satiety. Miss it and hunger "
                    "accrues.",
        entity_type=EntityType.INDIVIDUAL, priority=0,
        condition_symbol="HUNGER", condition_quantity=Decimal("1"),
    )
    # WARMTH: a flow good, stocked by fire/shelter/clothes. Unmet ->
    # EXPOSURE. 1.5/tick so that clothes (0.5) + shelter (1.0) exactly
    # cover it: full capital coverage is achievable but requires BOTH.
    needs.create_need(
        session, "WARMTH", WARMTH_PER_TICK, ["WARMTH"],
        name="Warmth",
        description="Drawn from the WARMTH stock made by fires, shelter and "
                    "clothes. Miss it and exposure accrues: clothes and "
                    "shelter together cover it exactly.",
        entity_type=EntityType.INDIVIDUAL, priority=1,
        condition_symbol="EXPOSURE", condition_quantity=Decimal("1"),
    )


def _create_markets(session: Session) -> None:
    _NAMES = {
        "LABOR": "Labor", "BERRIES": "Berries", "MEAT": "Raw Meat",
        "COOKED_MEAT": "Cooked Meat", "JERKY": "Jerky", "WOOD": "Wood",
        "YARN": "Yarn", "FLINT": "Flint", "SPEAR": "Spear", "BAG": "Bag",
        "TRAP": "Trap", "CLOTHES": "Clothes", "BED": "Bed",
    }
    for symbol in ("LABOR", "BERRIES", "MEAT", "COOKED_MEAT", "JERKY", "WOOD",
                   "YARN", "FLINT", "SPEAR", "BAG", "TRAP", "CLOTHES", "BED"):
        markets.create_market(session, symbol, COIN, name=_NAMES[symbol])


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
