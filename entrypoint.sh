#!/usr/bin/env bash
# Runs inside the TrueNAS bills app container after the repo is cloned to /app.
# Pulls the latest code, installs deps, then starts the requested command
# (defaults to the scheduler).
set -e

APP_DIR="${BILLS_APP_DIR:-/app}"
cd "$APP_DIR"

if [ -d "$APP_DIR/.git" ]; then
  echo "[entrypoint] git pull..."
  git pull --ff-only || echo "[entrypoint] git pull failed (continuing with current code)"
fi

echo "[entrypoint] installing requirements..."
pip install --no-cache-dir -r requirements.txt

echo "[entrypoint] starting: python -m bills ${*:-schedule}"
exec python -m bills "${@:-schedule}"
