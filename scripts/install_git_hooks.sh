#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/pre-push
printf 'Configured git hooks path: %s\n' "$(git config --get core.hooksPath)"
