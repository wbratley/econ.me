# stone_age pack roadmap — fire, night, water, sleep, the larder

Written 2026-09-06 after run 33 (breaker A/B closed, drain cap shipped as PR
#174) and the fire-notes review. Status: P1 SHIPPED (pure commons), P2
SHIPPED (torches + conditions register), P5 SHIPPED (the larder —
reordered ahead of P3/P4 by the user: runs 26–35 ALL died on the income
wall, so relief came first), P3 (water) next. One phase = one
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

## P3 — Water (MIXED)

WATER need + THIRST condition (lethal ~day 3: faster than hunger, slower
than today's exposure). DRINK free at the river (FISH synergy). Meals
partially hydrate (~20% berries) so day 1 isn't a death march. WATERSKIN
from a PELT — a dead wolf becomes capital; post stocks one as the price
anchor. Waterskin = portable drink capacity.

## P4 — Sleep (PRESSURE; the night becomes a budget)

REST need + FATIGUE condition: 2 sleepless nights = impaired (stat
penalties −ATK/−DEF — lean), 3 = dead, 8h clears. SLEEP: night-legal,
must be firelit-or-sheltered, duration 8h or shorter watches; an attack
on a sleeper INTERRUPTS it (wolves counter sleep). The existing idle BED
becomes real: shelter+bed = faster full recovery. Can't sleep, keep the
fire, and stand watch at once — three roles, one night.

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
