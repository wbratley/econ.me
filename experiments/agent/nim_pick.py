"""Pick NIM models for the run: list the catalog, probe candidates.

    # what does my key see? (env NVIDIA_API_KEY / NIM_API_KEY, or ~/.nim_api_key)
    .venv/bin/python -m experiments.agent.nim_pick --list

    # filter
    .venv/bin/python -m experiments.agent.nim_pick --list llama

    # do these three follow instructions? (latency + exact-match check)
    .venv/bin/python -m experiments.agent.nim_pick --probe \
        meta/llama-3.3-70b-instruct qwen/qwen2.5-7b-instruct \
        mistralai/mistral-small-24b-instruct-2501

The probe asks for one exact Lua line back — the smallest possible stand-in
for "reply with ONLY the script": it catches the failure modes that matter
for the loop (chatty models, fence-wrapped output is handled by
strip_fences, reasoning models that emit <think> blocks) before a 10-round
run pays for them.
"""

from __future__ import annotations

import argparse
import time

from .llm import NIM_DEFAULT_BASE, NimModel, nim_key

PROBE_WANT = "local x = 1"


def list_models(key: str, base: str = NIM_DEFAULT_BASE) -> list[str]:
    import httpx

    r = httpx.get(f"{base.rstrip('/')}/v1/models",
                  headers={"Authorization": f"Bearer {key}"}, timeout=60.0)
    r.raise_for_status()
    return sorted(m["id"] for m in r.json().get("data", []))


def probe(key: str, slug: str, base: str = NIM_DEFAULT_BASE) -> dict:
    model = NimModel(key, slug, base_url=base, max_tokens=256)
    t0 = time.monotonic()
    out = model.complete(
        "You write Lua behaviour scripts for a simulated economy. "
        "You always reply with raw Lua only — never prose, never fences.",
        f"Reply with ONLY this exact line and nothing else:\n{PROBE_WANT}")
    return {
        "slug": slug, "latency_s": round(time.monotonic() - t0, 1),
        "exact": out.strip() == PROBE_WANT, "replied": out.strip()[:80],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", nargs="?", const="", metavar="SUBSTR",
                    help="list catalog ids (optionally filtered by substring)")
    ap.add_argument("--probe", nargs="+", metavar="SLUG",
                    help="instruction-following probe for candidate slugs")
    ap.add_argument("--base", default=NIM_DEFAULT_BASE)
    args = ap.parse_args(argv)

    key = nim_key()
    if not key:
        raise SystemExit("no NIM key: set NVIDIA_API_KEY / NIM_API_KEY, or "
                         "put it in ~/.nim_api_key (first line)")
    if args.list is not None:
        ids = [i for i in list_models(key, args.base)
               if args.list.lower() in i.lower()]
        print(f"{len(ids)} model(s):")
        for i in ids:
            print(f"  {i}")
    if args.probe:
        for slug in args.probe:
            try:
                r = probe(key, slug, args.base)
                mark = "OK " if r["exact"] else "?? "
                print(f"  {mark}{slug}: {r['latency_s']}s — replied: "
                      f"{r['replied']!r}")
            except Exception as exc:
                print(f"  !! {slug}: {exc}")
    if args.list is None and not args.probe:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
