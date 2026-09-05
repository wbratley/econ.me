# The scripting API — complete reference

This is the full surface a script can touch: every `ctx` field, every
`ctx.query.*` and `ctx.action.*` member, the engine `std` library, and
the per-world `world`/`pack` libraries — with exact types, shapes and
nil-ability. It exists because the shapes are load-bearing: every
occupational death in the stone-age runs so far traces to a script that
guessed a shape wrong and failed silently (the fault catalogue in §7 is
real failing lines, kept so the next reader sees the classes).

Where the truth lives in code (this doc mirrors them; change both):

| Surface | Definition |
|---|---|
| `ctx` fields | `engine/econengine/tick.py::_build_script_ctx` |
| `ctx.action.*` stubs + arities | `engine/econengine/lua_engine.py` (`action_tbl`) |
| `ctx.query.*` | `engine/econengine/scripting.py::build_queries` |
| `std` library | `engine/econengine/lua_engine.py::_STDLIB_LUA` (pinned by fingerprint) |
| `world` library | `experiments/world/lua/world_lib.lua` (per world) |
| content pack | `experiments/world/lua/*.lua` → the `pack` namespace |

A script sees its API three ways: the injected namespaces themselves,
`get_script_libraries` (returns the sources verbatim), and this doc.
The agent harness's system prompt carries a shape crib derived from
§1–§2.

Script types (docs/scripting.md §2): BEHAVIOUR (runs every tick for an
entity), POLICY (every tick, world-wide), VALIDATOR/HOOK (per
operation, with `ctx.op`). Everything below applies to all four unless
noted.

## 1. Conventions — the laws that bite

**Exact decimals are strings.** Money balances, holding quantities,
need satisfactions, query results: all `"1.5000"`-style strings, so Lua
carries the exact decimal the engine will spend. Convert for
arithmetic, convert back for intents:

```lua
local wood = std.holding_qty("WOOD")          -- number (std converts)
local q    = tonumber(h.quantity) or 0        -- by hand: tonumber, always
ctx.action.place_order("WOOD", "sell", std.amount_str(2),
                       std.amount_str(1.25), acc)  -- strings back
```

Arithmetic/comparison on an unconverted string is a per-tick script
error (`attempt to perform arithmetic on a string`).

**Keys are strings; facts are tables.** The map has two readable
forms and they are NOT the same shape:

| Field | Shape | Use |
|---|---|---|
| `ctx.entity.place` | key string (`"HEARTH"`) or `nil` | compare: `ctx.entity.place == "HEARTH"` |
| `ctx.place` | facts table (`{key=, name=, kind=, region_id=, description=}`) or `nil` | read about where you stand |
| `ctx.places` | array of facts tables (every place on the map) | browse the map |

`ctx.entity.place.key` is **always nil** — that is a string, and Lua
strings have no `.key`. This exact line killed House Ivar (run 30; §7).
The submit gate refuses it statically now — member access on a known
string path, and reads of members `std`/`ctx.action`/`ctx.query` do not
carry, are REFUSED with the fix in the message. The gate catches what
it can prove; §7 still documents the whole class.

**Nil-ability is documented per field.** The rule of the engine: a
world that does not carry a feature ships its absence as the real
shape, never a lie — worlds without a clock give `ctx.clock = nil`
(and `std.hour()` returns nil, never errors); worlds without a map
give `ctx.place = nil`, `ctx.places = {}`. Guard with `if`, or use the
`std.*` helpers that encode the guard for you.

**Books can be bare.** `best_bid`/`best_ask`/`market_price` return nil
when there is nothing to read; `std.best_ask(sym, fallback)` exists so
you never branch on nil by accident.

**Tables are Lua tables.** Arrays (`holdings`, `processes`, `places`,
`events`, …) work with `ipairs`; maps (`state`, `ctx.entity`) with
`.` and `pairs` where documented.

## 2. `ctx` — the injected context

