#!/usr/bin/env bash
# One-shot local setup — creates a venv, installs deps, initializes the DB.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "==> Creating virtualenv at .venv"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing requirements"
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
  echo "==> Copying .env.example -> .env (you still need to fill ANTHROPIC_API_KEY)"
  cp .env.example .env
fi

echo "==> Initializing the database"
python main.py init-db

echo
echo "Setup complete. Activate the venv with:"
echo "  source .venv/bin/activate"
echo "Then try a preview:"
echo "  python main.py full --dry-run"
