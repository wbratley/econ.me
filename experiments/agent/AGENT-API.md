# The Agent-Facing API — everything a seat can see and do

The complete reference for how agent dynasties interact with the game:
the wire, every tool they can call, every prompt they receive (section by
section), where every artifact lands on disk per run, and how to query a
live world. Written for browsing — every section points at real files in
a real run dir (`~/econ-runs/stone-run52/...` in the examples).

```
┌─────────────┐  MCP JSON-RPC (HTTP POST /mcp, Bearer token per dynasty)
│  seat loop  │ ────────────────────────────►  FastAPI game server
│ (loop.py)   │ ◄──────────────────────────── (econ/api/mcp_tools.py →
└──────┬──────┘   tool results (JSON)          the same engine the REST
       │                                          API serves humans)
       │ chat completion (OpenAI-compatible)
       ▼
   LLM provider (deepseek / nim / local llama)
```

Three things to internalize:

1. **The LLM never talks to the game.** The seat harness (`loop.py`) is
   the only MCP client. It observes via tools, builds a prompt, gets Lua
   back from the model, lints/submits it via tools. The model's ONLY
   channel into the world is the reply text it puts in the chat.
2. **Tool calls are choreographed, not model-chosen.** The seat calls a
   fixed sequence of tools every round (§4) — the model can't decide to
   call `leaderboard` mid-thought; everything it learns arrives in the
   prompt.
3. **One door per audience.** MCP (`/mcp`) is the agent door; REST
   (`/api/...`) is the human/browser door; both run the same handlers
   and the same engine. A dynasty token reaches only public facts + its
   own private state — never admin, never another house's affairs.

---

## 1. The wire

MCP over plain HTTP — JSON-RPC 2.0 `tools/call`, one round trip per call
(`experiments/agent/nim_run.py::http_transport`):

```python
httpx.Client(base_url=base, timeout=240.0,
             headers={"Authorization": f"Bearer {token}"})
POST /mcp {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "round_state", "arguments": {}}}
```

The result body is `{"content": [{"type": "text", "text": "<json>"}]}`;
`isError: true` carries a refusal. `tools/list` returns the whole
registry (§2) with schemas. Tokens are JWTs minted at launch
(`econ/api/auth.py::create_token`, dev `SECRET_KEY` default), one per
dynasty + one admin for the runner.

## 2. The tool registry (19 tools)

`econ/api/mcp_tools.py` — `TOOLS`, rendered below. Args marked ●required.
"Cut" = what the privacy doctrine (§13) lets through.

### Identity & own state
| Tool | Args | Returns | Cut |
|---|---|---|---|
| `join` | — | founded entity (INDIVIDUAL) w/ endowment | creates you |
| `my_entities` | — | every entity you own, status, age | own |
| `entity_state` | `entity_id`● | accounts, holdings, needs, processes, parcels, place, unlocks, active behaviour id+state, **condition telemetry** | own |
| `entity_events` | `entity_id`●, `last_ticks` (1–50, d=3) | per-entity event digest — same filtered feed the behaviour script gets in `ctx.events` | own |
| `entity_activity` | `entity_id`●, `last_ticks`, `witnessed` | audit trail as prose: trades, orders, processes, need outcomes, crashes, **rejected intents with reasons**; `witnessed=true` = what others said that you heard | own |

### Behaviour authoring (the autonomy path)
| Tool | Args | Returns |
|---|---|---|
| `get_behaviour` | `entity_id`● | full Lua source + state of active behaviour |
| `get_script_libraries` | — | the three injected tiers verbatim: `std` / `world` / `pack` |
| `set_behaviour` | `entity_id`●, `source`● (Lua), `description` | submit gate verdict: accept or refusal (lint/strict-globals/smoke), `lint_warnings` |
| `dry_run_behaviour` | `entity_id`●, `source`● | the SAME gate without installing: per smoke-state verdicts (day/night × hearth/thicket × stocked/empty) |
| `perform_action` | `entity_id`●, `action`● (`say`), `params`● (`text`) | direct controller voice — one utterance, queued under the world clock |

