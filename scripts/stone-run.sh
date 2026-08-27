#!/usr/bin/env bash
# Launch a stone-age dynasty run, detached, with its LAN dashboard sidecar.
#
# usage: scripts/stone-run.sh <run-number> [rounds]
#   e.g.: scripts/stone-run.sh 14          # 40 rounds (default)
#         scripts/stone-run.sh 14 20       # 20 rounds
#
# Ports are allocated per run number -- keep this table current:
#   run 10 -> 8915/8109/8110   run 11 -> 8916/8111/8112
#   run 12 -> 8917/8113/8114   run 13 -> 8917/8113/8114 (reused after reboot)
#   run 14 -> 8918/8115/8116   run 15 -> 8919/8117/8118
#   (defaults below; edit before your next run)
set -euo pipefail

REPO=/home/ice52/git/econ.me
PY=$REPO/.venv/bin/python

N=${1:?usage: stone-run.sh <run-number> [rounds]}
ROUNDS=${2:-40}
OUT=/tmp/stone-run$N
PORT=8919      # world API
DASH=8117      # dashboard (served by the run itself)
LAN=8118       # LAN sidecar for $OUT
MODELS=(nvidia/nemotron-3-super-120b-a12b nvidia/nemotron-3-super-120b-a12b nvidia/nemotron-3-super-120b-a12b)
# (run 15's first attempt, llama-3.3-70b, died 410 EOL 2026-08-26 —
#  NIM retired the model between runs 14 and 15)

# Distinct Norse house names per seat (edit to taste).
NAMES=("House Ivar" "House Lagertha" "House Harald")

mkdir -p "$OUT"
cd "$REPO"

# Pre-flight: a port still held by a previous run's orphaned uvicorn
# (killing nim_run leaves the world server behind) means the new run's
# clients silently attach to the OLD world and die on its stale gate --
# run 15 attempt 2 lost 20 minutes to exactly that. Refuse instead.
for P in "$PORT" "$DASH" "$LAN"; do
  if ss -tlnH "sport = :$P" 2>/dev/null | grep -q .; then
    echo "refusing to launch: port $P is already held:" >&2
    ss -tlnp "sport = :$P" 2>/dev/null >&2
    echo "kill the stale process (e.g. pkill -f 'uvicorn.*--port $P') " \
         "or move this run to fresh ports" >&2
    exit 1
  fi
done
setsid nohup "$PY" -m experiments.agent.nim_run \
  --models "${MODELS[@]}" \
  --names "${NAMES[@]}" \
  --scenario stone_age --rounds "$ROUNDS" --ticks-per-round 20 \
  --edit-mode --diary --port "$PORT" --serve "$DASH" --out "$OUT" \
  > "$OUT/run.log" 2>&1 &

cd "$OUT"
setsid nohup "$PY" -m http.server "$LAN" --bind 0.0.0.0 > side.log 2>&1 &

sleep 5
echo "run $N: rounds=$ROUNDS world=:$PORT dash=:$DASH out=$OUT"
echo "LAN dashboard: http://192.168.8.184:$LAN/dashboard.html"
echo "watch: tail -f $OUT/run.log   |   health: pgrep -af nim_run"
