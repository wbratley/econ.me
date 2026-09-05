"""Re-fire a persisted round prompt at the provider and dump the stream.

Track-0 forensics (run 31: Harald burned ~85 minutes per attempt thinking,
three attempts in one round, and nobody could say WHAT the model was doing
all that time — the reasoning text was discarded client-side). AgentLoop
now persists the exact prompts (seat-*.round-N.*.prompt.md in the run dir);
this tool re-fires one of them and captures everything:

    .venv/bin/python -m experiments.agent.replay_prompt \
        --prompt ~/econ-runs/stone-run31/seat-house-harald.round-3.author.prompt.md \
        --attempt 2

Writes <prompt>.replay-<attempt>.txt: the trace stats (elapsed, finish
reason, delta counts), then the full reasoning channel, then the final
content — the artifact that answers "what does an hour of reasoning
look like" with text instead of inference. Costs one real API call at
whatever budget you pass (--max-tokens, default the model family's).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .llm import DeepSeekModel, NimModel, deepseek_key, nim_key


def parse_prompt_file(path: Path) -> dict:
    """The persisted format: '# key: value' header lines, '## SYSTEM'
    and '## USER (attempt N)' sections. Returns model/seat/round/kind,
    the system text, and the per-attempt user texts."""
    head: dict[str, str] = {}
    sections: list[tuple[str, str]] = []      # (title, body)
    title, buf = "preamble", []

    for line in path.read_text().splitlines():
        if line.startswith("# model:") or line.startswith("# seat:") \
                or line.startswith("# round:") or line.startswith("# kind:"):
            k, _, v = line[2:].partition(":")
            head[k.strip()] = v.strip()
            continue
        if line.startswith("## "):
            sections.append((title, "\n".join(buf).strip()))
            title, buf = line[3:].strip(), []
            continue
        buf.append(line)
    sections.append((title, "\n".join(buf).strip()))

    users = [body for name, body in sections
             if name.startswith("USER")]
    system = next((body for name, body in sections if name == "SYSTEM"), "")
    return {"model": head.get("model", ""), "seat": head.get("seat"),
            "round": head.get("round"), "kind": head.get("kind"),
            "system": system, "users": users}


def build_model(spec: str, on_trace) -> object:
    """'nim:slug' / 'deepseek:slug' (loop names) or a bare NIM slug."""
    if spec.startswith("deepseek:"):
        key = deepseek_key()
        if not key:
            raise SystemExit("no DeepSeek key: set DEEPSEEK_API_KEY or "
                             "~/.deepseek_api_key")
        return DeepSeekModel(key, spec[len("deepseek:"):],
                             on_trace=on_trace)
    slug = spec[len("nim:"):] if spec.startswith("nim:") else spec
    key = nim_key()
    if not key:
        raise SystemExit("no NIM key: set NVIDIA_API_KEY or ~/.nim_api_key")
    return NimModel(key, slug, on_trace=on_trace)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="re-fire a persisted round prompt, dump the stream")
    ap.add_argument("--prompt", required=True,
                    help="seat-*.round-N.*.prompt.md from a run dir")
    ap.add_argument("--attempt", type=int, default=1,
                    help="which USER section to re-fire (default 1)")
    ap.add_argument("--max-tokens", type=int, default=None,
                    help="override the family default budget")
    ap.add_argument("--reasoning-effort", default=None,
                    help="DeepSeek: low/medium/high (default the seat's)")
    ap.add_argument("--out", default=None,
                    help="dump file (default <prompt>.replay-<n>.txt)")
    args = ap.parse_args()

    path = Path(args.prompt)
    parsed = parse_prompt_file(path)
    if not parsed["model"] or not parsed["users"]:
        raise SystemExit(f"{path} is not a persisted prompt file")
    if not 1 <= args.attempt <= len(parsed["users"]):
        raise SystemExit(f"attempt {args.attempt} out of range "
                         f"(file has {len(parsed['users'])})")

    captured: dict = {}

    def on_trace(trace: dict) -> None:
        captured.update(trace)

    model = build_model(parsed["model"], on_trace)
    if args.max_tokens is not None:
        model._max_tokens = args.max_tokens

    t0 = time.monotonic()
    error = ""
    try:
        text = model.complete(parsed["system"],
                              parsed["users"][args.attempt - 1])
    except Exception as exc:                    # exhaustion, refusal, transport
        text, error = "", f"{type(exc).__name__}: {exc}"

    elapsed = time.monotonic() - t0
    reason = captured.get("reasoning_text") or (
        "(none captured — the attempt failed before streaming, "
        "or finished normally)")
    dump = (f"# replay of {path.name} attempt {args.attempt}\n"
            f"# model={parsed['model']} seat={parsed['seat']} "
            f"round={parsed['round']} kind={parsed['kind']}\n"
            f"# wall {elapsed:.1f}s  error={error or '-'}\n"
            f"# trace {json.dumps(captured, default=str)}\n\n"
            f"===== REASONING ({len(reason)} chars) =====\n\n{reason}\n\n"
            f"===== FINAL CONTENT ({len(text)} chars) =====\n\n{text}\n")

    out = Path(args.out) if args.out else \
        path.with_suffix(f".replay-{args.attempt}.txt")
    out.write_text(dump)
    print(f"wrote {out} ({elapsed:.1f}s wall, "
          f"reasoning {len(captured.get('reasoning_text') or '')} chars, "
          f"content {len(text)} chars"
          + (f", ERROR {error}" if error else ""))


if __name__ == "__main__":
    main()
