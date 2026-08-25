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
#   run 14 -> 8918/8115/8116   (defaults below; edit before your next run)
set -euo pipefail

REPO=/home/ice52/git/econ.me
PY=$REPO/.venv/bin/python

N=${1:?usage: stone-run.sh <run-number> [rounds]}
ROUNDS=${2:-40}
OUT=/tmp/stone-run$N
PORT=8918      # world API
DASH=8115      # dashboard (served by the run itself)
LAN=8116       # LAN sidecar for $OUT
MODELS=(meta/llama-3.3-70b-instruct meta/llama-3.3-70b-instruct meta/llama-3.3-70b-instruct)

# Distinct Norse house names per seat (edit to taste).
NAMES=("House Ivar" "House Lagertha" "House Harald")

mkdir -p "$OUT"
cd "$REPO"
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
