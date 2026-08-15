"""Run the agent loop against a live server.

    # offline, scripted model (no API key needed — the reproducible demo):
    .venv/bin/python -m experiments.agent.run \
        --base http://127.0.0.1:8901 --token <player-jwt> \
        --scripted /tmp/econ-demo/agent-responses.jsonl \
        --admin-token <admin-jwt> --advance 3 --journal /tmp/agent.jsonl

    # real model (one env var away):
    ANTHROPIC_API_KEY=... ECON_AGENT_MODEL=claude-sonnet-4-5 \
        .venv/bin/python -m experiments.agent.run --base ... --token ... --cycles 5

Doctrine (docs/actors.md fork A): the agent plays only through MCP. Round
advancement is the operator step — this runner does it with a *separate*
admin client between cycles, never inside the loop, so the separation an
agent must respect is visible in the code that demonstrates it.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from .llm import Model, ScriptedModel, model_from_env
from .loop import AgentLoop, McpClient


def http_transport(base: str, token: str):
    client = httpx.Client(base_url=base, timeout=120.0,
                          headers={"Authorization": f"Bearer {token}"})

    def transport(method: str, params: dict) -> dict:
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                      "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"MCP {method}: {body['error']}")
        return body["result"]

    return transport


def admin_advancer(base: str, admin_token: str):
    """The operator step: resolve the open round (POST /admin/rounds/advance)
    so the next cycle observes a world that actually moved."""
    client = httpx.Client(base_url=base, timeout=180.0,
                          headers={"Authorization": f"Bearer {admin_token}"})

    def advance(cycle_no: int) -> dict:
        r = client.post("/admin/rounds/advance")
        if r.status_code != 201:
            sys.exit(f"!! round advance failed after cycle {cycle_no}: "
                     f"{r.status_code} {r.text[:200]}")
        return r.json()

    return advance


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, help="server base URL")
    ap.add_argument("--token", required=True, help="the player's bearer token")
    ap.add_argument("--entity-id", help="play this entity (default: first owned, else join)")
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="lint-refusal retries per cycle before keeping the old behaviour")
    ap.add_argument("--advance", type=int, default=0, metavar="N",
                    help="advance a round after each of the first N cycles "
                         "(operator step; needs --admin-token)")
    ap.add_argument("--admin-token", help="admin bearer for --advance")
    ap.add_argument("--scripted", help="JSONL of canned responses (offline model)")
    ap.add_argument("--journal", default="/tmp/agent-journal.jsonl")
    args = ap.parse_args(argv)

    model: Model
    if args.scripted:
        model = ScriptedModel.from_file(args.scripted)
    else:
        model = model_from_env()  # env-configured (scripted file / API keys)

    between = None
    if args.advance:
        if not args.admin_token:
            ap.error("--advance needs --admin-token")
        advancer = admin_advancer(args.base, args.admin_token)
        between = lambda i: print(f"    round advanced: "
                                  f"ticks {advancer(i).get('ticks')}")  # noqa: E731

    loop = AgentLoop(McpClient(http_transport(args.base, args.token)),
                     model, entity_id=args.entity_id,
                     max_attempts=args.max_attempts, journal_path=args.journal)
    eid = loop.ensure_entity()
    print(f"agent {model.name} playing entity {eid}: {args.cycles} cycle(s)")

    for entry in loop.run(args.cycles, between=between):
        mark = "accepted" if entry["accepted"] else "KEPT OLD (refused)"
        print(f"  cycle {entry['round']}: {mark} after {entry['attempts']} attempt(s)"
              + (f" — {entry['refusal'][:100]}" if entry["refusal"] else ""))
        if entry["warnings"]:
            print(f"    warnings: {entry['warnings']}")
    print(f"journal: {args.journal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
