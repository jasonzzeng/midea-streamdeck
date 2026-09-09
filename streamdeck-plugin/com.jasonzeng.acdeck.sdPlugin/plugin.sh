#!/bin/bash
# Stream Deck launches this with -port/-pluginUUID/-registerEvent/-info.
cd "$(dirname "$0")"
exec ../../.venv/bin/python plugin.py "$@"
