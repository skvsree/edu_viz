#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -x .venv/bin/python ]; then
  PYTHON_BIN=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python)
else
  echo "No Python interpreter found for flake8." >&2
  exit 1
fi

"$PYTHON_BIN" -m flake8 app tests scripts *.py
