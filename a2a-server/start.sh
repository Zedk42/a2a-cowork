#!/usr/bin/env bash
# One-key start for Linux: stops any previous instance, then runs the server
# in the background with the new pid recorded in a2a.pid. Usage: ./start.sh [stop]
set -euo pipefail
cd "$(dirname "$0")"
PID_FILE=a2a.pid
LOG=a2a-server.log

stop_old() {
  [ -f "$PID_FILE" ] || return 0
  local pid; pid=$(cat "$PID_FILE")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    echo "stopped old server (pid $pid)"
  fi
  rm -f "$PID_FILE"
}

if [ "${1:-start}" = stop ]; then
  stop_old
  exit 0
fi

stop_old   # a fresh start replaces the old instance
[ -f server.yaml ] || cp server.example.yaml server.yaml
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
nohup .venv/bin/python server.py >"$LOG" 2>&1 &
echo $! >"$PID_FILE"
sleep 1
if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "server running (pid $(cat "$PID_FILE"), log $LOG)"
else
  echo "server failed to start; last lines of $LOG:"; tail -5 "$LOG"; rm -f "$PID_FILE"; exit 1
fi
