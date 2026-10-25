# Roadmap: the living world — real-time 2D, multiple clients, spectators

Status: **W1 core and the first W2 walk/talk slice are shipped; final
device/scale acceptance remains.** The repo now has the read-only backend
and client slice, deterministic chunks, the live snapshot/delta channel, an
automated Chromium rendering smoke test, and a small possession channel for
owned entities. This is the durable scope for spatial step S6. It keeps the
boundary in `docs/design.md` §4.3 and `docs/spatial.md`, but replaces the
old Luanti-first implementation assumption: the first navigable world is a
custom, server-authoritative **2D tile world** with a web reference client.
Luanti remains a possible later voxel geometry; Godot remains a possible
later client/runtime. Neither defines the world/engine boundary.

The first product slice is one existing stone-age world in which human and
AI entities share a cosy, animated map. Players walk, talk, and interact;
pure spectators explore the whole world, follow entities, and watch what
happens without owning an entity or having any write access.

## Try it locally

For a live multi-agent stone-age run that is browser-observable and
browser-joinable, launch `nim_run` with the world clock and a browser
account (the full operator notes live in `experiments/agent/README.md`):

```bash
.venv/bin/python -m experiments.agent.nim_run \
    --models <slug-1> <slug-2> <slug-3> \
    --names "House Llama" "House Qwen" "House Mistral" \
    --scenario stone_age --rounds 40 --ticks-per-round 20 \
    --edit-mode --diary --port 8917 --serve 8113 \
    --world-tick-seconds 15 --browser-user "Your Name" \
    --out /tmp/stone-run

# in a second terminal
cd web
npm install
ECON_API_PROXY_TARGET=http://127.0.0.1:8917 npm run dev
```

Open <http://localhost:5173>, paste the printed browser token, and the
living map appears in spectator mode. **Join the world** uses the ordinary
`POST /join`; **Possess** then hands local walking to the browser while
speech still resolves through `say` on the next world tick. The old round
dashboard remains at <http://127.0.0.1:8113>. The world-clock flag matters:
a browser account that joins a readiness-only world becomes eligible for
consent and can hold the round open, and an unarmed world refuses
world-layer speech.

## 1. What already exists (build on it, do not route around it)

- **Topology without geometry** (`spatial.md` S1–S4): places, roads,
  entity location, presence gates, travel-as-process, and located danger.
  `Place.extent_ref` and `Parcel.extent_ref` are the opaque joins to
  world-layer geometry. Coordinates never enter `econengine`.
- **Economic state worth looking at**: parcels, facilities, deposits,
  processes, travel routes, threats, and entities. These drive the visible
  scene; the client does not invent a parallel simulation.
- **One action surface**: `POST /intents` and the direct-action channel run
  the same resolver as script intents. The world server is another client
  of that surface, not a privileged back door.
- **A real-time economic clock when wanted** (`game.md` §9.2): the world
  clock advances ticks on a wall-clock interval. Batched worlds remain
  valid and spectatable; their economic state advances only when their
  rounds resolve.
- **Live public facts**: the round/tick SSE stream, catalog, map, activity,
  and leaderboards already provide the low-frequency observation seams.
- **Speech and witness**: `say` is an engine intent with bounded cadence,
  audit, and delivery to AI behaviour scripts. Human conversation must not
  become a separate human-only chat system.
- **AI entities need no client**: their BEHAVIOUR scripts already act. The
  world layer projects those entities into the same scene as human-driven
  entities.

S5b (fulfillment presence) is not a blocker for the read-only spectator
slice. Places, travel, processes, and speech are enough to prove the world
layer's visual and authority boundaries.

## 2. Locked direction

1. **2D first.** Cosy pixel art, animated sprites, a large chunked map,
   nearest-neighbour rendering, and integer-friendly scaling. The existing
   stone-age artwork is a mood reference, not a baked runtime map.
2. **Web reference client.** TypeScript and a dependency-light Canvas 2D
   renderer (chosen by the first visual spike). Phone and desktop browsers
   are targets from the first playable slice; native shells and
   independently written clients come later.
3. **Server authority.** Clients send input and commands, never
   authoritative positions, arrivals, interactions, or economic outcomes.
4. **Spectator is a session class from day one.** It is not a player with
   permissions patched afterwards.
5. **Chunked, generated terrain plus live scene overlays.** No single
   static world image and no client-side authoritative resource placement.
