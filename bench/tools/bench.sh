#!/usr/bin/env bash
# Bench commands, relayed to the bench page (index.html) that owns the serial port.
#   bench.sh mark <text>                  write "[bench] MARK <text>" into the capture
#   bench.sh since <text>                 capture lines since that mark (ROM noise filtered)
#   bench.sh reset <id>                   EN pulse (RTS) through the page
#   bench.sh arm <id> <regex> [delay_ms]  EN pulse when a console line matches
# The page must hold the UART port (console or loader mode), or commands wait in the queue.
set -euo pipefail
BASE=http://localhost:${BENCH_PORT:-8080}
cmd() { curl -sf -X POST -H 'Content-Type: application/json' --data "$1" "$BASE/api/cmd" >/dev/null \
          || { echo "bench.sh: bench server unreachable at $BASE" >&2; exit 1; }; }
# The capture path comes from the running server (BENCH_LOGS is its setting, not ours).
capture() {
  local log
  log=$(curl -sf "$BASE/api/info" | python3 -c 'import json,sys; print(json.load(sys.stdin)["capture"])') \
    || { echo "bench.sh: bench server unreachable at $BASE" >&2; exit 1; }
  [ -f "$log" ] || { echo "bench.sh: capture $log does not exist (no line received yet?)" >&2; exit 1; }
  echo "$log"
}
case "${1:-}" in
  # Acknowledged by the page: a later "since <mark>" never races the mark itself.
  mark)  python3 "$(dirname "$0")/benchctl.py" mark "$2" --timeout "${BENCH_MARK_TIMEOUT:-30}" >/dev/null \
           || { echo "bench.sh: mark '$2' not acknowledged by the page" >&2; exit 1; } ;;
  since) LOG=$(capture); grep -qF "MARK $2" "$LOG" || { echo "bench.sh: mark '$2' not found in $LOG" >&2; exit 1; }
         awk -v m="MARK $2" 'index($0,m){p=1} p' "$LOG" | grep -av "load:0x\|Build:Mar\|SPIWP\|mode:DIO" || true ;;
  reset) cmd "{\"op\":\"reset\",\"id\":\"$2\"}" ;;
  arm)   cmd "{\"op\":\"arm\",\"id\":\"$2\",\"pattern\":\"$3\",\"delay_ms\":${4:-0}}" ;;
  *)     sed -n '2,7p' "$0"; exit 2 ;;
esac
