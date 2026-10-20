# Camp storage — spec

Status: **designed, not built.** Three storage surfaces, one new table, four
new actions. Closes both of run-57's load-trap death classes (at-home and
away-from-home) and adds shared drop / pickup as a first-class resource
dynamic (theft, scavenging, the camp woodpile).

## The problem it fixes

run-57 killed 2 of 3 houses on a crossed `load < N` gate (Lagertha THIRST
at the hearth, Harald HUNGER away at the thicket). `load` is a *script
fiction* — the engine never checks it, and travel is never load-gated. The
killer is that once a house's carried goods cross the threshold, **there is
no action that reduces the load**: selling needs the post, the post needs
travel, and the same gate blocks the travel. The house stands still and a
need finishes it.

Storage is the load-release valve. This spec builds **three** surfaces so
the valve opens everywhere:

| Surface | Where | Owned by | Capacity | Stealable? |
|---|---|---|---|---|
| **Private store** | the hearth (home) | the house | capped (40 units) | no — only you |
| **Shared drop** | every place **except** the hearth | nobody (shared) | unbounded | **yes** — anyone picks up |
| **Shared woodpile** | the hearth | nobody (shared) | unbounded | yes (wood only) |

- **At home** you set goods down in your **private store** (safe, capped) —
  fixes Lagertha's at-home trap.
- **Away from home** you **drop** goods at the place you're at (unbounded,
  but anyone can pick them up) — fixes Harald's away-from-home trap.
- **At the hearth** there is also a **shared woodpile** — the camp's common
  wood, **pulled from first** when wood is needed.

## The theft / scavenging layer (emergent, no engine change)

"Shared, other people can pick it up" is the interesting part, and it needs
**no new engine mechanism**. The key insight:

- **Drop** = holdings → shared pool (you initiated; a normal goods move).
- **Pickup** = shared pool → holdings (the pool is owned by nobody, so taking
  from it is a normal goods move — not a transfer between entities).

So once goods are dropped they are **in a shared pool, not in anyone's
holdings**. "Theft" is just the emergent outcome that someone picked them up
before you. There is no "steal from another entity's holdings" mechanism —
that would violate the ownership invariant. Conservation holds: goods move
between holdings and pools (and rot); nothing is created or destroyed except
by decay.

**Scavenging** is emergent for the same reason: the Trading Post can't move,
but a house can `pickup` dropped goods at a drop zone and then
`place_order(sell)` them at the post. That's already composable from
`pickup` + the existing market actions — no new mechanic. Whether an agent
makes a job of it is the experiment.

## What persists, what rots

