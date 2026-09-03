# Roadmap: the spatial layer — places, travel, and located economies

This document plans the engine-side spatial layer: places entities can be
at, travel between them as real work, presence-gated actions, located
markets and danger. It absorbs the spatial half of `notes.txt` (entity
locations, proximity requirements, interchangeable coordinate systems,
graph and grid navigation, transport modes) and replaces game.md §10's
deferral — the trigger that section named ("once the single-market game
proves engaging") has been earned by twenty-six postmortemed stone-age
runs: the single-region economy is deep, and its frontier is now
geography (run 25's dead trading post is the poster child — a market no
one can walk to is a market that dies).

The governing architecture principles live in `docs/design.md` (§2
mechanism/data/policy, §4.3 the world-layer boundary, §4.5 what the
engine demands of itself). The consumer of this layer is first the
batched agent game (`docs/game.md`) and the experiment campaign
(`experiments/`); the Luanti/Godot navigable world stays platform-era and
is deliberately last (S6).

## 1. The framing

Today the engine is everywhere at once. An entity tenders a fire it is
not standing near, sells wood on a market with no location, and is
threatened by wolves that circle the whole world at night. This is
correct for the modelling product — an abstract economy has no map — and
it was correct for v0 of the game (game.md §10). What it cannot express:

- **Costly movement.** Every interesting economic friction of the real
  world starts with "it takes time and risk to be where the action is":
  commutes, trade routes, arbitrage across distance, settlement
  patterns, strategic depth.
- **Local scarcity.** Berries where the thicket is, flint at the scrape,
  fish at the river. Today resources sit in global holdings; deposits
  made them parcel-located but entities never had to show up.
- **Territorial danger.** A wolf with a home range is a very different
  animal from ambient night pressure: it creates no-go zones at no-go
  hours, and makes the road itself a decision.
- **Place as identity.** "The house at the river crossing" should be a
  fact the world records, not flavor text.

The layer must deliver these without breaking the two properties the
engine is for: determinism/auditability (a replayable ledger) and
world-optionality (the modelling product must run with zero spatial
data, unchanged).

## 2. What already exists (do not rebuild)

- **Parcels, facilities, deposits** (design.md § parcels, shipped Step
  12): located resources with ownership, reservation, regeneration.
  Extraction is already parcel-bound; what is missing is *presence*.
- **Requirements with reservation** on recipes (present-but-not-consumed
  checks) — the exact mechanism a proximity gate generalizes.
- **Processes** (`models/process.py`): work-over-ticks with inputs
  consumed at start, completion at `completes_tick`, parcel binding —
  travel slots into this without a new scheduler concept.
- **The intent protocol** and its three consumers (HTTP routers, Lua
  scripts, `POST /intents` machine batches) — one resolver
  (`scripting.resolve_intent`) dispatches everything; a `travel` intent
  lands here like any other.
- **Threats and combat** (threats.py, combat.py): ambient night
  pressure credited to conditions, and an attack intent with targeting —
  both are location-blind today and gain scope here, not replacement.
- **The clock** (day/night hours) and **needs that tick during
  processes** — being on the road is automatically *expensive in the
  existing currency*: hunger accrues, warmth decays, no gathering.
- **Packs as data + Lua** under the manifest gate (scripting.md §5):
  a map is pack content. The engine mechanism must be authorable by a
  pack, exactly like recipes and threats are.
- **Opaque refs as the join vocabulary** (§4.5): `region_id`,
  `extent_ref`. The spatial layer extends the vocabulary by two words —
  *place* and *edge* — and no further.

## 3. The doctrine: topology in the engine, geometry outside it

notes.txt asks for interchangeable coordinate systems — a 2D grid, a 3D
contiguous space, a weighted graph of trails — and design.md §4.3
answers "position never enters the engine". These reconcile exactly:

> The engine stores a **topology**: named places (opaque refs, like
> `extent_ref`) and weighted, mode-tagged edges between them. It never
> stores a coordinate. Whether the topology was projected from a grid, a
> voxel map, a real-world geodata extract, or drawn by hand is a
> pack-authoring fact that stops at the boundary.

A grid *is* a graph with 4-adjacency and terrain-typed edge weights; a
3D contiguous space is the same with 6-adjacency; Luanti's voxel world
resolves a place ref to a chunk. The engine's route computation,
presence gates, and events are identical in all cases. This is the same
split as parcels: who controls what and what stands on it is engine;
where that is in meters is the world layer.

Consequences:

- **Movement is data.** Edge weight (cost in ticks), mode (WALK, ROAD,
  RAFT, RIDE, AIR…), one-way-ness, passability — all pack data rows.
  "Some terrain types are impassable, some faster" is an edge set, not
  an engine branch.
- **Travel is a Process** (transport-as-recipe, game.md §10's own
  phrase). Needs tick on the road; the entity is observable mid-route;
  cancellation and interception compose for free.
- **Presence is a requirement**, checked exactly where the other
  recipe requirements are checked. No Lua validator is needed for
  correctness; Lua may layer policy on top (a world that wants
  "no gathering while wounded" can still say so).
- **Spatial is opt-in per world.** Every column nullable, every gate
  fires only on declared data, the inequality scenario ships zero
  places and must not notice the layer exists. Green suite unchanged
  after every step below.

## 4. The forks (decisions, with the chosen option marked)

### Fork 1 — Nodes with names, or coordinates in disguise?

**Chosen: opaque place refs + edges.** A `places` table (unique key,
name, region id, a kind tag for gates) and a `spatial_edges` table
(from, to, mode, cost_ticks). No x/y/z anywhere in the schema, so no
world can ever be locked into one geometry and the world layer keeps
its monopoly on meters. Rejected: a `location(x,y,z)` column — it is
the §4.3 boundary broken in miniature, and grids/graphs/voxels stop
being interchangeable the day the engine can do arithmetic on
coordinates.

### Fork 2 — Teleport with cost, or travel as real work?

**Chosen: Process per hop, hop cost from the edge.** One process per
edge traversed (not one per journey): mid-route state is auditable,
interception and detours compose, and the event stream reads like an
itinerary. Arrival sets `location_place_id`; departure emits an event.
Rejected: atomic jump-with-duration — cheaper, but uninterceptable,
unobservable, and it hides the exact per-tick exposure (hunger, cold,
wolves) that makes roads *economic*.

### Fork 3 — Who computes the route?

**Chosen: the engine routes, the script chooses destination and mode
constraints.** The `travel` intent takes a destination place and
optional allowed modes; the engine runs Dijkstra over mode-compatible
edges and starts the first hop; the entity's script never authors a
path. Rationale: paths are engine truth (identical replay), Lua gets a
read-only `world.route(from, to, modes)` / `world.distance_ticks(a, b)`
for *decisions*, and pack authors cannot accidentally create
pathfinding exploits. Policy can still shape cost — edge weights are
data, and a world that wants tolls makes them edge inputs.

### Fork 4 — Where do proximity rules live?

**Chosen: on the recipe/market rows, as data.** A recipe declares
`requires_place_kind: HEARTH` (or a specific place); a market declares
its place. Checked in the same requirement pass as everything else,
rejecting with the same ordinary intent rejection. Rejected: a
VALIDATOR-hook per spatial rule — per-tick correctness belongs to
mechanism + data, validators are for world *policy* on top (the
doctrine the compute-budget work restated).

### Fork 5 — Global or local markets?

**Chosen: per-market nullable place, NULL = global (today's behavior,
default).** Local is opted into by placing a market at a place. The
global-vs-local *vote* (design.md §4.3) is then ordinary parameter
policy over these rows. Deferred: goods-presence at fulfillment and
carrying capacity (S5) — the gate first, the logistics after.

### Fork 6 — What does "no location" mean?

**Chosen: NULL = unplaced, and unplaced is the legacy citizen.** An
entity with no place is subject to no gates (it may work any
ungated recipe, trade on any global market) and to *ambient* (global)
threats only. Worlds that never author places run identically to
today; the moment a pack places entities and gates recipes, absence of
location becomes a real state the pack chose to make meaningful.

## 5. Build order (smallest coherent slice first)

| step | scope | engine change? | ships green with |
|---|---|---|---|
| **S1** | Places + entity location | additive models + migration + Lua/observation surface | zero behavior change |
| **S2** | Presence gates on recipes and markets | requirement-pass extension + describe | gates off unless data present |
| **S3** | Topology + travel (edges, route, travel processes) | `travel` intent + routing + process marks | new behavior, opt-in |
| **S4** | Located danger + the stone-age map | threat/spawn/combat scoping + pack content | wolves with home ranges |
| **S5** | Local markets made real: carrying + fulfillment presence | travel inputs/limits + order checks | couriers parked |
| **S6** | The Luanti boundary | none (platform) — node↔voxel mapping, fork copies | gated on platform era |

Each step is PR-sized, independently shippable, and ordered so the
next one only consumes shipped surface. The full suite (1189 tests
today) must pass unchanged after S1–S2 — they add capability nothing
existing uses.

## 6. Step designs

### S1 — Places and entity location

- `places` table: `id`, unique `key` (pack namespace), `name`,
  `description`, `region_id`, `kind` (uppercase tag: HEARTH, THICKET,
  FOREST, RIVER, POST…), nullable `extent_ref` (world-layer geometry
  hint, ignored by the engine), pack provenance columns like every
  other content row.
- `entities.location_place_id` nullable FK; `Parcel.place_id` nullable
  FK (a parcel's resource *node* — the join that makes extraction
  presence-gateable later).
- Manifest: a `places` section (and `spatial_edges` from S3) joins the
  counts and pins — a map is versioned pack content like goods.
- Surface: the behaviour observation gains `place` (key, name, kind,
  region); `world.places()` lists; `world.place(key)` reads. The
  witness carries place names on events that have them (arrivals, S3).
- Engine helpers: `move_entity(session, entity, place)` — the only
  writer of `location_place_id` besides travel completion (S3);
  genesis may place spawn-time entities (`spawns.py` grows an optional
  place).

### S2 — Presence gates

- `Recipe.requires_place_kind` (nullable), `Recipe.requires_place_id`
  (nullable): checked in `start_process`'s requirement pass — the
  entity's current place must match; rejection is the ordinary
  `rejected` result with a readable reason ("must be at a HEARTH").
- `Market.place_id` (nullable): `place_order` refuses when the market
  is placed and the entity is elsewhere. NULL markets unchanged.
- `describe` gains the requirement text ("requires presence at a
  HEARTH") — readable-surface doctrine: every new gate explains
  itself to scripts and players.
- Events: no new types; the rejection reason carries the place name.

### S3 — Topology and travel

- `spatial_edges`: `from_place_id`, `to_place_id`, `mode` (uppercase
  tag), `cost_ticks` (int ≥ 1), `bidirectional` (default true),
  optional `region_id` for fork-copy scoping (S6), pack provenance.
- `travel` intent: params `to` (place key), `modes` (optional
  allow-list, default all). Resolver: Dijkstra over edges whose mode is
  allowed; refuses unreachable with the readable reason; creates one
  Process per hop — recipe synthesized from the pack's travel recipe
  for that mode (packs author `TRAVEL_WALK`, `TRAVEL_RAFT`, … with
  normal requirements: a RAFT recipe may require holding 1 RAFT, a
  RIDE recipe 1 MOUNT — vehicles and mounts are *modes unlocked by
  requirements*, data end to end), `completes_tick = now + edge cost`.
  On hop completion: set location, emit `travel_arrived`
  `{place, remaining_hops}`; on start: `travel_departed`. Cancellation
  mid-route strands the entity at the last place — a real state, the
  pack's business.
- Processes marked `is_travel` with `edge_id` — skipped by
  production-side statistics, visible to census.
- Lua: `world.route(from_key, to_key, modes?)` (read-only itinerary:
  hops, modes, total ticks), `world.distance_ticks(a, b, modes?)`,
  `entity.place` in the observation, `travel` in the intent surface.
- Packs may *generate* topologies at install (a Lua builder that
  materializes a grid as places + edges) — grid worlds, voxel
  projections, and hand-drawn trail maps are the same rows.

### S4 — Located danger, and the stone-age map

- `Threat.place_id` nullable — NULL stays ambient-everywhere (today).
  A placed threat pressures only entities at (or, for edge-scoped
  menaces, traversing) its place. Home ranges are pack data.
- Spawned menace entities (`spawn_entity`) take an optional place —
  wolves get dens; their BEHAVIOUR script (already Lua, already an
  entity) may `travel` like anyone: a roaming wolf is not new
  mechanism, it is a wolf with a pass.
- Combat gate: `attack` requires target co-location (same place), and
  travel processes expose their traveler to placed threats at each
  hop — the road at night is a risk profile, computed from the same
  rows.
- The pack (`experiments/world/stone_age.py`) authors the map: hearth
  clearing (start), berry thicket (1h), river (2h, fish), flint
  scrape (2h), deep forest (3h, spear game, wolf range), trading post
  (4h). Recipes gain presence kinds (gather at THICKET, hunt at
  FOREST, tend at HEARTH); the post's market becomes placed. The
  starter script learns to walk.

### S5 — Local markets made real

- Carrying: travel recipes declare `carry_capacity` (units of goods
  the traveler may take along, by mode — WALK small, CART large).
  Departure snapshots the manifest; arrival credits it. Self-carry
  only in this step.
- Fulfillment presence: an order on a placed market fills only from
  goods present at the market's place — sellers must deliver. This is
  where regional price differences become earnable.
- Parked (named, not built): courier contracts (delivering for
  another entity), escrowed trade windows, vehicle entities with
  their own damage. None block the payoff above.

### S6 — The Luanti boundary (platform era, gated)

Node ↔ voxel-chunk mapping through `extent_ref`; fork petitions copy
regions as engine place/edge/parcel rows + world-layer chunks
(design.md §4.2/§4.3 — chunked terrain is why); physics-level votes
mirror the engine's parameter surface. Decision Luanti vs Godot stays
what design.md says: prototype the farming loop first. Nothing in
S1–S5 assumes it; the batched game never needs it.

## 7. What stays explicitly unbuilt

- **Coordinates in the engine.** Permanent (Fork 1). Distance is
  always ticks-through-topology, never meters.
- **Per-entity pathfinding policy.** The engine routes; scripts pick
  destinations. If a world wants dumb agents to get lost, it authors
  a Lua validator that refuses good routes — policy, not mechanism.
- **Visibility/fog-of-war.** Observation stays the per-entity digest:
  what the script can see is its own state, its place, published
  routes, and witnessed events. Hidden-map play is a world-layer
  concern.
- **Convoy/fleet movement.** Single-entity travel only; grouping is
  emergent timing, not mechanism.
- **Research/tech-tree-information** (the second half of notes.txt —
  non-rival goods, skills outside the estate): a different layer
  (knowledge, not space), planned separately when earned. Recorded
  here only so notes.txt can be retired.

## 8. Proving experiments

The campaign doctrine applies unchanged: one variable per run,
declared exhibitions, postmortem census, archive with epitaph.

- **S4's proving run** (the natural run 26 or 27): same wolves,
  same post, plus the map. Census additions: travel census (hops,
  mode-miles, strandings), location histogram, time-at-hearth vs
  time-on-road, and the market question — does the post live when it
  costs four hours to reach?
- **Wolf-range run**: ambient → placed threats as the single
  variable. Do houses learn the forest's hours?
- **Two-markets run** (after S5): a second, nearer, thinner market.
  Arbitrage, or loyalty by geography?

Success is not "houses survive the map" — run 25 says the interesting
failure modes are economic. Success is *spatial behavior in the
census*: routes chosen, risks priced, a house that moves for flint.

## Status

- **S1 shipped (#149):** places + entity location — `models/place.py` and the
  `places.py` helpers (create/claim/list/facts + `move_entity` as the single
  writer), `entities.location_place_id` and `parcels.place_id` (nullable,
  migration `j6b9d4f2a8c1`), the manifest's `places` counts + stamp, the
  behaviour ctx's `ctx.place`/`ctx.places` and `world.places()`/
  `world.place(key)`, MCP `entity_state`'s `place`, spawn templates' optional
  `place` (dens). Dormant by construction: no pack ships a map until S4, so
  every world runs exactly as before. **S2 (presence gates) is the entry
  point now.**
