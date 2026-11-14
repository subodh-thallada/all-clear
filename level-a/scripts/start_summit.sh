#!/usr/bin/env bash
# Start Summit Foods' node: the same code as ours, its own database, its own
# API server and two workers (spec "Sub-recall, agent to agent").
#
#   NODE_ID=summit MONGODB_DB=levela_summit PORT=8200 PARENT_URL=http://127.0.0.1:8000 scripts/start_summit.sh
#
# Settings (nothing else differs between the two nodes):
#   NODE_ID      this node's name (default summit)
#   MONGODB_DB   its database on the same cluster (default levela_summit)
#   PORT         its API port (default 8200)
#   PARENT_URL   our node's API (default http://127.0.0.1:8000)
# The pairing's secret comes from AGENT_LINK_SECRET (or LINK_SECRET_MAPLE_FARM)
# in the environment or .env, the same value on both nodes; it is never stored.
# NODE_SIM_RATE sets simulated hours per real second for Summit's run (default 1).
# PYTHON picks the interpreter (default: python3). Ctrl+C stops everything.
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
export NODE_ID="${NODE_ID:-summit}"
export MONGODB_DB="${MONGODB_DB:-levela_summit}"
export ATLAS_DB="$MONGODB_DB"          # levela.config reads the database name from ATLAS_DB
export PORT="${PORT:-8200}"
export PARENT_URL="${PARENT_URL:-http://127.0.0.1:8000}"
export API_BASE_URL="http://127.0.0.1:${PORT}"
# Summit's node has no distributor order systems and no run of its own to check.
export SCHEDULER_ENABLED="${SCHEDULER_ENABLED:-0}"
export VOICE_PROVIDER="${VOICE_PROVIDER:-sim}"

if [ -z "${AGENT_LINK_SECRET:-}" ] && [ -z "${LINK_SECRET_MAPLE_FARM:-}" ] && ! grep -qs '^AGENT_LINK_SECRET=.\+' .env; then
  echo "start_summit: set AGENT_LINK_SECRET (the same value on both nodes)" >&2
  exit 2
fi

echo "Summit Foods node: database $MONGODB_DB, port $PORT, parent $PARENT_URL"
"$PYTHON" -m levela.node seed

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

WORKER_NAME=summit-worker-1 "$PYTHON" -m levela.node work & pids+=($!)
WORKER_NAME=summit-worker-2 "$PYTHON" -m levela.node work & pids+=($!)
"$PYTHON" -m uvicorn levela.api.main:create_app --factory --host 0.0.0.0 --port "$PORT" & pids+=($!)
wait -n "${pids[@]}"
