#!/usr/bin/env bash
# One-shot start for Linux / macOS.
set -euo pipefail
cd "$(dirname "$0")"
[ -f worker.yaml ] || { echo "missing worker.yaml (copy worker.example.yaml)"; exit 1; }
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q pyyaml
fi
exec .venv/bin/python worker.py
