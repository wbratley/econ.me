#!/usr/bin/env bash
# Launch the EXHIBITION run: three champion-seeded gpt-oss houses plus a
# LIVE fourth seat (House Excalibur) played through a file rendezvous by
# a human-driven agent — run 25, "the real house vs 3 champion scripts".
#
# usage: scripts/excal-run.sh <run-number> [rounds] [champion.lua]
#   e.g.: scripts/excal-run.sh 25 40 ~/econ-runs/script-archive/.../rev029.lua
#
# The live seat's protocol (FileModel in experiments/agent/llm.py): every
# agent cycle drops  seat-house-excalibur.prompt.md  in the OUT dir and
# WAITS (24h default timeout); the player answers by ATOMICALLY writing
# seat-house-excalibur.response.txt (write a .tmp, then mv). The run
# blocks on the seat like on any slow model — same rules, same
# --max-attempts, same failure isolation. The champion seed (3rd arg)
# installs on the three NIM houses only; the live seat takes the stock
# starter — a real player earns their own script.
#
# Ports are the same fixed trio every run (see stone-run.sh history).
set -euo pipefail

REPO=/home/ice52/git/econ.me
PY=$REPO/.venv/bin/python

N=${1:?usage: excal-run.sh <run-number> [rounds] [champion.lua]}
ROUNDS=${2:-40}
SEED=${3:-}
OUT=$HOME/econ-runs/stone-run$N
PORT=8925      # world API
DASH=8129      # dashboard (served by the run itself)
LAN=8130       # LAN sidecar for $OUT

MODELS=(openai/gpt-oss-20b openai/gpt-oss-20b openai/gpt-oss-20b)
NAMES=("House Ivar" "House Lagertha" "House Harald" "House Excalibur")
LIVE="House Excalibur"

mkdir -p "$OUT"
cd "$REPO"

EXTRA=(--live-seat "$LIVE")
if [ -n "$SEED" ]; then
  EXTRA+=(--seed-script "$SEED")
fi
# `resume` as the fourth word continues an interrupted exhibition run.
if [ "${4:-}" = "resume" ]; then
  EXTRA+=(--resume)
fi

# Pre-flight: refuse if a previous run's orphans still hold the ports
# (same trap stone-run.sh guards against).
for P in "$PORT" "$DASH" "$LAN"; do
  if ss -tlnH "sport = :$P" 2>/dev/null | grep -q .; then
    echo "refusing to launch: port $P is already held:" >&2
    ss -tlnp "sport = :$P" 2>/dev/null >&2
    echo "kill the stale holder or archive the previous run first" >&2
    exit 1
  fi
done
setsid nohup "$PY" -m experiments.agent.nim_run \
  --models "${MODELS[@]}" \
  --names "${NAMES[@]}" \
  "${EXTRA[@]}" \
  --scenario stone_age --rounds "$ROUNDS" --ticks-per-round 24 \
  --edit-mode --diary --port "$PORT" --serve "$DASH" --out "$OUT" \
  > "$OUT/run.log" 2>&1 &

cd "$OUT"
ln -sfn dashboard.html "$OUT/index.html"
setsid nohup "$PY" -m http.server "$LAN" --bind 0.0.0.0 > side.log 2>&1 &

sleep 5
echo "exhibition run $N: rounds=$ROUNDS world=:$PORT dash=:$DASH out=$OUT"
echo "live seat: watch $OUT/seat-house-excalibur.prompt.md"
echo "LAN dashboard (fixed port, every run): http://192.168.8.184:$LAN/"
