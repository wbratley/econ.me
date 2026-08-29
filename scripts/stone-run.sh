#!/usr/bin/env bash
# Launch a stone-age dynasty run, detached, with its LAN dashboard sidecar.
#
# usage: scripts/stone-run.sh <run-number> [rounds]
#   e.g.: scripts/stone-run.sh 14          # 40 rounds (default)
#         scripts/stone-run.sh 14 20       # 20 rounds
#         scripts/stone-run.sh 14 40 resume # continue run 14 after a crash
#
# Ports are allocated per run number -- keep this table current:
#   run 10 -> 8915/8109/8110   run 11 -> 8916/8111/8112
#   run 12 -> 8917/8113/8114   run 13 -> 8917/8113/8114 (reused after reboot)
#   run 14 -> 8918/8115/8116   run 15 -> 8919/8117/8118
#   run 16 -> 8920/8119/8120   run 17 -> 8921/8121/8122 (defaults)
set -euo pipefail

REPO=/home/ice52/git/econ.me
PY=$REPO/.venv/bin/python

N=${1:?usage: stone-run.sh <run-number> [rounds]}
ROUNDS=${2:-40}
# Under $HOME, not /tmp: a reboot wiped /tmp mid-run-15 (attempt 5 died ~12
# minutes into its 40 rounds). $HOME survives.
OUT=$HOME/econ-runs/stone-run$N
PORT=8921      # world API
DASH=8121      # dashboard (served by the run itself)
LAN=8122       # LAN sidecar for $OUT
MODELS=(openai/gpt-oss-20b openai/gpt-oss-20b openai/gpt-oss-20b)
# (Seat history: llama-3.3-70b EOL'd 2026-08-26; nemotron-3-super played 3
#  lukewarm rounds; llama-3.2-90b-vision brains-but-glacial (>15min calls
#  at 6KB prompts). gpt-oss-20b won the audition: careful reasoning, 21s
#  calls, and its 32k completion budget is already wired in llm.py.)

# Distinct Norse house names per seat (edit to taste).
NAMES=("House Ivar" "House Lagertha" "House Harald")

mkdir -p "$OUT"
cd "$REPO"

# `resume` as the third word continues the interrupted run in $OUT: its
# world.db and seats survive the runner dying (a reboot took run 16 down
# at round 8 of 40); rounds already on disk are skipped, journals append.
EXTRA=()
if [ "${3:-}" = "resume" ]; then
  EXTRA+=(--resume)
fi

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
  --scenario stone_age --rounds "$ROUNDS" --ticks-per-round 24 \
  --edit-mode --diary --port "$PORT" --serve "$DASH" --out "$OUT" \
  "${EXTRA[@]}" \
  > "$OUT/run.log" 2>&1 &

cd "$OUT"
setsid nohup "$PY" -m http.server "$LAN" --bind 0.0.0.0 > side.log 2>&1 &

sleep 5
echo "run $N: rounds=$ROUNDS world=:$PORT dash=:$DASH out=$OUT"
echo "LAN dashboard: http://192.168.8.184:$LAN/dashboard.html"
echo "watch: tail -f $OUT/run.log   |   health: pgrep -af nim_run"
