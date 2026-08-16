"""The NIM dynasty run: boot a world, let 3 models play it, dashboard it.

    # pick three usable models first (needs the key):
    .venv/bin/python -m experiments.agent.nim_pick --list
    .venv/bin/python -m experiments.agent.nim_pick --probe <slug> <slug> <slug>

    # the run (key via NVIDIA_API_KEY / NIM_API_KEY / ~/.nim_api_key):
    .venv/bin/python -m experiments.agent.nim_run \
        --models meta/llama-3.3-70b-instruct \
                 qwen/qwen2.5-7b-instruct \
                 mistralai/mistral-small-24b-instruct-2501 \
        --rounds 10 --out /tmp/nim-run

    # offline dress rehearsal (no key, canned responses — proves the pipe):
    .venv/bin/python -m experiments.agent.nim_run --scripted a.jsonl b.jsonl c.jsonl \
        --rounds 3 --out /tmp/nim-rehearsal

What happens: fresh SQLite world; the content-pack substrate
(experiments/world/scenario) with SYMMETRIC seats owned by the dynasties
(same endowment, same parcel bundle, same starter — nothing primed but
the model itself); readiness gate ON; a uvicorn on a scratch port; then
`multi.run_rounds` — each round every dynasty cycles (observe -> think ->
submit through its own MCP surface) and readies, the final ready resolves
the round, a snapshot lands in out/round-XX.json. After the last round:
one self-contained HTML dashboard. The operator built the world and then
stepped back — no admin client paces anything.

Each round costs one model call per dynasty (plus one per lint refusal,
bounded by --max-attempts): 10 rounds ≈ 30-45 calls total.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .dashboard import build_dashboard
from .llm import Model, NimModel, ScriptedModel, nim_key
from .loop import AgentLoop, McpClient
from .multi import Dynasty, build_agent_world, run_rounds


def http_transport(base: str, token: str):
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


def slug(name: str) -> str:
    """A house name as a filename-safe id: `House Llama` -> `house-llama`."""
    return re.sub(r"\W+", "-", name.lower()).strip("-")


def _atomic_write(path: Path, text: str) -> None:
    """Write-then-rename, so a browser (or nginx) never reads a file
    mid-rewrite when the dashboard lands after a round."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def serve_run(out: Path, port: int) -> bool:
    """Serve the run's out dir on 127.0.0.1:<port> from a daemon thread:
    `/` is the live dashboard (rewritten after every round), and the
    round-XX.json snapshots + journals ride along as plain files. nginx
    can do this job just as well — the dir is static — but watching a
    run shouldn't need root to install one."""
    import http.server
    import socketserver

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(out), **kw)

        def translate_path(self, path):
            if path.split("?")[0] in ("/", "/dashboard"):
                path = "/dashboard.html"
            return super().translate_path(path)

        def log_message(self, *a):      # the run log stays about the run
            pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        srv = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"dashboard: not serving ({exc})")
        return False
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="dashboard").start()
    return True


def bootstrap(out: Path, names: list[str], dynasties: list[Dynasty]):
    """Fresh DB, one admin + one user per dynasty, in-process (the OAuth
    surface is for humans; a harness mints directly). Must run BEFORE any
    app import so DATABASE_URL points at the run's own database."""
    db_path = out / "world.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    # The harness outlives an hour-long human session: tokens must last the
    # whole run (a 20-round world runs past 60 minutes — learned the hard
    # way when round 12 bounced 401 on set_ready).
    os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = "100000"
    if db_path.exists():
        db_path.unlink()

    from sqlalchemy.orm import Session
    from econengine.models import Base, User
    from econ.db import engine as db_engine
    from econ.api.auth import create_token

    Base.metadata.create_all(db_engine)
    with Session(db_engine) as s:
        s.add(User(id="u-admin", email="admin@run", name="Operator",
                   provider="test", provider_id="0", is_admin=True))
        for d in dynasties:
            s.add(User(id=d.user_id, email=f"{d.user_id}@run", name=d.name,
                       provider="test", provider_id=d.user_id[-1]))
        s.commit()
        admin_token = create_token("u-admin", "admin@run", True)
        for d in dynasties:
            d.token = create_token(d.user_id, f"{d.user_id}@run", False)
    return admin_token


