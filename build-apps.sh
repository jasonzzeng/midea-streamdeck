#!/bin/bash
# Builds silent .app launchers that Stream Deck's System > Open action can run
# without flashing a Terminal window.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/streamdeck"
mkdir -p "$OUT"

build() {
  local name="$1" cmd="$2"
  rm -rf "$OUT/$name.app"
  osacompile -o "$OUT/$name.app" -e "do shell script \"$ROOT/bin/ac-$cmd.sh\"" 
  echo "  built $OUT/$name.app"
}

echo "Building Stream Deck launchers..."
build "AC On"     on
build "AC Off"    off
build "AC Toggle" toggle
echo "Done. In Stream Deck, drag 'System > Open' onto a key and point it at one of these apps."
