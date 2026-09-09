#!/bin/bash
cd "$(dirname "$0")" && exec .venv/bin/python setup.py "$@"
