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
from .llm import (Model, DeepSeekModel, NimModel, ScriptedModel,
                  deepseek_key, nim_key)
from .loop import AgentLoop, McpClient
from .multi import (Dynasty, build_agent_world, read_world_meta,
                    run_rounds)


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
    app import so DATABASE_URL points at the run's own database.
    Idempotent users: a --resume bootstrap finds them already there and
    just re-mints tokens (adding them again would violate id uniqueness);
    main() refuses to touch an existing world without --resume, so this
    never unlinks."""
    db_path = out / "world.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    # The harness outlives an hour-long human session: tokens must last the
    # whole run (a 20-round world runs past 60 minutes — learned the hard
    # way when round 12 bounced 401 on set_ready).
    os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = "100000"

    from sqlalchemy.orm import Session
    from econengine.models import Base, User
    from econ.db import engine as db_engine
    from econ.api.auth import create_token

    Base.metadata.create_all(db_engine)
    with Session(db_engine) as s:
        if s.get(User, "u-admin") is None:
            s.add(User(id="u-admin", email="admin@run", name="Operator",
                       provider="test", provider_id="0", is_admin=True))
        for d in dynasties:
            if s.get(User, d.user_id) is None:
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
    ap.add_argument("--resume", action="store_true",
                    help="continue an interrupted run in --out: keep its "
                         "world.db and seats, skip rounds already on disk "
                         "(round-XX.json), append to its journals. Without "
                         "an existing world this just runs fresh.")
    ap.add_argument("--keep-server", action="store_true")
    ap.add_argument("--seed-script", default=None, metavar="PATH",
                    help="lua file installed as every dynasty's starting "
                         "behaviour instead of the scenario starter, "
                         "EXCEPT a live seat (a real player earns their "
                         "own script); smoke-run gated at build time")
    ap.add_argument("--live-seat", default=None, metavar="NAME",
                    help="the --names entry played live through a file "
                         "rendezvous (seat-<slug>.prompt.md / .response."
                         "txt in --out) instead of a hosted model — the "
                         "exhibition run's fourth house")
    ap.add_argument("--live-timeout-s", type=float, default=86400.0,
                    metavar="SEC",
                    help="how long a live seat's call waits before "
                         "failing the attempt (default 24h)")
    args = ap.parse_args(argv)

    if not args.models and not args.scripted:
        ap.error("need --models (NIM) or --scripted (offline rehearsal)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    models: list[Model]
    if args.scripted and args.live_seat:
        ap.error("--live-seat can't ride --scripted (it needs a run dir "
                 "for the rendezvous files)")
    if args.scripted:
        models = [ScriptedModel.from_file(p) for p in args.scripted]
        model_names = [Path(p).name for p in args.scripted]
    else:
        key = nim_key()
        if not key:
            raise SystemExit("no NIM key: set NVIDIA_API_KEY / NIM_API_KEY, "
                             "or put it in ~/.nim_api_key")
        names = (args.names or [f"House {i + 1}"
                                for i in range(len(args.models))])
        if args.live_seat and args.live_seat not in names:
            ap.error(f"--live-seat {args.live_seat!r} is not among "
                     f"--names {names}")
        # NIM seats cover every name except the live one; stone-run
        # pairs them 1:1, the exhibition run 3:1.
        nim_names = [n for n in names if n != args.live_seat]
        if len(nim_names) != len(args.models):
            ap.error(f"--models lists {len(args.models)} but "
                     f"{len(nim_names)} seats need one "
                     "(--names minus the live seat)")
        pairs = dict(zip(nim_names, args.models))
        models, model_names = [], []
        for n in names:
            if n == args.live_seat:
                from .llm import FileModel
                models.append(FileModel(n, out,
                                        timeout_s=args.live_timeout_s))
                model_names.append(f"live:{n}")
            else:
                seat_model = pairs[n]
                if seat_model.startswith("deepseek:"):
                    # a DeepSeek seat: same streamed OpenAI-compatible
                    # call, prepaid credit, off-peak billing gate —
                    # see DeepSeekModel for the window rules
                    dk = deepseek_key()
                    if not dk:
                        raise SystemExit(
                            "no DeepSeek key: set DEEPSEEK_API_KEY, or put "
                            "it in ~/.deepseek_api_key")
                    models.append(DeepSeekModel(
                        dk, seat_model[len("deepseek:"):]))
                else:
                    models.append(NimModel(key, seat_model))
                model_names.append(seat_model)

    names = args.names or [f"House {i + 1}" for i in range(len(model_names))]
    seed_source = (Path(args.seed_script).read_text()
                   if args.seed_script else None)
    if len(names) != len(model_names):
        ap.error(f"{len(model_names)} models but {len(names)} names — "
                 "one name per dynasty")

    dynasties = [
        Dynasty(user_id=f"u-{slug(name)}", name=name,
                model_name=mn, token="")
        for name, mn in zip(names, model_names)
    ]

    # A reboot-safe runner must not silently destroy an interrupted run:
    # notice the existing world BEFORE bootstrap or anything else opens it.
    if (out / "world.db").exists() and not args.resume:
        raise SystemExit(
            f"{out} already holds a world — pass --resume to continue it, "
            "or point --out at a fresh directory")
    started = _dt.datetime.now(_dt.timezone.utc)
    bootstrap(out, args.names, dynasties)

    # The world, in-process against the run DB (content pack + owned seats
    # + readiness gate), before the server ever starts.
    from sqlalchemy import select as _select
    from sqlalchemy.orm import Session
    from econ.db import engine as db_engine
    from econengine.models import Entity, WorldSetting

    start_round, prior_snaps = 1, []
    for p in sorted(out.glob("round-*.json")):
        prior_snaps.append(json.loads(p.read_text()))
        start_round = max(start_round, prior_snaps[-1]["round"] + 1)

    with Session(db_engine) as s:
        gate = s.execute(_select(WorldSetting.key)
                         .where(WorldSetting.key == "round.gate")).first()
        if gate is not None:
            # Attach to the existing seats: same deterministic user ids,
            # entity ids recovered from ownership — the world IS the state.
            for d in dynasties:
                ent = s.execute(_select(Entity)
                                .where(Entity.owner_id == d.user_id)
                                ).scalar_one_or_none()
                if ent is None:
                    raise SystemExit(
                        f"--resume: no seat owned by {d.user_id} — is "
                        f"{out} the run dir for these --names?")
                d.entity_id = ent.id
            world_meta = read_world_meta(s)
            print(f"resuming: world intact, rounds 1..{start_round - 1} "
                  f"on disk; continuing at {start_round}")
        else:
            world_meta = build_agent_world(
                s, dynasties, scenario=args.scenario,
                seeds=({n: seed_source for n in names
                        if n != args.live_seat}
                       if seed_source else None))
    manual = (world_meta or {}).get("manual")
    catalog = (world_meta or {}).get("catalog")
    world = {d.name: {"user_id": d.user_id,
                      "entity_id": d.entity_id, "model": d.model_name}
             for d in dynasties}

    if args.resume and start_round > args.rounds:
        print(f"nothing to do: {start_round - 1} rounds on disk, "
              f"--rounds {args.rounds} — run complete already")
        return 0

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
                edit_mode=args.edit_mode, diary=diary, manual=manual,
                catalog=catalog, trace_dir=str(out), seat=d.name)
            loops.append((d, lp))

        print(f"dynasties: {', '.join(f'{d.name} = {d.model_name}' for d in dynasties)}")
        t0 = time.monotonic()

        def write_dash(snaps: list[dict], status: str = "live") -> None:
            """The dashboard rewrite: after every round while live, once
            more at the finish (status flips, auto-refresh drops off —
            "extinct" when the last dynasty died before round N)."""
            meta = {
                "title": f"econ.me dynasty run — {args.rounds} rounds",
                "ticks_per_round": args.ticks_per_round,
                "seed_script": args.seed_script,
                "generated": _dt.datetime.now(_dt.timezone.utc)
                             .isoformat(timespec="seconds"),
                "elapsed_s": round(time.monotonic() - t0, 1),
                "world": world,
                "round": snaps[-1]["round"] if snaps else 0,
                "rounds_total": args.rounds,
                "status": status,
            }
            if status == "live":
                meta["refresh_s"] = 10
            _atomic_write(out / "dashboard.html",
                          build_dashboard(snaps, meta))
            _atomic_write(out / "meta.json", json.dumps(meta, indent=1))

        snapshots = run_rounds(loops, args.rounds, out,
                               on_round=write_dash,
                               start_round=start_round,
                               snapshots=prior_snaps)
        elapsed = time.monotonic() - t0
    finally:
        if not args.keep_server:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)

    write_dash(snapshots, status="complete" if len(snapshots) >= args.rounds
               else "extinct")
    (out / "snapshots.json").write_text(json.dumps(snapshots, indent=1))

    print(f"\ndone in {elapsed:.0f}s — {snapshots[-1]['ticks'][-1]} ticks, "
          f"{len(snapshots)} rounds")
    print(f"dashboard: {out}/dashboard.html"
          + (f" (was http://127.0.0.1:{args.serve}/)" if args.serve else ""))
    print(f"snapshots: {out}/round-*.json, journals: {out}/journal-*.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
