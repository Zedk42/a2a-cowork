@echo off
rem One-shot start for Windows. Save this file in ANSI encoding if edited with Chinese text.
cd /d %~dp0
rem The venv comes BEFORE the worker.yaml check: a first run without worker.yaml
rem still exits with an error, but leaves the interpreter api.py join needs.
if not exist .venv (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo python not found - install Python 3 with the py launcher from python.org
    exit /b 1
  )
  .venv\Scripts\pip install -q pyyaml
)
if not exist worker.yaml (
  echo missing worker.yaml ^(copy worker.example.yaml^)
  exit /b 1
)
.venv\Scripts\python worker.py
