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
their ears), eat raw meat -- their iron stomach makes a proper meal of
carrion (EAT_CARRION: the born CARNIVORE trait -- a kill wrung dry,
jerky-dense, never sickening), and keep warm by PACE. The packs RANGE (run
26's census: denned wolves whose houses slept out of reach starved
in two days -- the map made sit-and-wait a death sentence): by day
they run the same game the houses hunt, and in the last daylight a
hungry pack walks -- three hours of road to the fire-ground where
the people sleep, walked while the light lasts (wolves carry no
torch: the dark road is not theirs), home again by daybreak. Players
are not their only food; they are the
rich exception. Combat (combat.py) is entity-vs-entity under the
pack's COMBAT_RULES: daylight refuses, a lit hearth -- or a burning
torch -- DETERS (a loud
miss), hit% = clamp(50 + 5*(ATK-DEF), 5, 95) on the commit-reveal RNG,
damage = max(1, ATK-DEF) (+1 crit), HITS drain, zero = the ordinary
incapacity/estate machinery, victor seizes {PELT 1, MEAT 3}. Houses
are ATTACK 1 / DEFENSE 1 / 20 HITS; weapons are carried, not born
(SPEAR +3 ATK, CLOTHES +1 DEF). Population renews via spawns.py at
round boundaries: from round 5, every 5 rounds, up to 2 more packs,
never more than 3 alive (run 23 eased the wave from 3-and-4: the
siege was scenery, but 11 packs a run fed the post six kills and a
jerky empire while houses starved beside noise). The failure modes to watch: the fireless
hunted down over two cold nights, and the loud targeted by name --
speech is free by day, priced at night.