6. **Geometry is replaceable; the economy boundary is not.** `tile2d` is
   the first geometry implementation, not a new engine concept.
7. **AI and humans share the world and the rules.** Both are rendered as
   actors; both ultimately act through the same economic intents and
   travel processes.

## 3. Systems and authority

| Concern | Authoritative owner | Client receives |
|---|---|---|
| Ledger, ownership, holdings, recipes, governance | Economy engine | Public/authorised projections only |
| Economic place and inter-place arrival | Economy engine travel process | Place/route state and progress |
| Local coordinate, collision, interaction range | World server | Position/animation deltas |
| Terrain and geometry | World server geometry adapter | Versioned chunks and geometry descriptor |
| Facility/deposit/parcel existence | Economy engine | Scene objects resolved through `extent_ref` |
| Walking, working, speaking, combat animation | World server, derived from real state | Semantic appearance/action state |
| Player/spectator permissions | World server + platform identity | Session capabilities, never self-asserted |

The world server may hold high-frequency positions in memory and its own
store. It must **not** write 10–20Hz coordinates into the economy database.
The engine learns only the semantic facts it already understands, through
the ordinary intent/process paths.

### Local movement versus economic travel

This distinction is the central conformance rule:

- **Within a place**, a player may walk freely in real time. The world
  server owns collision and local interaction range.
- **Between places**, crossing a route starts the existing `travel` intent.
  The world server may animate the actor smoothly along the road, but the
  engine's travel process alone determines economic arrival.
- A disconnect, reconnect, visual interpolation error, or malicious client
  cannot complete a journey early or skip its inputs, carrying limit,
  needs, or danger.
- Interacting with a fire, resource, facility, or market performs a local
  proximity check and then submits the ordinary economic intent. The
  engine revalidates presence, ownership, inputs, and capabilities.

A real-time player world arms the existing world clock. A batched world can
still be watched, but the world layer must not pretend that cosmetic motion
has advanced its economy between resolved ticks.

## 4. Multiple clients and coordinate systems

These are related but different requirements.

- **Multiple clients** share a versioned session/snapshot/delta protocol.
  A client implements the geometry kinds it supports and negotiates them
  during bootstrap; an unsupported geometry is refused readably.
- **Multiple coordinate systems** are world-layer adapters. The first is
  `tile2d.v1`; later adapters might project the same engine topology from
  a voxel world, a hand-drawn graph, or real-world geodata.
- The common protocol covers actors, sessions, speech, interaction
  requests, spectator subscriptions, and public scene events. Positions,
  chunks, and geometry-specific commands live in versioned geometry
  payloads rather than pretending every space is a 2D coordinate.
- A world selects its authoritative geometry. Clients may render it
  differently, but may not each select incompatible authoritative physics
  for that world. Alternative read-only projections are safe; alternative
  movement authorities are not.

Do not build a universal geometry framework before the second adapter.
`tile2d.v1` establishes the seam; a second, meaningfully different adapter
is the test that the seam is honest.

## 5. The generated map and art pipeline

The map has two deliberately separate layers:

1. **Base terrain chunks** are generated server-side from
   `(world_seed, chunk_coordinate, generator_version)` and cached. Every
   client asking for the same version receives the same terrain; phones do
   not need to reproduce procedural generation locally.
2. **Dynamic scene objects** are projected from places, parcels,
   facilities, deposits, processes, routes, threats, and entities. Chunks
   and scene state have independent revisions so a growing crop or a moved
   actor does not regenerate terrain.

Cosmetic decoration may be procedural. Anything interactive or economically
meaningful must correspond to engine data: the renderer cannot grow a berry
bush the economy does not contain. Procedural expansion of the *economic*
map — new places, parcels, deposits, and roads — is later genesis/content
work, not a side effect of loading a visual chunk.

Packs gain a world-layer scene manifest alongside their existing content:
geometry kind/version, seeds and anchors, tile/sprite atlases, and bindings
from semantic state to appearance. Clients render `appearance` and `action`
keys; they do not hard-code a special renderer for every recipe. Labels and
entity positions are dynamic, never baked into artwork.

## 6. Real-time protocol

- **HTTP bootstrap**: world/session capabilities, geometry descriptor,
  asset versions, current economic tick, a snapshot, and chunk URLs.
