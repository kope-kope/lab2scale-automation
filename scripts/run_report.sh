#!/usr/bin/env bash
# Compile and send the weekly intelligence brief. Used by local cron / Railway.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Pass through any extra args (e.g. --dry-run) so the same script works for
# both real sends and local previews.
exec python main.py report "$@"
