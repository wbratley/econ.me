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
  * Survival consumes most of the daylight LABOR budget (1
    auto-issued labor-hour per HOUR OF DAYLIGHT -- 14 a day, none at
    night): bare-handed living runs close to the line the genesis
    buffers only briefly cover. The way out is CAPITAL -- spear, bag,
    trap make food cheap; fire, shelter, clothes make warmth free -- so
    specialization and trade have room to pay.

THE CLOCK (run 18): tick = hour, round = 24 ticks = one day. Daylight
is hours 06..19: LABOR issues only then, and gathering/hunting are
refused in the dark with a clear error (facts, not advice -- the model
plans its day around them via std.hour()/std.is_night()). WARMTH draws
1/hour by day and 3/hour at night -- night is the expensive half of
the world. FOOD is 0.5/hour (12/day): the old 1/tick against 20-tick
rounds becomes 0.5/hour against 24-hour days so the daylight work-day
(14 labor-hours) still treadmills a bare-handed house the way it did
at 20 one-tick rounds. Durations are honest hours (a spear is an
afternoon, smoking is a watch of the night fire).

CONSCIOUS EATING (run 19): the engine no longer chews for you. FOOD's
only satisfier is SATIETY -- the stomach -- and only EAT recipes fill
it: EAT_BERRIES (~3h fed), EAT_COOKED (~4h), EAT_JERKY (~5½h, the
densest), EAT_RAW (~1h, a one-in-four disease lottery). Meals are
labor-free, instant and night-legal; a fed body still burns ~0.6 an
hour (0.5 need + a tenth of the stomach, compound -- ~14/day), so the
treadmill is unchanged arithmetically -- but eating is now a decision
with a cadence (two meals a day), and preservation economics is real:
berries thin, jerky dense, exactly the trade a trader should
arbitrage. The predicted failure mode is the point: starving beside a
full larder is economics, not a bug.

WOLVES (run 20, second cut): creatures, not pressure. Wolves are
spawned ENTITIES -- stats rows (ATTACK 4 / DEFENSE 1), health as a
HITS holding (12), the same FOOD/WARMTH needs as a house, and a
hunting program (wolf_pack.lua): they hunt at night because they are
hungry, target the loudest speaker they heard (the witness feed is
their ears), eat raw meat (their constitution, same 25% disease), and
keep warm by PACE. Combat (combat.py) is entity-vs-entity under the
pack's COMBAT_RULES: daylight refuses, a lit hearth DETERS (a loud
miss), hit% = clamp(50 + 5*(ATK-DEF), 5, 95) on the commit-reveal RNG,
damage = max(1, ATK-DEF) (+1 crit), HITS drain, zero = the ordinary
incapacity/estate machinery, victor seizes {PELT 1, MEAT 3}. Houses
are ATTACK 1 / DEFENSE 1 / 20 HITS; weapons are carried, not born
(SPEAR +3 ATK, CLOTHES +1 DEF). Population renews via spawns.py at
round boundaries: from round 5, every 5 rounds, up to 3 more packs,
never more than 4 alive. The failure modes to watch: the fireless
hunted down over two cold nights, and the loud targeted by name --
speech is free by day, priced at night.

Goods: MEAT, BERRIES, WOOD, YARN, FLINT (gathered/hunted), COOKED_MEAT,
JERKY (smoked or bought), SPEAR, BAG, TRAP, CLOTHES, BED, plus the flows
WARMTH/SATIETY and the
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

from econengine import combat, goods, markets, needs, parcels, production, scripting, services, spawns
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
HOUSE_HITS = Decimal("20")        # a body: drained by combat, never regrown
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
FOOD_PER_TICK = Decimal("0.5")
WARMTH_PER_TICK = Decimal("1")
WARMTH_PER_NIGHT_TICK = Decimal("3")
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
THE DAY IS THE BUDGET: tick = hour, and the day has 24 of them.
Daylight is hours 06..19 -- you get 1 LABOR each daylight hour and
exactly one LABOR-costing recipe runs per hour -- the first script
call that takes it wins, later calls bounce. NIGHT issues no LABOR and
refuses gathering and hunting (too dark); tending, cooking, smoking,
eating and resting all work. WARMTH draws 3/hour at night vs 1 by day:
bank warmth stock before dark, or sleep by a fire (std.hour() and
std.is_night() read the clock). Unspent labor is nearly worthless.

