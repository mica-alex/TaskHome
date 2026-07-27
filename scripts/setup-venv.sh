#!/usr/bin/env bash
#
# TaskHome environment bootstrap / self-repair.
#
# Creates .venv if missing, and REBUILDS it if it has become unusable -- most
# commonly because the interpreter it was built against was deleted or moved.
# That is exactly how this venv broke once already: it was built against
# /Applications/Xcode-16.1.0-Beta.app, which was later removed, leaving every
# .venv/bin/python* as a dangling symlink.
#
# Usage:
#   ./scripts/setup-venv.sh            # create or repair as needed, then sync deps
#   ./scripts/setup-venv.sh --check    # report health only, change nothing (exit 1 if unhealthy)
#   ./scripts/setup-venv.sh --force    # discard and rebuild unconditionally
#
# Safe to run repeatedly. Never touches the JSON data files.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
REQUIREMENTS="$REPO_ROOT/requirements.txt"
MIN_MINOR=9 # minimum Python 3.x we accept

MODE="repair"
case "${1:-}" in
    --check) MODE="check" ;;
    --force) MODE="force" ;;
    "") ;;
    *)
        echo "usage: $0 [--check|--force]" >&2
        exit 2
        ;;
esac

# --- pretty output ------------------------------------------------------------
if [[ -t 1 ]]; then
    B=$'\033[1m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; D=$'\033[2m'; X=$'\033[0m'
else
    B=""; R=""; G=""; Y=""; D=""; X=""
fi
ok()   { echo "  ${G}✓${X} $*"; }
warn() { echo "  ${Y}!${X} $*"; }
bad()  { echo "  ${R}✗${X} $*"; }
info() { echo "  ${D}·${X} $*"; }

# --- interpreter discovery ----------------------------------------------------
# Ordered best-first. Homebrew interpreters come first because they are
# independent of Xcode / Command Line Tools and therefore survive the failure
# mode this script exists to recover from. Xcode-anchored interpreters are
# accepted only as a last resort, and warned about.
candidate_interpreters() {
    local brew_prefix=""
    if command -v brew >/dev/null 2>&1; then
        brew_prefix="$(brew --prefix 2>/dev/null || true)"
    fi
    [[ -z "$brew_prefix" && -x /opt/homebrew/bin/brew ]] && brew_prefix=/opt/homebrew
    [[ -z "$brew_prefix" && -x /usr/local/bin/brew ]] && brew_prefix=/usr/local

    if [[ -n "$brew_prefix" ]]; then
        # Newest Homebrew Python first (3.14, 3.13, ... 3.9).
        local minor
        for minor in 14 13 12 11 10 9; do
            echo "$brew_prefix/bin/python3.$minor"
        done
    fi

    # Distro / pyenv / anything else on PATH.
    local minor
    for minor in 14 13 12 11 10 9; do
        command -v "python3.$minor" 2>/dev/null || true
    done

    # macOS Command Line Tools: stable across Xcode.app renames and deletions.
    echo /Library/Developer/CommandLineTools/usr/bin/python3

    # Generic fallbacks. On macOS /usr/bin/python3 dispatches through
    # `xcode-select -p`, so it can point into an Xcode.app that may vanish.
    command -v python3 2>/dev/null || true
    echo /usr/bin/python3
}

# Echoes a usable interpreter path, or returns 1.
find_interpreter() {
    local candidate seen=""
    while read -r candidate; do
        [[ -z "$candidate" ]] && continue
        [[ -x "$candidate" ]] || continue
        # de-duplicate
        case "$seen" in *"|$candidate|"*) continue ;; esac
        seen="$seen|$candidate|"
        # Must actually execute and meet the minimum version.
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, $MIN_MINOR) else 1)" \
            >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done < <(candidate_interpreters)
    return 1
}

# Warn if the chosen interpreter lives somewhere historically unstable.
check_interpreter_stability() {
    local interp="$1" base
    base="$("$interp" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || echo "")"
    case "$base" in
        */Applications/Xcode*)
            warn "This interpreter resolves into ${B}$base${X}"
            warn "Xcode app bundles get renamed and deleted; that is what broke the venv before."
            warn "Prefer: ${B}brew install python@3.13${X}, then re-run this script with --force."
            ;;
    esac
}

