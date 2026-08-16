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
   events only), `market_prices`, `leaderboard`, `epoch_state`
2. **think** — model completes: system = identity + tier sources; user =
   observation digest + current behaviour + findings since last time
3. **submit** — `set_behaviour`; `isError` (lint refusal) appends the
   finding to the feedback and re-thinks (≤ `max_attempts`, default 3);
   exhaustion keeps the old behaviour — never destructive
4. **journal** — one JSONL line per cycle (attempts, accepted,
   warnings, source sha, model)

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
