# stone_age expansion — materials, craftables, skills, conditions, tech tree

Status: **Bundle A shipped; B and C are design.** One phase = one PR =
one run (the pack's doctrine — see `experiments/world/ROADMAP.md`). Written after the run-56/57
postmortems (the DISEASE death with no exit, the load trap), alongside
run-58's camp-storage build (`docs/camp-storage.md` -- whose surfaces this
doc assumes below), from a low-tech dig across **Rust, Old School RuneScape,
Minecraft, D&D 5e, and Skyrim**. Most of what follows is data: `create_good` /
`create_recipe` / `create_technology` rows plus branch tables. Two pieces
are **small declarative engine extensions**, named where they appear: a
combat-rules row that grants WOUND on a landed bite (the `bite_loot`
precedent -- a declared row that credits on a landed bite), and a
conditions-pass grant of one condition from another (WOUND->INFECTION,
EXPOSURE->FROSTBITE -- the deficit->condition precedent one tier up).
The shield needs no extension at all: `effective_defense` already reads
the held `armor` row (`{"CLOTHES": 1}` is live -- clothes are armor
today), so a shield is a number added to that dict. No migrations.

The point is not to add *stuff*. The point is to add **chains** — a raw
material that only becomes valuable once a rank exists to work it — because
a chain is what produces the trader-vs-crafter specialization the single
`Good`+`Recipe` rows already support but the current table barely uses.

---

## What the pack already ships

So additions are incremental, not a re-skin.

- **Materials (durable Goods):** WOOD, YARN, FLINT, CLAY, SALT, PELT.
- **Flow Goods:** LABOR (auto-issued), WARMTH, SATIETY, WATER/SKINWATER,
  WATERSKIN (capacity), TORCH/LIT_TORCH (carried burn), SHOP_KEY/SHOP_OPEN.
- **Capital (held, never consumed):** SPEAR, AXE, BOW, TRAP, BAG, CLOTHES,
  FINE_CLOTHES, BED, CHICKEN (the one live good; EGGS its laying).
- **Conditions (Goods with `incapacitates_at` / `productivity_factor`):**
  HUNGER, EXPOSURE, DISEASE, THIRST, FATIGUE, SQUALOR.
- **Needs:** FOOD, WARMTH, WATER, REST, COMFORT (+ the conditions they
  grant -- comfort's absence from earlier lists was exactly the run-55
  wound; it ships, and the beasts omit it by profile).
- **Storage (run 58, `docs/camp-storage.md`):** a capped private store at
  the hearth, a shared unowned drop at every other place, and the shared
  woodpile at the hearth (pulled from first when wood is wanted). Load is
  no longer a script fiction with no exit: `store`/`take`/`drop`/`pickup`
  are the release valves every bundle below assumes -- leather and arrows
  are stockpilable, a depot is droppable beside a market, and the woodpile
  is the commons the ignition economy (Bundle C) prices against.
- **Craft ladder (Technology ranks, ENTITY scope):** KNAPPING →
  FIRE_HARDENING (bow/traps), TANNING (clothes/waterskin), POTTERY,
  SALT_CURING.
- **Markets:** one per Good, COIN-settled; the Trading Post anchors the shelf.

**The survival core is dense** (food, fire, water, sleep, warmth, trade,
combat). The gaps are exactly where the five reference games live: a **tool
economy** (Rust/MC: tools replace the bare hand), an **ammunition economy**
(Rust/OSRS/MC: bows eat arrows), a **medical economy** (Skyrim/D&D/Rust:
herbs→cures, wounds, disease), and **material chains** (Skyrim:
hide→leather→strips; bone as a material).

---

## The low-tech line (the boundary)

Everything below stops **before any ingot or forge**. Stone, bone, wood,
hide, plant, resin, salt — no metal, no smelting, no ingots.

Deliberately **not** added (and why):

- **No tool durability / grindstone / repair.** This engine's tools are
  "held, never consumed" — there is no durability stat to repair. A repair
  loop would need a new engine surface (a `durable` holding that decays with
  use and refuses at zero). Out of scope; revisit only if a durability
  mechanism lands.
- **No cultivation / composter / crops.** GATHER is *wild* foraging; there is
  no plant-a-seed mechanic to feed a composter or planter. Out of scope for
  stone age.
- **No metal tier.** Iron/copper/bronze, ingots, the forge, the anvil — that
  is the boundary. The whole doc is the layer *under* it.
