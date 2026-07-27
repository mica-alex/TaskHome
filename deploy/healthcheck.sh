#!/usr/bin/env bash
#
# healthcheck.sh - is TaskHome actually working?
#
# Exits 0 when healthy, 1 otherwise, so it can drive a monitor or a systemd
# watchdog. Checks more than "the port answers": a process that is serving
# pages while its scheduler thread has died is exactly the failure that went
# unnoticed for a week before P0-1 was found.
#
#   ./deploy/healthcheck.sh [--url http://127.0.0.1:5000] [--quiet]

set -uo pipefail

URL="http://127.0.0.1:${TASKHOME_PORT:-5000}"
QUIET=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --url) URL="$2"; shift 2 ;;
        --quiet) QUIET=1; shift ;;
        *) echo "usage: $0 [--url URL] [--quiet]" >&2; exit 2 ;;
    esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${TASKHOME_DATA_DIR:-$REPO/data}"
LOG_DIR="${TASKHOME_LOG_DIR:-$REPO/logs}"
failures=0

say() { [[ "$QUIET" == "1" ]] || echo "$@"; }
ok()   { say "  ✓ $1"; }
bad()  { say "  ✗ $1"; failures=$((failures + 1)); }

say "TaskHome health — $URL"

# 1. Serving.
if curl -sf -o /dev/null --max-time 5 "$URL/"; then
    ok "web responding"
else
    bad "web not responding"
fi

# 2. State readable. A store that failed to load is write-blocked, which is
#    silent from the outside but means changes are being discarded.
for store in config tasks history listeners; do
    file="$DATA_DIR/$store.json"
    if [[ ! -f "$file" ]]; then
        say "  · $store.json absent (fine on a fresh install)"
    elif python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$file" 2>/dev/null; then
        ok "$store.json parses"
    else
        bad "$store.json is corrupt — writes to it are blocked"
    fi
done

# 3. The scheduler is alive. It logs each poll; silence for more than a few
#    minutes means the thread died while the web server kept answering.
LOG="$LOG_DIR/taskhome.log"
if [[ -f "$LOG" ]]; then
    age=$(( $(date +%s) - $(stat -f %m "$LOG" 2>/dev/null || stat -c %Y "$LOG") ))
    if (( age < 600 )); then
        ok "log written ${age}s ago"
    else
        bad "log untouched for ${age}s — the scheduler may have died"
    fi
else
    say "  · no log file yet"
fi

# 4. Printer. Absence is not fatal: occurrences queue and retry.
if curl -sf --max-time 5 "$URL/" | grep -q 'Not connected'; then
    say "  · printer not connected (receipts will retry)"
else
    ok "printer connected"
fi

if (( failures )); then
    say "UNHEALTHY ($failures problem(s))"
    exit 1
fi
say "healthy"
