# bills

Unified bill downloader for multiple providers. Each provider is a small
**addon**; a built-in, env-configurable scheduler runs them on their own cron.
Playwright Chromium runs **in-process** inside the bills container (no external
Selenium Grid).

## Addons

| Addon      | Source                | Auth                              | Download path        |
|------------|-----------------------|-----------------------------------|----------------------|
| `vodafone` | MeinVodafone (Angular)| username + password               | `/downloads/vodafone`|
| `cursor`   | Stripe billing portal | session cookies (+ optional FlareSolverr) | `/downloads/cursor`  |
| `proton`   | Proton VPN subscription | username + password (session cookies fallback) | `/downloads/proton`  |

Invoices are saved as `YYYY-MM-DD <Provider> <number>.pdf`. Files are never
re-downloaded if they already exist, and an email is only sent for **new**
invoices. All invoice and mail metadata lives in SQLite (`/config/bills.db`).

## Layout

```
bills/
  entrypoint.sh             # clone/pull + pip + playwright install + run
  requirements.txt
  bills/
    __main__.py             # CLI: schedule | run <addon> | list
    config.py               # env + settings.json parsing
    db.py                   # SQLite schema, migration, CRUD
    store.py                # InvoiceStore (replaces .manifest.json)
    scheduler.py            # croniter loop
    web.py                  # Flask web UI
    invoices.py             # invoice list helper
    core/                   # browser (Playwright), flaresolverr, mailer
    addons/                 # vodafone.py, cursor.py, proton.py
```

## Database

**Path:** `/config/bills.db` (WAL mode, on the ZFS config mount)

| Table         | Purpose |
|---------------|---------|
| `invoices`    | One row per known invoice (addon, key, filename, dates, file path, sha256) |
| `mail_events` | SMTP send history per invoice (recipient, subject, sent_at, success) |
| `runs`        | Addon run log (started/finished, exit code, trigger, log summary) |
| `schedules`   | Per-addon cron expressions |
| `settings`    | Optional operational key/value store (secrets stay in `settings.json`) |
| `schema_meta` | Migration markers |

**Migration (first startup):** existing `.manifest.json` files → `invoices` +
inferred `mail_events` (legacy entries treated as already mailed); orphan PDFs
scanned into `invoices`; `schedule.json` → `schedules`; `/config/logs/*-last.log`
→ `runs`.

**Retired:** per-addon `.manifest.json` writes (read-only migration source).
**Kept:** `/config/settings.json` (secrets, web config form), `/config/logs/*.log`
(latest run text files, also summarized in `runs`).

## CLI

```bash
python -m bills schedule          # web UI (thread) + scheduler loop (default)
python -m bills web               # web UI only
python -m bills run cursor        # run one addon once
python -m bills run               # run all enabled addons once
python -m bills list              # list registered addons
```

## Web UI

`python -m bills schedule` also starts a small Flask UI (bound to
`0.0.0.0:${BILLS_WEB_PORT:-8080}`) in a daemon thread. Pages:

- **Dashboard** — trigger on-demand runs, live status/logs per addon.
- **Invoices** — table from SQLite with mail status; download and Mail/Re-send buttons.
- **Config** — edit settings persisted to `/config/settings.json`, per-addon cron schedules, and optional web login.
- **Send mail** — test email and re-send latest invoice per addon.

## Configuration

Resolution order: `/config/settings.json` → environment → default.

Key variables:

- `BILLS_ADDONS`, `BILLS_*_CRON`, `BILLS_RUN_ON_START`, `BILLS_TZ`
- `BILLS_HEADLESS=true` — headless Playwright Chromium
- `BILLS_DOWNLOAD_DIR=/downloads`, `BILLS_CONFIG_DIR=/config`
- Web auth: `BILLS_WEB_USERNAME` (default `admin`), `BILLS_WEB_PASSWORD` (unset = no login required)
- `BILLS_WEB_SECRET` — session signing key; change from default in production
- `FLARESOLVERR_ENABLED`, `FLARESOLVERR_URL`
- SMTP: shared `BILLS_SMTP_*` for all addons (same recipient/from/server for every plugin)
- Mail templates: `BILLS_MAIL_SUBJECT` / `BILLS_MAIL_BODY` with placeholders
  `{provider}`, `{addon}`, `{filename}`, `{date}`, `{number}`, `{recipient}`

### Vodafone

Set `VODAFONE_USERNAME` and `VODAFONE_PASSWORT`. Playwright logs in, navigates
to Meine Rechnungen, and downloads PDFs via `page.expect_download()`.

### Cursor

Cursor login is protected by Cloudflare Turnstile. Use session cookies:

1. Log in to Cursor in a normal browser.
2. Export cookies as JSON.
3. Place at `/config/cursor-session-cookies.json`.

Alternatively set `CURSOR_STRIPE_PORTAL_URL` to skip login. FlareSolverr optional.

### Proton VPN

Invoices live at [account.protonvpn.com/subscription#invoices](https://account.protonvpn.com/subscription#invoices).

Set `PROTON_USERNAME` and `PROTON_PASSWORT`. Playwright logs in at account.proton.me,
then lists invoices via Proton's payments API and downloads PDFs directly.

If login fails (e.g. 2FA), export session cookies as a fallback:

1. Log in at account.proton.me or account.protonvpn.com in a normal browser.
2. Export cookies for `.proton.me` / `.protonvpn.com` as JSON.
3. Place at `/config/proton-session-cookies.json`.

## Deployment (TrueNAS)

Single custom app — one service (`bills`) on `python:3.12-slim`:

- Bootstrap script clones this **public** repo into `/app`.
- `entrypoint.sh` pip-installs deps and runs `playwright install --with-deps chromium`
  (cached after first boot).
- Mounts: `/zfs/bills -> /downloads`, `/zfs/bills/config -> /config`.
- Port: host `8512` → container `8080` (web UI).
- Network: `ix-bills-net` (FlareSolverr if enabled).

No external Selenium Grid required.
