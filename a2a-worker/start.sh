#!/usr/bin/env bash
# One-shot start for Linux / macOS.
set -euo pipefail
cd "$(dirname "$0")"
# The venv comes BEFORE the worker.yaml check: a first run without worker.yaml
# still exits with an error, but leaves the interpreter api.py join needs.
if [ ! -d .venv ]; then
  python3 -m venv .venv || { echo "venv creation failed — install python3 (plus python3-venv on Debian/Ubuntu)"; exit 1; }
  .venv/bin/pip install -q pyyaml
fi
[ -f worker.yaml ] || { echo "missing worker.yaml (copy worker.example.yaml)"; exit 1; }
exec .venv/bin/python worker.py
