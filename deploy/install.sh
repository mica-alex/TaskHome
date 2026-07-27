#!/usr/bin/env bash
#
# install.sh - install TaskHome as a service on this machine.
#
# Detects the platform and installs the right unit. Prints what it is about to
# do and asks before touching anything under /etc or ~/Library.
#
#   ./deploy/install.sh              # install
#   ./deploy/install.sh --uninstall  # remove
#   ./deploy/install.sh --dry-run    # show the steps only

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE=install
DRY=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) MODE=uninstall ;;
        --dry-run) DRY=1 ;;
        *) echo "usage: $0 [--uninstall] [--dry-run]" >&2; exit 2 ;;
    esac
done

run() {
    echo "  \$ $*"
    [[ "$DRY" == "1" ]] || "$@"
}

confirm() {
    [[ "$DRY" == "1" ]] && return 0
    read -r -p "$1 [y/N] " reply
    [[ "$reply" == "y" || "$reply" == "Y" ]]
}

case "$(uname -s)" in
  Linux)
    UNIT=/etc/systemd/system/taskhome.service
    RULES=/etc/udev/rules.d/99-taskhome-printer.rules
    if [[ "$MODE" == "uninstall" ]]; then
        echo "Removing the systemd service and udev rule."
        confirm "Proceed?" || exit 1
        run sudo systemctl disable --now taskhome || true
        run sudo rm -f "$UNIT" "$RULES"
        run sudo systemctl daemon-reload
        run sudo udevadm control --reload-rules
        echo "Removed. Data in $REPO/data was left alone."
        exit 0
    fi

    echo "This will install:"
    echo "  $UNIT   (edit User=/Group= first — it ships with CHANGE_ME)"
    echo "  $RULES  (non-root printer access)"
    echo
    if grep -q CHANGE_ME "$REPO/deploy/taskhome.service"; then
        echo "!! deploy/taskhome.service still contains CHANGE_ME."
        echo "   Set User=, Group= and WorkingDirectory= before installing."
        exit 1
    fi
    confirm "Proceed?" || exit 1
    run sudo cp "$REPO/deploy/taskhome.service" "$UNIT"
    run sudo cp "$REPO/deploy/99-taskhome-printer.rules" "$RULES"
    run sudo systemctl daemon-reload
    run sudo udevadm control --reload-rules
    run sudo udevadm trigger
    run sudo systemctl enable --now taskhome
    echo
    echo "Installed. Replug the printer so the new udev rule applies, then:"
    echo "  journalctl -u taskhome -f"
    echo "  ./deploy/healthcheck.sh"
    ;;

  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.micatechnologies.taskhome.plist"
    if [[ "$MODE" == "uninstall" ]]; then
        confirm "Unload and remove $PLIST?" || exit 1
        run launchctl unload -w "$PLIST" || true
        run rm -f "$PLIST"
        echo "Removed. Data in $REPO/data was left alone."
        exit 0
    fi
    if grep -q CHANGE_ME "$REPO/deploy/com.micatechnologies.taskhome.plist"; then
        echo "!! the plist still contains CHANGE_ME — set your username first."
        exit 1
    fi
    echo "This will install $PLIST"
    confirm "Proceed?" || exit 1
    run mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"
    run cp "$REPO/deploy/com.micatechnologies.taskhome.plist" "$PLIST"
    run launchctl unload -w "$PLIST" 2>/dev/null || true
    run launchctl load -w "$PLIST"
    echo
    echo "Loaded. Note port 5000 is AirPlay Receiver's on macOS; the plist"
    echo "sets TASKHOME_PORT=5001. Check with ./deploy/healthcheck.sh --url"
    echo "http://127.0.0.1:5001"
    ;;

  *)
    echo "Unsupported platform: $(uname -s)" >&2
    exit 1
    ;;
esac