def spawn_server(port: int, log_path: Path) -> subprocess.Popen:
    """uvicorn on the run's DB. Env rides the command (the footgun the
    readiness demo taught: a server without its DATABASE_URL is pointing
    at some other world)."""
    env = dict(os.environ)          # carries DATABASE_URL, ECON_TICKS_PER_ROUND
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [".venv/bin/python", "-m", "uvicorn", "econ.api.main:app",
         "--port", str(port)],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=os.getcwd())
    import httpx

    for _ in range(60):             # wait for readiness, then hand it back
        try:
            if httpx.get(f"http://127.0.0.1:{port}/openapi.json",
                         timeout=2.0).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"server did not come up; see {log_path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", nargs="+", metavar="SLUG",
                    help="NIM catalog slugs, one per dynasty (seat order)")
    ap.add_argument("--names", nargs="+", default=None,
                    help="dynasty names, one per model (default: House 1..N)")
    ap.add_argument("--scripted", nargs="+", metavar="JSONL",
                    help="offline: canned-response files, one per dynasty")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--ticks-per-round", type=int, default=5)
    ap.add_argument("--scenario", default="frontier",
                    choices=["frontier", "stone_age"],
                    help="content pack to build the world from")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--edit-mode", action="store_true",
                    help="models may answer with SEARCH/REPLACE edit blocks "
                         "or KEEP instead of a full rewrite")
    ap.add_argument("--diary", action="store_true",
                    help="strategy diary per house per round even in a "
                         "scripted run (fixtures must interleave them)")
    ap.add_argument("--no-diary", action="store_true",
                    help="skip the per-round strategy-diary model call")
    ap.add_argument("--port", type=int, default=8906)
    ap.add_argument("--serve", type=int, default=8090, metavar="PORT",
                    help="live dashboard: serve the out dir here "
                         "(0 disables; nginx on the same dir works too)")
    ap.add_argument("--out", default="/tmp/nim-run")
    ap.add_argument("--keep-server", action="store_true")
    args = ap.parse_args(argv)

    if not args.models and not args.scripted:
        ap.error("need --models (NIM) or --scripted (offline rehearsal)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    models: list[Model]
    if args.scripted:
        models = [ScriptedModel.from_file(p) for p in args.scripted]
        model_names = [Path(p).name for p in args.scripted]
    else:
        key = nim_key()
        if not key:
            raise SystemExit("no NIM key: set NVIDIA_API_KEY / NIM_API_KEY, "
                             "or put it in ~/.nim_api_key")
        models = [NimModel(key, slug) for slug in args.models]
        model_names = args.models

    names = args.names or [f"House {i + 1}" for i in range(len(model_names))]
    if len(names) != len(model_names):
        ap.error(f"{len(model_names)} models but {len(names)} names — "
                 "one name per dynasty")

    dynasties = [
        Dynasty(user_id=f"u-{slug(name)}", name=name,
                model_name=mn, token="")
        for name, mn in zip(names, model_names)
    ]

    started = _dt.datetime.now(_dt.timezone.utc)
    bootstrap(out, args.names, dynasties)

    # The world, in-process against the run DB (content pack + owned seats
    # + readiness gate), before the server ever starts.
    from sqlalchemy.orm import Session
    from econ.db import engine as db_engine

    with Session(db_engine) as s:
        build_agent_world(s, dynasties, scenario=args.scenario)
    world = {d.name: {"user_id": d.user_id,
                      "entity_id": d.entity_id, "model": d.model_name}
             for d in dynasties}

    os.environ["ECON_TICKS_PER_ROUND"] = str(args.ticks_per_round)
    proc = spawn_server(args.port, out / "server.log")
    base = f"http://127.0.0.1:{args.port}"
    print(f"world up: {len(dynasties)} dynasties, gate=readiness, "
          f"K={args.ticks_per_round} ticks/round, {args.rounds} rounds")

    if args.serve:
        # A placeholder so the URL answers from second zero — round 1's
        # LLM calls take minutes, and the watcher should see that, not a
        # 404. Replaced by the first real rewrite at round 1's resolution.
        (out / "dashboard.html").write_text(
            '<!doctype html><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="10">'
            "<title>econ.me — warming up</title>"
            '<body style="font:14px sans-serif;background:#0f1115;'
            'color:#e5e7eb;padding:28px"><h1>warming up…</h1>'
            "<p>round 1 is in flight; the dashboard lands when it resolves "
            "(this page retries every 10s).</p></body>")
        if serve_run(out, args.serve):
            print(f"dashboard: http://127.0.0.1:{args.serve}/ "
                  "(live — rewritten after every round)")

    try:
        loops = []
        # the diary defaults ON for NIM runs (one short extra call per
        # house per round, inside the same parallel window) and OFF for
        # scripted rehearsals (legacy fixtures carry one line per round)
        diary = args.diary or (not args.no_diary and not args.scripted)
        for d, model in zip(dynasties, models):
            lp = AgentLoop(
                McpClient(http_transport(base, d.token)),
                model,
                entity_id=d.entity_id, max_attempts=args.max_attempts,
                journal_path=str(out / f"journal-{slug(d.name)}.jsonl"),
                edit_mode=args.edit_mode, diary=diary)
            loops.append((d, lp))

        print(f"dynasties: {', '.join(f'{d.name} = {d.model_name}' for d in dynasties)}")
        t0 = time.monotonic()

        def write_dash(snaps: list[dict], done: bool = False) -> None:
            """The dashboard rewrite: after every round while live, once
            more at the finish (status flips, auto-refresh drops off)."""
            meta = {
                "title": f"econ.me dynasty run — {args.rounds} rounds",
                "ticks_per_round": args.ticks_per_round,
                "generated": _dt.datetime.now(_dt.timezone.utc)
                             .isoformat(timespec="seconds"),
                "elapsed_s": round(time.monotonic() - t0, 1),
                "world": world,
                "round": snaps[-1]["round"] if snaps else 0,
                "rounds_total": args.rounds,
                "status": "complete" if done else "live",
            }
            if not done:
                meta["refresh_s"] = 10
            _atomic_write(out / "dashboard.html",
                          build_dashboard(snaps, meta))
            _atomic_write(out / "meta.json", json.dumps(meta, indent=1))

        snapshots = run_rounds(loops, args.rounds, out,
                               on_round=write_dash)
        elapsed = time.monotonic() - t0
    finally:
        if not args.keep_server:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)

    write_dash(snapshots, done=True)
    (out / "snapshots.json").write_text(json.dumps(snapshots, indent=1))

    print(f"\ndone in {elapsed:.0f}s — {snapshots[-1]['ticks'][-1]} ticks, "
          f"{len(snapshots)} rounds")
    print(f"dashboard: {out}/dashboard.html"
          + (f" (was http://127.0.0.1:{args.serve}/)" if args.serve else ""))
    print(f"snapshots: {out}/round-*.json, journals: {out}/journal-*.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
