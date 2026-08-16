"""The multi-agent run: N dynasties, M rounds, snapshots every round.

The NIM run's mechanics, separated from the CLI the way the loop is
separated from run.py: everything here works over *transports* (the same
`(method, params) -> result` adapter `McpClient` takes), so tests drive a
whole three-dynasty world through the FastAPI TestClient with
ScriptedModels and no network, while the live runner injects httpx
transports pointed at a spawned server. Same doctrine, one level up.

The world is the content pack's substrate (experiments/world/scenario.py:
goods, tech, recipes, needs, markets, the tiered libs) with one twist —
the seats are SYMMETRIC: every dynasty gets scenario.make_house's
identical bundle (same money, FARM + FORGE + ORE seam, both unlocks)
and the plain survival starter, which runs only until its first cycle
replaces it. From then on every mind in the world is a model rewriting
Lua between rounds; the readiness gate (§9.1) paces it: each round,
every dynasty cycles then readies, and the final ready resolves the
round in-request. No admin in the pacing loop — the operator built the
world, then stepped back.

Snapshots are taken from each dynasty's OWN MCP surface (the §13 parity
set plus leaderboard/prices): the dashboard's data is exactly what the
players could see, never an omniscient dump.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .loop import AgentLoop, McpClient, McpError

# ---------------------------------------------------------------------------
# The dynasties and their world
# ---------------------------------------------------------------------------


@dataclass
class Dynasty:
    """One player seat: a user, a house name, and the model that will
    mind it. `entity_id` fills in at world build."""
    user_id: str
    name: str
    model_name: str
    token: str
    entity_id: str | None = None


def build_agent_world(session, dynasties: list[Dynasty]) -> None:
    """Content-pack substrate + symmetric, agent-owned seats.

    `scenario.create_content` gives goods/tech/recipes/needs/markets and
    installs the tiered libs through the gate (with manifest pinning).
    Then each dynasty gets `scenario.make_house`'s identical endowment —
    no role priming, no seat-specific starter — and the readiness gate
    is switched to `readiness` mode: from here the world paces itself.
    """
    from econengine.models import Script, ScriptType, WorldSetting

    from experiments.world import scenario

    if not dynasties:
        raise ValueError("need at least one dynasty")

    scenario.create_content(session)

    for d in dynasties:
        entity = scenario.make_house(session, d.name)
        entity.owner_id = d.user_id          # the join-tool pattern: the
        d.entity_id = entity.id               # seat becomes a player's own
        session.add(Script(
            name=f"house-behaviour-{entity.id}",
            script_type=ScriptType.BEHAVIOUR,
            source=scenario._read_lua("starter.lua"),
            entity_id=entity.id,
            timeout_ms=200,
            state={},
        ))

    # The players' clock (game.md §9.1): rounds close when every dynasty
    # consents. Operator fiat at world creation — mode is data, not code.
    session.add(WorldSetting(key="round.gate", value={"mode": "readiness"}))
    session.commit()


# ---------------------------------------------------------------------------
# The run: cycle -> ready, per dynasty, per round
# ---------------------------------------------------------------------------

@dataclass
class RoundSnapshot:
    """Everything the dashboard eats, for one resolved round."""
    round: int
    ticks: list[int]
    resolved: dict
    market: list[dict]
    dynasties: dict[str, dict]            # house name -> view (below)
    events_by_type: dict[str, int] = field(default_factory=dict)
    taken_at: str = ""

    def to_json(self) -> dict:
        return {
            "round": self.round, "ticks": self.ticks,
            "resolved": self.resolved, "market": self.market,
            "events_by_type": self.events_by_type,
            "taken_at": self.taken_at,
            "dynasties": self.dynasties,
        }


def _dynasty_view(mcp: McpClient, d: Dynasty, entry: dict | None) -> dict:
    """One dynasty's slice of the round, from its own MCP surface."""
    eid = d.entity_id
    state = mcp.call("entity_state", {"entity_id": eid})
    try:
        behaviour = mcp.call("get_behaviour", {"entity_id": eid})
    except McpError:
        behaviour = {}
    board = mcp.call("leaderboard")
    row = next((r for r in board.get("rows", []) if r["user_id"] == d.user_id), None)
    return {
        "model": d.model_name,
        "leaderboard": row,
        "accounts": state.get("accounts", []),
        "holdings": state.get("holdings", []),
        "needs": state.get("needs", []),
        "processes": state.get("processes", []),
        "parcels": len(state.get("parcels", [])),
        "unlocks": state.get("unlocks", []),
        "behaviour": {
            "sha": hashlib.sha256(
                (behaviour.get("source") or "").encode()).hexdigest()[:16],
            "description": behaviour.get("description"),
            "state": behaviour.get("state"),
            "source": behaviour.get("source"),
        },
        "entry": entry,               # this round's journal entry (or failure)
    }


