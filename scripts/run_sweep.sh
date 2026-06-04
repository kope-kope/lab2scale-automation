#!/usr/bin/env bash
# Run a monitoring sweep. Used by local cron / systemd timers / Railway.
set -euo pipefail
cd "$(dirname "$0")/.."

# Activate the venv only if it exists — in Docker / Railway we run on the
# system Python already.
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

exec python main.py sweep