MEALS ARE DECISIONS: nothing is eaten for you. Meals are labor-free,
instant and night-legal -- but they do not happen by themselves.
The FOOD need drinks SATIETY at 0.5/hour plus a tenth of the stomach
each hour (a day costs ~14), and only EAT recipes fill the stomach:
EAT_BERRIES (2 berries, ~3h), EAT_COOKED (~4h), EAT_JERKY (~5½h),
EAT_RAW (~1h, one-in-four disease). A full larder feeds nobody until
someone runs the recipe: starving beside one is a choice, and the
clock will make it for you if you let it -- two meals a day is the
natural cadence.

THE NIGHT HAS TEETH: wolves are creatures -- entities with stats,
health (HITS) and hunger, the same physics as you. They hunt in the
dark, guided by sound: EVERY say at night tells a listening pack where
you are. A lit hearth (WARMTH >= 1) turns a wolf at the door; a spear
(+3 ATTACK) or clothes (+1 DEFENSE) prices into the fight: hit% =
50 + 5 x (ATTACK - DEFENSE), damage = max(1, ATK - DEF). A landed
bite feeds the wolf (it tears flesh); a kill is a carcass: MEAT is
torn from it by any victor, while everything the dead CARRIED moves
only to victors with hands -- wolves cannot loot, what a beast kills
rots where it fell; by day wolves hunt the same game you do (HUNT) --
starve them
out and they die like anything else. A house has 20 HITS and never
regrows them; a wolf has 12, and it WEARS its pelt: kill one and the
pelt and the meat it carried are yours. Combat is an action anyone may take: attack(<entity id>) --
you learn a wolf's id by hearing it hunt (combat is loud: every house
hears every fight). The packs breed: from day 5, every fifth day, up
to three more, never more than four alive.
Speech is free by day. At night it has a price.

A fed
entity slowly heals its conditions (~0.95/tick); conditions fade 5%/tick
on their own too, but thresholds are thresholds -- the catalog says
where each one kills.

== THE LADDER (rough order; a gather averages ~0.75 of a needed food) ==
1. FIRE first (2 WOOD + an hour): cooking + warmth. Do not sleep fireless.
2. EAT what spoils first: berries within hours, cooked within a day;
   JERKY never spoils -- the deep pantry. (Eating is on the ladder now:
   hunger kills the careless before any tool matters.)
3. BAG (3 YARN-ish, one hour): doubles EVERY future gather, finds COIN.
4. SPEAR (flint+yarn, an afternoon): meat surplus -> COOKED_MEAT stock,
   or SMOKE_MEAT it into JERKY (5 slow hours, costs a log, NEVER rots,
   ~6 hours fed per strip) -> sell MEAT.
4a. AXE (flint+wood+yarn, an afternoon): CHOP_WOOD = 3 certain logs an
   hour, six times the bare gather's wood -- the fire never wants again;
   and +2 ATK at the door, the half of a spear in a fist.
5. SHELTER + CLOTHES (7 WOOD + 7 YARN): daytime warmth becomes FREE;
   nights still draw 3/hour -- the fire you stop paying for by day is
   the one you need at dusk.
6. TRAPs: convert surplus WOOD+YARN into the best hunt table.
A tooled house gathers ~2.5 food per LABOR against a ~0.6/hour burn -- the
surplus is what markets are for. The starter script never builds ANY of
this: it is the floor you inherit, not the ceiling.

