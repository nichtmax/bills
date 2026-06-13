# bills

Unified bill downloader for multiple providers. Each provider is a small
**addon**; a built-in, env-configurable scheduler runs them on their own cron.
Designed to run as a single TrueNAS custom app against a shared Selenium Grid.

## Addons

| Addon      | Source                | Auth                              | Download path        |
|------------|-----------------------|-----------------------------------|----------------------|
| `vodafone` | MeinVodafone (Angular)| username + password               | `/downloads/vodafone`|
| `cursor`   | Stripe billing portal | session cookies (+ optional FlareSolverr) | `/downloads/cursor`  |

Invoices are saved as `YYYY-MM-DD <Provider> <number>.pdf`. Files are never
re-downloaded if they already exist, and an email is only sent for **new**
invoices (tracked in a per-addon `.manifest.json`).

## Layout

```
bills/
  entrypoint.sh             # clone/pull + pip + run (used by the container)
  requirements.txt
  bills/
    __main__.py             # CLI: schedule | run <addon> | list
    config.py               # env parsing
    scheduler.py            # croniter loop (runs each addon in a subprocess)
    core/                   # browser, flaresolverr, mailer, manifest, addon base
    addons/                 # vodafone.py, cursor.py
```

## CLI

```bash
python -m bills schedule          # run the scheduler loop (default)
python -m bills run cursor        # run one addon once
python -m bills run               # run all enabled addons once
python -m bills list              # list registered addons
```

## Configuration

All settings come from environment variables. See [`.env.example`](.env.example).

Key variables:

- `BILLS_ADDONS=vodafone,cursor` — enabled addons.
- `BILLS_VODAFONE_CRON`, `BILLS_CURSOR_CRON` — per-addon schedule (cron).
- `BILLS_RUN_ON_START`, `BILLS_TZ`.
- `SELENIUM_REMOTE_URL` — shared `bills-selenium` Grid (required).
- `BILLS_DOWNLOAD_DIR=/downloads`, `BILLS_CONFIG_DIR=/config`.
- SMTP: `BILLS_SMTP_SERVER` / `BILLS_EMAIL_FROM` / `BILLS_EMAIL_PASSWORD` /
  `BILLS_EMAIL_TO` (per-addon `VODAFONE_*` / `CURSOR_*` override these).

### Vodafone

Set `VODAFONE_USERNAME` and `VODAFONE_PASSWORT`. The browser runs on the shared
Grid; finished PDFs are pulled back via Selenium's managed-download API, so the
Grid must have managed downloads enabled (`SE_ENABLE_MANAGED_DOWNLOADS=true`).

### Cursor

Cursor login is protected by Cloudflare Turnstile, which headless Selenium
cannot solve. Authenticate with session cookies instead:

1. Log in to Cursor in a normal browser.
2. Export the `cursor.com` cookies as a JSON array.
3. Provide them via either:
   - `CURSOR_SESSION_COOKIES` (the JSON array inline), or
   - a file at `/config/cursor-session-cookies.json`
     (override with `CURSOR_SESSION_COOKIES_FILE`).

Alternatively set `CURSOR_STRIPE_PORTAL_URL` to a fresh Stripe billing-portal
session URL to skip Cursor login entirely. FlareSolverr can be enabled with
`FLARESOLVERR_ENABLED=true` + `FLARESOLVERR_URL`.

## Deployment (TrueNAS)

A single custom app with two services on one compose:

- `bills` — `python:3.12-slim`. Its command runs a small bootstrap script that
  clones this **public** repo into `/app` (no token needed) and runs
  `entrypoint.sh` (pip install + scheduler):

  ```sh
  if [ -d /app/.git ]; then cd /app && git pull --ff-only || true; \
  else apt-get update -qq && apt-get install -y -qq git >/dev/null; \
       git clone "https://${BILLS_REPO}" /app; fi
  exec bash /app/entrypoint.sh schedule
  ```

- `bills-selenium` — `selenium/standalone-chromium:latest` (hostname
  `bills-selenium`, `shm_size: 2gb`, `SE_ENABLE_MANAGED_DOWNLOADS=true`),
  mounting `/zfs/bills -> /downloads` so the browser writes invoices to the
  same dataset the `bills` service reads.

Mounts (`bills`): `/zfs/bills -> /downloads`, `/zfs/bills/config -> /config`.
Network: `ix-bills-net`. `bills` `depends_on` `bills-selenium` and reaches it at
`http://bills-selenium:4444/wd/hub`.