- **HTTP chunks**: cacheable, generator-versioned terrain requests.
- **WebSocket live channel**: W1 sends a hello snapshot, field-level scene
  deltas, speech-relevant changes, pings, and resynchronisation notices for
  read-only spectators (first-message auth; no token in the URL). W2 uses
  the same channel for `possess`, `release`, `move`, and `say` commands;
  every command carries a per-seat sequence and receives a
  `command_result`. High-frequency local presence arrives as
  `presence_delta`; economic facts still arrive only through scene deltas.
- **Existing SSE**: low-frequency economic tick/round facts where useful;
  it is not stretched into a 20Hz position channel.

Start with JSON for debuggability. The initial world-server target is
10–20 simulation updates per second, with client interpolation for smooth
rendering; measure before adding binary encodings or client prediction more
complicated than local presentation. Inputs and snapshots carry sequence /
revision numbers so reconnects cannot duplicate commands or leave a client
on a stale scene.

## 7. Speech, visibility, and AI

In-world speech uses `say`, so a spoken line can appear as a bubble and in
the persistent public conversation feed while remaining audible to AI
behaviour scripts and present in the audit trail. If one-say-per-tick or
its delivery latency proves unsuitable for real-time conversation, revise
that speech policy explicitly; do not add a parallel chat world that AI
cannot hear.

The spectator projection exposes public spatial facts and visible scene
states — walking, travelling, working, speaking, fighting, incapacity —
without exposing private holdings, prompts, behaviour source, or ledger
detail. A visible animation is not a licence to serialize the underlying
private Process row. The W1 vocabulary is deliberately generic:
`idle`, `work`, `travel`, and `incapacitated`; recipe identity, holdings,
and private process detail remain behind the animation.

V1 is a public-map world: spectators may free-camera across it, with no fog
of war. Whole-world visibility does not imply full-rate updates for every
actor on an arbitrarily large map. The protocol leaves room for viewport /
follow subscriptions, low-rate overview markers, and clustering.

AI entities are projected whether or not any seat is connected. A human
seat is one possible controller of an entity, not a different species of
actor.

## 8. Build order

Each step is independently demonstrable. W1 deliberately comes before
multiplayer writes: it proves the aesthetic, generated map, public
projection, and AI visualisation without risking movement or ownership
rules.

### W1 — Living spectator

**Current slice (shipped):** `GET /world/bootstrap`,
`GET /world/scene?since=<revision>`,
`GET /world/chunks/{cx}/{cy}?v=<cache-key>`, and WebSocket
`/world/stream` (hello snapshot + field-level scene deltas; W2 possession
commands are refused for a pure spectator). The spectator session requires
no entity and has no accepted world-layer writes. Terrain chunks
are deterministic; engine places resolve through `tile2d:v1:<x>,<y>`
`extent_ref`s (or a stable fallback layout); roads, parcels, facilities,
deposits, actors, generic work/travel state, and recent public speech are
projected read-only. `web/` is the TypeScript/Vite Canvas reference client,
with an automated Chromium smoke test for phone layout, rendering, live
deltas and reconnect, follow mode, and desktop resize.

**Still open before W1 is done:** richer pack art manifests/atlases,
broader load testing, and the full W1 acceptance pass on real devices. An
initial synthetic check generated 24 chunks in 0.091s (~3.8ms/chunk) and
projected 500 actors + 900 objects in ~0.019–0.025s; that is a smoke
measurement, not the W4 scale result.

- `tile2d.v1` bootstrap, chunk, and read-only scene projection APIs.
- Server-generated terrain with deterministic tests and dynamic overlays
  from the current stone-age world.
- Web client with pixel-perfect rendering, animated actors, camera/zoom,
  follow mode, and a public speech/activity feed.
- Human and AI entities use the same actor rendering. Offline AI entities
  remain visible and animate from real travel/process/event state.
- Pure spectator sessions have no entity requirement and no write verbs.

**Done when:** two browsers show identical terrain and public scene state;
a spectator can explore and follow an AI without owning an entity; a
reconnect restores snapshot + deltas; and no coordinate or real-time state
has entered `econengine`.

### W2 — Walk and talk