| Field | Type | Shape / notes |
|---|---|---|
| `ctx.tick` | number | the current tick number |
| `ctx.clock` | table or nil | day/night facts (below); nil in worlds without a clock |
| `ctx.entity` | table | your entity, read-only |
| `ctx.accounts` | array | `{{id=, currency=, balance="10.0000"}, ...}` — balance is a string |
| `ctx.holdings` | array | `{{symbol="WOOD", quantity="2.0000"}, ...}` — quantity is a string |
| `ctx.processes` | array | your RUNNING processes (completed ones leave it) |
| `ctx.parcels` | array | parcels you own, with facilities and deposits |
| `ctx.needs` | array | every active need with satisfaction and kill condition |
| `ctx.place` | table or nil | facts about where you stand (§1) |
| `ctx.places` | array | the whole map, public facts |
| `ctx.unlocks` | array | technology codes you can use (array of strings) |
| `ctx.events` | array | last tick's events for THIS entity (+ what was delivered to it) — §6 |
| `ctx.state` | table | PERSISTS across your script's runs; `local` variables do not |
| `ctx.op` | table | the operation being validated/hooked (VALIDATOR/HOOK only) |
| `ctx.query.*` | functions | §3 |
| `ctx.action.*` | functions | §4 |

`ctx.clock` (worlds that have one):

```lua
{ tick = 42, day = 2, hour = 18, is_day = true, is_night = false,
  daylight_hours = "06:00-19:00" }
```

`ctx.entity`:

```lua
{ id = "uuid", name = "House Ivar", entity_type = "individual",
  age = 42,                      -- ticks since birth, or nil
  is_monetary_authority = false,
  capabilities = {},             -- capability codes, array of strings
  place = "HEARTH" }             -- KEY STRING or nil — §1
```

`ctx.processes[i]`:

```lua
{ id = "uuid", recipe = "GATHER", parcel_id = "uuid" or nil,
  started_tick = 40, completes_tick = 41, is_travel = false }
```

`ctx.parcels[i]`:

```lua
{ id = "uuid", parcel_type = "camp", region_id = "uuid" or nil,
  facilities = {"FIRE"},                  -- facility types on it
  deposits = { IRON = "120.0000" } }      -- map symbol → string qty
```

`ctx.needs[i]`:

```lua
{ code = "FOOD", priority = 1,
  quantity_per_tick = "0.5000",           -- string (§1)
  satisfiers = {"SATIETY"},               -- goods that satisfy it
  satisfaction = "0.5000",                -- string; "0" before first pass
  condition = "STARVATION" }              -- incap condition if unmet, or nil
```

A need is met by CONSUMING its satisfier goods (that is what
`EAT_BERRIES` does: it consumes `BERRIES`, produces `SATIETY`). A
satisfier is itself a holdable good — `WARMTH` is both "the thing the
fire produces" and "what the need consumes", so `std.holding_qty("WARMTH")`
is the warmth banked in your pantry, NOT `ctx.needs` satisfaction.

## 3. `ctx.query.*` — read-only economy queries

All return exact-decimal STRINGS, or nil when there is nothing /
you may not see it (private-holdings worlds scope `holding`/`holders`
to your own entity). Prefer the `std.*` wrappers for the common ones.

| Query | Signature | Returns |
|---|---|---|
| `balance` | `(account_id)` | string or nil |
| `total_supply` | `(currency)` | string |
| `market_price` | `(symbol)` | last print, string or nil |
| `best_bid` | `(symbol)` | best OPEN buy, string or nil |
| `best_ask` | `(symbol)` | best OPEN sell, string or nil |
| `holding` | `(entity_id, symbol)` | string or nil |
| `unreserved` | `(entity_id, symbol)` | held minus process reservations, string or nil |
| `has_unlock` | `(entity_id, code)` | boolean |
| `holders` | `(symbol)` | array of holder rows |
| `age` | `(entity_id)` | number or nil |
| `lifespan` | `(entity_id)` | number or nil |
| `population` | `()` | array of entity rows |
| `parents` / `children` | `(entity_id)` | arrays of ids |
| `route` | `(from_key, to_key, modes?)` | `{hops={{from,to,mode,cost_ticks}…}, total_ticks}` or nil |
| `distance_ticks` | `(from_key, to_key, modes?)` | number or nil |
| `world_setting` | `(key)` | value or nil |
| `fiscal_policy` | `()` | table or nil |
| `constitution` | `()` | table or nil |
| `active_script` | `(lineage_id)` | active version row or nil |
| `script_history` | `(lineage_id)` | array of version rows |
| `proposal` | `(proposal_id)` | row or nil |
| `proposals` | `(status?)` | array of rows |
| `tally` | `(proposal_id)` | tally row or nil |

`modes` is optional: a comma string (`"WALK,RAFT"`) or a list; nil
means every mode the map wires.

## 4. `ctx.action.*` — queue an intent

