#!/bin/bash
# Remove ScreenFind's background service and virtual environment.
#
#   ./uninstall.sh          keep your index and settings (~/.screenshot-search)
#   ./uninstall.sh --all    also delete the index and settings
#
# Your screenshots are never touched.
set -euo pipefail

DATA_DIR="$HOME/.screenshot-search"
LABEL="com.screenfind.server"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "✓ Background service removed"

rm -rf "$DATA_DIR/venv"
echo "✓ Virtual environment removed"

if [[ "${1:-}" == "--all" ]]; then
  rm -rf "$DATA_DIR"
  echo "✓ Index and settings deleted ($DATA_DIR)"
else
  echo "  Index and settings kept in $DATA_DIR (run with --all to delete them)"
fi
echo "  You can also remove the program from System Settings → Privacy & Security → Full Disk Access."
