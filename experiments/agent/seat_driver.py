"""The seat kit's reference driver (M2b): occupy a seat in an always-on
world from anywhere, over nothing but the public surfaces.

M2a made the host always-on — rounds resolve by consent (or a deadline
backstop) and announce themselves on `GET /rounds/events`. This module
is the client half: a long-lived, event-driven process that connects
with a Bearer token minted by the host (`POST /admin/tokens`), sleeps
on the round stream, and takes exactly one turn per round —
observe -> author -> submit -> ready, all through the MCP endpoint via
the UNCHANGED AgentLoop. What the driver adds is the WHEN (round
events, not a run loop) and the race guards:

  - dedupe by round number: `hello` on (re)connect and `round_opened`
    both mean "a round is open" — one turn per round, never two;
  - catch-up: if the round moves mid-turn (deadline fired while the
    model thought, or our own final ready resolved it), the turn
    re-cycles for the round now open — consent never attaches to a
    round the seat didn't play;
  - elimination: the dead get no turn (multi._extinct_entry's shape) —
    a tombstone, then exit (or spectate with --spectate).

The autonomy dial decides WHO answers the prompt:

  proxy     FileModel — the file rendezvous: whoever writes
            seat-<slug>.response.txt IS the seat (a human with a
            coding agent; AGENTS.md in the scaffold is their manual)
  assisted  ApprovalModel — a hosted model drafts, the human approves
            or replaces through the same rendezvous
  auto      a hosted model, unattended — exactly a builtin seat, with
            the server's deadline as the only wall-clock bound

Everything rides public surfaces, so the driver needs no database, no
repo-local imports of the engine to PLAY (only the catalog fold renders
the world_catalog state into prompt text, and it degrades to None) —
and tests drive it against the TestClient with injected event streams.

    ECON_SEAT_TOKEN=... python -m experiments.agent.seat_driver \
        --base http://127.0.0.1:8925 --workspace ~/my-seat \
        --seat "House Mine" --autonomy proxy --init-workspace
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

from .llm import ApprovalModel, FileModel, Model, ScriptedModel
from .loop import AgentLoop, McpClient, McpError


# ===========================================================================
# SSE: a line-oriented parser and the httpx stream that feeds it
# ===========================================================================

class SseParser:
    """Incremental server-sent-events parsing, one `feed(line)` per line
    (no trailing newline), returning a dispatched `(event, data)` when a
    blank line closes a frame. Comments (`: keepalive`) and frames with
    no data are dropped; `data:` is JSON-decoded when it parses (the
    round stream always sends objects) and passed through as text when
    it doesn't. Good enough for the one stream this driver reads — the
    five-pound hammer, no spec edge cases.
    """

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, line: str) -> tuple[str, dict | str] | None:
        if line == "":                       # blank line: frame boundary
            if self._event is None and not self._data:
                return None
            data = "\n".join(self._data)
            event, self._event = self._event or "message", None
            self._data = []
            if not data.strip():
                return None
            try:
                payload: dict | str = json.loads(data)
            except ValueError:
                payload = data
            return event, payload
        if line.startswith(":"):             # comment / heartbeat
            return None
        if line.startswith("event:"):
            self._event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            self._data.append(line[len("data:"):].removeprefix(" "))
        return None


def sse_events(base: str) -> Iterable[tuple[str, dict | str]]:
    """Yield (event, data) from the public round stream until the
    connection ends; transport errors raise to the caller (the CLI's
    reconnect loop owns the policy). No read timeout — heartbeats are
    for proxies, and the driver would rather block than poll."""
    import httpx

    with httpx.stream(
        "GET", f"{base.rstrip('/')}/rounds/events",
        timeout=httpx.Timeout(10.0, read=None),
    ) as r:
        r.raise_for_status()
        parser = SseParser()
        for line in r.iter_lines():
            frame = parser.feed(line)
            if frame is not None:
                yield frame


# ===========================================================================
# The driver
# ===========================================================================

# A mid-turn catch-up bound: every catch-up costs a full cycle (minutes
# for a hosted model), and the round only moves at resolutions — more
# than a few in one turn means something is deeply wrong, and the seat
# readies on what it has rather than withholding consent forever.
MAX_CATCHUPS = 5


class SeatDriver:
    """Event-driven seat: one turn per round, races guarded (see the
    module docstring). Transport-free on both sides — the AgentLoop's
    MCP client is injected, and `run()` iterates whatever stream it is
    handed (the CLI passes `sse_events`; tests pass a list)."""

    def __init__(self, loop: AgentLoop, seat: str, workspace: Path,
                 spectate: bool = False, max_rounds: int | None = None,
                 log: Callable[[str], None] = print):
        self.loop = loop
        self.seat = seat
        self.workspace = Path(workspace)
        self.journal = self.workspace / "driver.jsonl"
        self.spectate = spectate
        self.max_rounds = max_rounds
        self._log = log
        self.last_round: int | None = None    # dedupe: the round last played
        self.turns = 0
        self.stopped = False                  # elimination or --max-rounds
        self._extinct = False                 # tombstone already journaled

    # -- journal ----------------------------------------------------------

    def _journal_line(self, kind: str, **fields) -> None:
        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(
                timespec="seconds"),
            "seat": self.seat, "kind": kind, **fields,
        }
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            with open(self.journal, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass                # the journal must never be the failure

    # -- one turn ----------------------------------------------------------

    def _tombstone(self, round_no: int, status: str) -> dict:
        """The dead get no turn — multi._extinct_entry's shape: no model
        call, no observation, the last behaviour stands, the journal
        says what happened."""
        return {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(
                timespec="seconds"),
            "entity": self.loop.entity_id, "model": self.loop.model.name,
            "round": round_no, "attempts": 0, "accepted": False,
            "kept_old": True, "action": "extinct", "refusal": None,
            "warnings": [], "source_sha": None, "thoughts": "",
            "prompt_bytes": 0, "status": status,
        }

    def take_turn(self) -> dict:
        """One round's turn: cycle until the round is stable (the
        catch-up guard), then consent. Even a failed cycle readies —
        one dead model must not stop the world (multi.run_rounds'
        doctrine); the entity keeps its behaviour and the world moves."""
        eid = self.loop.ensure_entity()
        entry: dict = {}
        catchups = 0
        while True:
            state = self.loop.mcp.call("entity_state", {"entity_id": eid})
            status = (state.get("entity") or {}).get("status")
            if status != "active":
                round_no = self.loop.mcp.call("round_state").get(
                    "current_round")
                entry = self._tombstone(round_no, status)
                self.loop.journal_lines.append(entry)
                if self.loop.journal_path:
                    with open(self.loop.journal_path, "a") as fh:
                        fh.write(json.dumps(entry) + "\n")
                self._journal_line("eliminated", round=round_no,
                                   status=status)
                self._log(f"{self.seat}: eliminated (status={status}) "
                          f"after round play — tombstone written")
                self._extinct = True
                if not self.spectate:
                    self.stopped = True
                return entry
            entry = self.loop.cycle()
            played = entry.get("round")
            current = self.loop.mcp.call("round_state").get("current_round")
            if current == played:
                break
            catchups += 1
            self._journal_line("catchup", played=played, current=current,
                               n=catchups)
            self._log(f"{self.seat}: round moved {played} -> {current} "
                      f"mid-turn (deadline? opponent consent?) — "
                      f"catching up")
            if catchups >= MAX_CATCHUPS:
                self._log(f"{self.seat}: catch-up bound hit; readying on "
                          f"the round now open")
                break
        self.last_round = entry.get("round")
        self.turns += 1
        try:
            out = self.loop.set_ready()
            if out.get("resolved"):
                self._journal_line(
                    "resolved", round=out["resolved"].get("round_number"),
                    by="own consent")
        except McpError as exc:
            entry["refusal"] = f"set_ready refused: {exc}"
            self._journal_line("ready_refused", round=entry.get("round"),
                               error=str(exc))
            self._log(f"{self.seat}: set_ready refused: {exc}")
        if self.max_rounds is not None and self.turns >= self.max_rounds:
            self._log(f"{self.seat}: --max-rounds {self.max_rounds} "
                      f"reached — stopping")
            self.stopped = True
        return entry

    # -- the event loop ----------------------------------------------------

    def handle_event(self, event: str, data) -> bool:
        """One stream event; returns False when the driver is done. The
        two wake-ups — `hello` (connect/reconnect snapshot) and
        `round_opened` — both mean `a round is open`; dedupe is by round
        number, so reconnect storms and replayed frames cost nothing."""
        if self.stopped:
            return False
        if event == "hello":
            rnd = (data or {}).get("current_round") \
                if isinstance(data, dict) else None
            if rnd is not None and (self.last_round is None
                                    or rnd > self.last_round):
                self.take_turn()
                return not self.stopped
        elif event == "round_opened":
            if self._extinct:      # spectating the world we left
                return True
            rnd = (data or {}).get("round") if isinstance(data, dict) else None
            if rnd is not None and (self.last_round is None
                                    or rnd > self.last_round):
                self.take_turn()
                return not self.stopped
        elif event == "round_closed":
            if isinstance(data, dict):
                self._journal_line(
                    "round_closed", round=data.get("round_number"),
                    eliminations=data.get("eliminations"))
        return True

    def run(self, source: Callable[[], Iterable[tuple]]) -> None:
        """Iterate ONE stream to its end; reconnect policy is the
        caller's (the CLI wraps with backoff; tests pass a list)."""
        for event, data in source():
            if not self.handle_event(event, data):
                break