== THE TRADING POST ==
THE POST TRADES COIN FOR WOOD, MEAT, YARN, FLINT and BERRIES, and it
sells safe food (BERRIES, COOKED_MEAT while they last, and JERKY --
salted meat that never rots, so the shop always has food). The trader
is a man who has done this a while, and it shows: his hearth never
dies, he never speaks after dark, and what comes at him in the night
he answers armed (he hits like a wolf and guards like one tooled up
-- worse things than wolves have tried). He is killable flesh all
the same -- 20 HITS like yours -- and he cannot be everywhere. Kill
him and his shelf and his purse go to whoever has hands: his death
is an estate, the estate is loot, and the market dies with him. He
is a BUSINESS (no needs): he does not starve, freeze, or age; only
violence ends him. His prices haggle: each sale raises its ask 5%,
each purchase it fills lowers its bid 5%, and 3 quiet ticks move prices
the other way (ask -5%, bid +3%). His bids are small (4 units, and
never more than his COIN covers) and he stops bidding for a good he
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
    """The market maker: a man who has done this a while (a BUSINESS
    body: no needs -- he does not starve or freeze; his hearth is the
    world's doing, auto-issued WARMTH), with a purse of COIN, a
    larder of safe food, and the haggling behaviour. He is killable
    flesh like anyone -- 20 innate HITS -- but very hard to kill:
    ATTACK 4 / DEFENSE 4 (careful, tooled up), firelit and sheltered
    (deterrence turns wolves at his door), silent after dark, and he
    answers what bites him. Kill him and his shelf and purse go to
    whoever has hands: he is a man, not a building -- his death is an
    estate, and the estate is loot."""
    post = services.create_entity(session, "Trading Post", EntityType.BUSINESS)
    services.create_account(session, post, COIN, initial_balance=POST_COIN)
    for sym, qty in POST_FOOD.items():
        markets.adjust_holding(session, post, sym, qty)
    combat.create_stat(session, post.id, "HITS", Decimal("20"))
    combat.create_stat(session, post.id, "ATTACK", Decimal("4"))
    combat.create_stat(session, post.id, "DEFENSE", Decimal("4"))
    combat.create_stat(session, post.id, "CARRY", Decimal("100"))
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
    _create_combat(session)
    spawn_trading_post(session)
    make_wolf(session, "Wolf Pack I")
    make_wolf(session, "Wolf Pack II")
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
    # The action ration: one auto-issued labor-HOUR per hour of DAYLIGHT
    # (14 a day, none at night — the clock, run 18). Unspent labor
    # fades fast: use it or lose it.
    goods.create_good(
        session, "LABOR", name="Labor",
        description="One labor-hour, auto-issued to every individual each "
                    "HOUR OF DAYLIGHT (06..19; night issues nothing). "
                    "Unspent labor fades fast: use it or lose it.",
        decay_per_tick=Decimal("0.5"),
        auto_issue_quantity=Decimal("1"),
        auto_issue_entity_type=EntityType.INDIVIDUAL,
        auto_issue_daylight_only=True,
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
    # ...except JERKY: salted meat keeps forever. The Trading Post always
    # stocks it (run 4's timing gap: agents arrive coin-poor early and
    # coin-rich late, so the late coin needs something to buy that rot
    # did not eat) -- and since the smokehouse landed, a seat can salt
    # its own: SMOKE_MEAT is the preservation path from surplus hunts.
    goods.create_good(session, "JERKY", name="Jerky",
                      description="Salted meat that never rots — bought from "
                                  "the Trading Post, or smoked by hand at a "
                                  "fire (SMOKE_MEAT: a slow fire and a log "
                                  "turns raw meat into the only food that "
                                  "keeps).")
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
    goods.create_good(session, "AXE", name="Stone Axe",
                      description="Held while chopping, never consumed: certain "
                                  "firewood, and a fighting chance the spear "
                                  "does not have to give alone.")
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
    # ticks, not forever. SATIETY is the stomach: filled only by EAT
    # recipes (conscious eating, run 19 -- the engine no longer chews
    # for you), drawn by the FOOD need every hour, and spilling a tenth
    # of itself each hour besides -- a banked belly keeps about a day,
    # no more (compound spill: a full stomach is a wasting asset).
    # The trader's hearth: the world keeps a standing fire lit for
    # its businesses (he has done this a while; fire and shelter are
    # why wolves almost never reach him). Houses get no such mercy --
    # their warmth is their own labor. Top-up of 1 survives the night
    # pass order (issues before scripts resolve attacks, decay after).
    goods.create_good(session, "WARMTH", name="Warmth",
                      description="A flow, not a stock to hoard: made by fires, "
                                  "shelter and clothes, fades fast. The WARMTH "
                                  "need drinks it every tick.",
                      decay_per_tick=Decimal("0.2"),
                      auto_issue_quantity=Decimal("1"),
                      auto_issue_entity_type=EntityType.BUSINESS)
    goods.create_good(session, "SATIETY", name="Satiety",
                      description="The stomach. Only EAT recipes fill it "
                                  "(berries thin, jerky dense, raw meat a "
                                  "disease lottery); the FOOD need draws it "
                                  "every hour and it spills on its own. Meals "
                                  "are free, instant and night-legal -- but "
                                  "they do not happen by themselves.",
                      decay_per_tick=Decimal("0.1"))
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
    # Wolves (run 20): creatures, not pressure. A wolf is an ENTITY --
    # stats, health, needs, a hunting program -- and combat happens
    # between entities (combat.py, rules below). HITS is every
    # creature's health: a holding, drained by damage, zero is death
    # through the ordinary incapacity/estate machinery. PELT is the
    # trophy the victor seizes; the post pays for it.
    goods.create_good(
        session, "HITS", name="Hits",
        description="Health of the body: every creature spawns with a "
                    "stock, combat drains it, and zero is death -- the "
                    "same estate rule as any other. It does not grow back.",
    )
    goods.create_good(
        session, "PELT", name="Wolf Pelt",
        description="Seized from a killed wolf; the post pays for "
                    "trophies.",
        decay_per_tick=Decimal("0.05"),
    )


def _create_combat(session: Session) -> None:
    """The physics of fighting (run 20): stats are born, weapons are
    carried, rules are declared. Resolution: hit% = clamp(50 + 5 x
    (ATK - DEF), 5, 95); damage = max(1, ATK - DEF), +1 on a clean
    opening; daylight refuses the hunt; a lit hearth (WARMTH >= 1)
    turns the attacker at the door (a loud miss -- the world hears
    it); the victor seizes the loot. Wolves: ATK 4 / DEF 1 / 12 HITS.
    Houses: ATK 1 / DEF 1 / 20 HITS, +3 ATK with a spear, +2 more with
    an axe, +1 DEF in clothes. A cold unarmed house bleeds ~3 a night-hour
    at 65% --
    roughly two dark nights of being hunted; a spear makes the duel
    even (65% both ways, 4 hits kills a wolf: pelt + 3 meat). A kill
    is a carcass: MEAT 3 is torn from it by any victor; the "*"
    estate (everything the dead carried, purse included) moves only
    to a victor with the CARRY stat -- houses inherit, wolves just
    eat."""
    combat.set_rules(session, {
        "night_only": True,
        "deterrence": {"WARMTH": 1},
        "weapons": {"SPEAR": 3, "AXE": 2},
        "armor": {"CLOTHES": 1},
        "loot": {"*": 1, "MEAT": 3},
        "carry_stat": "CARRY",
        "bite_loot": {"MEAT": 1},
        "base_hit": 50, "per_point": 5,
    })
    spawns.set_script_source(
        session, "wolf", _gate_pack_script("wolf_pack.lua"))
    spawns.set_rules(session, {
        "from_round": 5, "every_rounds": 5, "up_to": 3, "max_alive": 4,
        "name_prefix": "Wolf Pack",
        "template": {
            "entity_type": "individual",
            "stats": {"ATTACK": 4, "DEFENSE": 1, "HITS": 12},
            "holdings": {"MEAT": 1, "PELT": 1},
            "script_setting": "wolf",
            "account": {"COIN": 0},
        },
    })


def _create_recipes(session: Session) -> None:
    D = Decimal

    # --- Subsistence: gather and hunt --------------------------------------
    # One gather = one loot-table roll of ONE resource (you find what you
    # find): 45% 3 BERRIES, 25% 2 WOOD, 15% 1 YARN, 15% 1 FLINT... and on
    # the doubled BAG table a ~5% branch of 1 COIN -- shiny stones, minted
    # by the ground itself (production credits a banked symbol to the
    # account, production._credit_output). The bare table finds none:
    # scarcity first, then the supply grows with better tools.
    # Expected food value 1.35/hour against a need of 0.5/hour -- bare
    # subsistence spends ~1/3 of the 14 daylight hours on food.
    production.create_recipe(
        session, "GATHER", name="Gather",
        description="One loot-table roll of a single resource: you find what "
                    "you find. Bare-handed subsistence. Daylight only.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=1,
        requires_daylight=True,
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
                    "has minted a coin. Daylight only.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=1,
        good_requirements={"BAG": D("1")},
        requires_daylight=True,
        branches=[
            {"weight": D("40"), "outputs": {"BERRIES": D("6")}, "label": "berries"},
            {"weight": D("22"), "outputs": {"WOOD": D("4")}, "label": "wood"},
            {"weight": D("13"), "outputs": {"YARN": D("2")}, "label": "yarn"},
            {"weight": D("13"), "outputs": {"FLINT": D("2")}, "label": "flint"},
            {"weight": COIN_WEIGHT, "outputs": {COIN: D("1")}, "label": "shiny"},
            {"weight": D("7"), "outputs": {}, "label": "nothing"},
        ],
    )
    production.create_recipe(
        session, "CHOP_WOOD", name="Chop Wood",
        description="The axe's whole point: an hour at the treeline, three "
                    "certain logs -- no loot table, no gamble. Daylight only.",
        inputs={"LABOR": D("1")},
        outputs={"WOOD": D("3")}, duration_ticks=1,
        good_requirements={"AXE": D("1")},
        requires_daylight=True,
    )
    # Hunting: slow (2 ticks), risky bare-handed (55% total loss), better
    # with a SPEAR held (never consumed) and best with TRAPs (consumed --
    # the supply chain: wood+yarn per hunt).
    production.create_recipe(
        session, "HUNT", name="Hunt",
        description="Slow, and bare-handed: mostly nothing, sometimes "
                    "dinner. Daylight only.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        requires_daylight=True,
        branches=[
            {"weight": D("55"), "outputs": {}, "label": "nothing"},
            {"weight": D("35"), "outputs": {"MEAT": D("2")}, "label": "small"},
            {"weight": D("10"), "outputs": {"MEAT": D("4")}, "label": "big"},
        ],
    )
    production.create_recipe(
        session, "HUNT_SPEAR", name="Hunt with a Spear",
        description="A held spear (never consumed) turns a desperate hunt "
                    "into a living. Daylight only.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        good_requirements={"SPEAR": D("1")},
        requires_daylight=True,
        branches=[
            {"weight": D("25"), "outputs": {}, "label": "nothing"},
            {"weight": D("55"), "outputs": {"MEAT": D("3")}, "label": "small"},
            {"weight": D("20"), "outputs": {"MEAT": D("6")}, "label": "big"},
        ],
    )
    production.create_recipe(
        session, "HUNT_TRAPS", name="Hunt with Traps",
        description="The best odds craft can buy, at the price of a consumed "
                    "trap per hunt. Daylight only.",
        inputs={"LABOR": D("1"), "TRAP": D("1")},
        outputs={}, duration_ticks=3,
        requires_daylight=True,
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
                    "you well into the night — bank warmth before dark.",
        inputs={"LABOR": D("1"), "WOOD": D("1")},
        outputs={"WARMTH": D("10")}, duration_ticks=1, requires_facility="FIRE",
    )
    production.create_recipe(
        session, "COOK_MEAT", name="Cook Meat",
        description="Fire-cooked meat: no disease, keeps a little better.",
        inputs={"LABOR": D("1"), "MEAT": D("2")},
        outputs={"COOKED_MEAT": D("2")}, duration_ticks=1,
        requires_facility="FIRE",
    )
    # The smokehouse: preservation as a craft. Smoking is deliberately
    # SLOW (5 ticks vs cooking's 1) and costs a log -- you pay time and
    # wood for food that never rots. It is the house-made answer to the
    # post's salted shelf, and the only way a hunt outlives the week:
    # meat decays at 0.30/tick, jerky at 0. Run 16's houses starved
    # beside rotting larders; now the ladder has a preservation rung.
    production.create_recipe(
        session, "SMOKE_MEAT", name="Smoke Meat",
        description="Slow-smoke raw meat over your fire: a log and a long "
                    "wait turn hunts into jerky — the only food that never "
                    "rots. Time is the price of permanence.",
        inputs={"LABOR": D("1"), "MEAT": D("2"), "WOOD": D("1")},
        outputs={"JERKY": D("2")}, duration_ticks=5,
        requires_facility="FIRE",
    )
    # --- Eating: meals as decisions (run 19) --------------------------------
    # Conscious eating: the FOOD need drinks only SATIETY, and only EAT
    # recipes fill the stomach. All meals are labor-free, instant
    # (duration 0: satiety lands before this tick's draw) and
    # night-legal -- hunger does not keep daylight hours. The density
    # ladder is the point: berries are thin (2 berries ~ 3 hours),
    # jerky dense (one strip ~ 5½ hours, and it never rots) -- the trade
    # a trader should arbitrage, the craft a hunter should practice.
    # A fed body burns 0.5/hour plus a tenth of the stomach each hour
    # (compound spill), so a day costs ~14: two jerky meals and change,
    # or a dozen-and-a-half berries eaten as you gather them.
    production.create_recipe(
        session, "EAT_BERRIES", name="Eat Berries",
        description="A belly of berries: thin food, eaten as gathered -- "
                    "they spoil within hours anyway. ~3 hours fed.",
        inputs={"BERRIES": D("2")}, outputs={"SATIETY": D("2")},
        duration_ticks=0,
    )
    production.create_recipe(
        session, "EAT_COOKED", name="Eat Cooked Meat",
        description="Fire-cooked, safe, satisfying: ~4 hours fed per meal.",
        inputs={"COOKED_MEAT": D("1")}, outputs={"SATIETY": D("2.4")},
        duration_ticks=0,
    )
    production.create_recipe(
        session, "EAT_JERKY", name="Eat Jerky",
        description="The densest meal in the world: one strip ~ 5½ hours "
                    "fed, and the strip itself never rots. Preservation "
                    "you can taste.",
        inputs={"JERKY": D("1")}, outputs={"SATIETY": D("3.6")},
        duration_ticks=0,
    )
    # Eating raw: free (no LABOR -- desperation does not wait), instant
    # (duration 0: SATIETY lands before this tick's consumption pass), and
    # a 25% chance of DISEASE. The thinnest meal at the highest risk --
    # the alternative to cooking, priced in disease. ~1 hour fed.
    production.create_recipe(
        session, "EAT_RAW", name="Eat Raw Meat",
        description="Desperation does not wait: free, instant, ~1 hour fed -- "
                    "and a one-in-four chance of disease.",
        inputs={"MEAT": D("1")}, outputs={}, duration_ticks=0,
        branches=[
            {"weight": D("75"), "outputs": {"SATIETY": D("0.6")}, "label": "fine"},
            {"weight": EAT_RAW_DISEASE_WEIGHT,
             "outputs": {"SATIETY": D("0.6"), "DISEASE": D("1")}, "label": "sick"},
        ],
    )

    # --- The night has teeth: fighting, between creatures (run 20) ----
    # Combat is not a recipe: it is the attack intent resolved by
    # combat.py under the declared rules (see _create_combat). Any
    # entity may attack any entity it can name; the spear simply prices
    # into ATTACK (+3). The recipes that matter here are the wolf's
    # biology: raw meat is dinner (their constitution, not ours), and
    # PACE is a moving animal keeping itself warm.
    production.create_recipe(
        session, "PACE", name="Pace",
        description="A moving animal stays warm: wolves den awake. "
                    "Labor-free, night-legal.",
        inputs={}, outputs={"WARMTH": D("6")}, duration_ticks=1,
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
    # SPEAR (held, never worn) and BAG (held) upgrade hunt and gather;
    # AXE (held) makes the wood certain and fights at +2 when the spear
    # is on the wall. TRAP is ammunition. BED is declared for the future
    # REST mechanics -- craftable and tradeable now, mechanically idle
    # (the expansion hook).
    production.create_recipe(
        session, "MAKE_SPEAR", name="Make Spear",
        description="An afternoon at the whetstone: three honest hours of "
                    "hafting and binding. Daylight only.",
        inputs={"LABOR": D("1"), "FLINT": D("1"),
                                        "WOOD": D("2"), "YARN": D("1")},
        outputs={"SPEAR": D("1")}, duration_ticks=3,
        requires_daylight=True,
    )
    production.create_recipe(
        session, "MAKE_AXE", name="Make Axe",
        description="An afternoon of knapping and lashing: a stone head on a "
                    "haft. Held, never consumed. Daylight only.",
        inputs={"LABOR": D("1"), "FLINT": D("1"),
                                        "WOOD": D("1"), "YARN": D("1")},
        outputs={"AXE": D("1")}, duration_ticks=3,
        requires_daylight=True,
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
    # FOOD: SATIETY only -- the stomach. Conscious eating (run 19): the
    # engine no longer eats for you; EAT recipes are the only path from
    # a full larder to a fed body, and starving beside one is the
    # experiment, not a bug. Unmet -> HUNGER, as ever.
    needs.create_need(
        session, "FOOD", FOOD_PER_TICK, ["SATIETY"],
        name="Food",
        description="The stomach's hourly draw (0.5). Only EAT recipes fill "
                    "it -- EAT_BERRIES (thin), EAT_COOKED, EAT_JERKY (dense), "
                    "EAT_RAW (a disease lottery). A larder is not a meal: "
                    "food in holdings satisfies nothing until you eat it. "
                    "Miss the draw and hunger accrues.",
        entity_type=EntityType.INDIVIDUAL, priority=0,
        condition_symbol="HUNGER", condition_quantity=Decimal("1"),
    )
    # WARMTH: a flow good, stocked by fire/shelter/clothes. Unmet ->
    # EXPOSURE. The clock (run 18): 1/hour by day, 3/hour at night --
    # shelter+clothes still cover the day exactly (1.0 + 0.5 drips vs 1
    # draw is surplus), but no capital covers a 3-draw night alone:
    # every night wants either the fire tended at dusk or the warmth
    # stock banked against it. Night is the expensive half of the day.
    needs.create_need(
        session, "WARMTH", WARMTH_PER_TICK, ["WARMTH"],
        name="Warmth",
        description="Drawn from the WARMTH stock made by fires, shelter and "
                    "clothes: 1 per hour of day, 3 per hour of night. Miss "
                    "it and exposure accrues -- nights bite three times as "
                    "hard, and only a tended fire covers them.",
        entity_type=EntityType.INDIVIDUAL, priority=1,
        condition_symbol="EXPOSURE", condition_quantity=Decimal("1"),
        night_quantity_per_tick=WARMTH_PER_NIGHT_TICK,
    )


def _create_markets(session: Session) -> None:
    _NAMES = {
        "LABOR": "Labor", "BERRIES": "Berries", "MEAT": "Raw Meat",
        "COOKED_MEAT": "Cooked Meat", "JERKY": "Jerky", "WOOD": "Wood",
        "YARN": "Yarn", "FLINT": "Flint", "SPEAR": "Spear", "AXE": "Stone Axe",
        "BAG": "Bag",
        "TRAP": "Trap", "CLOTHES": "Clothes", "BED": "Bed",
        "PELT": "Wolf Pelt",
    }
    for symbol in ("LABOR", "BERRIES", "MEAT", "COOKED_MEAT", "JERKY", "WOOD",
                   "YARN", "FLINT", "SPEAR", "AXE", "BAG", "TRAP", "CLOTHES", "BED",
                   "PELT"):
        markets.create_market(session, symbol, COIN, name=_NAMES[symbol])


# ---------------------------------------------------------------------------
# The agent seat
# ---------------------------------------------------------------------------

def make_house(session: Session, name: str = "House") -> Entity:
    """One symmetric stone-age seat: coins, a day of berries, a night of
    warmth, a body (20 HITS, ATTACK 1 / DEFENSE 1 -- the spear is
    carried, not born), and a bare CAMP parcel -- no fire, no shelter,
    no tools, no unlocks. Everything beyond the body is the player's
    to build."""
    house = services.create_entity(session, name, EntityType.INDIVIDUAL)
    services.create_account(session, house, COIN, initial_balance=SEAT_COIN)
    markets.adjust_holding(session, house, "BERRIES", BERRY_BUFFER)
    markets.adjust_holding(session, house, "WARMTH", WARMTH_BUFFER)
    markets.adjust_holding(session, house, "HITS", HOUSE_HITS)
    combat.create_stat(session, house.id, "ATTACK", Decimal("1"))
    combat.create_stat(session, house.id, "DEFENSE", Decimal("1"))
    combat.create_stat(session, house.id, "HITS", HOUSE_HITS)
    # Hands: a house can carry what it kills. Wolves lack the stat --
    # what a beast kills rots where it fell.
    combat.create_stat(session, house.id, "CARRY", Decimal("20"))
    parcels.create_parcel(session, "LAND", name=f"{name}'s Camp", owner=house)
    return house


def make_wolf(session: Session, name: str) -> Entity:
    """One wolf: a creature, not a mechanic. Same needs as a house
    (hunger is why it hunts), 12 innate HITS, ATTACK 4 / DEFENSE 1,
    a starting strip of meat, and the pelt it wears -- killing one
    pays whoever can carry it. No CARRY: what a wolf kills rots where
    it fell; it eats the bite and the carcass, never the estate."""
    return spawns.spawn_one(session, name, {
        "entity_type": "individual",
        "stats": {"ATTACK": 4, "DEFENSE": 1, "HITS": 12},
        "holdings": {"MEAT": 1, "PELT": 1},
        "script_setting": "wolf",
        "account": {"COIN": 0},
    })