def _snapshot(mcps: list[tuple[Dynasty, McpClient]], resolved: dict,
              entries: dict[str, dict]) -> RoundSnapshot:
    market = mcps[0][1].call("market_prices")
    snap = RoundSnapshot(
        round=resolved["round_number"], ticks=resolved.get("ticks", []),
        resolved=resolved, market=market,
        events_by_type=resolved.get("events_by_type", {}),
        taken_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        dynasties={d.name: _dynasty_view(mcp, d, entries.get(d.name))
                   for d, mcp in mcps},
    )
    return snap


def _decide(d: Dynasty, lp: AgentLoop, round_no: int) -> dict:
    """One dynasty's decision turn, ready to run on its own thread: the
    dynasties' cycles are independent (each reads only its own entity's
    surface, and ticks only move at resolution — after everyone has
    decided), so their LLM latencies can overlap instead of summing."""
    try:
        return lp.cycle()
    except Exception as exc:                # provider death: keep playing
        return {
            "entity": d.entity_id, "model": d.model_name,
            "round": round_no, "attempts": 0, "accepted": False,
            "kept_old": True, "refusal": f"model failure: {exc}",
            "warnings": [], "source_sha": None, "prompt_bytes": 0,
        }


def run_rounds(loops: list[tuple[Dynasty, AgentLoop]], rounds: int,
               out_dir: str | Path,
               admin_advance=None) -> list[dict]:
    """Rounds 1..N. Each round: every dynasty cycles — CONCURRENTLY, the
    decisions are independent — then readies in order, and the final
    ready resolves the round in-request (readiness gate); a snapshot
    lands in `out_dir/round-XX.json`. A dynasty whose model hard-fails
    (network, provider) keeps its behaviour, journals the failure, and
    STILL readies — one dead model must not stop the world.
    `admin_advance` is the referee fallback for a round nobody resolved
    (never expected in readiness mode; keeps long runs unstickable)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mcps = [(d, lp.mcp) for d, lp in loops]
    snapshots: list[dict] = []

    for round_no in range(1, rounds + 1):
        resolved, entries = None, {}
        with ThreadPoolExecutor(max_workers=len(loops)) as pool:
            futures = [(d, pool.submit(_decide, d, lp, round_no))
                       for d, lp in loops]
            for d, fut in futures:
                entries[d.name] = fut.result()
        # Readying stays sequential: consent order is free (the gate
        # resolves in whichever ready lands last), and a slow set_ready
        # costs a round trip, not an LLM call.
        for d, lp in loops:
            try:
                out_ready = lp.set_ready()
                if out_ready.get("resolved"):
                    resolved = out_ready["resolved"]
            except McpError as exc:
                # e.g. an eliminated dynasty is no longer eligible — its
                # consent is not required, and it must not stop the world
                entries.setdefault(d.name, {}).setdefault(
                    "refusal", f"set_ready refused: {exc}")
        if resolved is None:
            if admin_advance is None:
                raise RuntimeError(
                    f"round {round_no} did not resolve after every dynasty "
                    "readied (gate not in readiness mode?)")
            resolved = admin_advance(round_no)

        snap = _snapshot(mcps, resolved, entries)
        (out / f"round-{round_no:02d}.json").write_text(
            json.dumps(snap.to_json(), indent=1))
        snapshots.append(snap.to_json())
        kinds = ", ".join(f"{k}×{v}" for k, v in
                          sorted(snap.events_by_type.items())) or "quiet"
        print(f"  round {round_no} resolved (ticks {snap.ticks[0]}.."
              f"{snap.ticks[-1] if snap.ticks else '?'}): {kinds}")
    return snapshots


# ---------------------------------------------------------------------------
# Wealth arithmetic for the dashboard (money + holdings at last prices)
# ---------------------------------------------------------------------------

def dynasty_money(view: dict) -> Decimal:
    return sum((Decimal(a["balance"]) for a in view.get("accounts", [])),
               Decimal("0"))


def dynasty_assets(view: dict, prices: dict[str, Decimal]) -> Decimal:
    total = Decimal("0")
    for h in view.get("holdings", []):
        qty = Decimal(h["quantity"])
        price = prices.get(h["symbol"])
        if price is not None and qty > 0:
            total += qty * price
    return total


def price_table(market: list[dict]) -> dict[str, Decimal]:
    return {m["symbol"]: Decimal(m["last_price"])
            for m in market if m.get("last_price") is not None}
