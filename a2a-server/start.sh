#!/usr/bin/env bash
# One-shot start for Ubuntu: creates venv, installs deps, runs the server.
set -euo pipefail
cd "$(dirname "$0")"
[ -f server.yaml ] || cp server.example.yaml server.yaml
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python server.py
