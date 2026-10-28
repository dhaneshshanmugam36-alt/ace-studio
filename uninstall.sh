#!/usr/bin/env bash
# Removes Ace Studio. Your copied clips in ~/Videos/Ace Pro 2 are left alone.
set -euo pipefail
rm -rf "$HOME/.local/share/ace-studio"
rm -f "$HOME/.local/bin/ace-studio" "$HOME/.local/share/applications/ace-studio.desktop"
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/ace-studio"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Ace Studio removed. Your clips in ~/Videos/Ace Pro 2 were not touched."