### Round clock & consent
| Tool | Args | Returns |
|---|---|---|
| `round_state` | — | open round, ticks run, K (ticks/round), readiness gate (mode + who readied) |
| `set_ready` | `ready` (d=true) | vote to close the round; final ready resolves it; `false` withdraws |
| `epoch_state` | — | victory condition, winner, whether YOU were eliminated |
| `governance_current` | — | window rounds, next window, docketed proposals + tallies |

### Public world (same facts for every house)
| Tool | Args | Returns |
|---|---|---|
| `leaderboard` | — | rows: entity counts, oldest lineage, tech unlocks, epoch wins, status, money |
| `market_prices` | — | last-trade price per active market |
| `world_catalog` | — | every good (+derived effect line), recipe (inputs→outputs, odds, gates), tech tree, needs (satisfiers, hourly draws), threats, markets |
| `world_map` | — | places, roads (mode, tick cost), every entity's location |
| `world_activity` | `last_ticks` | unattributed public facts as prose: auction summaries, decay, auto-issue |

## 3. What the harness feeds the model (the observation)

Every round the seat builds ONE digest (`loop.py::observe`, §5 of the
prompt) — this is *everything the model learns about the world*:

```json
{ "round":   <round_state>,
  "entity":  <entity_state incl. condition telemetry>,
  "events":  <entity_events, last_ticks window>,
  "prices":  <market_prices>,
  "leaderboard": <rows with money POPPED — holdings privacy>,
  "epoch":   <epoch_state>,
  "seen":    [{"tick", "who", "what"}]  // witnessed speech, names resolved
}
```

Plus four feedback channels folded into the prompt as FINDINGS:
lint refusals & warnings from the submit gate; **per-tick script
crashes** (the loudest finding); **rejected intents with the engine's
reasons**; and **condition telemetry** (run 52's lever — "condition
HUNGER at 10.1418 of kill line 15.0000…" when a condition rides toward
its kill line).

## 4. The per-round choreography (what runs, in order)

The seat's calls are deterministic — this IS the tool-call log for every
round; only the *arguments* (entity ids, sources) vary:

1. `my_entities` (once at boot; `join` if empty)
2. `leaderboard` → money popped from rows
3. `entity_activity` (`last_ticks: 50, witnessed: true`) → the `seen` digest
4. `round_state`, `entity_state`, `entity_events`, `market_prices`, `epoch_state`
5. `world_catalog` — **once per run**, cached (goods are static)
6. `get_script_libraries`, `get_behaviour`
7. ⟵ model call(s) — up to `max_attempts` (3), each attempt re-prompted with accumulated findings
8. `set_behaviour` — on accept, done; on lint refusal → finding → back to 7. (KEEP submits nothing; SAY replies add `perform_action(say)` after the decision)
9. `set_ready` — consent, never blocking on the world (clock mode)
10. diary model call (separate short prompt, `--diary` runs)

So a full author round = 8–10 tool calls + 1+ model calls; a KEEP round
= same observes, no submit.

## 5. The prompts, section by section

Three prompt shapes, all persisted per round (§6). System prompt
(`loop.py::system_prompt`, ~50–70KB, grows with the world lib):

| Section | Contents |
|---|---|
| Identity | "mind of entity X… whole agency is one Lua BEHAVIOUR script… Survival first" |
| `ctx` vocabulary | tick/clock/entity/accounts/holdings/processes/parcels/needs/place/places/unlocks/**events**/**state** (persists across runs) |
| `ctx.action.*` | start_process, cancel_process, travel, place_order, cancel_order, transfer, transfer_parcel, attack, say — with arity/validation rules |
| `ctx.query.*` | balance, market_price, best_bid/ask, holding, holders, route, fiscal_policy, constitution, proposals… — "strings or nil" |
| Shape laws | place keys are strings, decimal strings everywhere, bare books return nil, rejected intents land in `ctx.events` |
| Reply grammar | edit-mode: edit blocks (preferred) / full rewrite / KEEP — plus the optional opening `SAY:` line |
| `std.*` | engine stdlib **source, verbatim** |
| `world.*` | this world's library **source, verbatim** |
| `pack.*` | content pack's play opinions **verbatim** |
| World catalog | generated from installed content: every number (costs, odds, durations, gates, **kill lines**) |
| World notes | the pack's authored manual (strategy the numbers can't spell) |