Intents resolve AFTER all scripts return; the engine validates each
one and records the outcome in next tick's `ctx.events` (status
`"applied"` or `"rejected"` with a `reason` — read them, that is your
debugging loop). Extra arguments are REFUSED at the call site with the
expected arity in the message. Nothing here acts immediately.

**The survival/economy set** (what a BEHAVIOUR script mostly uses):

```lua
ctx.action.start_process(recipe, parcel_id?)   -- begin production;
                                               -- parcel_id for facility-
                                               -- bound recipes (e.g. a fire)
ctx.action.cancel_process(process_id)
ctx.action.travel(to_place_key, modes?)        -- engine routes; each hop
                                               -- is a process that takes
                                               -- ticks. Walking is
                                               -- labor-free but is the
                                               -- tick's act.
ctx.action.place_order(symbol, side, quantity, limit_price, account_id)
ctx.action.cancel_order(order_id)
ctx.action.transfer(from_account_id, to_account_id, amount, reference)
ctx.action.attack(target_id_or_nil)            -- nil = prowl: engine
                                               -- picks the noisiest
                                               -- speaker of the night
ctx.action.say(text)                           -- speech, ≤256 chars, one
                                               -- utterance per entity
                                               -- per tick; rivals hear
                                               -- it next tick
ctx.action.transfer_parcel(parcel_id, to_entity_id)
```

Quantities and prices go as STRINGS (`std.amount_str`), matching §1.
`side` is `"buy"`/`"sell"`.

**The governance set** (capability- or vote-gated; the resolver
enforces the gates — see docs/game.md):

```lua
ctx.action.issue_money(account_id, amount, reference)
ctx.action.retire_money(account_id, amount, reference)
ctx.action.levy(from_account_id, to_account_id, amount, rule_ref)
ctx.action.seize(from_entity_id, spec, rule_ref)
ctx.action.set_fiscal_policy(policy_table)
ctx.action.set_script(script_type, lineage_id, source, bound_entity_id?)
ctx.action.set_validator(lineage_id, source, bound_entity_id?)
ctx.action.set_constitution(params_table)
ctx.action.grant_capability(to_entity_id, capability)
ctx.action.revoke_capability(to_entity_id, capability)
ctx.action.create_proposal(target_id, mutations, weight_model?,
                           threshold?, quorum?, title?, proposal_type?)
ctx.action.vote(proposal_id, choice)
ctx.action.enact(proposal_id)
ctx.action.spawn_entity(parents, opts?)
```

## 5. `std` and `world` libraries

`std` is engine-wide and pinned (its fingerprint rides pack.json). The
injected source is the normative doc; summary:

