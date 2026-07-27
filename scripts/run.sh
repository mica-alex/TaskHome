#!/usr/bin/env bash
#
# Start TaskHome, repairing the virtual environment first if it has broken.
#
# This is the intended entry point for both interactive use and service
# managers (systemd / launchd), so that a machine which loses its Python
# interpreter -- an OS upgrade, a deleted Xcode, a moved Homebrew -- recovers on
# the next start instead of failing forever.
#
# Usage:
#   ./scripts/run.sh              # repair if needed, then start on port 5000
#   ./scripts/run.sh --no-repair  # fail fast instead of self-healing
#
# Set TASKHOME_NO_REPAIR=1 to disable self-repair via the environment.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"

REPAIR=1
[[ "${TASKHOME_NO_REPAIR:-0}" == "1" ]] && REPAIR=0
[[ "${1:-}" == "--no-repair" ]] && REPAIR=0

if ! "$REPO_ROOT/scripts/setup-venv.sh" --check >/dev/null 2>&1; then
    if [[ "$REPAIR" == "0" ]]; then
        echo "TaskHome: environment is unhealthy and self-repair is disabled." >&2
        echo "Run ./scripts/setup-venv.sh to fix it." >&2
        exit 1
    fi
    echo "TaskHome: environment is unhealthy; repairing before start..." >&2
    "$REPO_ROOT/scripts/setup-venv.sh"
fi

# exec so signals (and systemd's stop/restart) reach Python directly rather
# than this wrapper.
cd "$REPO_ROOT"
exec "$VENV/bin/python" app.py
