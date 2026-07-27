#!/usr/bin/env bash
#
# healthcheck.sh - is TaskHome actually working?
#
# Exits 0 when healthy, 1 otherwise, so it can drive a monitor or a systemd
# watchdog. Checks more than "the port answers": a process that is serving
# pages while its scheduler thread has died is exactly the failure that went
# unnoticed for a week before P0-1 was found.
#
# The app answers that question itself at /api/health (P6-4) and returns a
# non-200 when something needs a human. This script prefers that, and falls
# back to inspecting files directly when the endpoint is unreachable — which is
# itself the most important case, since a process that is not listening cannot
# report on its own health.
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
failures=0

say() { [[ "$QUIET" == "1" ]] || echo "$@"; }
ok()   { say "  ✓ $1"; }
bad()  { say "  ✗ $1"; failures=$((failures + 1)); }

# Prefer the interpreter the app runs under; fall back to whatever is on PATH.
PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"

say "TaskHome health — $URL"

response="$(curl -s --max-time 5 -w $'\n%{http_code}' "$URL/api/health" 2>/dev/null)"
code="$(printf '%s' "$response" | tail -n1)"
payload="$(printf '%s' "$response" | sed '$d')"

if [[ "$code" == "200" || "$code" == "503" ]]; then
    ok "web responding"

    # The app has already decided. Report what it said rather than re-deriving
    # it from log timestamps and scraped HTML, which is what this used to do.
    if [[ -n "$PY" ]]; then
        summary="$(printf '%s' "$payload" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(2)

if d["printer"]["connected"]:
    print("OK printer connected")
else:
    print("NOTE printer not connected (receipts queue and retry)")

s = d["scheduler"]
age = s.get("age_seconds")
line = "OK scheduler " + s["status"]
if age is not None:
    line += " (last tick " + str(age) + "s ago)"
print(line)

q = d["queue"]
print("OK queue: " + str(q["waiting"]) + " waiting, " + str(q["parked"]) + " parked")

for p in d.get("problems", []):
    print("PROBLEM " + p)
' 2>/dev/null)"

        if [[ -n "$summary" ]]; then
            while IFS= read -r line; do
                case "$line" in
                    "PROBLEM "*) bad "${line#PROBLEM }" ;;
                    "NOTE "*)    say "  · ${line#NOTE }" ;;
                    "OK "*)      ok "${line#OK }" ;;
                esac
            done <<< "$summary"
        else
            say "  · could not parse /api/health output"
        fi
    fi

    # Belt and braces: honour the status code even if parsing produced nothing.
    if [[ "$code" == "503" && $failures -eq 0 ]]; then
        bad "app reports unhealthy"
    fi
else
    bad "web not responding (no answer from /api/health)"

    # Fall back to what can be checked from outside the process. A store that
    # failed to load is write-blocked, which is silent from the outside but
    # means changes are being discarded.
    for store in config tasks history listeners queue; do
        file="$DATA_DIR/$store.json"
        if [[ ! -f "$file" ]]; then
            say "  · $store.json absent (fine on a fresh install)"
        elif [[ -n "$PY" ]] && "$PY" -c 'import json,sys; json.load(open(sys.argv[1]))' "$file" 2>/dev/null; then
            ok "$store.json parses"
        else
            bad "$store.json is corrupt — writes to it are blocked"
        fi
    done
fi

if (( failures )); then
    say "UNHEALTHY ($failures problem(s))"
    exit 1
fi
say "healthy"