`apply_decay` already rots the market-escrow shelf (*"a market is not a
cellar… the world's rot ledger has one column"*). Extend it to the storage
pools: **dropped/stored/stashed goods rot at the same rate as carried
goods.** So:

- **Food** (MEAT .30 / BERRIES .15 / COOKED .25 / APPLES .08 / EGGS .05)
  dropped in the wild **rots** — a shared food drop is a race between rot and
  being picked off.
- **Wood / flint / yarn / clay / salt / jerky / tools (decay 0)** dropped in
  the wild **persist forever**.

So "stays until it decays" is only true for food; **non-rotting goods
accumulate unbounded at the drop zones.** That is deliberate and is the thing
to *watch*: if rubbish piles up, add a cleanup mechanic later. **No cleanup
mechanic in this build** — observe first.

## The shared woodpile (the camp's wood)

The hearth's **shared woodpile** is the camp's common wood. "Pulled from
first" means: when a wood-consuming recipe runs at the hearth, it draws wood
**from the woodpile before** the house's own wood holdings.

**Implementation: engine-level, in `start_process`.** Verified against the
code: `start_process` (production.py line 318) checks the unreserved balance
(line 560: `available = held - reserved_quantity`) and draws each input at
start via one `adjust_holding(entity, symbol, -qty)` (line 619) — inputs are
**drawn at start, not merely reserved**, and are forfeit on cancel (line 651).
So the change is localized and atomic:

1. **Availability** (line 560): for a `WOOD` input, add the woodpile pool at
   the process's place to the available amount —
   `available = (unreserved WOOD holding) + (WOOD in the WOODPILE pool here)`.
2. **Draw** (line 619): for a `WOOD` input at a place with a WOODPILE, draw
   `min(pool, needed)` from the pool first (`FacilityHolding` row
   `owner_id="*", symbol="WOOD"` −= n), then draw the remainder from the
   holding as today.

Because the draw happens **at start** (inside the tick's transaction, not a
reservation), there is no double-booking: the pool is decremented the moment
the process starts. The only shared-state concern is the availability read,
which is consistent within the tick.

**Why engine-level, not starter-level:** "pulled from first" is a *world
rule* (the camp's wood comes from the camp pile), not a house strategy — a
rewrite that forgets it would silently stop using the woodpile. Engine-level
enforces it. It also reinforces the load valve: a house sheds wood-load by
`drop`ping into the woodpile, and recipes draw from the woodpile, so the
house's wood holding stays low and the pile is the active source. The
lighter alternative (starter does `pickup(WOOD)` before each wood recipe) is
acceptable but breakable; the consequence of breaking it is mild (the house
just uses its own wood), so either is safe — engine-level is recommended.

`std.unreserved("WOOD")` / `std.holding_qty("WOOD")` report the **holding**
only (the pool is not a holding), so a script that wants the effective wood
reads `dropped("WOOD")` (the shared pool at its place — the woodpile at the
hearth) and adds it. The starter does `wood = holding + dropped("WOOD")` at
home.

## Model

One new table, `FacilityHolding`, covers all three surfaces (private +
shared). It mirrors `MarketEscrow` (S5b) but keys on a facility and an owner:

```python
# models/facility_holding.py
class FacilityHolding(Base):
    """Goods physically at a facility's shelf — per (facility, owner, symbol).
    owner = an entity id -> that entity's PRIVATE store (only it may touch
    the row); owner = "*" -> the SHARED pool at the facility (any entity may
    deposit/pickup). The drop/pickup pair is what makes theft and scavenging
    emergent: once dropped, goods are in a pool nobody owns. Pools rot like
    carried goods (apply_decay) and a house's private rows burn/inherit with
    the estate; shared rows outlive their dropper."""
    __tablename__ = "facility_holdings"
    __table_args__ = (UniqueConstraint("facility_id", "owner_id", "symbol",
                                       name="uq_facility_holding"),)
    id          # str pk
    facility_id # FK facilities.id
    owner_id    # String(36), NOT NULL, NO FK: entity id (private) or "*" (shared)
    symbol      # String(32)
    quantity    # Numeric(18,4) >= 0 (service-enforced, as in adjust_holding)
    created_at  # DateTime(timezone)
```

- `owner_id` is **NOT NULL** with a `"*"` sentinel for the shared pool so the
  unique constraint is DB-enforced (SQLite treats NULLs as distinct in a
  UNIQUE index, which would allow duplicate shared rows). No FK on `owner_id`
  (it can be the sentinel); the service checks ownership.
- **Capacity** is a *policy* on the private store only: the sum of a
  facility's rows for one owner ≤ the facility's `capacity` (the pack sets
  40 for the private store). Shared pools (owner `"*"`) are **unbounded** —
  the capacity check is skipped for them. `Facility.capacity` is reused (no
  column change); no recipe `requires_facility` these types, so the
  `facility_capacity` binding-sum is never consulted for them.

## Facility types (pack-placed)

| `facility_type` | Placed at | Access | Capacity | Meaning |
|---|---|---|---|---|
| `STORAGE` | the hearth parcel | private (per owner) | 40 | the house's private store |
| `DROP` | **every place except the hearth** (incl. the post) | shared | unbounded | the shared drop zone |
| `WOODPILE` | the hearth parcel | shared | unbounded | the camp's shared wood (WOOD only) |

The pack places these at genesis (like the commons `FIRE`): one `STORAGE`
and one `WOODPILE` on the hearth parcel, and one `DROP` on every other
place's parcel. `WOODPILE` accepts **only `WOOD`** (the service rejects any
other symbol). `DROP` accepts any symbol. `STORAGE` accepts any symbol.

## Actions

Four new `ctx.action.*`, all instant and labor-free, all **presence-gated**
(the entity must be standing at the facility's place). Wired in
`lua_engine.py` exactly like `_say`/`_travel` (build an `Intent`, register in
`action_tbl`), resolved in `resolve_intent` with `with session.begin_nested()`
(rollback = atomic). None are in `INTENT_CAPABILITIES` (self-directed, like
`travel`/`say`).

- **`store(symbol, qty)`** — holdings → **private** `STORAGE` at the hearth.
  Capped (owner's total ≤ 40).
- **`take(symbol, qty)`** — **private** `STORAGE` → holdings.
- **`drop(symbol, qty)`** — holdings → the **shared** pool at the caller's
  place: the `WOODPILE` for `WOOD` at the hearth, the `DROP` anywhere else
  (and `DROP` for non-wood at the hearth is rejected — there is no shared
  non-wood drop at home; you `store` it privately or carry it to the field).
- **`pickup(symbol, qty)`** — the **shared** pool at the caller's place →
  holdings. This is the theft/scavenge action: it draws the pool regardless of
  who dropped the goods.

Reject reasons (the debug loop), readable: `"no storage at {place}"`,
`"no drop at {place}"`, `"holds 3.0000 WOOD, cannot drop 4.0000"`,
`"the woodpile takes WOOD only"`, `"the shared pile has 2.0000 WOOD, cannot
pickup 3.0000"`, `"store is full (40/40)"`.

## Queries

- **`ctx.query.stored(symbol)`** — the caller's **private** store quantity of
  `symbol` (nil when none / not at home).
- **`ctx.query.dropped(symbol)`** — the **shared** pool quantity of `symbol`
  at the caller's place (the woodpile for WOOD at the hearth, the drop zone
  elsewhere; nil when none). Named `dropped` (not `shelf`) because
  `ctx.query.shelf(symbol)` already exists for the market escrow.

A script computes `load` from holdings as today (stored/dropped goods are
automatically excluded — that is the point); `stored(symbol)` +
`dropped(symbol)` tell it what it can `take`/`pickup`.

## Decay

Extend `goods.apply_decay` (line 169) to also decay `FacilityHolding` rows
in the same per-good loop, same math, same single `decay` event — the
*"world's rot ledger has one column"* rule already set for `MarketEscrow`.
A camp and a drop zone are not cellars: stowed/dropped berries rot like
carried berries.

## Estate

Extend `conditions._apply_estate` (the holdings sweep) to also process the
entity's **private** `FacilityHolding` rows (owner = the entity), in the same
pass: `burn` policy burns them with the holdings; `heir` transfers them to
the recipient; condition symbols always burn. **Shared rows (owner `"*"`)
outlive their dropper** — goods dropped into a shared pool are no longer the
dropper's, so they stay in the pool (available to others) and are not part of
the estate. That is consistent with "once dropped, it isn't yours."

## Events / audit

Applied intents emit events (the applied line is the record):
`facility_store` / `facility_take` / `facility_drop` / `facility_pickup`,
each `{entity_id, facility_type, symbol, quantity, place}`. `describe.py`:
add the four intent types to the intent-text set (lines 43–45) and a branch
each in `_intent_text` (line 57). These are also the witness/audit trail for
"who dropped what where, and who picked it up" — the theft record.

## Migration

One new alembic revision: `op.create_table("facility_holdings", …)` with the
unique `(facility_id, owner_id, symbol)` and the FK to `facilities.id`
(`owner_id` has no FK). **No change to `facilities`.** Downgrade drops the
table.

## Pack (stone_age.py) + starter

- Genesis: place `STORAGE` (capacity 40) + `WOODPILE` on the hearth parcel
  (the same `ground` parcel as the commons `FIRE`, lines 1084–1087), and a
  `DROP` on every other place's parcel.
- Starter models the valve so rewrites inherit it:
  - At home, when load-high, `store` the surplus (the non-rotting ballast:
    wood/flint/yarn/eggs) into the private store, and `drop` surplus **wood**
    into the shared woodpile (feed the camp).
  - When it needs wood to stoke/craft, read `dropped("WOOD")` (the woodpile)
    and rely on the engine's pull-from-first (or `pickup` it, starter-level
    alternative).
  - Away from home, when load-high or stranded, `drop` the non-essential
    goods at the place (they persist; a neighbour or a future self can
    `pickup` them).
  - The `load >= 16 → travel home` line and the `water < 1 → travel river`
    drink walk stay exactly as they are.
- `pack.json`: regenerate after the starter edit; the new facilities are
  genesis data (`Facility` rows, not counted content rows), so content counts
  are unaffected — only the starter's sha changes.

## What it fixes / what it doesn't

- **Fixes the at-home overload trap** (Lagertha): `store` the flint/yarn/
  pelt/eggs → `load < 16` → gather/relight/drink reopen.
- **Fixes the away-from-home trap** (Harald): `drop` the wood/flint at the
  thicket → load falls → he moves. (No storage at the thicket before; now
  there's a `DROP`.)
- **Adds the shared layer**: theft (a house can `pickup` another's drop),
  scavenging (a house can scavenge drops and sell them at the post), and the
  camp woodpile (shared, pulled-from-first wood).
- **Does not** add a cleanup mechanic (observe whether rubbish accumulates),
  does not add a shared *food* drop at the hearth (non-wood at home is
  private-store-or-carry), and does not make the Trading Post a scavenger
  (it can't move; scavenging is a house's job).

## Risks / open questions (watch, don't build yet)

- **Unbounded non-rotting accumulation** at drop zones + the woodpile (wood
  never rots). Deliberate — observe, then decide on cleanup.
- **Theft as a death class**: a house that drops food to shed load can have it
  picked off and starve *because of someone else*. Intended (the user wants
  theft), but it's a new way to die — watch the post-mortems.
- **One house griefing the other two** in a 3-house world (steal everything).
  Intended tension; no anti-griefing in this build.
- **Private-store cap** (40): a house with >40 units of *non-wood* goods at
  home can't stow the excess privately (no shared non-wood drop at the
  hearth) — it must carry some to the field to `drop`. Rare; the cap is a
  tunable knob.

## Explicit non-goals

- No shared (commons) shelf for *food* at the hearth — home is private.
- No per-symbol capacity — one total-unit cap on the private store only.
- No drop/destroy action (storage *preserves*; `drop` = set down, not lose).
- No cleanup/rot-of-piles mechanic (observe first).
- No geometry/coordinates — the engine already knows "the entity is at the
  place" and the facilities are on the parcels; nothing new spatial.
- No "steal from another entity's holdings" — theft is pool-pickup only.

## Build order (when implemented)

1. `models/facility_holding.py` + register in `models/__init__.py`.
2. Alembic migration (create table).
3. `scripting.py`: the four `resolve_intent` branches + `stored` / `dropped`
   queries.
4. `lua_engine.py`: the four `ctx.action` handlers + `action_tbl`.
5. `goods.apply_decay`: decay `FacilityHolding` rows.
6. `conditions._apply_estate`: clear/transfer the entity's private rows.
7. `production.start_process`: the WOOD pull-from-first (availability line
   560 + draw line 619).
8. `describe.py`: render the four actions.
9. `stone_age.py`: place `STORAGE`+`WOODPILE` at the hearth, `DROP` at every
   other place; starter models store/take/drop/pickup; `pack.json`.
10. Tests: `test_camp_storage.py` (store/take + drop/pickup conservation, the
    presence gate, the private cap, the shared unbounded + pickup-by-another =
    theft, decay-on-the-pool, estate-burns-private-but-not-shared, the WOOD
    pull-from-first) and the stone_age pack test asserting the facilities
    exist at genesis.
