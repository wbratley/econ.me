# stone_age pack roadmap — fire, night, water, sleep, the larder

Written 2026-09-06 after run 33 (breaker A/B closed, drain cap shipped as PR
#174) and the fire-notes review. Status: P1 SHIPPED (pure commons), P2
SHIPPED (torches + conditions register), P5 SHIPPED (the larder —
reordered ahead of P3/P4 by the user: runs 26–35 ALL died on the income
wall, so relief came first), P3 SHIPPED (water — the thirst clock;
run 37 validated it: zero THIRST deaths, water kills via the TIME tax
— the river pilgrimage priced a gather day, the commute drink is the
affordable rhythm — the larder playbook), P4 SHIPPED (sleep — the
night becomes a budget; run 37's exposed wound was night LOCATION:
the twins froze one road from their lit hearth and the champion PACEd
26 night-hours). One phase = one
PR = one run; every phase re-asserts
the three balance policies (do-nothing dies <2 rounds; starter survives
indefinitely; tooled policies accumulate surplus).

## Diagnosis: fire today vs a real fire

Current FIRE is a personal warmth vending machine: lives on YOUR parcel,
TEND_FIRE converts 1 WOOD -> +10 WARMTH banked in YOUR holdings (a full
night prepaid), invisible to everyone else. Against the notes:

- One fire warms many / anyone / up to a max -> private parcel, single user.
- Burns until wood runs out (fuel stock, 1 wood ~ 1h, cap ~6h) -> no fuel
  state; fire is eternal, warmth stock is the only consumable.
- Freezing -> warm in ~1h, can't bank warmth -> +10 banked instantly;
  banking IS the mechanic.
- Visible from a distance -> invisible except to its owner.
- Day fires cook, night fires warm/ward -> fire does everything, privately.

Lucky substrate facts: a duration-1 recipe with requires_facility already
gets slot capacity (reservations bind per running process); cooking rides
the same facility (day/night contention for free); consume_per_tick_inputs
is the precedent for a recurring burn draw.

## P1 — Fire rework (RELIEF lever; the commons) — SHIPPED (pure commons)

Shipped as the `fire-commons` PR: engine (facility access/capacity/fuel +
burn pass, `max_holding` clip, `facility_fuel_output` /
`requires_facility_lit` / `builds_facility_access`+`config` recipe knobs,
`ctx.query.public_facilities`), pack (commons fire-ground genesis,
STOKE_FIRE / WARM_BY_FIRE, TEND_FIRE deleted, MAKE_FIRE public-on-build
and born lit, COOK/SMOKE on the commons fire, manual, starter), tests
(policy trio green + 13 new). The ambient curve was cut to P6 as planned
(simple fixed night draw unchanged: 3/h night, 1/h day).

Engine:
- Facility.access (owner / place; default owner — backward compatible).
- Facility fuel stock + per-tick burn pass (1 fuel/hour while lit;
  lit = fuel > 0; burn regardless of occupancy).
- WARMTH holding cap (~6) on the good — banking ends (engine: enforce cap
  at credit; hot-water-bottle tech later raises it).
- Optional simple ambient curve (fixed per-hour WARMTH-draw modifier,
  colder 22:00–04:00) — included per lean.

Pack:
- Standing COMMUNAL fire at the Hearth clearing, capacity ~4 slots.
- STOKE_FIRE: anyone, 1 WOOD -> +1h fuel, max 6h fuel. Night-legal,
  labor-free.
- WARM_BY_FIRE: duration 1, labor-free, night-legal, +2 WARMTH while
  seated -> frozen-to-warm ~1h; sitting occupies a fire slot (capacity
  = max concurrent warmers).
- Delete TEND_FIRE (+10 bank). MAKE_FIRE stays: builds ADDITIONAL fires
  (more slots, more fuel demand) — public on build.
- COOK_MEAT / SMOKE_MEAT ride the public facility — day fires cook, night
  fires warm, one capacity to contend for.
- WARMTH_BUFFER start 15 shrinks (no-banking doctrine).
- Wolves: door-deterrence unchanged (target WARMTH >= 1 composes); add
  world.lit_fires() query (agents + wolf scripts see the beacon).
- Manual: rewrite fire/warmth sections.

Balance question: shared fire -> wood surplus & specialization, or tragedy
of the commons (free-riders, fire dies at 02:00)? Either is a good run.
Rough math: a night is ~3 fuel for ONE shared fire vs 4 houses x ~3 wood
banked each today — the sharing dividend funds traps/spears/trade.

## P2 — Night kit: torches (MIXED) — SHIPPED

Shipped shape (drifted from the sketch below, deliberately — see the
decisions log): the torch is a CARRIED BURNING STATE, not a per-hop
recipe cost; the night gate is AMBIENT (travel.rules), not a travel-
recipe input; and the new substrate is the conditions register
(statuses.py): the pack declares LIT / EMBER / LOUD under
`conditions.rules`, every reader (gate, deterrence, ctx.entity.conditions,
world.who_is_loud()) speaks those names. Kit: MAKE_TORCH (1 WOOD +
1 YARN, daylight) / LIGHT_TORCH (any lit fire, free) / CHAIN_TORCH (own
flame or its two-tick ember, anywhere). Decay proportional (half the
flame an hour; fresh brand ~2h, carry of two ~3h, carry cap 2). Torchlit
night departures + speech are LOUD (graded; wolves aim at it); a flame
dying mid-road halts the route for a relight window (2 ticks), then
strands. Wolves carry no torches: dusk raid walk, hold when caught out,
home by daybreak. Starter travels by day only.

Original sketch (kept for the record): TORCH (1 WOOD + 1 YARN, craft
~2). Night travel consumes 1 torch per hop — the dark road refuses you
otherwise (travel-recipe input gate). Held torch = +deterrence (a lit
brand) but visible/loud at night — travelling lit tells every listening
pack where you are.

## P3 — Water (MIXED) — SHIPPED

Shipped after P5 (the user's reorder; see the decisions log). Shape
as landed (numbers PROVISIONAL — the census tunes the tap):

- WATER need 0.25/h (6/day, night unchanged), satisfiers
  [SKINWATER, WATER] — the skin drains first, the cup is the reserve.
  THIRST: grant 0.5/tick, decay 0.05, equilibrium 10, threshold 7.5.
- The tap: DRINK free/instant/night-legal at the river (cup 4);
  FILL_SKIN at the bank tops a skin (8, seeps 0.02/h). MAKE_WATERSKIN
  = 1 PELT + 1 LABOR, 2h — a dead wolf becomes capacity. The post
  stocks one skin at ask 6.00 (the anchor; quiet-drift can discount
  it, like any shelf good).
- Meals part-hydrate: berries/apples 0.6, cooked 0.8, eggs 0.5, raw
  0.4, carrion 0.9, jerky 0.2 (dry — the design's teeth). A wet diet
  equilibrates ~2 THIRST (felt, never fatal — day 1 is not a death
  march, per the clause); a FED dry larder (jerky/eggs) dies on day
  three — thirst targets the rich, the larder's counterweight.
- Cup 4 + skin 8 ≈ two dry days net of meal credits: the river is a
  rhythm. New road THICKET–RIVER (2h): the gather commute drinks and
  the boar reaches the tap without the fire-ground. Wolves drink via
  the post road (3h each way — more wolf traffic through the grinder;
  watch at census).
- Beasts: born with a 2-water cup; wolves' carrion carries 0.9 water
  a meal; boars graze wet apples. Their programs walk to water by
  day (water < 1 → RIVER, drink at < 2, leave — "enough", not
  "full": the draw keeps any vessel under the seam, and a body that
  waits for a full cup stands in the river all day).
- Water is never marketable (the tap is free — no free-good arbitrage;
  the SKIN is what coin buys). Zero engine surfaces.

## P4 — Sleep (PRESSURE; the night becomes a budget) — SHIPPED

Shipped as the `sleep` PR. REST need (0.25/h flat, six a day — paid in
hours the house already spends idle at night: the P3 lesson, no third
time-tax) + FATIGUE condition (grant 0.5/decay 0.05 — the THIRST
twin; two sleepless nights impaired −1/−1 at the floor, three dead).
Sleep is the duration-1 idiom, not a duration-8 process: the WATCH is
emergent in the script — sleep ticks, stoke between them, fire fuel is
the watch clock — so nobody sleeps through the fuel and freezes. The
original sketch said "duration 8h or shorter watches"; the census
arithmetic said duration-1 (see the decisions log). The comfort
ladder: commons fire (REST+1/WARMTH+3 — the fire does double duty),
shelter (leaky warmth — survivable misery), bed (REST+1.5 — the inert
hook pays off, faster recovery). An attack on a sleeper wakes them
(deterred bark included — the sleep_recipes convention), and the tired
fight worse (stat_penalties) — wolves counter sleep, sleepers are
prey, prowling has a price (the packs den too: SLEEP_DEN, CARNIVORE-
gated). The post shelves one BED at 5.00. Can't sleep, keep the fire,
and stand watch at once — three roles, one night, and the answer is a
plan: home by dusk, stoked, asleep in watches.

## P5 — The larder (RELIEF; attacks the income wall) — SHIPPED

Shipped as the `larder` PR (reordered ahead of P3/P4 by the user
2026-09-12: ten straight runs died on the income wall — the census
verdict on run 35 sealed it). Shape (numbers PROVISIONAL — rebalance
with run data, the point of the run):

- APPLES: orchard branch on BOTH gather tables (bare 25% x3, bag 30%
  x6); keeps a day and a half (decay 0.08); EAT_APPLES 1½ -> 2.8
  satiety (~5h fed). Bare food income ~3.5 satiety-equivalent/hour
  (+47%), bag ~7.1 — the wall breaks on income, and the shelf makes
  surplus bankable.
- CHICKENS & EGGS: CHICKEN live good (durable, max_holding 4 — the
  pen's worth); post SELLS hens (2 on the shelf, ask 4.00) and BUYS
  eggs (1.20) and apples (0.80). MAKE_PEN (1 LABOR + 3 WOOD, 2h,
  camp); COLLECT_EGGS (1 LABOR, needs the pen) scales with the flock
  — 1 EGG per hen held at completion, cap 4, one hour serves the
  whole flock. First income that arrives while you sleep; a hen
  returns her price in four eggs. Engine surfaces: recipe
  `scales_with` knob (outputs x min(cap, floor(held)) at completion,
  honest in the catalog) and spawns.rules as a LIST of programs.
- WILD BOARS: second spawn program (from day 3, every 3rd day, up to
  2 alive) at the THICKET — dangerous PREY, not predator: ATK 4 /
  DEF 2 / 12 HITS, born carrying its carcass (6 MEAT + 2 PELT -> ~9
  meat for the kill), retaliates and remembers its foe (boar.lua);
  never walks to the fire-ground. Night-only combat makes it a
  torchlit errand with real death risk.
- BOW: flint+2 wood+yarn, 3h; HUNT_BOW is the best day table (EV 3.45
  vs spear 2.85) with nothing hunting back; +3 ATK in any fight.
- Fold-ins: wolf dusk departure is now a FINISH-in-light gate
  (hour + road < 20 — run 35's packs died stranded mid-road);
  starter eats apples; post quotes hens/eggs/apples/pelts for real.

Original sketch (kept for the record):

- APPLES: orchard/thicket branch; keeps mid-shelf (between berries and
  jerky).
- WILD BOARS: aggressive spawn — fights back when hunted, high meat, big
  pelt, real HITS risk.
- CHICKENS & EGGS: CHICKEN live good + PEN facility; ~1 egg/day per hen —
  the pack's first compounding capital (pastoralism).
- BOW: ranged hunt upgrade — better odds without wolf exposure (flint +
  wood + yarn).

## P6 — Seasons / ambient arc (optional, later)

Full seasonal arc: mild early days -> deep winter by round ~30, forcing
jerky + firewood stockpiles; hot-water-bottle tech raises the warmth bank
cap. A whole run's story of its own.

## Balance doctrine

Runs 26–33 died on the INCOME wall, so every pressure phase ships its
relief in the same PR, and ordering alternates relief/pressure:
P1 relief -> P2 mixed -> P3 mixed -> P4 pressure -> P5 relief. The
user reordered P5 ahead of P3/P4 after runs 34–35 repeated the wall
(one variable per run: the larder IS the variable).

## Decisions log

- Fire ownership: PURE COMMONS (user, 2026-09-06). No access fees; anyone
  may stoke/warm/cook. Named/chargeable fires would need a bigger engine
  lift — revisit only if the commons fails boringly.
- Ambient curve: CUT to P6 — a per-hour curve wants its own needs-engine
  review (and P1's fixed 3/h-night / 1/h-day draw already carries the
  night pressure); noted at spec time, confirmed at ship.
- Warming numbers (shipped): warmth cap 6 (clip, never refuse), +6 for a
  seated hour (WARM_BY_FIRE, duration 1, lit-gated, labor-free), stoke
  1 WOOD -> +2 fuel (cap 6h, refused when full), burn 1 WOOD/h whether
  anyone sits, fire capacity 4, COOK/SMOKE ride the same fire (day cook,
  night warm — capacity is seats, so day and night never contend).
- Sleep impairment: stat penalties (lean, decide at P4).
- Torch shape (P2, user 2026-09-10): a carried burning state, NOT a
  recipe cost — "more like just another condition, but an ambient one".
  Hence the conditions register (statuses.py) as the read layer: named
  derivations (holding floors / lit-window / loud-window), pack-declared,
  engine-derived; future derivations (P4 FATIGUE from need state, injury
  rows) extend it without touching readers.
- Loud doctrine (P2): pull-query, not witness delivery — `loud: true`
  markers on applied says + torchlit night departures; readers scan
  (world.who_is_loud() / carriers("LOUD")); the witness frozenset stays
  untouched. v1 is world-wide broadcast; distance rules are a later seam.
- Ember semantics (P2, engine): stamped post-decay every tick LIT holds;
  EMBER = LIT now OR lit within 2 ticks (a burning brand strikes its own
  successor — no waiting for a tick boundary); strictly no deterrence
  through the window (LIT only); relight window 2 ticks then strand.
- LIT floor arithmetic (P2): decay is PROPORTIONAL — floor 0.25 of a
  brand with rate 0.5/h gives ~2 lit hours a fresh brand, ~3 for a full
  carry of two; the carry cap clips at 2 (clip-never-refuse).
- Run 34 (drain-cap validation): superseded by default — the cap is ON in
  main and rides whichever run goes next (P1 + P2 = run 35 by content
  count, numbered whatever it lands as).
- P5 reorder (user, 2026-09-12): larder BEFORE water/sleep — "every run
  dies on the income wall; relief first". P3/P4 wait their turn; the
  alternation doctrine survives with a user amendment on record.
- P5 numbers provisional (user, 2026-09-12): "compact and go, cam can
  always rebalance later once we have more data" — the numbers above
  are first guesses; run 36's census is the tuning pass.
- scales_with semantics (P5, engine): factor = min(cap,
  floor(held at COMPLETION)) — no start-snapshot; selling hens
  mid-process shrinks the day's laying (honest); refuse at 0 held
  (good_requirements gates the start); the catalog renders the scaled
  row honestly (per-hen output, cap).
- Boar placement (P5): THICKET, not FOREST — the boar hunt never crosses
  wolf range ("better odds without wolf exposure" honored); wolves keep
  the forest and the raids, boars keep the thicket.
- PELT renamed to "Pelt" (P5): boars wear a double pelt too; the post's
  3.00 bid now stands behind real orders (it was a reference price
  only before).
- River road past the thicket (P3): THICKET–RIVER 2h added — the
  gather commute drinks on the way home, and the boar (which never
  walks to the fire-ground) reaches the tap on its own road. Wolves
  keep the post road to the bank (3h): more wolf traffic through the
  grinder, watched at census.
- Water is never marketable (P3): the tap is free — a SKINWATER sell
  order would be coin minted from the river. The post anchors the
  SKIN (one on the shelf, ask 6.00, quiet-drift applies like any
  shelf good).
- "Enough, not full" drink thresholds (P3, learned in test): the need
  draw keeps any vessel just under its seam, so a `water < cap`
  condition drinks forever — a body that waits for a full cup stands
  in the river all day (the first wolf test starved standing in the
  shallows). Programs drink at `water < 2` and move on.
- THIRST arithmetic (P3): grant 0.5/decay 0.05 -> equilibrium 10,
  threshold 7.5 — the death is delivered by the BUFFER (cup 4 + skin
  8 ≈ two dry days), not the climb rate, because a climb fast enough
  to beat a 25%-hydrated diet would also beat day one. Thirst targets
  the rich: wet diets equilibrate ~2 (felt, never fatal — the
  day-one clause), the dry larder dies on day three.
- Beasts drink (P3): wolves are watered mostly by their kills
  (carrion 0.9), boars by the orchard (apples 0.6) — both still run
  dry and walk to the bank by day. Creatures, not pressure: they
  share the physics and the roads.
- The watch is duration-1, not duration-8 (P4): process outputs land
  at completion, and WARMTH is a flow the body cannot bank (cap 6,
  fades 20%/h) — a committed 8h sleep would freeze hours before its
  outputs arrived. So SLEEP_* are 1h recipes repeated by the script
  (WARM_BY_FIRE's idiom): the watch emerges in lua, fire fuel is its
  clock, and "8h clears" is 8 ticks.
- FATIGUE arithmetic under PROPORTIONAL decay (P4, corrected in
  flight): the plan's flat-decay numbers (threshold 18) would never
  fire — grant 0.5 against 5%-of-held decay equilibrates at 10 < 18.
  Kept the approved TIMELINE with the THIRST twin's numbers instead:
  threshold 7.5, impaired floor 5 — the born REST buffer (4) empties
  ~16h, the climb crosses 5 near the end of night two (~32h) and
  7.5 mid-night-three (~48h). Two nights tired, three dead, as
  sketched.
- stat_penalties + sleep_recipes are combat RULES, not code (P4): the
  pack declares {"FATIGUE": {floor 5, ATTACK −1, DEFENSE −1}} and
  ["SLEEP_*"] patterns; the engine applies holding-floor penalties
  (the deterrence precedent) and cancels a defender's RUNNING sleep
  on any resolved attack (deterred included). Default rules: empty —
  no behavior change for other packs. Two lean surfaces, no
  migrations.
- Sleepers are prey, by deterrence arithmetic (P4): sleeping by fire
  covers the night draw exactly but never banks — a sleeper rides
  WARMTH below the deterrence floor, holds no torch, and a wolf that
  finds one finds the counter-sleep payoff. The watch's price is
  vigilance's absence.
- The den is CARNIVORE-gated (P4): both wolves and boars carry the
  born trait, so ONE recipe (SLEEP_DEN, free, night-legal, anywhere)
  covers every beast — and no house can curl up in it. The packs'
  programs den when fed and below REST 4; hunger outranks sleep (the
  starving pack prowls anyway and pays the morning for it).
- Dusk discipline in the floor (P4): run 37's twins froze AT the
  thicket one road from their lit hearth — the inherited starter now
  leaves the woods when the walk home would land in the dark
  (hour + road >= 19), and sleeps in watches by the lit fire with
  warmth banked first (the stoke hour between sleeps draws
  uncovered — a seat before the watch, a seam never bit).
- The commons fire is a SHARED cost (P4 census note): a lone floor
  house rides tired (gather rolls ~1 wood a day against a 14-hour
  night — the 40-tick policy window is the suite's operational
  "indefinitely", as it was for exposure pre-P4); the pressure is
  the point — keeping one fire lit is a group project, and sleep is
  what it buys. Run 38 DID starve the fire (all three houses dead of
  FATIGUE d10-d12 with every other clock quiet; fire dark 22x,
  SLEEP_BY_FIRE refused on dark fire, ~3-4 sleep-hours against the
  ~6 break-even) — and took the pre-registered lever, the
  economy-side one: WOOD-RICH GATHER, not the cheaper night.
- Wood-rich gather (run 38's lever, shipped): share, not size — wood
  is now a quarter of every gather (bare 25% x3, bag 20% x6; EV 0.75
  and 1.20 logs a roll, 2.5x the famine tables) because frequency
  beats magnitude for famine: P(zero wood in a six-gather day)
  falls 0.38 -> 0.18. The berry share donated its weight (bare food
  3.53 -> 3.00 satiety-equivalent/h — food had slack: run 38 sat
  0.771, zero hunger deaths). Polity arithmetic: three houses
  gathering ~5 apiece bank ~11 logs/day against the ~10-log night:
  the famine becomes a margin economy, not a structural
  impossibility. The axe rung stays premium (3 certain logs/h =
  4x the bare roll, was 6x).
- Berries restored, crafts pay (run 39's lever): the wood-rich
  donation was priced wrong — run 38's 0.771 food sat was an average
  carried by Lagertha's 14 apple meals and Ivar's bought jerky; the
  marginal seat-day had no slack. Run 39 funded the fire (36 logs
  gathered vs 33 stoked, zero exposure deaths, sleep-by-fire ran)
  and starved the larder: HUNGER d3h16 and d5h03 (Harald zero apple
  meals, 13 gathers in 3.6 days against a 33-gather run-38 window —
  the commute-and-meal day leaves ~3.5 gather-hours), and Ivar
  FATIGUE at the river at 5am, still running the drink pilgrimage.
  So the berries return to 40x4 bare / 35x8 bagged (food 3.00 ->
  3.53 satiety-equivalent/h, bag 6.22 -> 6.76) while wood keeps its
  quarter (0.75 / 1.20 — both levers stand), and yarn+flint pay the
  bill (bare 10 -> 5 each; bag 8 -> 5 and 5 -> 3) — run 39 was not
  binding on crafts (pens built, torches lit). The new binding
  constraint is the daylight hour itself; the 2h river roads are the
  named next lever (run 41: river proximity, wolves revive,
  sleepers-are-prey finally testable).
- Stoke doubles (run 40's lever, shipped as run 41's): run 40
  validated the food half (zero hunger deaths, ~6 gathers/seat/day,
  36 berry meals) and died all-FATIGUE d4h19 anyway -- the night
  collapsed behaviorally: sleeps 3/6/8 (run 39: 14/25/26), stokes
  14 (run 39: 33), fire dark from ~h22 every night but one, and
  both gpt-oss rewrites discarded the starter's dusk/night doctrine
  (Harald r1 "removing the low-fuel and food-threshold checks";
  Ivar's r4 rewrite never landed -- model failure). The lever chosen
  is the night's price, not the seats' scripts: STOKE_FIRE 1 WOOD ->
  +4 fuel (was +2), one variable -- cap stays 6, burn stays 1/h,
  MAKE_FIRE stays 2 WOOD. The ten dark hours now cost a dusk relight
  plus two stokes (~4 WOOD, was ~6); the observed run-40 watch (14
  stokes) would have bought 56 of the ~80 dark hours instead of 28.
  River proximity stays parked (named next for the run AFTER the
  night is ruled out as the killer; run 40's Ivar died at the hearth,
  not the river road).
