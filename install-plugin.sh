#!/bin/bash
# Symlink the Stream Deck plugin into place and restart Stream Deck.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)/streamdeck-plugin/com.jasonzeng.acdeck.sdPlugin"
DEST="$HOME/Library/Application Support/com.elgato.StreamDeck/Plugins/com.jasonzeng.acdeck.sdPlugin"

if [ ! -f "$SRC/manifest.json" ]; then
  echo "Plugin source not found at $SRC" >&2; exit 1
fi

rm -rf "$DEST"
ln -s "$SRC" "$DEST"
echo "Linked $DEST"

if pgrep -f "Elgato Stream Deck" >/dev/null; then
  echo "Restarting Stream Deck..."
  osascript -e 'quit app "Elgato Stream Deck"' 2>/dev/null || true
  sleep 3
fi
open -a "Elgato Stream Deck"
echo "Done. The actions appear under the 'Midea AC' category."
