"""In-process Playwright Chromium for bill addons.

Each addon run launches its own browser inside the bills container. Downloads
are captured via Playwright's download API and saved directly under
``/downloads/<addon>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class BrowserSession:
    """Owns a Playwright browser lifecycle for one addon run."""

    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page

    def close(self) -> None:
        for obj in (self.context, self.browser):
            try:
                obj.close()
            except Exception:
                pass
        try:
            self.playwright.stop()
        except Exception:
            pass


def launch_context(*, download_path: str, headless: bool = True) -> BrowserSession:
    """Launch Chromium in-process with downloads enabled."""
    Path(download_path).mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    context = browser.new_context(
        user_agent=USER_AGENT,
        accept_downloads=True,
        viewport={"width": 1440, "height": 960},
    )
    page = context.new_page()
    print(f"Playwright Chromium launched (headless={headless})", flush=True)
    return BrowserSession(pw, browser, context, page)


def normalize_cookie(cookie: dict) -> dict | None:
    """Normalise a browser-exported cookie for Playwright ``add_cookies``."""
    name = cookie.get("name") or cookie.get("Name")
    value = cookie.get("value") or cookie.get("Value")
    if not name or value is None:
        return None
    domain = cookie.get("domain") or cookie.get("Domain")
    if not domain:
        return None
    out: dict = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": cookie.get("path") or cookie.get("Path") or "/",
    }
    if cookie.get("secure") is not None:
        out["secure"] = bool(cookie.get("secure"))
    if cookie.get("httpOnly") is not None:
        out["httpOnly"] = bool(cookie.get("httpOnly"))
    expiry = cookie.get("expiry") or cookie.get("expires") or cookie.get("expirationDate")
    if expiry:
        try:
            out["expires"] = int(float(expiry))
        except (TypeError, ValueError):
            pass
    return out


def inject_cookies(context: BrowserContext, cookies: list[dict], log=print) -> int:
    """Inject exported cookies grouped by domain (Playwright requires domain)."""
    by_domain: dict[str, list[dict]] = {}
    for raw in cookies:
        norm = normalize_cookie(raw)
        if not norm:
            continue
        host = norm["domain"].lstrip(".")
        by_domain.setdefault(host, []).append(norm)

    added = 0
    for host, domain_cookies in by_domain.items():
        try:
            context.add_cookies(domain_cookies)
            added += len(domain_cookies)
        except Exception as exc:  # noqa: BLE001
            log(f"  cookies for {host} skipped: {exc}")
    return added
