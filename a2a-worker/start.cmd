@echo off
rem One-shot start for Windows. Save this file in ANSI encoding if edited with Chinese text.
cd /d %~dp0
if not exist worker.yaml (
  echo missing worker.yaml ^(copy worker.example.yaml^)
  exit /b 1
)
if not exist .venv (
  py -3 -m venv .venv
  .venv\Scripts\pip install -q pyyaml
)
.venv\Scripts\python worker.py