- **No FRIGHTENED / BLINDED / POISONED as full conditions** (D&D). Nice
  combat flavor; the high-value conditions here are WOUND, INFECTION,
  FROSTBITE. Poison is only worth it if a poison *source* is added (a venomous
  plant or a barbed trap) — parked.

---

## What each reference game's low-tech layer teaches

| Game | The low-tech teaching | What it contributes here |
|---|---|---|
| **Rust** | T0 = stone hatchet/pickaxe (wood+stone), wooden spear, **bow + wooden arrows (25 wood + 10 stone each)**, bandage (cloth), rope, fire starter, water barrel, camp cot. Medical = bandage / honey bandage (heals + stops infection). | Ammunition as a **consumable**, tools that replace the bare hand, a **wound/medical** layer, a fuel/ignition layer |
| **OSRS** | 23 skills *trained by repetition*, **interlaced** (woodcutting→firemaking→cooking). Early: stone knife, flint, firemaking, cooking, **fletching**, herblore. | Skills as **progression** (not just gates), **fletching** (arrows), a medical/alchemy craft |
| **Minecraft** | Stick→wood tools→**stone tools (pickaxe/axe/hoe)**, flint+steel, torch, **composter, smoker, grindstone, anvil, beehive/honey**, bucket, boat. | **Tool tiers by material**, a passive **beehive** income, waste→value (composter/smoker) |
| **D&D 5e** | 18 skills (Survival, Medicine, Nature, Animal Handling, …), 15 conditions incl. **Diseased, Poisoned, Exhaustion, Frightened**. | Survival/foraging as a skill, a rich **condition vocabulary** |
| **Skyrim** | 5 crafting skills — smithing, **woodworking, leatherworking (hides→leather→strips), boning (bone+flint), alchemy (herbs→potions)**. Survival = hunger/cold/**frostbite**, **diseases that transform** (Sanguinare Vampiris). | **Two-stage material chains**, **bone as a material**, an herb→potion medical loop, a transforming disease |

The recurring shape across all five: **a raw drop → a worked material → a
rank that gates the craft → a finished good that a *different* body buys.**
That is the pattern to add, not a list of nouns.

---

## The three bundles

Ranked by value, each independently shippable (one run, one variable). The
dependency arrow is B → A only where a combat output names a condition A
cures; otherwise they are independent.

| Bundle | Theme | Closes |
|---|---|---|
| **A — The Healer's Shelf** | medicine | **DISEASE has no cure today** (a real hole); adds a craftable medical loop, makes herbs valuable, adds a second passive-income good (honey) |
| **B — The Hunter's Kit** | combat economy | **BOW exists with no ammunition** (HUNT_BOW only holds a BOW); adds a resource-dependent ranged hunt/combat and an injury economy |
| **C — The Tanner's Bench** | material chains | the tanner/tailor **split** (a worked material that trades), a fuel/ignition economy |

Build order: **A → B → C.** A first because it closes the one real hole
(an incurable disease) and its herb/honey loop feeds the other two.

---

## Bundle A — The Healer's Shelf (medicine)

The medical economy. Today DISEASE only *decays* — a bad meal can be a death
sentence with nothing to do about it. This bundle gives every condition a
**craftable** exit, which is what makes the raw herbs worth foraging and the
treatment worth trading.

### New Goods

```python
# The forager's raw: a new branch on the gather tables (see recipes below),
# or a dedicated MEADOW place. Durable, light, worthless without ALCHEMY --
# the rank is studied, not held.
goods.create_good(session, "HERB", name="Herb",
    description="Gathered greens and roots: the raw of the healer's shelf. "
                "Keeps forever; feeds nothing, cures nothing until the "
                "ALCHEMY rank works it into a poultice or tonic.")

# The beehive's laying (the second compounding-income good, next to EGGS).
# Slow-decay food AND a medicine (a wound dressed in honey keeps -- the
# Rust honey bandage, the Skyrim ingredient).
goods.create_good(session, "HONEY", name="Honey",
    description="Combed from a hive: keeps a long time, feeds like a dense "
                "meal, and dresses a wound that would otherwise infect. "
                "The first food that is also a cure.",
    decay_per_tick=Decimal("0.02"))

# The worked medicines (finished goods, traded like any shelf good).
goods.create_good(session, "POULTICE", name="Poultice",
    description="Herbs worked with water into a dressing: binds a wound and "
                "starves the infection that follows it.")
goods.create_good(session, "TONIC", name="Tonic",
    description="Herbs worked into a dose: the craftable answer to a "
                "disease that would otherwise ride to the threshold.")
```

### New conditions (the threat the shelf answers)

WOUND and INFECTION are the health dimension combat has always lacked (an
attack today is a hit-and-miss, not an *injury*). They are granted as
**recipe-branch outputs** by combat (Bundle B wires the grants) and cured by
**consuming them as recipe inputs** (below). FROSTBITE is the deep-cold
injury EXPOSURE has only hinted at.

```python
goods.create_good(session, "WOUND", name="Wound",
    description="An open hurt from a fight: slows the body, and left "
                "undressed it festers into infection. Bind it.",
    decay_per_tick=Decimal("0.02"),        # it scabs slowly on its own
    incapacitates_at=Decimal("12"),        # a body dies of the blood, not the blow
    productivity_factor=Decimal("0.8"),
    carry_exempt=True)
goods.create_good(session, "INFECTION", name="Infection",
    description="The wound that was not bound: it does not heal and it "
                "spreads. The poultice and the honey are the only exits.",
    decay_per_tick=Decimal("0.01"),        # nearly inert -- it is the WOUND's tail
    incapacitates_at=Decimal("8"),
    productivity_factor=Decimal("0.85"),
    carry_exempt=True)
goods.create_good(session, "FROSTBITE", name="Frostbite",
    description="Cold that bit through the warmth: the injury beneath the "
                "EXPOSURE number, when a cold night was not just tiring.",
    decay_per_tick=Decimal("0.05"),
    incapacitates_at=Decimal("10"),
    productivity_factor=Decimal("0.75"),
    carry_exempt=True)
```

> **Why consume-the-condition as the cure.** A condition is a Good held as a
> holding (EAT_RAW's "sick" branch credits `DISEASE: 1` exactly this way).
> A recipe with `inputs={"DISEASE": 1, ...}` *reserves and removes* that
> holding — so "cure" is a normal goods move, auditable, and the treatment
> is a **consumable** (the poultice is spent). No new engine surface.
> Doses are quantized and conditions are not: a raw meal sickens by whole
> points but the register reads fractions (run 57's Ivar died at 2.76) --
> a dose buys back one point, death's door is two or three, and a
> sub-point disease is left to decay.

### New Recipes

```python
# Herbs ride the gather (a sixth roll on both tables) OR a dedicated
# MEADOW place. Either way ALCHEMY is what makes the roll pay.
#   GATHER branch: {"weight": D("5"), "outputs": {"HERB": D("2")}, "label": "herb"}

# The beehive: live capital, the CHICKEN idiom (durable, capped, laid by).
goods.create_good(session, "BEEHIVE", name="Beehive",
    description="A smoked hive: the second income that arrives while you "
                "sleep. Held, never eaten -- a yard, not a wild.",
    max_holding=Decimal("2"))

# Acquisition is the CHICKEN rung, not a craft: the post's retail anchor
# stocks hives at a stiff genesis ask, the same idiom that puts chickens
# on the shelf. The APIARY rank is what lets you keep the comb; the hive
# itself is bought, like a hen.

# COLLECT_HONEY -- the scales_with idiom (the CHICKEN precedent):
# output x min(cap, floor(held BEEHIVE)) at completion, one hour serves the lot.
production.create_recipe(session, "COLLECT_HONEY", name="Collect Honey",
    description="Comb the hive: an hour of smoked hands yields the day's "
                "honey, priced by the hives held.",
    inputs={"LABOR": D("1")}, outputs={"HONEY": D("1")},
    duration_ticks=1,
    good_requirements={"BEEHIVE": D("1")},
    scales_with={"BEEHIVE": 2},
    requires=["APIARY"])

# The two work-crafts (ALCHEMY-gated). WATER is the solvent (free at the
# river); the herb is the raw; the output is the finished, tradable cure.
production.create_recipe(session, "MAKE_POULTICE", name="Make Poultice",
    description="Work the herbs into a dressing: a wound's answer.",
    inputs={"HERB": D("2"), "WATER": D("1")}, outputs={"POULTICE": D("1")},
    duration_ticks=1, requires=["ALCHEMY"], requires_daylight=True)
production.create_recipe(session, "MAKE_TONIC", name="Make Tonic",
    description="Work the herbs into a dose: a disease's answer.",
    inputs={"HERB": D("2"), "WATER": D("1")}, outputs={"TONIC": D("1")},
    duration_ticks=1, requires=["ALCHEMY"], requires_daylight=True)

# The cures -- each consumes the condition (a holding) and the treatment.
# duration 0, instant, night-legal (a fever does not keep daylight hours).
production.create_recipe(session, "BIND_WOUND", name="Bind a Wound",
    description="Dress the wound: the bleeding stops and the infection "
                "that followed it is starved.",
    inputs={"WOUND": D("1"), "POULTICE": D("1")}, outputs={},
    duration_ticks=0)
production.create_recipe(session, "CLEANSE", name="Cleanse an Infection",
    description="The honey and the poultice: the festering wound gives up.",
    inputs={"INFECTION": D("1"), "HONEY": D("1")}, outputs={},
    duration_ticks=0)
production.create_recipe(session, "TAKE_TONIC", name="Take a Tonic",
    description="The dose: the disease rides back from the threshold.",
    inputs={"DISEASE": D("1"), "TONIC": D("1")}, outputs={},
    duration_ticks=0)

# The meal (HONEY is food first -- its own description says so; the doc
# review caught the missing food path). Dense, wet, instant, night-legal.
production.create_recipe(session, "EAT_HONEY", name="Eat Honey",
    description="The comb: dense, wet, keeping -- the larder's sweetest "
                "shelf, and the raw of the CLEANSE dose.",
    inputs={"HONEY": D("1")},
    outputs={"SATIETY": D("3"), "WATER": D("0.3")},
    duration_ticks=0)
```

### New Technologies (ranks)

```python
tech.create_technology(session, "ALCHEMY", prerequisites=["POTTERY"],
    name="Alchemy",
    description="The healer's rank: work a gathered herb into a poultice "
                "or a tonic. Learnt at a fire -- the solvent is water, the "
                "craft is the hand. Gates the two work-crafts only; the "
                "cures themselves are free acts on a held medicine.")
```

> **The prize ruling (operator, run-58 review): alchemy is a PRIZE, and
> the tree stays deep on purpose** -- KNAPPING -> FIRE_HARDENING ->
> POTTERY -> ALCHEMY, four studies up. Every dilution of the prize makes
> it less desirable; the healer's rank is worth precisely the ladder
> under it. The run-57 reachability hole (the disease kills day 3-5; the
> ranked tonic arrives days later) is NOT closed by shallowing the tree --
> it is closed by the gateless cousin being TRADE, which sharpens the
> prize rather than diluting it: **the post stocks three tonics at a stiff
> genesis ask** (its retail-anchor idiom -- jerky, salt; no bid, thin
> shelf, priced so a coin purse buys one or two doses, not immunity). A
> desperate house can BUY the exit before any rank exists; the alchemist's
> prize is producing them for two herbs and a cup of river water and
> selling into demand the post's shelf has already proven.

```python
tech.create_technology(session, "APIARY", prerequisites=["KNAPPING"],
    name="Apiary",
    description="The pastoralist's second flock: smoke a hive and it "
                "lays honey while you sleep. Gates the beehive and the "
                "combing -- the CHICKEN rung, one shelf over.")
```

### What this bundle buys

- **DISEASE stops being a death sentence.** A 25%-a-raw-meal roll that
  today only decays now has `TAKE_TONIC` -- and the exit is reachable on
  day one by coin (the post's thin tonic shelf, the prize ruling above)
  even though the craft itself is four studies up. The disease economy
  becomes a *trade* (herbs up, tonics down) instead of a lottery with no
  exit.
- **Herbs become valuable for the first time.** A gather roll that is
  worthless without ALCHEMY and the corner-stone of a market with it — the
  exact "raw is worthless until a rank exists" pattern.
- **HONEY is food *and* medicine** and a **second passive-income good**
  (the CHICKEN precedent, a different shelf). The beehive is pastoral
  capital; the honey is the larder's dense, keeping, curative staple.
- **Combat becomes lethal in the slow sense** (WOUND/INFECTION), which is
  what Bundle B's weapons need to have *stakes*.

---

## Bundle B — The Hunter's Kit (combat economy)

The ammunition and injury economy. Today BOW is held and *never consumed* —
a ranged hunt costs only LABOR and a place, so the best day-table (EV 3.45)
has **no resource price on its shot**. Rust's bow eats an arrow *per arrow*;
this bundle puts the cost back on the arrow, and makes a wolf bite cost
more than a miss.

### New Goods

```python
# The two drops that enable the kit. FEATHER from a hunt (wolf/ibex/boar
# carcass), BONE from the same kills -- a dead predator yields a pelt AND
# a bone AND a feather, not one tradeable. The wiring is the beasts'
# estate holdings: the wolf/boar templates already ride into the world
# carrying MEAT and PELT (the estate seizure on a kill is the point), and
# BONE and FEATHER join that same held set -- data, not a recipe.
goods.create_good(session, "FEATHER", name="Feather",
    description="Picked off a kill: the fletch on an arrow. Durable, "
                "light, worth nothing but the fletcher's bench.")
goods.create_good(session, "BONE", name="Bone",
    description="Harvested from a kill: the material of needles, points, "
                "and the fine edge flint cannot reach. Durable.")
goods.create_good(session, "ARROW", name="Arrow",
    description="Flint, wood, and feather: one shot, spent on the flight. "
                "The bow's real cost -- a quiver is a bank of odds.")
goods.create_good(session, "KNIFE", name="Knife",
    description="A flint blade on a haft: the general cutter. Speeds the "
                "cook, the tan, the cord -- the tool the world has lacked.")
goods.create_good(session, "SHIELD", name="Shield",
    description="A wood-and-flint board: the answer to an opening shot. "
                "Defense, where the spear and axe give only offense.")
```

### New Recipes

```python
# The fletcher's bench (FLETCHING-gated). Consumable: one arrow, one shot.
production.create_recipe(session, "MAKE_ARROW", name="Make Arrow",
    description="Flint point, straight shaft, three feathers: one shot, "
                "spent on the flight. A quiver is a bank of odds.",
    inputs={"WOOD": D("1"), "FLINT": D("1"), "FEATHER": D("1")},
    outputs={"ARROW": D("1")}, duration_ticks=1,
    requires=["FLETCHING"], requires_daylight=True)

# The two general tools.
production.create_recipe(session, "MAKE_KNIFE", name="Make Knife",
    description="A flint edge on a haft: the general cutter.",
    inputs={"FLINT": D("1"), "WOOD": D("1")},
    outputs={"KNIFE": D("1")}, duration_ticks=1, requires_daylight=True)
production.create_recipe(session, "MAKE_SHIELD", name="Make Shield",
    description="A board of wood and flint: the answer to an opening shot.",
    inputs={"WOOD": D("2"), "FLINT": D("1")},
    outputs={"SHIELD": D("1")}, duration_ticks=2, requires_daylight=True)
```

```python
# KNIFE and SHIELD, the mechanisms named (the review catch: "speeds" and
# "defense" are not mechanisms):
# - KNIFE works as RECIPE VARIANTS -- the doctrine's "better machinery"
#   shape: COOK_MEAT_KNIFE / TANN_HIDE_KNIFE / MAKE_ROPE_KNIFE are rows
#   with good_requirements={"KNIFE": 1} and better duration or output.
#   The bare rows stay (the gateless cousins); the knife buys margin,
#   never access.
# - SHIELD is DATA on an existing row: the combat rules' "armor" dict is
#   a per-unit-held defense bonus that effective_defense already reads
#   ({"CLOTHES": 1} is live -- clothes are armor today). The pack's
#   COMBAT_RULES gains "SHIELD": D("2") in that dict. No engine change;
#   the spear and the axe give offense, the shield answers it, and
#   clothes already buy a point of the answer.
```

**The arrow wires into the existing hunt** (this is the behavioral change —
no new recipe, one new `good_requirement` + a consumed input):

```python
# HUNT_BOW today: good_requirements={"BOW": 1}, inputs={"LABOR": 1} (BOW held,
# never consumed). Becomes:
#   good_requirements={"BOW": 1}               # the bow is held, never spent
#   inputs={"LABOR": 1, "ARROW": 1}            # the arrow is the shot's price
# A quiver of 5 arrows = 5 day-hunts; the bow's EV 3.45 now has a per-shot
# price, and a fletcher who sells arrows is the trader the bow's maker is not.
```

**Combat grants the injury** (the slow lethality). The existing wolf/boar
attack resolution already produces a win/lose; the losing branch (and a
scratched win) credits a WOUND, and an old wound that was never bound
festers:

```python
# Two declarable rows, engine-built once (the review catch: the original
# draft said "script-side" festering -- IMPOSSIBLE, scripts never touch
# holdings; the money-scope invariant is the pack's oldest law):
# 1. The injury, as a combat-rules row -- the bite_loot precedent
#    exactly (bite_loot {"MEAT": 1} already credits the ATTACKER on a
#    landed bite; this row credits the bitten side instead):
#      "on_hit_grants": {"WOUND": D("1")}   # a landed bite tears
# 2. The festering, as a conditions-pass grant -- the deficit->condition
#    precedent one tier up (condition->condition, declared per
#    condition): a WOUND held past its scab window unbound grants
#      INFECTION: 1   (the tail; Bundle A's CLEANSE is the exit)
#    The same row shape grants FROSTBITE from EXPOSURE past a severity
#    floor (Bundle A's deep-cold injury).
```

### New Technologies

```python
tech.create_technology(session, "FLETCHING", prerequisites=["FIRE_HARDENING"],
    name="Fletching",
    description="The bow's missing cost: feather, shaft, and point into a "
                "shot that is spent on the flight. Gates the arrow -- the "
                "bow's maker and the arrow's maker are finally different "
                "bodies.")
tech.create_technology(session, "BONEWORK", prerequisites=["KNAPPING"],
    name="Bonework",
    description="The finer edge: bone where flint is too brittle. The "
                "needle, the point, the scrape. Gates the fine tools a "
                "pelt-and-bone kill earns.")
```

### What this bundle buys

- **The bow has a price again.** The best day-table costs a shot per shot;
  a fletcher emerges as a trader the bow's maker never was.
- **Combat has stakes.** A wolf bite is a WOUND, an unbound wound is an
  INFECTION, and both are Bundle A's market — the fight and the medicine
  are one economy.
- **A kill yields three tradeables** (pelt, bone, feather), not one — the
  hunt's economics thicken without touching its odds.

---

## Bundle C — The Tanner's Bench (material chains)

The specialization and fuel economy. Today TANNING jumps straight from
PELT to CLOTHES/WATERSKIN — one rank, one body, no intermediate that trades.
Skyrim's leatherworking is two stages (hide→leather→strips), and the
intermediate **is the market**. This bundle splits the chain and adds the
ignition economy (resin/charcoal → kindling) that makes a fire a *managed*
resource, not a free stoke.

### New Goods

```python
# The worked hide -- the intermediate that TANNING used to skip.
goods.create_good(session, "LEATHER", name="Leather",
    description="A pelt scraped, smoked, and greased: the worked hide. "
                "The tanner's output, the tailor's input -- the first "
                "material that only exists because a rank made it.")
# The ignition economy. RESIN is the raw (a new roll on the thicket
# gather tables -- the forest has no gather, only hunts); CHARCOAL is
# the processed fuel (wood sealed in a pit); KINDLING is what actually
# lights.
goods.create_good(session, "RESIN", name="Resin",
    description="Poured from a cut: the forest's own fire. Durable, the "
                "raw of the ignition shelf.")
goods.create_good(session, "CHARCOAL", name="Charcoal",
    description="Wood sealed in a pit and starved of air: denser fuel than "
                "the log, and the charcoal's own heat. The processing step.")
goods.create_good(session, "ROPE", name="Rope",
    description="Yarn laid and bound: a step above the cord. The bowstring, "
                "the trap, the net, the repair.")
goods.create_good(session, "KINDLING", name="Kindling",
    description="Charcoal and resin: the reliable strike. The difference "
                "between a fire that takes and one that needs three tries.")
```

### New Recipes

```python
# The tanner's bench (LEATHERWORK-gated): the split that creates the market.
production.create_recipe(session, "TANN_HIDE", name="Tann a Hide",
    description="Scrape, smoke, and grease a pelt into leather: the "
                "intermediate that makes the tailor a different body.",
    inputs={"PELT": D("1"), "WATER": D("1")}, outputs={"LEATHER": D("1")},
    duration_ticks=2, requires=["LEATHERWORK"], requires_daylight=True)
# CLOTHES / MAKE_WATERSKIN take LEATHER from here on (a pelt is raw,
# leather is worked) -- the tailor's input, bought from the tanner, not
# held by them. MAKE_FINE_CLOTHES KEEPS IBEX_PELT (the review ruling): the
# far valley's coat is its own chain -- one rank, one export -- and P4's
# winter-door balance is test-pinned on it as-is; re-routing the coat
# through leather would deepen the one good that closes the winter day
# exactly. The tailor-side thinning (CLOTHES now a trade leg deeper) is
# real: re-pin the winter-door test in this bundle, and check the
# gateless cousin still holds (a shelter-and-fire winter without the
# tanner).

# The ignition chain.
production.create_recipe(session, "MAKE_ROPE", name="Make Rope",
    description="Lay and bind the yarn into rope: a step above the cord.",
    inputs={"YARN": D("2"), "WOOD": D("1")}, outputs={"ROPE": D("1")},
    duration_ticks=1, requires_daylight=True)
production.create_recipe(session, "MAKE_CHARCOAL", name="Make Charcoal",
    description="Seal wood in a pit and starve it of air: denser fuel than "
                "the log. A slow, closed-pit burn.",
    inputs={"WOOD": D("3")}, outputs={"CHARCOAL": D("1")},
    duration_ticks=4, requires_facility="FIRE", requires_facility_lit=True)
production.create_recipe(session, "MAKE_KINDLING", name="Make Kindling",
    description="Charcoal and resin: the reliable strike.",
    inputs={"CHARCOAL": D("1"), "RESIN": D("1")}, outputs={"KINDLING": D("1")},
    duration_ticks=1)
# RESIN rides the thicket gather (GATHER/GATHER_BAG): {"weight": D("5"), "outputs": {"RESIN": D("1")},
# "label": "resin"}
```

### New Technologies

```python
tech.create_technology(session, "LEATHERWORK", prerequisites=["TANNING"],
    name="Leatherworking",
    description="The split that makes a trade: a pelt into leather, the "
                "worked hide the tailor buys. The tanner and the tailor "
                "are finally different bodies.")
```

### What this bundle buys

- **The tanner/tailor split.** LEATHER is a real intermediate: the tanner
  sells it, the tailor buys it, and CLOTHES/WATERSKIN move
  from "hold a pelt" to "hold a leather" (FINE_CLOTHES keeps IBEX_PELT --
  the ruling above) — the first material chain that
  is *two* bodies and a market between them.
- **Fire becomes managed.** KINDLING **improves** MAKE_FIRE (a variant
  row: fewer wood, faster light) -- never gates. The free stoke stays
  free (the starter floor does not move); the ignition shelf sells
  margin, not access. The charcoal *stoke* leg is dominated and parked:
  stoking is free and instant at +2h per log, so no stoke made of three
  logs out-earns stoking those three logs (a "double" stoke is 1.33h per
  wood against 2h; even a 3x stoke only ties it and costs four hours of
  labor to make) -- the charcoal's value is the KINDLING leg.
- **A processing step** (wood→charcoal) that is the shape of every later
  industry: input in, a slower better fuel out, and a trader between.

---

## The skills, mapped (D&D's 18 → this pack's model)

This pack has no free skill rolls: a "skill" is either a **Technology rank**
(gates a craft, studied via a `unlocks=` recipe) or a **bare Holding** (a
stat that modifies a branch table). The reference skills that fit, and
where they land:

| Reference skill | Here | Form | Effect |
|---|---|---|---|
| **Medicine / Alchemy** (Skyrim/D&D) | **ALCHEMY** | rank | gates MAKE_POULTICE / MAKE_TONIC |
| **Animal Handling / Pastoral** | **APIARY** | rank | gates BEEHIVE / COLLECT_HONEY |
| **Fletching** (OSRS) | **FLETCHING** | rank | gates MAKE_ARROW |
| **Boning / Leatherworking** (Skyrim) | **BONEWORK / LEATHERWORK** | rank | gates bone tools / TANN_HIDE |
| **Survival / Nature / Foraging** (D&D) | *GATHER tuning* | bare Holding (optional) | a `GATHER` skill holding widens the food branch / adds HERB+RESIN rolls |
| **Hunting / Thieving** (D&D) | *HUNT odds* | bare Holding (optional) | a `HUNT` skill holding shifts the hunt branch weights |
| **Fishing** (OSRS) | *FISH odds* | bare Holding (optional) | a `FISH` skill holding shifts the FISH table |

The three **ranks** (ALCHEMY, APIARY, FLETCHING, BONEWORK, LEATHERWORK) are
the additions above. The three **bare-Holding skills** (GATHER/HUNT/FISH
modifiers) are *optional* — they deepen an existing table rather than add a
row, and only if a run shows a house wanting to *specialize* in a gather
rather than just work it. Parked until a run asks for them.

---

## The conditions, mapped (D&D's 15 → this pack's register)

The pack's condition vocabulary is already most of D&D's. The gaps, in
value order:

| D&D condition | Here | Status |
|---|---|---|
| Diseased | DISEASE | have (no cure → **Bundle A adds one**) |
| **Wounded** | **WOUND** | **new (Bundle A)** |
| **Infected** (Rust) | **INFECTION** | **new (Bundle A)** |
| Exhausted | FATIGUE | have (the sleep clock) |
| **Frostbitten** (Skyrim) | **FROSTBITE** | **new (Bundle A)** — the deep-cold injury under EXPOSURE |
| Poisoned | — | parked (needs a poison *source*) |
| Frightened / Blinded / Deafened | — | skip (combat flavor, no economy) |
| Grappled / Restrained / Prone | — | skip (no grapple mechanic) |

The register pattern is identical for all of them: a Good with
`decay_per_tick`, `incapacitates_at`, `productivity_factor`, granted as a
recipe-branch output (or by the needs pass), cured by consuming it as a
recipe input. No new mechanism.

---

## Build order and what each run proves

One phase = one PR = one run, per the pack's doctrine. Each bundle is one
variable; the census is the tuning pass (numbers below are first guesses).

1. **Run A — The Healer's Shelf.** Ship Bundle A in full. Proves: DISEASE
   now has an exit (a house that eats raw and then takes a tonic survives
   the roll that used to kill it), herbs forage and trade, the beehive
   lays while the house sleeps, and the cures work end to end (a
   test-injected WOUND, a raw-meal DISEASE -- the combat grant wiring
   itself ships in B). Watch: does a house *stock* tonics, or is the medicine
   only ever bought at death's door (the post's shelf, like jerky)?
2. **Run B — The Hunter's Kit.** Ship Bundle B. Proves: the bow now costs a
   shot per shot (a fletcher appears as a trader), a wolf bite is a WOUND
   and an unbound wound festers (the fight has a slow price), and a kill
   yields pelt+bone+feather. Watch: does the arrow cost *discourage* the
   best day-table, or does it just make the fletcher the house that
   specializes in it?
3. **Run C — The Tanner's Bench.** Ship Bundle C. Proves: the tanner sells
   LEATHER and the tailor buys it (two bodies, one market), and a fire now
   has a fuel *cost and quality* (kindling/charcoal) rather than a free
   stoke. Watch: does the ignition cost make the commons fire a *scarcity*
   again, or does it just price the stoke?

Each run re-asserts the three balance policies: do-nothing dies <2 rounds;
starter survives indefinitely; tooled policies accumulate surplus. And two
re-pins ride every bundle's definition of done (the review additions):

- **The genesis soak re-runs** (`experiments/world/test_genesis_soak.py`):
  the gather table is exactly 100 today (40 berries / 25 apples / 25 wood
  / 5 yarn / 5 flint); HERB and RESIN each take 5, and every re-slice of
  the food/wood share re-rolls the soak's ~60% house survival and the
  wolves' hunt economy. Re-baseline consciously or the bar starts flaking
  and gets quietly weakened.
- **The winter door re-pins** (Bundle C at latest): P4's
  salter/idler/trader balance is test-pinned on the current coat and
  shelf; any chain that moves them re-runs it.

---

## Open balance questions (for the census, not the design)

- **HONEY decay.** 0.02 keeps ~a week at the larder; is the honey *food*
  enough that a house stocks it, or is it only ever the medicine? (Rust's
  honey bandage is medicine-first.)
- **Arrow cost vs bow EV.** The bow's day-table is EV 3.45; an arrow of
  wood+flint+feather prices it. If the shot is dearer than the meat it
  buys, the fletcher is the only house that can afford the bow — which may
  be the point (specialization) or the wall (unreachability). The run
  decides.
- **WOUND decay vs cure supply.** If WOUND scabs fast enough, the poultice
  is insurance, not necessity; if it scabs slow, the healer's shelf is the
  only thing standing between a fight and an infection. 0.02 is the
  first guess; the census tunes it against how often a house actually
  fights.
- **KINDLING's price point.** Ruled in review: kindling *improves* MAKE_FIRE
  (a variant row -- fewer wood, faster light), it never gates; the free
  stoke stays. The charcoal stoke leg is **parked, not tunable**: stoking
  is free and instant at +2h per log, so no charcoal stoke out-earns
  stoking the three logs it is made of -- the open question is only
  whether the KINDLING leg (the ignition shelf) sells anything the plain
  log does not. If not, the bundle shrinks to RESIN + KINDLING.
