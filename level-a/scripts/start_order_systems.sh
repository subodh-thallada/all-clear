#!/usr/bin/env bash
# Start the two mock distributor order systems (V2 section 7) and keep them running:
# Distributor North and Distributor East, one process each, so each has its own
# version switch and its own faults.
#
#   scripts/start_order_systems.sh [--scenario recall_long] [--host 127.0.0.1]
#                            [--north-port 8101] [--east-port 8102]
#
# The API reaches them at ORDER_NORTH_URL and ORDER_EAST_URL (default
# http://127.0.0.1:8101 and :8102): change those in .env if you change the
# ports. Each order system checks X-Admin-Token against ADMIN_TOKEN from .env, the
# same token the API sends when the operator presses "Switch order system to v2".
# Ctrl+C stops both; if one dies, the other is stopped too.
# PYTHON picks the interpreter (default: python).
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"
SCENARIO="recall_long"
HOST="127.0.0.1"
NORTH_PORT=8101
EAST_PORT=8102
READY_S=30

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --scenario) SCENARIO="${2:?--scenario needs a value}"; shift 2 ;;
    --host) HOST="${2:?--host needs a value}"; shift 2 ;;
    --north-port) NORTH_PORT="${2:?--north-port needs a value}"; shift 2 ;;
    --east-port) EAST_PORT="${2:?--east-port needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# An order system bound to every interface still answers on the loopback address.
PROBE_HOST="$HOST"
[ "$HOST" = "0.0.0.0" ] && PROBE_HOST="127.0.0.1"

pids=()
stop_all() {
  for pid in ${pids[@]+"${pids[@]}"}; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap stop_all EXIT
trap 'exit 130' INT TERM

start_order_system() {
  "$PYTHON" -m levela.mock.order_system --name "$1" --port "$2" --host "$HOST" --scenario "$SCENARIO" &
  pids+=("$!")
}

# Waits until the order system serves its OpenAPI document (the repairer reads it too).
wait_ready() {
  "$PYTHON" - "$1" "http://$PROBE_HOST:$2" "$READY_S" <<'PY'
import sys, time, urllib.request
name, base, limit = sys.argv[1], sys.argv[2], float(sys.argv[3])
deadline = time.monotonic() + limit
while time.monotonic() < deadline:
    try:
        urllib.request.urlopen(f"{base}/openapi.json", timeout=2).read()
        print(f"order system {name} is up at {base}")
        sys.exit(0)
    except OSError:
        time.sleep(0.5)
sys.exit(f"order system {name} did not answer {base}/openapi.json within {limit:.0f} s")
PY
}

start_order_system north "$NORTH_PORT"
start_order_system east "$EAST_PORT"
wait_ready north "$NORTH_PORT"
wait_ready east "$EAST_PORT"
echo "Both order systems are up (scenario $SCENARIO). Ctrl+C stops both."

while kill -0 "${pids[0]}" 2>/dev/null && kill -0 "${pids[1]}" 2>/dev/null; do
  sleep 1
done
echo "An order system stopped; stopping the other." >&2
exit 1