**Current slice (shipped):** the `/world/stream` session can possess one
account-owned, ACTIVE, placed, non-fixed individual whose engine state is idle.
The process-local world runtime then owns that actor's cosmetic local
coordinate inside a small place bound, validates normalized direction
vectors, enforces strictly increasing per-seat sequences and a 20 command/s
limit, applies simple object/actor collision, and broadcasts
`presence_delta`s to every spectator. `say` submits the ordinary engine
intent on an armed world clock (queued for the next tick, so witness delivery
and the one-say budget are unchanged); an unarmed world refuses world-layer
speech rather than pretending an off-tick utterance was witnessed. Keyboard
and touch D-pad input are implemented in `web/`; reconnecting releases possession and returns the actor to its
engine-projected position.

**Control arbitration (chosen):** direct-action precedence with conflict
refusal. Possession does not pause or rewrite the entity's BEHAVIOUR script,
but local walking is refused while an engine work/travel process is running,
and the world server releases the seat if a later tick gives the entity a
`work`, `travel`, or `incapacitated` state. The engine's direct-intent
outbox already drains before script intents, so a human command and a script
command share one resolver and one per-tick budget without a second steering
wheel.

**Position recovery (chosen for W2):** presence is process-local memory. A
server restart or reconnect loses the exact local coordinate and restores
the actor at its deterministic scene anchor. Durable exact-position recovery
belongs to W4's scale/restart work.

- Real-time world-server sessions, server-side local positions, collision,
  WebSocket commands/deltas, keyboard and touch input. *(first slice shipped)*
- Player versus spectator authorisation, rate limits, sequence handling,
  disconnect/reconnect, and anchor-based position recovery. *(first slice
  shipped; durable exact recovery remains W4)*
- Local walking within a place and in-world speech through `say` on an armed
  world clock. *(shipped)*
- Explicit control arbitration so a human and a BEHAVIOUR script cannot
  steer the same entity simultaneously. *(chosen and shipped: direct-action
  precedence with conflict refusal)*

**Done when:** one human and several AI entities share a live scene across
phone and desktop browsers; another client can spectate them; and a
spectator's forged write command is refused.

### W3 — Touch the economy

- Route crossings become engine travel intents; visible road progress never
  outruns the travel Process.
- Proximity-gated interaction verbs submit ordinary intents and render
  accepted work, travel, combat, and incapacity states.
- Rejected intents return useful local feedback without exposing another
  entity's private state.
- Human and AI actors follow identical economic travel and interaction
  gates.

**Done when:** the first playable slice works end to end: a human walks
from the hearth, travels to the thicket, gathers, speaks, and returns while
AI dynasties act nearby and spectators watch. No client can arrive early,
work from the wrong place, or create a resource by manipulating the scene.

### W4 — Scale and broaden

- Interest management, whole-world low-rate overview, chunk caching,
  position persistence, restart recovery, and load tests.
- Better spectator tools: minimap, clusters, activity filtering, and
  follow/jump navigation.
- Native shells or independently written clients against the same protocol.
- A second geometry adapter; only then generalise the geometry interface.
- Multi-world registry, pack selection, fork petitions, terrain-region
  copies, and physics-level votes continue as their own platform tracks.

## 9. Explicit non-goals for W1–W3

- 3D/voxel implementation, or simultaneous incompatible geometries in one
  world.
- Infinite procedural expansion of economic resources.
- Player terrain editing, free-form building physics, or physics votes.
- Native app store releases and MMO-scale population claims.
- Replay/time travel, fog of war, proximity voice, or private omniscient
  spectator views.
- Multi-world forking and the identity/meta-layer (still `design.md` §4
  platform work).
- Any human-only economic or conversational action path around the engine.

## 10. Open questions (resolve at the named step)

- **Human/script control arbitration (resolved in W2):** direct-action
  precedence with conflict refusal. Possession never pauses BEHAVIOUR;
  running engine work/travel/incapacity takes the steering wheel back.
  Needs and other engine passes continue either way; two steering wheels
  do not.
- **Real-time cadence and conversation (W2):** which world-clock interval
  gives interactions and `say` acceptable latency without compromising the
  economic simulation? Movement smoothness must not be used to hide an
  unsuitable speech cadence.
- **Local-position recovery (chosen for W2):** exact coordinates are
  process-local; restart/reconnect falls back to the scene's deterministic
  place anchor. W4 decides whether durable positions justify a world-layer
  store.
- **Map scale (W1/W4):** chunk dimensions, source tile resolution, initial
  world extent, and when overview clustering becomes necessary.
- **Economic map generation (after W3):** when a generated visual frontier
  should materialise new engine places/parcels/roads, and through which
  audited genesis or governance path.
