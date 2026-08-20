# The agent loop — a real client that plays (experiments/agent)

The proving experiment for the whole player surface: an LLM-driven agent
that plays the game as a player (docs/actors.md fork A — the agent owns
its entity's BEHAVIOUR and rewrites it between rounds). Everything it
does goes through MCP, exactly the bytes a remote agent client sends;
its observation set is the §13 parity set — what its own script sees,
plus public facts. No operator surfaces inside the loop: rounds are
advanced by the driver (`run.py` with a separate admin client), a live
operator, or — in a readiness-gated world (game.md §9.1) — by the
players themselves: `AgentLoop.set_ready` / `run.py --ready` casts the
loop's clock vote over MCP, and the final ready closes the round.

It is also the payoff of the scripting arc (docs/scripting.md):

- `get_script_libraries` puts the exact tier vocabulary (`std`/`world`/
  `pack` sources, verbatim) in the system prompt — a model authoring
  from scratch reads the world's API instead of hallucinating it.
- Phase 3's submit-time lint makes hallucination cheap: a nil-call trap
  is a *refused submission* whose finding is fed straight back — the fix
  costs one round-trip and the entity keeps its working behaviour
  throughout, instead of a per-tick zombie (the first live demo's
  founder).
- Warnings and per-tick `script_error` events ride into the next cycle's
  prompt: the model sees the consequences of its own last rewrite.

## Shape

| File | What it is |
|---|---|
| `llm.py` | The model seam: `ScriptedModel` (offline FIFO, records every prompt), `AnthropicModel` / `OpenAIModel` (plain httpx, no SDK), `model_from_env()` |
| `loop.py` | `McpClient` (transport-free JSON-RPC) + `AgentLoop`: observe → think → submit, lint-refusal retry (bounded), JSONL journal |
| `run.py` | CLI against a live server; advances rounds between cycles with a *separate* admin client (only if asked) |
| `test_agent_loop.py` | The loop over the real MCP surface with a scripted model |

## One cycle

1. **observe** — `round_state`, `entity_state`, `entity_events` (own
   events only), `market_prices`, `leaderboard` (standings without the
   money column — rival privacy; own cash arrives via `entity_state`),
   `epoch_state`, and, when the pack ships one, its manual in the system
   prompt (see experiments/world/README.md, "three pack fixes")
2. **think** — model completes: system = identity + tier sources; user =
   observation digest + current behaviour + findings since last time.
   The reply goes through a reasoning/code separator (`extract_script`):
   `<think>` blocks are peeled, then the first fenced block, the last
   fenced block, and the longest Lua-looking line island compete — the
   first candidate that *compiles* wins (stone-run5: 15 of Nemotron's
   16 refusal classes were prose/code separation failures). Nothing
   compiles → the raw text submits unchanged and lint judges.
3. **submit** — `set_behaviour`; `isError` (lint refusal) appends the
   finding to the feedback and re-thinks (≤ `max_attempts`, default 3);
   exhaustion keeps the old behaviour — never destructive
4. **journal** — one JSONL line per cycle (attempts, accepted,
   warnings, source sha, model, and — on a refused round — the head of
   the last raw reply: failed attempts stopped evaporating; on an
   accepted round whose extraction had a choice to make, a 200-char
   reply head plus the extractor's decision — candidate count, winner
   index, per-candidate shas, quotes bypassed)

## Running

```bash
# offline, scripted (no API key; reproducible):
.venv/bin/python -m experiments.agent.run \
    --base http://127.0.0.1:8901 --token $PLAYER_JWT \
    --scripted agent-responses.jsonl \
    --admin-token $ADMIN_JWT --advance 2

# real model (one env var away):
ANTHROPIC_API_KEY=... ECON_AGENT_MODEL=claude-sonnet-4-5 \
  .venv/bin/python -m experiments.agent.run \
    --base http://127.0.0.1:8901 --token $PLAYER_JWT --cycles 5
```

Scripted responses are a JSONL file of strings (one model reply per
line) — the natural place to script one deliberately bad first draft
(the zombie trap) and watch the lint feedback fix it in a round-trip.
Model selection: `ECON_AGENT_SCRIPTED_FILE` > `ANTHROPIC_API_KEY` >
`OPENAI_API_KEY`; `ECON_AGENT_MODEL` overrides the provider slug.

## Doctrine notes

- The loop never advances rounds itself — batched economy, agent acts
  between resolution; the operator step stays visible as such in the
  driver. The one exception is by design: `set_ready` (§9.1) is not an
  operator surface but a *player* one — closing the round by consent is
  in-band agency, and in a readiness-gated world the last agent's ready
  fires the resolution with no admin anywhere.
- The prompts are assertions, not vibes: `ScriptedModel.calls` records
  exactly what a provider would receive, and the tests pin the parity
  set (own entity, public facts, no other dynasty) and the tier
  vocabulary's presence.
- Determinism of the *harness*: a scripted run is byte-stable (same
  observations → same prompts → same journal); a real-model run journals
  source shas so successive cycles are diffable.

## The dynasty run: 3 models, N rounds, one dashboard (NIM)

`multi.py` + `nim_run.py` + `dashboard.py` lift the loop to a
self-pacing multi-agent world: dynasties over the content pack's
substrate (symmetric seats — same endowment, same parcel bundle, same
starter; nothing primed but the model), readiness gate ON —
every round each dynasty cycles then readies, the final ready resolves
the round, a snapshot lands on disk. No admin paces anything.

```bash
# 0) the key (never in the repo): one of
export NVIDIA_API_KEY=nvapi-...            # or NIM_API_KEY, or
echo nvapi-... > ~/.nim_api_key            # first line of this file

# 1) pick three usable models from your catalog:
.venv/bin/python -m experiments.agent.nim_pick --list
.venv/bin/python -m experiments.agent.nim_pick --probe \
    meta/llama-3.3-70b-instruct qwen/qwen2.5-7b-instruct \
    mistralai/mistral-small-24b-instruct-2501

# 2) the run (spawns its own server on a scratch port, tears it down):
.venv/bin/python -m experiments.agent.nim_run \
    --models <slug-1> <slug-2> <slug-3> \
    --names "House Llama" "House Qwen" "House Mistral" \
    --rounds 10 --out /tmp/nim-run

# 3) watch it live — the out dir is served while the run is on
#    (--serve PORT, default 8090; 0 disables):
xdg-open http://127.0.0.1:8090/
```

The live page is the dashboard itself: rewritten atomically after every
resolved round (write-then-rename, so a browser never reads a torn
file), wearing a `● LIVE · round N of K · elapsed` header and a 10s
self-refresh that drops off at the finish. From second zero the URL
answers a "warming up" placeholder until round 1 resolves. The served
root also hands out `round-XX.json` and the journals — the whole out
dir is plain static files, so nginx does the same job with
`location /run/ { alias /tmp/nim-run/; index dashboard.html; }`; the
built-in server exists because watching a run shouldn't need root to
install one.

The dashboard is one self-contained HTML file (inline SVG, no CDN):
final standings (money / assets-at-last-price / wealth / rewrites /
kept-old rounds), wealth-money-prices-FOOD charts over rounds, a
round-by-round activity table (each house's attempts and refusals, the
round's event mix), and per-house strategy panels — the full current
behaviour source with the sha trail of every rewrite, so "what is House
Qwen doing?" is a scroll. Each panel also carries the house's
**strategy diary**: one short extra model call per house per round
(default ON for NIM runs, `--no-diary` to skip), made AFTER the
decision stands and carrying the complete round record — the rules
played under, every prompt with its findings, the model's own replies
verbatim (the code itself), and every platform response in between —
so the entry is grounded in what the model actually did, not a
hallucination of it; consistent across reasoning and non-reasoning
models alike. Everything in it is the §13 parity view: the
data the dynasties themselves can see.

Offline dress rehearsal (no key, canned responses, same pipe):
`--scripted a.jsonl b.jsonl c.jsonl --rounds 3`. A dynasty whose model
hard-fails keeps its behaviour, journals the failure, and STILL readies
— one dead model never stops the world.

Cost shape: one model call per dynasty per round (plus one per lint
refusal, bounded by `--max-attempts`); 10 rounds ≈ 30-45 calls.
`ECON_AGENT_NIM_BASE` points `NimModel` at a self-hosted NIM container
(same OpenAI-compatible protocol).

Rate shape: every NIM client in the process shares one sliding-window
budget — `ECON_AGENT_NIM_RPM` calls/minute, default 36, deliberately
under the hosted tier's 40 — so three dynasties bursting lint retries
still can't trip the meter (the 429 backoff stays as suspenders).
