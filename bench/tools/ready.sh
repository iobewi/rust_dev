#!/usr/bin/env bash
# ready.sh <mark>: wait until the device is serving after the LAST boot since <mark>.
# Ready = READY_RE seen after the last "rst:" AND https://$BENCH_OTA_HOST/health returns 200
# (TLS verified with $BENCH_CA). Never trusts a stale ready line from an earlier boot.
# With no boot since the mark it only waits (up to 200 s) and never resets. Otherwise,
# when the last boot logs a Wi-Fi join failure (FAIL_RE), it EN-resets, at most 5 times.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); B=$HERE/bench.sh; TAG=$1
HOST=${BENCH_OTA_HOST:-iobewi-ota.test}
: "${BENCH_CA:?set BENCH_CA to the test CA PEM}"
READY_RE=${READY_RE:-OTA ready}
FAIL_RE=${FAIL_RE:-connection to .* failed\|STA join failed}
health() { [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 --cacert "$BENCH_CA" "https://$HOST/health")" = 200 ]; }
# Abort on a missing capture or mark: resetting the board on unread logs would be blind.
$B since "$TAG" >/dev/null || { echo "ready.sh: cannot read the capture since mark '$TAG'" >&2; exit 1; }
last_boot() { $B since "$TAG" | awk '/rst:0x/{buf=""} {buf=buf $0 "\n"} END{printf "%s", buf}'; }
if ! last_boot | grep -q "rst:0x"; then
  for _ in $(seq 1 200); do
    health && { echo "health (no boot since mark)"; exit 0; }
    last_boot | grep -q "rst:0x" && break; sleep 1
  done
  last_boot | grep -q "rst:0x" || { echo "NO HEALTH, no reset done"; exit 1; }
fi
for attempt in 1 2 3 4 5; do
  for _ in $(seq 1 90); do
    out=$(last_boot)
    if grep -q "$READY_RE" <<<"$out" && health; then echo "ready+health (attempt $attempt)"; exit 0; fi
    grep -q "$FAIL_RE" <<<"$out" && break
    sleep 1
  done
  $B reset "$TAG-r$attempt"; sleep 2
done
echo "NOT READY"; exit 1
