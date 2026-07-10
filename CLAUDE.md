# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**bills** is a unified bill downloader with a plugin/addon architecture. Each provider (Vodafone, Cursor, Proton VPN) is implemented as a subclass of `Addon` in `bills/addons/`. A built-in cron scheduler runs addons on configurable schedules, downloading invoices and sending email notifications for new files.

**Tech Stack:** Python 3.12+, Playwright (Chromium, in-process), Flask (server-rendered), SQLite (WAL mode)

## Common Commands

```bash
# Development
python -m bills schedule          # Start web UI + scheduler (default)
python -m bills web               # Web UI only
python -m bills run cursor        # Run single addon once
python -m bills run               # Run all enabled addons once
python -m bills list              # List registered addons

# Production (containerized)
entrypoint.sh schedule            # git pull, pip install, playwright install, run
```

**No tests or linting configured** — manual testing via `python -m bills run <addon>`.

## Architecture

### Addon System

All providers subclass `Addon` from `core/addon.py`:

```python
class VodafoneAddon(Addon):
    def run(self) -> list[Invoice]:
        # 1. Launch browser (self.browser())
        # 2. Navigate, authenticate, download
        # 3. Return list[Invoice] objects
```

- Addons are registered in `addons/__init__.py` REGISTRY dict
- Shared utilities in `core/`: `browser.py` (Playwright launcher), `mailer.py` (SMTP), `flaresolverr.py` (Cloudflare bypass)
- Run via subprocess: `python -m bills run <addon>` (coordinated by `RunManager` in `runner.py`)

### Configuration Resolution (3-tier priority)

`/config/settings.json` (web UI editable) → Environment variables → Defaults

- `Config` dataclass in `config.py` handles resolution
- Secrets masked in web UI via `SECRET_KEYS` set
- Per-addon schedules stored in SQLite (`schedules` table), legacy `schedule.json` migrated on boot

### SQLite as Single Source of Truth

Database at `/config/bills.db` (WAL mode for concurrent access):

| Table         | Purpose |
|---------------|---------|
| `invoices`    | One row per known invoice (addon, key, filename, dates, file path, sha256) |
| `mail_events` | SMTP send history per invoice |
| `runs`        | Addon run log (started/finished, exit code, trigger, log summary) |
| `schedules`   | Per-addon cron expressions |
| `settings`    | Optional operational key/value store |
| `schema_meta` | Migration markers |

**First startup migration:** `.manifest.json` files → `invoices`, orphan PDFs scanned, `schedule.json` → `schedules`.

### Browser Automation Patterns

- Playwright Chromium launched **in-process** per-addon run (no external Selenium Grid)
- Downloads captured via `page.expect_download()` API
- Cookie injection support for session-based auth (Cursor, Proton fallback)
- FlareSolverr integration for Cloudflare Turnstile challenges
- `BILLS_HEADLESS=true` for headless mode

### Web UI

Flask serves HTML/CSS/JS inline in `web.py` (~28KB single file, no build process):

- Routes: Dashboard (live status/logs), Invoices (table with mail status), Config (settings + schedules + web auth), Send Mail
- Optional session-based login via `web_auth.py` (enable with `BILLS_WEB_USERNAME`/`BILLS_WEB_PASSWORD`)
- ProxyFix support for HTTPS behind Cloudflare/Traefik

### Run Management

`RunManager` in `runner.py` coordinates:
- Web UI manual triggers and scheduler runs
- Thread-safe status tracking with live log capture
- Subprocess isolation for addon runs
- Git pull before runs (auto-update from repo)

## Adding a New Provider

1. Create `bills/addons/<provider>.py` with a class inheriting `Addon`
2. Implement `run(self) -> list[Invoice]`
3. Add to `addons/__init__.py` REGISTRY
4. Add env vars to `.env.example` and `config.py` `Config` class
5. Update README.md addon table

See existing addons (`vodafone.py`, `cursor.py`, `proton.py`) for patterns.

## Key Files

| File | Purpose |
|------|---------|
| `__main__.py` | CLI entry point (schedule|web|run|list commands) |
| `config.py` | Environment + settings.json configuration |
| `db.py` | SQLite schema, migrations, CRUD operations |
| `store.py` | InvoiceStore facade for database invoice tracking |
| `scheduler.py` | Cron-based scheduler loop |
| `runner.py` | Shared background run manager (web + scheduler) |
| `web.py` | Flask web UI (all HTML/CSS/JS inline) |
| `core/addon.py` | Base Addon class with common patterns |
| `core/browser.py` | Playwright Chromium launcher & utilities |
| `core/mailer.py` | SMTP mailer with PDF attachments |
| `core/flaresolverr.py` | FlareSolverr client for Cloudflare challenges |

## Deployment

Containerized on TrueNAS SCALE as a single custom app:

- Bootstrap script clones repo into `/app`
- Mounts: `/zfs/bills → /downloads`, `/zfs/bills/config → /config`
- Port: host `8512` → container `8080` (web UI)
