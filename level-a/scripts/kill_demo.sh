#!/usr/bin/env bash
# The kill beat (spec "Demo script", 1:00): the worker dies between asking
# ElevenLabs for the call to Corner Mart and saving the answer, the phone rings
# once, the claim lands while the worker is down, and the restarted worker
# adopts the attempt from the evidence instead of dialling again.
#
#   scripts/kill_demo.sh [--recall-id RCL] [--api URL] [--no-release] [--workers N]
#
# 1. python scripts/demo.py kill-arm: arms the after_dial_request failpoint for
#    corner-mart through the API, releases Corner Mart and (unless
#    --no-release) Queen St Grocer's held reply, whose claim names Corner Mart.
#    It refuses while another worker is alive: stop the supervisor first.
# 2. Runs the worker in the foreground. When it dials Corner Mart the failpoint
#    kills it with exit code 137 (os._exit, like kill -9).
# 3. Counts the kill as the supervisor would (python -m levela count-kill:
#    runs.kills + 1 and "worker died with exit 137 (n)" on the wall), prints a
#    banner and waits for Enter while the phone rings and the teammate answers
#    "received 25, all still on the shelf".
# 4. Restarts the worker in the foreground: the resume line and the adoption
#    appear on the wall. Ctrl+C stops it; then restart the supervisor.
#
# Settings (API_BASE_URL, ADMIN_TOKEN, ATLAS_URI, the phones) are read from
# level-a/.env by the Python side, exactly as the API and the worker read them.
# PYTHON picks the interpreter (default: python).
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"
WORKERS="${WORKERS:-4}"
RECALL_ID=""
# Options passed through to scripts/demo.py: global ones, then kill-arm's own.
demo_opts=()
arm_opts=()

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --recall-id) RECALL_ID="${2:?--recall-id needs a value}"; demo_opts+=(--recall-id "$RECALL_ID"); shift 2 ;;
    --api) demo_opts+=(--api "${2:?--api needs a value}"); shift 2 ;;
    --no-release) arm_opts+=(--no-release); shift ;;
    --workers) WORKERS="${2:?--workers needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

worker_cmd=("$PYTHON" -m levela worker --workers "$WORKERS")
count_cmd=("$PYTHON" -m levela count-kill --exit-code 137)
if [ -n "$RECALL_ID" ]; then
  worker_cmd+=(--recall "$RECALL_ID")
  count_cmd+=(--recall "$RECALL_ID")
fi

banner() {
  printf '\n%s\n' "=================================================================="
  printf '  %s\n' "$@"
  printf '%s\n\n' "=================================================================="
}

# "${a[@]+"${a[@]}"}" expands an empty array to nothing under set -u, on
# every bash including macOS's 3.2.
"$PYTHON" scripts/demo.py ${demo_opts[@]+"${demo_opts[@]}"} kill-arm ${arm_opts[@]+"${arm_opts[@]}"}

banner "Starting the worker in the foreground." \
       "It dies the moment it has asked for the call to Corner Mart."
code=0
"${worker_cmd[@]}" || code=$?

if [ "$code" -ne 137 ]; then
  banner "The worker exited with code $code, not 137: the failpoint did not fire." \
         "Check the wall (python scripts/demo.py status) and the failpoint (python -m levela status)."
  [ "$code" -eq 0 ] && code=1
  exit "$code"
fi

# The supervisor is stopped for this beat, so count the kill the way it would:
# the wall's "Kills survived" and the restarted worker's "restart n" line must
# include the kill the room just watched. A failure here must not stop the beat.
"${count_cmd[@]}" || echo "Could not count the kill on the wall (see above); carrying on." >&2

banner "WORKER KILLED (exit 137) by failpoint after_dial_request." \
       "The call to Corner Mart was requested and never saved." \
       "Let the phone ring; the teammate answers \"received 25, all on the shelf\"." \
       "Watch the wall: the claim lands while the worker is down." \
       "Press Enter to restart the worker."
read -r _

banner "Restarting the worker: it reads what is due and what is in doubt," \
       "adopts the attempt from the evidence and does not dial again." \
       "Ctrl+C stops it; then restart the supervisor (make supervisor)."
exec "${worker_cmd[@]}"
