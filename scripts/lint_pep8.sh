#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python -m flake8 app tests scripts *.py
