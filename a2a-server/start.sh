#!/usr/bin/env bash
# One-key start for Linux. Runs the server in the background, records its pid
# to a2a.pid, and stops a previous instance first. Usage: ./start.sh [stop]
set -euo pipefail
cd "$(dirname "$0")"
PID_FILE=a2a.pid
LOG=a2a-server.log

if [ "${1:-start}" = stop ]; then
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$(cat "$PID_FILE")" 2>/dev/null || break; sleep 0.5; done
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
      sleep 1
      kill -0 "$(cat "$PID_FILE")" 2>/dev/null && { echo "cannot stop pid $(cat "$PID_FILE"); kill it manually"; exit 1; }
    fi
    echo "stopped (pid $(cat "$PID_FILE"))"
  fi
  rm -f "$PID_FILE"
  exit 0
fi

# a fresh start replaces any old instance
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  sleep 1
  echo "replaced old server (pid $(cat "$PID_FILE"))"
fi
rm -f "$PID_FILE"

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