# --- venv health --------------------------------------------------------------
# Returns 0 if the venv is usable, 1 otherwise. Prints the reason on failure.
venv_health() {
    if [[ ! -d "$VENV" ]]; then
        echo "no .venv directory"
        return 1
    fi
    if [[ ! -e "$VENV/bin/python" ]]; then
        # -e follows symlinks, so a dangling symlink fails here. That is the
        # deleted-interpreter case.
        if [[ -L "$VENV/bin/python" ]]; then
            echo "dangling interpreter symlink -> $(readlink "$VENV/bin/python")"
        else
            echo "missing .venv/bin/python"
        fi
        return 1
    fi
    if ! "$VENV/bin/python" -c 'import sys' >/dev/null 2>&1; then
        echo ".venv/bin/python will not execute"
        return 1
    fi
    # The recorded base interpreter may be gone even when bin/python still runs.
    local home
    home="$(sed -n 's/^home = //p' "$VENV/pyvenv.cfg" 2>/dev/null || true)"
    if [[ -n "$home" && ! -d "$home" ]]; then
        echo "base interpreter directory no longer exists: $home"
        return 1
    fi
    return 0
}

# Returns 0 if every direct dependency imports.
deps_health() {
    "$VENV/bin/python" - <<'PY' >/dev/null 2>&1
import importlib
for mod in ("flask", "escpos.printer", "usb.core", "requests", "dateutil.parser"):
    importlib.import_module(mod)
PY
}

rebuild_venv() {
    local interp="$1"
    if [[ -d "$VENV" ]]; then
        info "Removing unusable environment at .venv"
        rm -rf "$VENV"
    fi
    info "Creating .venv with $interp ($("$interp" --version 2>&1))"
    "$interp" -m venv "$VENV"
    ok "Virtual environment created"
}

# --- main ---------------------------------------------------------------------
echo
echo "${B}TaskHome environment${X}  ${D}$REPO_ROOT${X}"
echo

# 1. Environment health
health_reason="$(venv_health || true)"
if [[ -z "$health_reason" ]]; then
    ok "Virtual environment is healthy ($("$VENV/bin/python" --version 2>&1))"
    healthy=1
else
    bad "Virtual environment is broken: $health_reason"
    healthy=0
fi

if [[ "$MODE" == "check" ]]; then
    if [[ "$healthy" == "1" ]] && deps_health; then
        ok "All dependencies import cleanly"
        echo
        exit 0
    fi
    [[ "$healthy" == "1" ]] && bad "One or more dependencies fail to import"
    echo
    echo "  Run ${B}./scripts/setup-venv.sh${X} to repair."
    echo
    exit 1
fi

# 2. Repair or build
if [[ "$healthy" == "0" || "$MODE" == "force" ]]; then
    [[ "$MODE" == "force" ]] && info "Forced rebuild requested"
    if ! interpreter="$(find_interpreter)"; then
        bad "No usable Python 3.$MIN_MINOR+ interpreter found."
        echo
        echo "  Install one, then re-run:"
        echo "    macOS:  ${B}brew install python@3.13${X}"
        echo "    Debian: ${B}sudo apt install python3 python3-venv${X}"
        echo
        exit 1
    fi
    ok "Using interpreter: $interpreter"
    check_interpreter_stability "$interpreter"
    rebuild_venv "$interpreter"
fi

# 3. Dependencies
info "Installing dependencies from requirements.txt"
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install --quiet -r "$REQUIREMENTS"
ok "Dependencies installed"

# 4. Verify
if deps_health; then
    ok "All dependencies import cleanly"
else
    bad "A dependency still fails to import:"
    "$VENV/bin/python" -c 'import escpos.printer, usb.core, flask, requests, dateutil.parser' || true
    exit 1
fi

# 5. Advisory: libusb backend (needed for printing, not for the web app)
if ! "$VENV/bin/python" - <<'PY' >/dev/null 2>&1
import usb.core
usb.core.find()
PY
then
    warn "pyusb has no USB backend -- printer detection will always report 'Not connected'."
    warn "The web app still runs fine. To enable printing:"
    warn "  macOS:  ${B}brew install libusb${X}"
    warn "  Debian: ${B}sudo apt install libusb-1.0-0${X}"
else
    ok "pyusb has a working USB backend"
fi

echo
echo "  ${B}Ready.${X} Start TaskHome with ${B}./scripts/run.sh${X}"
echo