# ===========================================================================
# The CLI: workspace scaffold, model by autonomy, connect-and-listen
# ===========================================================================

def init_workspace(ws: Path, base: str, seat: str) -> None:
    """The seat kit scaffold: the coding agent's manual + the public
    connection facts. The token NEVER lands here — it rides the
    environment (`ECON_SEAT_TOKEN`), because workspaces get copied,
    zipped, and pasted into chats."""
    ws.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).parent / "seat_template" / "AGENTS.md"
    (ws / "AGENTS.md").write_text(template.read_text())
    (ws / "seat.json").write_text(
        json.dumps({"seat": seat, "base": base}, indent=1) + "\n")


def _http_transport(base: str, token: str):
    """One MCP JSON-RPC round-trip over httpx Bearer POST /mcp — same
    bytes nim_run sends, restated here so the driver is a single
    copyable file (self-contained for someone wiring their own seat)."""
    import httpx

    client = httpx.Client(base_url=base, timeout=240.0,
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


def _hosted_model(slug: str, scripted: str | None) -> Model:
    """The model behind `auto` (alone) and `assisted` (as the drafter):
    a NIM or DeepSeek slug, or an offline scripted file for dress
    rehearsals."""
    from .llm import DeepSeekModel, NimModel, deepseek_key, nim_key

    if scripted:
        return ScriptedModel.from_file(scripted)
    if not slug:
        raise SystemExit(
            "--model is required for hosted autonomy (a NIM slug, or "
            "deepseek:<slug>) — or pass --scripted for an offline "
            "rehearsal")
    if slug.startswith("deepseek:"):
        key = deepseek_key()
        if not key:
            raise SystemExit("no DeepSeek key: set DEEPSEEK_API_KEY, or "
                             "put it in ~/.deepseek_api_key")
        return DeepSeekModel(key, slug[len("deepseek:"):])
    key = nim_key()
    if not key:
        raise SystemExit("no NIM key: set NVIDIA_API_KEY / NIM_API_KEY, "
                         "or put it in ~/.nim_api_key")
    return NimModel(key, slug)


def build_model(autonomy: str, seat: str, workspace: Path, slug: str,
                scripted: str | None, timeout_s: float) -> Model:
    """The autonomy dial, as a Model: proxy = the rendezvous alone,
    assisted = draft-then-approve, auto = the hosted model alone."""
    if autonomy == "proxy":
        return FileModel(seat, workspace, timeout_s=timeout_s)
    if autonomy == "assisted":
        return ApprovalModel(_hosted_model(slug, scripted), seat, workspace,
                             timeout_s=timeout_s)
    return _hosted_model(slug, scripted)


def _catalog_fold(mcp: McpClient) -> str | None:
    """The readable world through the public surface: world_catalog
    returns the catalog STATE; the prompt wants its rendered text. A
    local rendering of a remote world's state — no DB, no secrets.
    Degrades to None (no catalog fold) when the surface or the
    import is unavailable."""
    try:
        from econengine.catalog import catalog_text
        return catalog_text(mcp.call("world_catalog"))
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, metavar="URL",
                    help="the world server, e.g. http://127.0.0.1:8925")
    ap.add_argument("--token", default=None,
                    help="seat Bearer token (default: $ECON_SEAT_TOKEN — "
                         "the host mints one with POST /admin/tokens)")
    ap.add_argument("--workspace", default="./econ-seat", metavar="DIR",
                    help="where the rendezvous files, journals and "
                         "AGENTS.md live")
    ap.add_argument("--seat", default="Seat", metavar="NAME",
                    help="the seat's display name (slugs the rendezvous "
                         "files and journals)")
    ap.add_argument("--autonomy", choices=["proxy", "assisted", "auto"],
                    default="proxy",
                    help="who answers the prompt: proxy = you (file "
                         "rendezvous), assisted = model drafts + you "
                         "approve, auto = model alone")
    ap.add_argument("--model", default=None, metavar="SLUG",
                    help="hosted model slug for assisted/auto "
                         "(deepseek:<slug> for DeepSeek)")
    ap.add_argument("--scripted", default=None, metavar="JSONL",
                    help="offline rehearsal: canned responses instead of "
                         "a hosted model (works with any autonomy)")
    ap.add_argument("--init-workspace", action="store_true",
                    help="write AGENTS.md + seat.json into the workspace "
                         "and exit")
    ap.add_argument("--max-rounds", type=int, default=None,
                    help="stop after this many turns (default: until "
                         "eliminated or the world ends)")
    ap.add_argument("--spectate", action="store_true",
                    help="keep listening (no turns) after elimination — "
                         "the default is to exit")
    ap.add_argument("--timeout-s", type=float, default=86400.0, metavar="SEC",
                    help="how long a proxy/assisted rendezvous waits "
                         "before failing the attempt (default 24h)")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="lint-retry bound per cycle (as nim_run)")
    ap.add_argument("--edit-mode", action="store_true",
                    help="replies may use SEARCH/REPLACE edit blocks or "
                          "KEEP (as nim_run)")
    ap.add_argument("--diary", action="store_true",
                    help="one extra strategy-diary call per round (a "
                         "second rendezvous for proxy/assisted seats)")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    if args.init_workspace:
        init_workspace(ws, args.base, args.seat)
        print(f"workspace ready: {ws} (AGENTS.md, seat.json)")
        return 0

    token = args.token or os.environ.get("ECON_SEAT_TOKEN")
    if not token:
        ap.error("no token: pass --token or set ECON_SEAT_TOKEN")

    # the loop's journal write opens append-only and does not mkdir —
    # nim_run always has its out dir, a fresh seat workspace does not
    # (the live smoke caught exactly this: ENOENT mid-cycle, forever)
    ws.mkdir(parents=True, exist_ok=True)

    model = build_model(args.autonomy, args.seat, ws, args.model,
                        args.scripted, args.timeout_s)
    mcp = McpClient(_http_transport(args.base, token))
    loop = AgentLoop(
        mcp, model, max_attempts=args.max_attempts,
        journal_path=str(ws / "journal.jsonl"), edit_mode=args.edit_mode,
        diary=args.diary, catalog=_catalog_fold(mcp),
        trace_dir=str(ws), seat=args.seat)
    driver = SeatDriver(loop, args.seat, ws, spectate=args.spectate,
                        max_rounds=args.max_rounds)

    print(f"{args.seat}: listening on {args.base}/rounds/events "
          f"(autonomy={args.autonomy}, model={model.name})", flush=True)
    backoff = 1.0
    while not driver.stopped:
        try:
            driver.run(lambda: sse_events(args.base))
            backoff = 1.0        # clean end (server restart): reconnect now
        except KeyboardInterrupt:
            print(f"{args.seat}: stopped by operator", flush=True)
            return 0
        except Exception as exc:
            print(f"{args.seat}: stream error ({exc}); reconnecting in "
                  f"{backoff:.0f}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    print(f"{args.seat}: driver stopped after {driver.turns} turns "
          f"(last round {driver.last_round})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