Author user prompt (`user_prompt`) — small, the whole per-round delta:

```
OBSERVATION (exactly what your entity can see): <the §3 JSON>
CURRENT BEHAVIOUR (runs every tick): <full Lua source>
FINDINGS since your last submission (address these): <refusals/crashes/rejections/telemetry>
Write the next behaviour: …
```

Diary prompt (`diary_prompt`) — after the decision stands: "strategist"
system + **the complete verbatim round record**: system prompt, every
prompt→reply pair (think-peeled), every platform response between
attempts. Replies are 1–3 plain sentences.

## 6. Where everything lands (per-run artifact map)

Run dir = `~/econ-runs/<run>/`. Run 52 example: 28 prompt files, 3
journals, 11 round snapshots.

| File | Contents | Answer the question… |
|---|---|---|
| `seat-<house>.round-N.author.prompt.md` | header (`# round: N`) + exact system + user prompt as sent | "what did Lagertha see in round 6?" |
| `seat-<house>.round-N.diary.prompt.md` | the diary call, full transcript included | "what was she told after?" |
| `calls.log` | JSONL per model attempt: model, attempt, ok, elapsed_s, finish_reason, content/reasoning chars & deltas, max_tokens, full token usage, seat, round | "how long did the ladder think?" |
| `journal-<house>.jsonl` | per round: action (keep/edit/rewrite/extinct/missed_window), accepted, kept_old, refusal, warnings, source_sha, reply_head, **thoughts (the diary)**, say+status | "why did she keep?" |
| `round-NN.json` | world snapshot: ticks, events_by_type, per-dynasty view (behaviour sha + entry), leaderboard | "what did the world do?" |
| `dashboard.html` / `index.html` | the browsable standings/round table/sha trails | "what happened at a glance?" |
| `world.db` | the engine's own DB (`ticks(number, events JSON)`, entities, holdings) | "ground truth, tick by tick" |
| `run.log` | runner stdout (round resolutions, extinctions) | "when did rounds close?" |

## 7. Querying

**Offline (finished runs)** — the prompts and journals ARE the record:

```bash
# every prompt a seat got, in order
ls ~/econ-runs/stone-run52/seat-house-lagertha.round-*.author.prompt.md
# re-fire any prompt at the provider, capture the full reasoning stream
.venv/bin/python -m experiments.agent.replay_prompt \
  --prompt ~/econ-runs/stone-run52/seat-house-lagertha.round-6.author.prompt.md
# ground truth from the world db
sqlite3 ~/econ-runs/stone-run52/world.db "select number, events from ticks where number between 200 and 215"
```

**Live (server up)** — mint a token with the same SECRET_KEY the server
runs under (dev default), then:

```bash
TOK=$(python -c "from econ.api.auth import create_token; print(create_token('u-admin','a@run',True))")
curl -s localhost:8925/mcp -H "Authorization: Bearer $TOK" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq
# swap method/params for any tools/call in §2; admin REST also open:
curl -s localhost:8925/api/rounds -H "Authorization: Bearer $TK" | jq
```

Browser surfaces while a run is live: `--serve` port (8129) serves the
dashboard dir; `/rounds/events` is the SSE stream of readiness changes
and round closures.

## 8. The one gap

Individual MCP tool calls are not yet written to a per-call log (the
choreography in §4 plus the persisted prompts reconstructs them); model
calls are fully logged (`calls.log`). A per-seat `mcp-calls.jsonl`
(method, args, elapsed, result size) would close it — small PR, sits in
`http_transport`.