THE MAP (S4, docs/spatial.md): the world has places, and they are
hours apart. Seats wake at the Hearth clearing (the fire-ground: the
commons fire stands there); the berry thicket is 1h (gather/chop), the river 2h
(FISH: certain-ish meat, no wolves), the flint scrape 2h (certain
flint), the deep forest 3h (the hunts, and the wolves' dens), the
trading post 1h, down the valley -- an easy walk (the forest and
river roads still run there, the long ways round). Presence gates bind work to
places; EVERY market trades at the post and only there; travel is
the TRAVEL_WALK recipe, one Process per hop (an hour a hop, priced
by the road, labor-free; the DARK road wants LIT -- see TORCHES),
and a traveller stands at a
hop's origin until arrival -- the road exposes you place by place,
and co-location gates both the bite and the prowl. The starter
script walks: hand-to-mouth now has a commute.

TORCHES (P2, the night kit): darkness got its teeth back. The
conditions register (statuses.py; the pack declares LIT / EMBER /
LOUD under the `conditions.rules` world setting) names per-tick,
queryable states that gates and scripts alike read by name: LIT = a
burning torch (a lit brand above a quarter-brand); EMBER = its
memory (LIT now, or lit within the last two ticks -- the window a
dying brand may still strike its successor); LOUD = torchlit night
departures and speech, graded by count (world.who_is_loud() --
wolves aim at it). The night gate is ambient (travel.rules:
night travel refuses the flameless; a route whose flame dies
mid-road waits two ticks for a relight, then strands), deterrence
accepts LIT beside WARMTH, and the kit is three recipes: MAKE_TORCH
(1 WOOD + 1 YARN, daylight), LIGHT_TORCH (at any lit fire, free --
the commons strikes brands), CHAIN_TORCH (off your own flame or its
ember, anywhere). Decay is proportional (half the flame an hour): a
fresh brand burns ~2h, a full carry of two ~3h -- the dark road is
an inventory and a cadence, not a fee. Wolves never carry torches:
their raid walks at dusk, holds when caught out, and is home by
daybreak (the departure gate is a FINISH-in-light gate: hour + road
< 20, never a dark road -- run 35's packs died stranded mid-road);
the starter travels by day only.

THE LARDER (P5, the relief of the income wall): ten runs of census
said the famine was never storage -- every house starved with empty
holdings. The wall breaks on the INCOME side, three rungs at once:
the ORCHARD (apples on both gather tables: bare-handed food income
rises ~47%, and a day-and-a-half shelf makes surplus worth
banking); the PEN (hens are live capital: bought at the post, penned
at camp, and COLLECT_EGGS scales with the flock -- one egg per hen,
capped at four, one labor hour serves the whole flock; the first
income that arrives while you sleep); and the BOW (the best day
hunt, ranged, and +3 ATK in a fight). The fourth piece is the BOAR:
dangerous prey at the thicket -- born carrying ~9 meat and a double
pelt, tusk 4/hide 2, retaliates and remembers -- the guarded shelf
of the larder, a torchlit night errand with real death risk. Engine
surfaces new in P5: spawns.rules takes a LIST of programs (wolves
and boars, separate cadences and lairs), and recipes scale with
holdings (scales_with: outputs x min(cap, floor(held)) at
completion; the honest catalog says so). Numbers are provisional --
cam rebalances with run data.

WATER (P3, the thirst clock): every individual draws 0.25 water an
hour -- six a day -- and the tap is the river: DRINK is free, instant
and night-legal at the bank, and FILL_SKIN tops a waterskin (1 PELT
to make, the post stocks one) with eight carried hours. Cupped hands
carry four (WATER, max_holding 4); a skin eight (SKINWATER, seeps a
little). Meals part-hydrate -- berries and apples wet (0.6), jerky
nearly dry (0.2) -- so the day-1 berry-grazer rides under the thirst
equilibrium while the run-36 shape (a fed jerky-and-egg larder that
never walks to the river) dries out and dies on day 3: THIRST targets
the rich, which is the point of the relief/pressure alternation. The
river road runs past the thicket (2h -- the gather commute drinks),
and FISH and DRINK share a walk. The beasts drink too -- wolves run
the post road to the bank, boars the thicket road; the river is the
one place every body in the world must visit. No engine surfaces new
in P3: multi-output meals, place-gated recipes, condition goods and
need satisfiers all existed; water is pure content. Numbers
provisional -- the census tunes the tap.

Goods: MEAT, BERRIES, APPLES, WOOD, YARN, FLINT (gathered/hunted),
COOKED_MEAT,
JERKY (smoked or bought), EGGS and CHICKEN (the larder: capital
that lays), WATER, SKINWATER and WATERSKIN (P3: the cup, the skin,
and the craft that turns a pelt into capacity), SPEAR, BAG, BOW, TRAP,
CLOTHES, BED, TORCH /
LIT_TORCH (the night kit), plus the flows
WARMTH/SATIETY and the
conditions HUNGER/EXPOSURE/DISEASE/THIRST. Money is COIN — found, not endowed:
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

from econengine import (
    combat, edges, goods, markets, needs, parcels, places, production,
    scripting, services, spawns, statuses, tech, travel,
)
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
# What a seat starts with: a few days of food and one cold evening's
# warmth (the WARMTH cap is 6 -- two dark hours' grace, no more). Not a
# lifestyle -- a runway. Bare survival (no tools, no capital) runs a LABOR
# deficit against FOOD+WARMTH, so these buffers only postpone the reckoning;
# capital (fire, clothes, shelter, tools) is the only way to a surplus.
BERRY_BUFFER = Decimal("8")
WARMTH_BUFFER = Decimal("6")

# --- The commons fire (P1, the fire rework: ROADMAP.md) --------------------
# One fire warms many. The fire-ground's standing FIRE is PUBLIC (four
# seats -- WARM_BY_FIRE occupies one for its hour, like every facility
# process), burns one fuel per hour WHETHER ANYONE SITS OR NOT, and banks
# at most six hours of burn (STOKE_FIRE: 1 WOOD = 2 hours -- splitting
# the log is the craft). No one owns it; anyone may stoke, warm, or cook
# at it, and everyone freezes when no one feeds it. WARMTH itself caps
# at 6 -- a body holds about two cold hours' grace -- so banking warmth
# is not a strategy (until hot-water-bottle tech); warming is presence
# at a lit fire. A night is ten dark hours: a dusk-full bank (6) plus a
# stoke or two from whoever sits carries the village for ~3 WOOD --
# against ~12 and a dozen labor-hours when every house banked alone.
WARMTH_CAP = Decimal("6")
FIRE_SEATS = 4
FIRE_FUEL_START = Decimal("6")
FIRE_FUEL_CAP = Decimal("6")
FIRE_FUEL_BURN = Decimal("1")
STOKE_FUEL = Decimal("2")
WARM_BY_FIRE_WARMTH = Decimal("6")

# --- Torches: the night kit (P2, ROADMAP.md) ------------------------------
# Darkness is ambient: night travel refuses you without LIT (a burning
# torch above the floor), travel-recipe gates stay untouched. A brand
# burns down HALF ITS FLAME an hour (decay is proportional), and the
# register calls LIT while a quarter-brand or more burns -- so a fresh
# brand lights two hours, a full carry of two burns about three, and
# the road's craft is to re-strike inside each ember window. Brands
# light from any lit fire (LIGHT_TORCH -- the commons strikes them) or
# off your own flame/its dying ember (CHAIN_TORCH wants EMBER: the
# 2-tick window after a burnout). Torchlit night departures are LOUD
# -- every listening pack hears where you walk.
TORCH_DECAY = Decimal("0.5")
TORCH_CARRY = Decimal("2")
LIT_TORCH_FLOOR = Decimal("0.25")
EMBER_TICKS = 2

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
#   THIRST:   grant 0.5/tick, decay 0.05 -> equilibrium 10, dies at 7.5.
#             Carried water delays the climb, the climb kills: cup 4 +
#             skin 8 (~2 dry days net of meal credits) then ~a day's
#             climb -- a fed larder that never walks to the river dies
#             on day 3 (P3: thirst targets the RICH -- jerky eats dry).
#             Meals part-hydrate, so the berry-grazer rides under the
#             threshold (equilibrium ~5 on a wet diet: day 1 is never a
#             death march, and the gatherer's road passes the river).
FOOD_PER_TICK = Decimal("0.5")
WARMTH_PER_TICK = Decimal("1")
WARMTH_PER_NIGHT_TICK = Decimal("3")
EAT_RAW_DISEASE_WEIGHT = Decimal("25")   # out of 100 per raw meal

# --- Water (P3, ROADMAP.md) -----------------------------------------------
# Thirst is the larder's counterweight: preservation is DRY, and the
# world's best diets (jerky, eggs -- run 36's survivor) hydrate least.
# The tap is the river (free, two hours out -- the FISH walk doubles as
# the water walk); the waterskin turns a dead wolf into portable
# capacity (1 PELT -> 8 carried hours). The cup everyone carries free
# (4) plus a skin (8) is ~2 dry days -- the river is a rhythm, not a
# treadmill: drink when you fish, fill on the gather road home (the
# river road runs past the thicket).
WATER_PER_TICK = Decimal("0.25")
THIRST_GRANT = Decimal("0.5")
WATER_CUP = Decimal("4")
WATERSKIN_CAP = Decimal("8")
WATERSKIN_DECAY = Decimal("0.02")
WATER_SEAT_BUFFER = Decimal("4")
WATER_BEAST_BUFFER = Decimal("2")

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
refuses gathering and hunting (too dark); stoking, warming, cooking,
smoking, eating and resting all work. WARMTH draws 3/hour at night vs
1 by day. THE FIRE IS A COMMONS, not a pantry: warmth is a SEAT, not a
stock -- a body holds at most 6 WARMTH (two cold hours' grace), and
only sitting at a lit fire fills it (WARM_BY_FIRE: +6 an hour seated,
labor-free, night-legal). The standing fire at the clearing seats FOUR
at once, burns one fuel an hour whether anyone sits or not, and banks
at most six hours (STOKE_FIRE: 1 WOOD = 2 hours, free and instant --
refused when the fire is fully banked). No one owns it; anyone may
feed it, warm at it, cook on it -- and everyone freezes when no one
feeds it. Keep a seat and keep it fed (std.hour() and std.is_night()
read the clock). Unspent labor is nearly worthless.

MEALS ARE DECISIONS: nothing is eaten for you. Meals are labor-free,
instant and night-legal -- but they do not happen by themselves.
The FOOD need drinks SATIETY at 0.5/hour plus a tenth of the stomach
each hour (a day costs ~14), and only EAT recipes fill the stomach:
EAT_BERRIES (1½ berries, ~3h), EAT_APPLES (1½ apples, ~5h -- they keep
a day and a half), EAT_COOKED (~4h), EAT_EGGS (~4h, keeps near a
week), EAT_JERKY (~5½h, never rots),
EAT_RAW (~1h, one-in-four disease). A full larder feeds nobody until
someone runs the recipe: starving beside one is a choice, and the
clock will make it for you if you let it -- two meals a day is the
natural cadence.
WATER IS THE SECOND STOMACH: the body draws 0.25 an hour (six a day)
from carried water -- the cup everyone is born with holds four
(DRINK at the river fills it, free and instant, night-legal), and a
WATERSKIN (made from a single PELT, or bought at the post) carries
eight more. Meals part-hydrate -- a berry or apple meal carries a
drink's worth (0.6), cooked a little more, but jerky eats dry (0.2)
-- so the wet diet of day 1 never kills, while a rich dry larder
(jerky, eggs) dries you out: THIRST's equilibrium rides above its
death threshold, cup and skin buy about two dry days, and a third
day without the river is the last. The river is two hours out and
its road runs past the thicket: drink when you fish, fill on the
gather road home. The skin seeps a little (0.02/hour) -- carried
water is a rhythm, not a bank.

THE NIGHT HAS TEETH: wolves are creatures -- entities with stats,
health (HITS) and hunger, the same physics as you. They hunt in the
dark, guided by sound: EVERY say at night tells a listening pack where
you are. A lit hearth (WARMTH >= 1) turns a wolf at the door; a spear
(+3 ATTACK) or clothes (+1 DEFENSE) prices into the fight: hit% =
50 + 5 x (ATTACK - DEFENSE), damage = max(1, ATK - DEF). A landed
bite feeds the wolf (it tears flesh); a kill is a carcass: MEAT is
torn from it by any victor, while everything the dead CARRIED moves
only to victors with hands -- wolves cannot loot, what a beast kills
rots where it fell. And the packs are not beggars at your door: they
LIVE off the forest (the CARNIVORE gut makes carrion a proper meal,
as dense as your jerky), ranging for game by day and walking to the
fire-ground only when hunger moves them. Starving them out is not a
strategy here -- meeting one is. A house has 20 HITS and never
regrows them; a wolf has 12, and it WEARS its pelt: kill one and the
pelt and the meat it carried are yours. Combat is an action anyone may take: attack(<entity id>) --
you learn a wolf's id by hearing it hunt (combat is loud: every house
hears every fight). The packs breed: from day 5, every fifth day, up
to three more, never more than four alive.
AND THE THICKET HAS A BOAR: dangerous prey, not a predator -- it never
walks to where you sleep; it lives on the thicket you gather at, born
carrying its own carcass (6 meat and a double pelt on the body, ~9
meat for the kill -- the richest quarry in the world). Tusk 4 against
hide 2: it hits 60% for 2 and it ANSWERS -- attack one and it fights
back, remembering you while you share its ground. The bow's day hunt
never meets a tusk; the spear duel is a coin toss that can kill you.
Boars breed from day 3, every third day, never more than two alive.
Speech is free by day. At night it has a price.

A seated body is warm and a warm body (WARMTH >= 1) turns a wolf at
the door: the fire that feeds you guards you. A fed
entity slowly heals its conditions (~0.95/tick); conditions fade 5%/tick
on their own too, but thresholds are thresholds -- the catalog says
where each one kills.

== THE MAP ==
THE WORLD HAS PLACES, AND THEY ARE HOURS APART. You wake at the
Hearth clearing (the fire-ground: safe nights, fires made and tended
there). The Berry thicket is one hour's walk (berries, wood, yarn);
the river two (FISH: meat without wolves; DRINK: the world's tap) --
and its road runs past the thicket, so the gather commute drinks;
the Flint scrape two
(certain flint); the Deep forest two by the valley road, three by
the deep wood (the hunts -- and the wolves' range); the Trading post
ONE, down the valley (every market trades there, and only there).
Roads run both ways; the deep wood and the river road are the long
ways.
WALKING is the TRAVEL_WALK recipe: an hour per hop, labor-free --
and you stand where a hop started until it ends, so a road exposes
you place by place. ctx.entity.place says where you stand;
world.route(from, to) prices any walk before you take it;
ctx.action.travel(to) sets out (nothing else may move you).
THE DARK ROAD WANTS A FLAME: by night, travel refuses you unless a
TORCH burns in your hand (LIT: a lit torch above a quarter-brand).
MAKE_TORCH (1 WOOD + 1 YARN, daylight) crafts a brand;
LIGHT_TORCH strikes it at any LIT fire (free, instant); CHAIN_TORCH
strikes a fresh one off your own flame -- or off its dying EMBER,
within two ticks of the burnout (anywhere, free, instant). A fresh
brand burns about two hours (it loses half its flame an hour; a
carry is two). Mind the arithmetic: a long dark road is a torch
inventory and the wit to re-strike inside each ember window. And
know what a torch buys: light is LOUD -- every pack in the dark
hears a torchlit walker (as it hears speech). A journey that stalls
in the dark (the flame dies mid-road) waits for a relight -- yours,
or dawn's; after two dark ticks it strands where it stands.
WORK IS WHERE YOU ARE: gathering and chopping want the thicket,
hunting wants the forest, fire-making, stoking and warming want the
hearth, flint-digging wants the scrape, fishing wants the river. The
refusal names where you stand and what the work wants.
THE MARKETS LIVE AT THE POST: every order -- buying and selling both --
requires standing there. An hour out, an hour back: the walk is
cheap, so the question is what you carry and what you quote -- not
whether the trip survives the day. Mind the dark on the way all the
same.

== THE LADDER (rough order; a gather averages ~0.75 of a needed food) ==
1. THE FIRE IS COMMON GROUND: a standing fire at the clearing already
   seats four and cooks for anyone -- keep it fed (1 WOOD stokes two
   hours) and take your seat by dark (WARM_BY_FIRE). MAKE_FIRE (2 WOOD)
   adds four public seats when the clearing is crowded. Do not sleep
   fireless.
1a. TORCHES are the night road: the same commons fire that warms you
   lights your brands (LIGHT_TORCH, free) -- MAKE them by daylight (1
   WOOD + 1 YARN), CHAIN them off your own flame (or its two-tick
   ember) when the road runs long. A burning torch turns a wolf at
   the door, too -- and tells every pack where you walk: a torchlit
   night journey is a loud fact.
2. EAT what spoils first: berries within hours, APPLES within two
   days (the gather's orchard branch -- the shelf between berries
   and jerky), cooked within a day;
   JERKY never spoils -- the deep pantry. (Eating is on the ladder now:
   hunger kills the careless before any tool matters.)
2a. THE RIVER IS THE TAP: keep the cup full and fill a skin when one
   is had -- the dry larder (jerky, eggs) kills on the third day
   without it, and the river road passes the thicket. One PELT makes
   a WATERSKIN: a dead wolf is eight carried hours.
3. BAG (3 YARN-ish, one hour): doubles EVERY future gather, finds COIN.
4. SPEAR (flint+yarn, an afternoon): meat surplus -> COOKED_MEAT stock,
   or SMOKE_MEAT it into JERKY (5 slow hours, costs a log, NEVER rots,
   ~6 hours fed per strip) -> sell MEAT.
4a. AXE (flint+wood+yarn, an afternoon): CHOP_WOOD = 3 certain logs an
   hour, six times the bare gather's wood -- the fire never wants again;
   and +2 ATK at the door, the half of a spear in a fist.
4b. BOW (flint+2 wood+yarn, an afternoon): the best DAY hunt (better
   than the spear's table, nothing hunting you back) and +3 ATK in
   any fight -- ranged capital that pays at the hunt and the door.
5. HENS (see THE LARDER below): a pen and four hens is the best
   food/labor in the world once the capital is paid -- income that
   arrives while you sleep.
6. SHELTER + CLOTHES (7 WOOD + 7 YARN): daytime warmth becomes FREE;
   nights still draw 3/hour -- the fire you stop paying for by day is
   the one you need at dusk.
7. TRAPs: convert surplus WOOD+YARN into the best hunt table.
A tooled house gathers ~2.5 food per LABOR against a ~0.6/hour burn -- the
surplus is what markets are for. The starter script never builds ANY of
this: it is the floor you inherit, not the ceiling.

== THE LARDER ==
THE INCOME WALL, AND HOW IT BREAKS: every world so far starved its
houses beside wealth -- the food a body FINDS (0.5/hour needed) cost
more daylight than the day had. The ladder's new rungs answer on the
income side. THE ORCHARD BRANCH: the gather tables now find APPLES
(bare-handed ~3.5 satiety-equivalent/hour, a bag ~7) -- apples keep
a day and a half, so the surplus is worth banking, and EAT_APPLES is
a proper meal (~5 hours fed).
HENS ARE CAPITAL THAT LAYS: the post SELLS hens (2 on the shelf to
start, ask ~4 COIN). A body carries at most FOUR -- the pen's worth.
MAKE_PEN (3 WOOD, 2 hours, at your camp) pens them, and COLLECT_EGGS
(one LABOR hour, needs the pen) gathers the day's laying: ONE EGG
PER HEN HELD, up to four -- the whole flock served by one hour. Eggs
keep near a week, feed like a cooked meal (~4 hours), and the post
buys them at ~1.20: a hen returns her price in four eggs -- two days
fed. Four hens is ~9.6 satiety a day for one labor hour: the best
food/labor in the world, paid for once. That is the compounding
rung: the first wealth that works while you sleep. (The honest
catch: eggs scale with the hens HELD at the hour of collection --
sell hens mid-day and the day's laying shrinks with the flock.)
THE BOAR IS THE GUARDED SHELF: see THE NIGHT HAS TEETH. Rich, at
home on the thicket, and it answers a spear with 60% for 2 -- a
night errand (combat refuses the day) that wants a torch, a spear,
and nerve; the bow hunts the forest by DAY and never meets a tusk.


== THE TRADING POST ==
THE POST TRADES COIN FOR WOOD, MEAT, YARN, FLINT, BERRIES, APPLES,
EGGS and PELTS, and it
sells safe food (BERRIES, COOKED_MEAT while they last, and JERKY --
salted meat that never rots, so the shop always has food) -- it
sells HENS (ask ~4: the larder's seed capital; a hen returns her
price in four eggs sold back), and ONE WATERSKIN (ask ~6: a pelt's
worth of carried river -- the price anchor for thirsty houses with
coin and no wolf). The trader
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
             "JERKY": Decimal("30"),
             # The larder's seed stock (P5): two hens on the shelf -- the
             # post is where pastoral capital enters the world. Sell-buys
             # round-trip the flock (a house's surplus hen is a house's
             # coin), but the world's hens start here.
             "CHICKEN": Decimal("2"),   # the salted shelf: JERKY never
                                    # rots, so late-arriving coin always
                                    # has something to buy (run 4: OSS
                                    # died holding 17 COIN beside an
                                    # empty, rotted larder)
             # The water kit's anchor (P3): one waterskin on the shelf
             # -- the price a thirsty house with coin reads. Water
             # itself is never sold: the river is free, and the SKIN
             # is what coin buys.
             "WATERSKIN": Decimal("1")}


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
    # His ground: the post stands at the post-place, where every
    # market trades (S4). Killable flesh, but only reachable on foot.
    places.move_entity(session, post, "POST")
    return post


def create_content(session: Session, verify: bool = True) -> None:
    """Goods, recipes, needs, markets -- the stone-age "physics".

    verify=False is the manifest's counting pass (regen): measurement,
    not installation -- the shipped pins are stale by definition while
    the author is regenerating them."""
    _create_map(session)   # the map first: everything sited below
    _create_commons(session)   # the fire-ground: the village hearth stands
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


def _create_map(session: Session) -> None:
    """The stone-age map (docs/spatial.md S4): six places, eight roads,
    and the walk that binds them. Places first (everything sited below
    resolves against them), then the roads, then the TRAVEL_WALK
    template every hop runs against -- inputs-free, night-legal, and
    priced by the ROAD (edge cost_ticks), not the template.

    Topology, hours from the hearth: thicket 1 (the commute for food
    and wood), river 2 (fish: meat without wolves), flint scrape 2
    (certain flint), deep forest 3 (the hunts and the wolves' range),
    trading post 1, down the valley (run 26's census: four hours made
    the coin cost the day -- the post was a trip houses died taking;
    now it is a neighbor, and the market question is what you carry,
    not whether you survive the walk). The valley road also shortens
    the forest to 2h through the post; the deep wood stays the long
    way in. The thicket-forest cut (2h) makes the hunting circuit
    thicket -> forest -> hearth a day's honest walk."""
    for key, kind, name, description in (
        ("HEARTH", "HEARTH", "Hearth clearing",
         "The fire-ground where every seat wakes: sheltered, warm-stoned, "
         "safe to sleep. Fires are made and tended here."),
        ("THICKET", "THICKET", "Berry thicket",
         "An hour out: berries, wood, yarn -- the subsistence walk."),
        ("RIVER", "RIVER", "The river",
         "Two hours out: fish from the bank, meat without wolves."),
        ("FLINT", "FLINT", "Flint scrape",
         "Two hours out: the ground gives certain flint to anyone who "
         "digs."),
        ("FOREST", "FOREST", "Deep forest",
         "The spear game and the wolves' range: two hours by the valley "
         "road (through the post), three by the deep wood. Be firelit "
         "by dark, or be meat."),
        ("POST", "POST", "Trading post",
         "An hour down the valley: every market trades here, and only "
         "here -- the man keeps a good shelf and a short walk for it."),
    ):
        places.create_place(session, key, kind=kind, name=name,
                            region_id="home-valley", description=description)
    for a, b, cost in (
        ("HEARTH", "THICKET", 1),
        ("HEARTH", "RIVER", 2),
        # The river road runs past the thicket (P3): the gather commute
        # drinks -- and the boar, which never walks to the fire-ground,
        # still reaches the tap. Two hours, the valley's other road.
        ("THICKET", "RIVER", 2),
        ("HEARTH", "FLINT", 2),
        ("HEARTH", "FOREST", 3),
        ("THICKET", "FOREST", 2),
        ("FOREST", "POST", 1),
        ("RIVER", "POST", 2),
        ("HEARTH", "POST", 1),
    ):
        edges.create_edge(session, a, b, "walk", cost)
    # The walk itself: an hour a hop against the road's true length.
    # No LABOR (the day's ration is for work; walking spends the day),
    # no recipe-level daylight gate -- night is the AMBIENT gate's
    # business (travel.rules: the dark road wants a burning torch),
    # no outputs (arrival is the product).
    production.create_recipe(
        session, "TRAVEL_WALK", name="Walk",
        description="One hour afoot along a road: an hour per hop of the "
                    "journey's true length (the road prices the walk). "
                    "Night-legal, labor-free -- but you stand where a hop "
                    "started until it ends, so the road exposes you place "
                    "by place.",
        inputs={}, outputs={}, duration_ticks=1,
    )


def _create_commons(session: Session) -> None:
    """The fire-ground (P1, the fire rework): the village hearth as Genesis
    furniture. One UNOWNED COMMONS parcel at the hearth clearing carrying a
    PUBLIC fire -- four seats, a fuel bank (6 hours max), burning one an
    hour lit or empty. Nobody's property precisely because everybody's
    survival runs through it: stoking is a gift to whoever sits next
    (free-riding is the experiment), and the fire dying at 02:00 is a
    village-wide fact. Houses may still BUILD fires (MAKE_FIRE, public on
    build) for more seats -- the commons is the floor, not the ceiling."""
    ground = parcels.create_parcel(
        session, "COMMONS", name="The fire-ground", place="HEARTH")
    parcels.add_facility(
        session, ground, "FIRE", access="PLACE", capacity=FIRE_SEATS,
        fuel=FIRE_FUEL_START, fuel_capacity=FIRE_FUEL_CAP,
        fuel_burn_per_tick=FIRE_FUEL_BURN,
    )


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
                      decay_per_tick=Decimal("0.15"))
    # The larder staple (P5): apples keep a day and a half where berries
    # rot in a morning -- the orchard branch is the thicket's shelf,
    # between the berry basket and the smokehouse. Runs 26-35 died on
    # the income wall: every house starved with empty holdings, so the
    # wall breaks on the INCOME side (a keeps-staple the hand can find).
    goods.create_good(session, "APPLES", name="Apples",
                      description="Orchard fruit: keeps a day and a half, "
                                  "feeds like a meal -- the larder staple "
                                  "between the berry basket and the "
                                  "smokehouse.",
                      decay_per_tick=Decimal("0.08"))
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
    # Pastoral capital (P5): a hen is the first good that PAYS while you
    # hold it. Durable, capped at four (the pen's worth -- a yard, not
    # a ranch), never consumed: the flock is wealth that compounds, and
    # the eggs it lays keep near a week.
    goods.create_good(session, "CHICKEN", name="Hen",
                      description="Live capital: pen her and she lays. Held, "
                                  "never eaten -- a body keeps at most four "
                                  "(the pen's worth). Sold at the post.",
                      max_holding=Decimal("4"))
    goods.create_good(session, "EGGS", name="Eggs",
                      description="A hen's daily laying: keeps near a week, "
                                  "feeds like a cooked meal. The first "
                                  "income that arrives while you sleep.",
                      decay_per_tick=Decimal("0.05"))
    goods.create_good(session, "TRAP", name="Trap",
                      description="One-shot hunting ammunition — consumed by "
                                  "the traps hunt, the best odds craft can buy.")
    goods.create_good(session, "BOW", name="Bow",
                      description="Ranged capital: flint-tipped, yarn-strung. "
                                  "Held while hunting, never consumed — the "
                                  "day hunt's best odds, and the opening shot "
                                  "prices into any fight (+3 ATTACK).")
    goods.create_good(session, "CLOTHES", name="Clothes",
                      description="Worn warmth: with a shelter, covers the whole "
                                  "WARMTH need forever, free.")
    goods.create_good(session, "BED", name="Bed",
                      description="Craftable comfort, mechanically idle — "
                                  "the expansion hook.")
    # The night kit (P2): a brand is capital you burn. TORCH is inert in
    # the pack; LIT_TORCH is the flame -- decays 0.5/tick (two hours a
    # brand), carries clip at 2, and while one burns above the floor the
    # register says LIT: the dark road opens, wolves turn at your door,
    # and your night departures are loud facts.
    goods.create_good(session, "TORCH", name="Torch",
                      description="A pitch-wrapped brand: inert in the pack, "
                                  "two hours of carried light once lit. "
                                  "Light it at any lit fire, or chain a "
                                  "fresh one off your own flame.")
    goods.create_good(session, "LIT_TORCH", name="Lit Torch",
                      description="A burning brand in the hand: the dark road "
                                  "opens, a wolf at the door thinks twice — "
                                  "and every pack in the dark hears you walk. "
                                  "Burns down half its flame an hour — a "
                                  "fresh brand lights two hours, a full "
                                  "carry of two about three. A body carries "
                                  "two flames.",
                      decay_per_tick=TORCH_DECAY,
                      max_holding=TORCH_CARRY)
    # The water kit (P3): the cup, the skin, the craft. WATER is the
    # cupped-hands carry (free, four hours -- everyone is born holding
    # a full one); SKINWATER rides only in a waterskin (eight hours,
    # seeping 2% -- carried water is a rhythm, not a bank); the
    # WATERSKIN is a pelt sewn shut -- a dead wolf becomes capacity.
    # Neither is marketable: the tap is free, and free goods make no
    # market (the post anchors the SKIN's price instead).
    goods.create_good(session, "WATER", name="Water",
                      description="Carried water, cupped: four hours' worth "
                                  "in any body's hands (DRINK at the river "
                                  "fills it, free). The WATER need draws "
                                  "it 0.25 an hour -- six a day.",
                      max_holding=WATER_CUP)
    goods.create_good(session, "SKINWATER", name="Skinful of Water",
                      description="Water that rides a waterskin: eight "
                                  "carried hours (FILL_SKIN at the river "
                                  "-- the skin must be held), seeping a "
                                  "little as it travels.",
                      decay_per_tick=WATERSKIN_DECAY,
                      max_holding=WATERSKIN_CAP)
    goods.create_good(session, "WATERSKIN", name="Waterskin",
                      description="A pelt sewn shut: the craft that turns "
                                  "one wolf into eight carried hours of "
                                  "river. Held, never consumed -- fill it "
                                  "at the bank (FILL_SKIN). One on the "
                                  "post's shelf, priced for the thirsty.")
    # Flows. WARMTH fades (0.2/tick) and now CAPS at 6 (the fire rework:
    # a body holds about two cold hours -- banking warmth against the night
    # is not a strategy; a SEAT at a lit fire is). SATIETY is the stomach:
    # filled only by EAT recipes (conscious eating, run 19 -- the engine no
    # longer chews for you), drawn by the FOOD need every hour, and spilling
    # a tenth of itself each hour besides -- a banked belly keeps about a
    # day, no more (compound spill: a full stomach is a wasting asset).
    # The trader's hearth: the world keeps a standing warmth for its
    # businesses (he has done this a while), a top-up of 1 -- under the
    # cap, covering the day draw and deterring wolves at his door.
    goods.create_good(session, "WARMTH", name="Warmth",
                      description="A seat at a fire, not a stock to hoard: made "
                                  "by fires (+6 an hour seated, WARM_BY_FIRE), "
                                  "shelter and clothes; fades fast and holds at "
                                  "most 6 -- two cold hours' grace. The WARMTH "
                                  "need drinks it every tick.",
                      decay_per_tick=Decimal("0.2"),
                      auto_issue_quantity=Decimal("1"),
                      auto_issue_entity_type=EntityType.BUSINESS,
                      max_holding=WARMTH_CAP)
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
    # THIRST (P3): the dry-larder killer. Meals part-hydrate (berries
    # and apples wet, jerky nearly dry), so the pressure lands on the
    # rich -- a fed house that never walks to the river. See the
    # equilibrium notes at the constants.
    goods.create_good(
        session, "THIRST", name="Thirst",
        description="The memory of dry days. A wet diet rides below the "
                    "threshold; a dry larder (jerky, eggs) rides above it "
                    "-- the river is the only cure, and cup and skin buy "
                    "about two days.",
        decay_per_tick=Decimal("0.05"),
        incapacitates_at=Decimal("7.5"),
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
        session, "PELT", name="Pelt",
        description="Seized from a killed beast — wolf or boar; the post "
                    "pays for trophies. A boar's is twice a wolf's.",
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
    eat. Boars (P5): ATK 4 / DEF 2 / 12 HITS, born carrying their own
    carcass (6 MEAT + 2 PELT), so a kill pays ~9 meat and a double
    pelt -- the richest quarry in the world, and it fights back at
    60% for 2 a landed tusk. The bow is the ranged answer: +3 ATK
    like the spear, and the day hunt it opens never meets a tusk."""
    combat.set_rules(session, {
        "night_only": True,
        # WARMTH: the lit hearth at the door. LIT: a carried flame —
        # the register condition (a burning torch turns a wolf while it
        # burns; the brand you are WALKING on is spent before the check).
        "deterrence": {"WARMTH": 1, "LIT": 1},
        "weapons": {"SPEAR": 3, "AXE": 2, "BOW": 3},
        "armor": {"CLOTHES": 1},
        "loot": {"*": 1, "MEAT": 3},
        "carry_stat": "CARRY",
        "bite_loot": {"MEAT": 1},
        "base_hit": 50, "per_point": 5,
    })
    # The conditions register (P2): named, per-tick, queryable states —
    # every reader (the night gate, deterrence, ctx.entity.conditions,
    # world.who_is_loud()) speaks these names. LIT = a burning torch at
    # the floor; EMBER = its memory (two ticks to re-strike); LOUD =
    # torchlit night departures and speech, graded by act count.
    statuses.set_rules(session, {
        "LIT": {"holding": {"LIT_TORCH": str(LIT_TORCH_FLOOR)}},
        "EMBER": {"lit_within_ticks": EMBER_TICKS, "after": "LIT"},
        "LOUD": {"loud_events_within_ticks": 1},
    })
    # The dark road (P2): night travel refuses you without LIT, halts a
    # journey whose flame dies mid-road for EMBER_TICKS, strands it if no
    # flame comes. Dawn needs nothing — day travel is unchanged.
    travel.set_travel_rules(session, {
        "night_travel_requires": ["LIT"],
        "relight_ticks": EMBER_TICKS,
    })
    spawns.set_script_source(
        session, "wolf", _gate_pack_script("wolf_pack.lua"))
    spawns.set_script_source(
        session, "boar", _gate_pack_script("boar.lua"))
    # Two programs (P5 -- spawns.rules takes a list; a dict is still one
    # program): the pack and the boars, separate cadences and lairs.
    # The wolves keep the deep forest and the night raids. The boars
    # den at the thicket's edge -- an hour's walk -- and are RICH
    # (born carrying their own carcass) but dangerous: they answer a
    # hunter in the night, tusk for spear. The bow hunts the forest by
    # DAY; the boar demands the dark, and the dark demands a torch.
    spawns.set_rules(session, [
        {
            "from_round": 5, "every_rounds": 5, "up_to": 2, "max_alive": 3,
            "name_prefix": "Wolf Pack",
            "template": {
                "entity_type": "individual",
                "stats": {"ATTACK": 4, "DEFENSE": 1, "HITS": 12},
                "holdings": {"MEAT": 1, "PELT": 1, "WATER": 2},
                "script_setting": "wolf",
                "account": {"COIN": 0},
                # The den (S4): wolves wake in the deep forest, and the
                # program ranges -- by day the forest's game, by night the
                # raid walk to the fire-ground (run 26's census: denned
                # wolves whose houses slept out of reach starved).
                "place": "FOREST",
                # Born traits: the CARNIVORE stomach (EAT_CARRION) -- game
                # meat feeds a pack without a house in reach.
                "technologies": ["CARNIVORE"],
            },
        },
        {
            "from_round": 3, "every_rounds": 3, "up_to": 1, "max_alive": 2,
            "name_prefix": "Wild Boar",
            "template": {
                "entity_type": "individual",
                "stats": {"ATTACK": 4, "DEFENSE": 2, "HITS": 12},
                # Born carrying its own carcass: the estate seizure on a
                # kill is the point -- high meat, big pelt.
                "holdings": {"MEAT": 6, "PELT": 2, "WATER": 2},
                "script_setting": "boar",
                "account": {"COIN": 0},
                # The thicket's edge, an hour out: the boar is the larder's
                # guarded shelf. Wolves keep the deep forest; the boar
                # never walks to the fire-ground -- it is dangerous PREY,
                # not a predator.
                "place": "THICKET",
                "technologies": ["CARNIVORE"],
            },
        },
    ])


def _create_recipes(session: Session) -> None:
    D = Decimal

    # Born traits (run 26's census): CARNIVORE is a wolf's stomach -- an
    # ENTITY-scoped technology no recipe grants, so it is exactly the
    # spawn template's to give (spawns.spawn_one reads
    # template["technologies"]). It gates EAT_CARRION: the forest's game
    # feeds the packs without a house in reach, and the houses' raw-meat
    # economics (the disease lottery) are nobody else's to farm.
    tech.create_technology(
        session, "CARNIVORE", name="Carnivore",
        description="A predator's constitution, born not learned: raw "
                    "flesh is proper food (EAT_CARRION -- wrung dry, "
                    "jerky-dense, never sickening). Wolves hold it; "
                    "nothing grants it to anyone else.")

    # --- Subsistence: gather and hunt --------------------------------------
    # One gather = one loot-table roll of ONE resource (you find what you
    # find). The thicket carries the larder's shelf (P5): the berry bushes
    # and, since the orchard branch, the apple boughs -- bare-handed finds
    # ~3.5 satiety-equivalent/hour (berries 40% x4, apples 25% x3, wood
    # 15% x2, yarn 10% x1, flint 10% x1) against a need of ~14/day: food
    # now costs ~4 of the 14 daylight hours bare-handed, ~2 with a bag --
    # the income wall (runs 26-35: every house starved) breaks on the
    # INCOME side, and apples keep a day and a half so the surplus is
    # worth banking. On the doubled BAG table a ~5% branch of 1 COIN --
    # shiny stones, minted by the ground itself (production credits a
    # banked symbol to the account, production._credit_output). The bare
    # table finds none: scarcity first, then the supply grows with tools.
    production.create_recipe(
        session, "GATHER", name="Gather",
        description="One loot-table roll of a single resource: you find what "
                    "you find. Bare-handed subsistence. Daylight only, at "
                    "the berry thicket.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=1,
        requires_daylight=True, requires_place_kind="THICKET",
        branches=[
            {"weight": D("40"), "outputs": {"BERRIES": D("4")}, "label": "berries"},
            {"weight": D("25"), "outputs": {"APPLES": D("3")}, "label": "apples"},
            {"weight": D("15"), "outputs": {"WOOD": D("2")}, "label": "wood"},
            {"weight": D("10"), "outputs": {"YARN": D("1")}, "label": "yarn"},
            {"weight": D("10"), "outputs": {"FLINT": D("1")}, "label": "flint"},
        ],
    )
    production.create_recipe(
        session, "GATHER_BAG", name="Gather with a Bag",
        description="The doubled table -- and a small chance the ground itself "
                    "has minted a coin. Daylight only, at the berry thicket.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=1,
        good_requirements={"BAG": D("1")},
        requires_daylight=True, requires_place_kind="THICKET",
        branches=[
            {"weight": D("35"), "outputs": {"BERRIES": D("8")}, "label": "berries"},
            {"weight": D("30"), "outputs": {"APPLES": D("6")}, "label": "apples"},
            {"weight": D("12"), "outputs": {"WOOD": D("4")}, "label": "wood"},
            {"weight": D("8"), "outputs": {"YARN": D("2")}, "label": "yarn"},
            {"weight": D("5"), "outputs": {"FLINT": D("2")}, "label": "flint"},
            {"weight": COIN_WEIGHT, "outputs": {COIN: D("1")}, "label": "shiny"},
            {"weight": D("5"), "outputs": {}, "label": "nothing"},
        ],
    )
    production.create_recipe(
        session, "CHOP_WOOD", name="Chop Wood",
        description="The axe's whole point: an hour at the treeline, three "
                    "certain logs -- no loot table, no gamble. Daylight "
                    "only, at the berry thicket.",
        inputs={"LABOR": D("1")},
        outputs={"WOOD": D("3")}, duration_ticks=1,
        good_requirements={"AXE": D("1")},
        requires_daylight=True, requires_place_kind="THICKET",
    )
    # The scrape's whole point: certain flint for an hour's work, two
    # hours from home -- the toolmaker's walk (vs the thicket table's
    # 15% flint rolls).
    production.create_recipe(
        session, "DIG_FLINT", name="Dig Flint",
        description="An hour at the scrape: two certain flints, no gamble. "
                    "Daylight only, at the flint scrape.",
        inputs={"LABOR": D("1")},
        outputs={"FLINT": D("2")}, duration_ticks=1,
        requires_daylight=True, requires_place_kind="FLINT",
    )
    # Hunting: slow (2 ticks), risky bare-handed (55% total loss), better
    # with a SPEAR held (never consumed) and best with TRAPs (consumed --
    # the supply chain: wood+yarn per hunt).
    production.create_recipe(
        session, "HUNT", name="Hunt",
        description="Slow, and bare-handed: mostly nothing, sometimes "
                    "dinner. Daylight only, in the deep forest.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        requires_daylight=True, requires_place_kind="FOREST",
        branches=[
            {"weight": D("55"), "outputs": {}, "label": "nothing"},
            {"weight": D("35"), "outputs": {"MEAT": D("2")}, "label": "small"},
            {"weight": D("10"), "outputs": {"MEAT": D("4")}, "label": "big"},
        ],
    )
    production.create_recipe(
        session, "HUNT_SPEAR", name="Hunt with a Spear",
        description="A held spear (never consumed) turns a desperate hunt "
                    "into a living. Daylight only, in the deep forest.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        good_requirements={"SPEAR": D("1")},
        requires_daylight=True, requires_place_kind="FOREST",
        branches=[
            {"weight": D("25"), "outputs": {}, "label": "nothing"},
            {"weight": D("55"), "outputs": {"MEAT": D("3")}, "label": "small"},
            {"weight": D("20"), "outputs": {"MEAT": D("6")}, "label": "big"},
        ],
    )
    production.create_recipe(
        session, "HUNT_TRAPS", name="Hunt with Traps",
        description="The best odds craft can buy, at the price of a consumed "
                    "trap per hunt. Daylight only, in the deep forest.",
        inputs={"LABOR": D("1"), "TRAP": D("1")},
        outputs={}, duration_ticks=3,
        requires_daylight=True, requires_place_kind="FOREST",
        branches=[
            {"weight": D("15"), "outputs": {}, "label": "nothing"},
            {"weight": D("60"), "outputs": {"MEAT": D("4")}, "label": "small"},
            {"weight": D("25"), "outputs": {"MEAT": D("8")}, "label": "big"},
        ],
    )
    # The bow (P5): ranged capital -- the best DAY hunt a lone crafter
    # can buy, with nothing hunting back. Better than the spear's table
    # at spear's price, and it prices into any fight besides (+3
    # ATTACK, the opening shot). The boar at the thicket is the richer
    # quarry but demands the night; the bow keeps a daylight living.
    production.create_recipe(
        session, "HUNT_BOW", name="Hunt with a Bow",
        description="Ranged hunting: the best odds a lone crafter can buy, "
                    "with nothing hunting you back. Held bow, never "
                    "consumed. Daylight only, in the deep forest.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        good_requirements={"BOW": D("1")},
        requires_daylight=True, requires_place_kind="FOREST",
        branches=[
            {"weight": D("15"), "outputs": {}, "label": "nothing"},
            {"weight": D("55"), "outputs": {"MEAT": D("3")}, "label": "small"},
            {"weight": D("30"), "outputs": {"MEAT": D("6")}, "label": "big"},
        ],
    )
    # The river: meat without wolves. Slower than a bad hunt but far
    # more certain, two hours from home -- the safe food walk. Fish
    # come out of the river as MEAT (cleaned on the bank): no new
    # pantry, just a second kitchen.
    production.create_recipe(
        session, "FISH", name="Fish",
        description="Two hours' certain-ish meat from the bank: slower than "
                    "a bad hunt, far more certain, and nothing here hunts "
                    "YOU. Daylight only, at the river.",
        inputs={"LABOR": D("1")}, outputs={}, duration_ticks=2,
        requires_daylight=True, requires_place_kind="RIVER",
        branches=[
            {"weight": D("30"), "outputs": {}, "label": "nothing"},
            {"weight": D("45"), "outputs": {"MEAT": D("2")}, "label": "small"},
            {"weight": D("25"), "outputs": {"MEAT": D("3")}, "label": "big"},
        ],
    )
    # The tap (P3): water is free at the bank, and the walk is shared
    # -- the FISH errand doubles as the drink. DRINK fills the cup
    # (four hours in anyone's hands); FILL_SKIN tops a waterskin (the
    # skin must be held -- one pelt, eight carried hours). Both free,
    # instant, night-legal (thirst does not keep daylight hours -- but
    # the road home still wants light or a flame, as ever).
    production.create_recipe(
        session, "DRINK", name="Drink",
        description="Cup your hands and drink your fill at the river: "
                    "the world's tap, free and instant. Fills the carried cup "
                    "(four hours' worth; the body holds no more).",
        inputs={}, outputs={"WATER": WATER_CUP}, duration_ticks=0,
        requires_place_kind="RIVER",
    )
    production.create_recipe(
        session, "FILL_SKIN", name="Fill Skin",
        description="Fill the waterskin at the river: eight carried hours "
                    "-- a pelt's worth of river that walks home with you. "
                    "Needs the skin in hand; fills it to the seam.",
        inputs={}, outputs={"SKINWATER": WATERSKIN_CAP}, duration_ticks=0,
        good_requirements={"WATERSKIN": D("1")},
        requires_place_kind="RIVER",
    )

    # --- The fire chain: a commons, not a pantry (P1, the fire rework) -----
    # The fire-ground's standing fire is PUBLIC (four seats, six hours of
    # banked burn, one an hour lit or empty -- see _create_commons).
    # MAKE_FIRE erects ANOTHER fire on your camp at the hearth: public on
    # build too -- four more seats and its own fuel bank, the commons's
    # answer to a crowded clearing. STOKE feeds any fire (1 WOOD = 2
    # hours, refused at the bank cap); WARM_BY_FIRE takes a seat for an
    # hour (+6 WARMTH -- a body caps at 6). Cooking and smoking bind the
    # same public fire: day fires cook, night fires warm, one capacity to
    # contend for.
    production.create_recipe(
        session, "MAKE_FIRE", name="Make Fire",
        description="Erects another fire on your camp at the hearth clearing: "
                    "PUBLIC like the commons fire -- four seats and its own "
                    "fuel bank, for a crowded clearing. Anyone may warm or "
                    "cook at it; you get the seat of honor, not the door. "
                    "Catches at two hours of fuel -- the wood that made it.",
        inputs={"LABOR": D("1"), "WOOD": D("2")},
        outputs={}, duration_ticks=1, builds_facility="FIRE",
        builds_facility_access="PLACE",
        builds_facility_config={
            "capacity": FIRE_SEATS, "fuel": D("2"),
            "fuel_capacity": FIRE_FUEL_CAP, "fuel_burn_per_tick": FIRE_FUEL_BURN,
        },
        requires_place_kind="HEARTH",
    )
    production.create_recipe(
        session, "STOKE_FIRE", name="Stoke Fire",
        description="Feed the fire: a split log buys two hours of light for "
                    "everyone it warms. Free (no labor), instant, night-legal, "
                    "at the hearth -- and refused when the fire is fully "
                    "banked (six hours; more wood wants patience, or a second "
                    "fire).",
        inputs={"WOOD": D("1")}, outputs={}, duration_ticks=0,
        requires_facility="FIRE", facility_fuel_output=STOKE_FUEL,
        requires_place_kind="HEARTH",
    )
    production.create_recipe(
        session, "WARM_BY_FIRE", name="Warm by the Fire",
        description="Take a seat at a lit fire: an hour's seated warmth, +6 "
                    "(a body holds at most six -- two cold hours' grace once "
                    "you stand up). No labor, night-legal, at the hearth; a "
                    "fire seats four at once, warming hour by hour. This is "
                    "how nights are survived: not banked, SAT.",
        inputs={}, outputs={"WARMTH": WARM_BY_FIRE_WARMTH}, duration_ticks=1,
        requires_facility="FIRE", requires_facility_lit=True,
        requires_place_kind="HEARTH",
    )
    # --- The night kit: torches (P2) ----------------------------------
    # MAKE (daylight labor -- the day's craft is the night's freedom),
    # LIGHT (at any lit fire -- the commons strikes brands, free and
    # instant), CHAIN (off your own flame, or its dying ember within two
    # ticks of the burnout -- the register's EMBER window is the gate).
    production.create_recipe(
        session, "MAKE_TORCH", name="Make Torch",
        description="Wrap a split log in yarn and pitch: one brand, two "
                    "hours of carried light once lit. Daylight labor -- the "
                    "day's craft is the night's road.",
        inputs={"LABOR": D("1"), "WOOD": D("1"), "YARN": D("1")},
        outputs={"TORCH": D("1")}, duration_ticks=2,
        requires_daylight=True,
    )
    production.create_recipe(
        session, "LIGHT_TORCH", name="Light Torch",
        description="Strike a brand at any LIT fire -- the commons lights "
                    "them free. Instant, night-legal, labor-free: a torch "
                    "becomes a lit torch, two hours of carried light.",
        inputs={"TORCH": D("1")}, outputs={"LIT_TORCH": D("1")},
        duration_ticks=0,
        requires_facility="FIRE", requires_facility_lit=True,
        requires_place_kind="HEARTH",
    )
    production.create_recipe(
        session, "CHAIN_TORCH", name="Chain Torch",
        description="Strike a fresh brand off your own flame -- or its "
                    "dying ember, within two ticks of the burnout. The road's "
                    "way to carry light without a fire: light the next "
                    "before the last is cold. Instant, night-legal, "
                    "labor-free, anywhere.",
        inputs={"TORCH": D("1")}, outputs={"LIT_TORCH": D("1")},
        duration_ticks=0,
        requires_conditions=["EMBER"],
    )
    production.create_recipe(
        session, "COOK_MEAT", name="Cook Meat",
        description="Fire-cooked meat: no disease, keeps a little better. "
                    "Needs a LIT fire -- coals do not cook.",
        inputs={"LABOR": D("1"), "MEAT": D("2")},
        outputs={"COOKED_MEAT": D("2")}, duration_ticks=1,
        requires_facility="FIRE", requires_facility_lit=True,
    )
    # The smokehouse: preservation as a craft. Smoking is deliberately
    # SLOW (5 ticks vs cooking's 1) and costs a log -- you pay time and
    # wood for food that never rots. It is the house-made answer to the
    # post's salted shelf, and the only way a hunt outlives the week:
    # meat decays at 0.30/tick, jerky at 0. Run 16's houses starved
    # beside rotting larders; now the ladder has a preservation rung.
    production.create_recipe(
        session, "SMOKE_MEAT", name="Smoke Meat",
        description="Slow-smoke raw meat over a lit fire: a log and a long "
                    "wait turn hunts into jerky — the only food that never "
                    "rots. Time is the price of permanence.",
        inputs={"LABOR": D("1"), "MEAT": D("2"), "WOOD": D("1")},
        outputs={"JERKY": D("2")}, duration_ticks=5,
        requires_facility="FIRE", requires_facility_lit=True,
    )
    # --- Eating: meals as decisions (run 19) --------------------------------
    # Conscious eating: the FOOD need drinks only SATIETY, and only EAT
    # recipes fill the stomach. All meals are labor-free, instant
    # (duration 0: satiety lands before this tick's draw) and
    # night-legal -- hunger does not keep daylight hours. The density
    # ladder is the point: berries are thin (1½ berries ~ 3 hours),
    # jerky dense (one strip ~ 5½ hours, and it never rots) -- the trade
    # a trader should arbitrage, the craft a hunter should practice.
    # A fed body burns 0.5/hour plus a tenth of the stomach each hour
    # (compound spill), so a day costs ~14: two jerky meals and change,
    # or ten-and-a-half berries eaten as you gather them.
    production.create_recipe(
        session, "EAT_BERRIES", name="Eat Berries",
        description="A belly of berries: thin food, eaten as gathered -- "
                    "they spoil within hours anyway. ~3 hours fed, and "
                    "wet: a berry meal carries its own drink (0.6 water).",
        inputs={"BERRIES": D("1.5")},
        outputs={"SATIETY": D("2"), "WATER": D("0.6")},
        duration_ticks=0,
    )
    production.create_recipe(
        session, "EAT_APPLES", name="Eat Apples",
        description="The larder staple: keeps a day and a half, feeds like "
                    "a meal. ~5 hours fed per sitting -- and the wettest "
                    "food that keeps (0.6 water).",
        inputs={"APPLES": D("1.5")},
        outputs={"SATIETY": D("2.8"), "WATER": D("0.6")},
        duration_ticks=0,
    )
    production.create_recipe(
        session, "EAT_COOKED", name="Eat Cooked Meat",
        description="Fire-cooked, safe, satisfying: ~4 hours fed per meal "
                    "-- and the juices go down wet (0.8 water).",
        inputs={"COOKED_MEAT": D("1")},
        outputs={"SATIETY": D("2.4"), "WATER": D("0.8")},
        duration_ticks=0,
    )
    production.create_recipe(
        session, "EAT_EGGS", name="Eat Eggs",
        description="A hen's wage: keeps near a week, feeds like a cooked "
                    "meal. ~4 hours fed -- drier than the fire's cooking "
                    "(0.5 water).",
        inputs={"EGGS": D("1")},
        outputs={"SATIETY": D("2.4"), "WATER": D("0.5")},
        duration_ticks=0,
    )
    production.create_recipe(
        session, "EAT_JERKY", name="Eat Jerky",
        description="The densest meal in the world: one strip ~ 5½ hours "
                    "fed, and the strip itself never rots. Preservation "
                    "you can taste -- and taste the cost of: salted dry "
                    "(0.2 water; the deep pantry is a dry one).",
        inputs={"JERKY": D("1")},
        outputs={"SATIETY": D("3.6"), "WATER": D("0.2")},
        duration_ticks=0,
    )
    # Eating raw: free (no LABOR -- desperation does not wait), instant
    # (duration 0: SATIETY lands before this tick's consumption pass), and
    # a 25% chance of DISEASE. The thinnest meal at the highest risk --
    # the alternative to cooking, priced in disease. ~1 hour fed.
    production.create_recipe(
        session, "EAT_RAW", name="Eat Raw Meat",
        description="Desperation does not wait: free, instant, ~1 hour fed -- "
                    "and a one-in-four chance of disease. Wet, at least "
                    "(0.4 water): the blood goes down too.",
        inputs={"MEAT": D("1")}, outputs={}, duration_ticks=0,
        branches=[
            {"weight": D("75"),
             "outputs": {"SATIETY": D("0.6"), "WATER": D("0.4")}, "label": "fine"},
            {"weight": EAT_RAW_DISEASE_WEIGHT,
             "outputs": {"SATIETY": D("0.6"), "WATER": D("0.4"), "DISEASE": D("1")},
             "label": "sick"},
        ],
    )
    # The wolf's table (run 26's census: every pack starved at its den).
    # The human one starves a wolf -- raw-at-0.6-satiety cannot cover a
    # 0.5/hour draw (a day of hunts' expected 7.7 MEAT x 0.6 = 4.6
    # satiety against ~14 needed), so denned packs died in two days
    # once the houses slept out of reach. A predator's gut wrings a
    # kill dry: carrion at 3.6 a strip (jerky-dense; nothing is
    # wasted) clears the bar with margin -- ~5.5 MEAT a hunting day
    # x 3.6 = ~20 against ~14, enough to bank against raid nights
    # (hours on the road, unfed). CARNIVORE-gated so it is the wolf's
    # alone: with it the packs live off the land, and houses are the
    # rich exception, not the only meal.
    production.create_recipe(
        session, "EAT_CARRION", name="Eat Carrion",
        description="A predator's gut: raw flesh is a wolf's proper diet "
                    "-- wrung dry, denser than any human meal keeps, and "
                    "never sickening. Wet through: the blood is half the "
                    "meal (0.9 water -- a kill drinks its killer). Born, "
                    "not learned (the CARNIVORE stomach only).",
        inputs={"MEAT": D("1")},
        outputs={"SATIETY": D("3.6"), "WATER": D("0.9")},
        duration_ticks=0,
        requires=["CARNIVORE"],
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

    # --- The larder: pastoral capital (P5) ---------------------------------
    # The pen and the flock: the first good that PAYS while you hold
    # it. A hen is bought at the post (or haggled for), penned, and
    # then the flock lays: COLLECT_EGGS credits one egg per hen held
    # (capped at four -- the pen's worth, which is also the body's
    # CHICKEN carry cap) for a single hour's labor -- the whole flock
    # served at once. Eggs keep near a week and feed like a cooked
    # meal; four hens under a pen is ~9.6 satiety a day for one labor
    # hour, the best food/labor in the world once the capital is paid
    # for (a hen returns her price in four eggs sold, or two days
    # fed). Income that compounds: the ladder's first rung above
    # craft. Runs 26-35 died on the income wall; this is the wall
    # breaking.
    production.create_recipe(
        session, "MAKE_PEN", name="Build Pen",
        description="A fenced yard for the flock: erects a pen on your "
                            "camp. Buy hens at the post, pen them, and they "
                            "lay -- capital that feeds you while you sleep.",
        inputs={"LABOR": D("1"), "WOOD": D("3")},
        outputs={}, duration_ticks=2, builds_facility="PEN",
        requires_place_kind="HEARTH",
    )
    production.create_recipe(
        session, "COLLECT_EGGS", name="Collect Eggs",
        description="Gather the day's laying: one egg per hen in the pen, "
                    "up to four, for a single hour's work -- the whole "
                    "flock served at once. The hens are held, never harmed.",
        inputs={"LABOR": D("1")}, outputs={"EGGS": D("1")}, duration_ticks=1,
        good_requirements={"CHICKEN": D("1")},
        requires_facility="PEN",
        scales_with={"CHICKEN": 4},
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
    # The water kit's craft (P3): one pelt sewn shut. A wolf's carcass
    # is MEAT 3 + PELT 1 -- the pelt is the CARRYING of eight hours of
    # river. The post stocks one at the anchor price for houses with
    # coin and no wolf of their own.
    production.create_recipe(
        session, "MAKE_WATERSKIN", name="Make Waterskin",
        description="A pelt sewn shut and greased: eight carried hours of "
                    "river. One wolf's hide is a season of dry larders -- "
                    "held, never consumed, filled at the bank.",
        inputs={"LABOR": D("1"), "PELT": D("1")},
        outputs={"WATERSKIN": D("1")}, duration_ticks=2,
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
    production.create_recipe(
        session, "MAKE_BOW", name="Make Bow",
        description="An afternoon of bending and binding: a flint-tipped, "
                    "yarn-strung bow. Held, never consumed. Daylight only.",
        inputs={"LABOR": D("1"), "FLINT": D("1"),
                                        "WOOD": D("2"), "YARN": D("1")},
        outputs={"BOW": D("1")}, duration_ticks=3,
        requires_daylight=True,
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
    # every night wants a SEAT at a fed fire (the rework: warmth caps
    # at 6 and WARM_BY_FIRE fills it; banking it is no longer a
    # strategy). Night is the expensive half of the day.
    needs.create_need(
        session, "WARMTH", WARMTH_PER_TICK, ["WARMTH"],
        name="Warmth",
        description="Drawn from the WARMTH made by fires (a seat, +6/hour, "
                    "holding at most 6), shelter and clothes: 1 per hour of "
                    "day, 3 per hour of night. Miss it and exposure accrues -- "
                    "nights bite three times as hard, and only sitting by a "
                    "fed fire covers them.",
        entity_type=EntityType.INDIVIDUAL, priority=1,
        condition_symbol="EXPOSURE", condition_quantity=Decimal("1"),
        night_quantity_per_tick=WARMTH_PER_NIGHT_TICK,
    )
    # WATER (P3): the thirst clock. 0.25/hour, six a day, drawn from
    # carried water -- the skin empties first (SKINWATER sorts before
    # WATER), the cup is the reserve. Meals part-hydrate, so the
    # pressure lands on the DRY larder (the rich, post-relief); the
    # river is free and the walk is shared with FISH. Night draws the
    # same -- thirst does not care about the dark.
    needs.create_need(
        session, "WATER", WATER_PER_TICK, ["WATER", "SKINWATER"],
        name="Water",
        description="The second stomach: 0.25 an hour (six a day) drawn "
                    "from carried water -- skin first, cup in reserve. "
                    "DRINK at the river fills the cup (four hours); a "
                    "WATERSKIN carries eight more, filled at the bank. "
                    "Meals part-hydrate (berries and apples wet, jerky "
                    "dry) -- miss the draw and thirst accrues toward a "
                    "third-day death.",
        entity_type=EntityType.INDIVIDUAL, priority=2,
        condition_symbol="THIRST", condition_quantity=THIRST_GRANT,
    )


def _create_markets(session: Session) -> None:
    """Every market trades AT the post (docs/spatial.md S4): a placed
    market fills only orders from entities standing there -- an
    hour's walk is the price of coin, cheap enough to pay often and
    steep enough to plan around."""
    _NAMES = {
        "LABOR": "Labor", "BERRIES": "Berries", "APPLES": "Apples",
        "MEAT": "Raw Meat",
        "COOKED_MEAT": "Cooked Meat", "JERKY": "Jerky", "EGGS": "Eggs",
        "WOOD": "Wood",
        "YARN": "Yarn", "FLINT": "Flint", "SPEAR": "Spear", "AXE": "Stone Axe",
        "BAG": "Bag", "BOW": "Bow", "CHICKEN": "Hen",
        "TRAP": "Trap", "CLOTHES": "Clothes", "BED": "Bed",
        "PELT": "Pelt", "WATERSKIN": "Waterskin",
    }
    for symbol in ("LABOR", "BERRIES", "APPLES", "MEAT", "COOKED_MEAT", "JERKY",
                   "EGGS", "WOOD",
                   "YARN", "FLINT", "SPEAR", "AXE", "BAG", "BOW", "CHICKEN",
                   "TRAP", "CLOTHES", "BED",
                   "PELT", "WATERSKIN"):
        markets.create_market(session, symbol, COIN, name=_NAMES[symbol],
                              place="POST")


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
    markets.adjust_holding(session, house, "WATER", WATER_SEAT_BUFFER)
    markets.adjust_holding(session, house, "HITS", HOUSE_HITS)
    combat.create_stat(session, house.id, "ATTACK", Decimal("1"))
    combat.create_stat(session, house.id, "DEFENSE", Decimal("1"))
    combat.create_stat(session, house.id, "HITS", HOUSE_HITS)
    # Hands: a house can carry what it kills. Wolves lack the stat --
    # what a beast kills rots where it fell.
    combat.create_stat(session, house.id, "CARRY", Decimal("20"))
    parcels.create_parcel(session, "LAND", name=f"{name}'s Camp", owner=house,
                          place="HEARTH")
    # The seat wakes at the hearth clearing (S4: "start"): fire-ground,
    # safe nights, and every walk out of the valley measured from here.
    places.move_entity(session, house, "HEARTH")
    return house


def make_wolf(session: Session, name: str) -> Entity:
    """One wolf: a creature, not a mechanic. Same needs as a house
    (hunger is why it hunts), 12 innate HITS, ATTACK 4 / DEFENSE 1,
    a starting strip of meat, and the pelt it wears -- killing one
    pays whoever can carry it. No CARRY: what a wolf kills rots where
    it fell; it eats the bite and the carcass, never the estate.
    Dens in the deep forest (S4) and RANGES: the CARNIVORE stomach
    (carrion is proper food) means the packs live off the forest's
    game -- houses are the rich exception, not the only meal."""
    return spawns.spawn_one(session, name, {
        "entity_type": "individual",
        "stats": {"ATTACK": 4, "DEFENSE": 1, "HITS": 12},
        "holdings": {"MEAT": 1, "PELT": 1, "WATER": WATER_BEAST_BUFFER},
        "script_setting": "wolf",
        "account": {"COIN": 0},
        "place": "FOREST",
        "technologies": ["CARNIVORE"],
    })