| Member | Returns |
|---|---|
| `std.holding_qty(symbol)` | number (0 when absent) |
| `std.at(key)` | boolean — true iff you stand on that place key (nil-safe; §1's trap, guarded for you) |
| `std.need_level(code)` | the need's satisfaction as a NUMBER, or nil when no such need |
| `std.balance(currency?)` | first account balance in that currency as a number (0 when none) |
| `std.unreserved(symbol)` | number or nil (spendable side of a holding) |
| `std.market_price(symbol, fallback?)` | number or fallback |
| `std.best_bid(symbol, fallback?)` / `std.best_ask(symbol, fallback?)` | number or fallback (never nil with a fallback) |
| `std.has_unlock(code)` | boolean |
| `std.need_by_code(code)` | the need row (satisfaction is a string) or nil |
| `std.running_recipe(code)` | boolean — guards "one per tick" |
| `std.facility_parcel(facility_type)` | parcel id or nil |
| `std.deposit_parcel(symbol)` | parcel id or nil |
| `std.amount_str(x)` | exact-decimal string |
| `std.hour()` / `std.day()` / `std.is_day()` / `std.is_night()` | number/number/boolean/boolean, or nil without a clock — never errors |

`world` is per-world (`world_lib.lua` in the world's source; stone_age
ships): `world.settle_last_orders()` (cancel-and-report order fills),
`world.places()`, `world.place(key?)` (bare call = where you stand,
facts table), `world.route(from, to, modes?)`,
`world.distance_ticks(from, to, modes?)`. The content pack's `pack`
namespace is world opinion (concession rules, ask schedules) — read
its source via `get_script_libraries`.

## 6. `ctx.events` — the feedback loop

What last tick produced FOR YOUR ENTITY (plus speech delivered to
you). Common types and their payload keys:

| Type | Keys | Notes |
|---|---|---|
| `start_process` | `params.recipe`, `status`, `reason` | status `rejected` carries why (no labor, night, wrong place…) |
| `process_completed` | `recipe`, outputs | instant recipes (meals) complete before your next run |
| `process_failed` | `recipe`, `reason` | |
| `need_satisfied` | `need`, `consumed`, `required`, `satisfaction` | satisfaction `"1.0000"` = fully met |
| `place_order` / `order_cancelled` | `params`, `order_id`, `status`, `reason` | |
| `trade` | `market`, `side`, `price`, `quantity`, `cost`, `order_id`, `trade_id` | ONE event per side — a match prints twice |
| `travel` | `params.to`, `status`, `reason` | `reason: "already at Berry thicket"` = you asked to go where you are |
| `travel_departed` / `travel_arrived` | route facts | |
| `combat` | `entity_id` (attacker), `target_id`, `attack`, `defense`, `hit`, `damage`, `target_hits`, `killed`, `loot` | |
| `entity_incapacitated` | `condition`, `quantity`, `threshold`, estate settlement | death; the estate record nests under `death` in the world feed |
| `decay` | goods lost | per-tick rotting |
| `script_error` / `script_reverted` / `compute_budget_exceeded` | | your script's own failures |
| `say` | speaker, `text` | speech delivered to you |

**Rejected intents are the debugging loop.** A script that reads place
wrong will spam `travel` rejections with a reason that says where you
actually are — reading those reasons is how you catch a wrong-shape
read (§7, first fault).

## 7. Fault catalogue — real lines from the runs

Each entry: the line that failed, what actually happened, the fix.
Kept verbatim so postmortems can cite this section.

1. **The two-places trap** (run 30, House Ivar, froze d3h13):
   `local place = ctx.entity.place and ctx.entity.place.key or nil` —
   `ctx.entity.place` IS the key string; `.key` on a string is nil,
   silently, every tick. Every place-gated branch collapsed to
   fallbacks (16 consecutive `travel` rejections it never read) while
   the entity walked nowhere and froze.
   **Fix:** `local place = ctx.entity.place` and compare `==`. The
   facts table is `ctx.place` (§1). Read `travel`-rejection reasons in
   `ctx.events` — "already at X" while your script thinks otherwise is
   exactly this bug. The submit gate now refuses this class verbatim
   (static shape lint, §1) — a fix-in-hand finding, not a silent nil.

2. **String arithmetic** (runs 26–27, multiple): `if h.quantity > 1`
   where `h.quantity` is `"2.0000"` — comparison/arithmetic on strings
   is a per-tick script error.
   **Fix:** `std.holding_qty(symbol)` or `tonumber(h.quantity) or 0`.

3. **Extra argument slips** (run 15's origin of the arity guard; run
   30 Lagertha r1): `place_order(sym, side, qty, price, acc, 30)` — a
   priority argument that does not exist; Lua would silently discard
   it, so the guard now refuses at the call site with the arity in the
   message.
   **Fix:** the arities in §4 are exhaustive.

4. **Nil clock at smoke time** (run 29, both seats' r2+ rewrites):
   scripts reading `ctx.clock` crashed the gate's synthetic ctx, were
   accepted with a warning, then crashed every live tick. The gate ctx
   now carries the truthful vocabulary (run 30, PR #162) and the
   harness rolls a crashing submission back.
   **Fix class:** none needed anymore — but prefer `std.hour()` /
   `std.is_night()`, which return nil instead of erroring.

5. **Bare book, no fallback** (run 24-era trading posts):
   `local ask = std.best_ask("JERKY")` then arithmetic on nil.
   **Fix:** `std.best_ask("JERKY", 0)` — the second argument is the
   fallback.

6. **Need satisfaction ≠ holding** (run 30, multiple drafts):
   confusing `std.need_by_code("WARMTH").satisfaction` with
   `std.holding_qty("WARMTH")`. The need consumes the good; the
   holding is the pantry. Both exist; they are different numbers.

7. **Prose or fences in the reply** (runs 2–3, every model at least
   once): the answer must be the Lua source alone; the lint refuses
   with a hint, costing one round-trip.

8. **Redeclaring globals**: `count = count + 1` at top level is a
   write to an undeclared global — refused. `local` always, or
   `ctx.state.count = (ctx.state.count or 0) + 1` for persistence.
