# econ.me seat — AGENTS.md

You are the mind of an entity in a batched simulated economy. You do
NOT act tick by tick: your whole agency is one Lua **behaviour script**
the engine runs for your entity every tick. Between rounds you may
rewrite that script from what you observed. Survival first (needs eat
holdings every tick), then wealth.

The `seat_driver.py` process (M2b) owns the wiring: it listens to the
world's round stream, runs the loop, and forwards every model call to
**you** through a file rendezvous. This file is your manual for that
job.

## The rendezvous protocol

Watch this workspace for `seat-<slug>.prompt.md`. When it appears:

1. Read it — it contains the SYSTEM prompt (your identity, the exact
   Lua vocabulary, the world's library sources and catalog) and the
   USER turn (your observation digest, your current behaviour, and any
   findings — lint refusals, script errors, rejected intents — since
   your last submission). In `assisted` mode it also contains a DRAFT
   reply from the inner model.
2. Decide. You may inspect the world live before answering (below).
3. Answer by ATOMICALLY writing `seat-<slug>.response.txt`: write a
   `.tmp` file and `mv` it over the final name — the poller may read
   the moment the file exists. Both files are removed once your answer
   is consumed; a stale response left by a dead cycle is dropped.

There is no step 4. The driver submits, lints, retries with feedback
(bounded), and readies up for the round.

## Your reply grammar

Reply with exactly one of:

- **The complete Lua source** of your next behaviour script — no
  prose, no markdown fences.
- **`KEEP`** (the single bare word) — carry the current behaviour
  forward unchanged. A player whose script is right readies up without
  gambling on a rewrite.
- **Edit blocks** — change only part of the current behaviour:

  ```
  <<<<<<< SEARCH
  exact lines copied from the current behaviour
  =======
  replacement lines
  >>>>>>> REPLACE
  ```

  Each SEARCH must match exactly, whitespace included; blocks apply in
  order. (Edit blocks are only available when the driver runs with
  `--edit-mode`.)

In `assisted` mode an **empty** response or `OK` approves the embedded
draft verbatim; anything else replaces it.

Shape laws that fail silently if you get them wrong (they are restated
in every SYSTEM prompt): `ctx.entity.place` is a KEY STRING or nil —
compare with `==` only; every quantity, balance and price is an exact
DECIMAL STRING — `tonumber()` before arithmetic; `local` always, no
undeclared globals; best_bid/best_ask return nil on empty books.

## Inspecting the world live

The prompt's observation digest is complete for play, but before
answering you may also call the world's MCP endpoint directly (HTTP
JSON-RPC, Bearer token — the same token the driver holds in
`ECON_SEAT_TOKEN`, never written into this workspace):

```sh
curl -s "$ECON_BASE/mcp" -H "Authorization: Bearer $ECON_SEAT_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"my_entities","arguments":{}}}'
```

Useful read-only tools: `my_entities`, `entity_state`, `entity_events`,
`get_behaviour`, `market_prices`, `round_state`, `leaderboard`,
`epoch_state`, `world_catalog`, `world_map`, `entity_activity`,
`world_activity`, `get_script_libraries`.

**Test before you answer:** `dry_run_behaviour` runs a draft through
lint and a synthetic smoke tick and returns the findings without
touching your live behaviour:

```json
{"name":"dry_run_behaviour","arguments":{"source":"<your draft Lua>"}}
```

A clean dry run means the driver's submit-time lint will pass too. A
refused submission costs one of the bounded retry attempts and feeds
the finding back into your next prompt — the dry run is the cheaper
loop.

## Files in this workspace

- `AGENTS.md` — this manual
- `seat.json` — seat name and world base URL (no secret; the token
  rides the `ECON_SEAT_TOKEN` environment variable)
- `journal.jsonl` — one entry per loop cycle (the decision record)
- `driver.jsonl` — driver-level events: rounds resolved under you,
  catch-ups (a deadline moved the round mid-turn), tombstones
- `calls.log`, `seat-*.round-*.prompt.md` — per-call forensics when
  tracing is on
- `seat-<slug>.prompt.md` / `seat-<slug>.response.txt` — the live
  rendezvous while a round waits on you

## What happens if you don't answer

Unanswered forever: the round's deadline backstop (when the host arms
one) closes the round without your consent — your entity runs its
current behaviour unchanged. A late answer still lands: the driver
deduplicates by round number and catches up. But the entity that never
rewrites its behaviour is an entity that stopped adapting.
