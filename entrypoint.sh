#!/usr/bin/env bash
# Runs inside the TrueNAS bills app container after the repo is cloned to /app.
set -e

APP_DIR="${BILLS_APP_DIR:-/app}"
cd "$APP_DIR"

if ! command -v git >/dev/null 2>&1; then
  echo "[entrypoint] git not found; installing..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y --no-install-recommends git
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache git
  else
    echo "[entrypoint] no supported package manager found to install git"
    exit 1
  fi
fi

if [ -d "$APP_DIR/.git" ]; then
  echo "[entrypoint] git pull..."
  git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
  git pull --ff-only || echo "[entrypoint] git pull failed (continuing with current code)"
fi

echo "[entrypoint] installing requirements..."
pip install --no-cache-dir -r requirements.txt

echo "[entrypoint] ensuring Playwright Chromium..."
if [ ! -d "${PLAYWRIGHT_BROWSERS_PATH:-/root/.cache/ms-playwright}" ] || \
   ! ls "${PLAYWRIGHT_BROWSERS_PATH:-/root/.cache/ms-playwright}"/chromium-* >/dev/null 2>&1; then
  playwright install --with-deps chromium
else
  echo "[entrypoint] Playwright Chromium already present"
fi

echo "[entrypoint] starting: python -m bills ${*:-schedule}"
exec python -m bills "${@:-schedule}"
