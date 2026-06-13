"""Remote Selenium WebDriver against the shared bills-selenium Grid.

Browsers run inside the Grid container, so files the browser downloads land
on the Grid, not on this container. We enable Selenium 4 managed downloads
(``se:downloadsEnabled``) so addons can pull finished files back into
``/downloads`` via ``get_downloadable_files`` / ``download_file``.
"""

from __future__ import annotations

import os
import sys

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def remote_url() -> str:
    url = os.getenv("SELENIUM_REMOTE_URL", "").strip()
    if not url:
        print(
            "ERROR: SELENIUM_REMOTE_URL is required (shared bills-selenium grid).",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    return url


def create_driver(
    *,
    download_path: str,
    headless: bool = True,
    enable_downloads: bool = False,
) -> webdriver.Remote:
    """Create a remote Chrome session on the shared Grid."""
    opts = Options()
    prefs = {
        "download.default_directory": download_path,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "safebrowsing.enabled": True,
    }
    opts.add_experimental_option("prefs", prefs)
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-agent={USER_AGENT}")
    if headless:
        opts.add_argument("--headless=new")
    if enable_downloads:
        opts.set_capability("se:downloadsEnabled", True)

    print(
        f"Connecting to remote Selenium: {remote_url().split('@')[-1]}",
        flush=True,
    )
    driver = webdriver.Remote(command_executor=remote_url(), options=opts)
    try:
        driver.set_window_size(1440, 960)
    except Exception:
        pass
    try:
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    except Exception:
        pass
    return driver


def normalize_cookie(cookie: dict) -> dict | None:
    """Normalise a browser-exported cookie into Selenium's add_cookie format."""
    name = cookie.get("name") or cookie.get("Name")
    value = cookie.get("value") or cookie.get("Value")
    if not name or value is None:
        return None
    out: dict = {"name": name, "value": value}
    domain = cookie.get("domain") or cookie.get("Domain")
    if domain:
        out["domain"] = domain
    out["path"] = cookie.get("path") or cookie.get("Path") or "/"
    if cookie.get("secure") is not None:
        out["secure"] = bool(cookie.get("secure"))
    if cookie.get("httpOnly") is not None:
        out["httpOnly"] = bool(cookie.get("httpOnly"))
    expiry = cookie.get("expiry") or cookie.get("expires") or cookie.get("expirationDate")
    if expiry:
        try:
            out["expiry"] = int(float(expiry))
        except (TypeError, ValueError):
            pass
    return out


def inject_cookies(driver: webdriver.Remote, cookies: list[dict], log=print) -> int:
    """Add cookies to the current driver session. Driver must already be on a
    page whose domain matches the cookies (navigate there first)."""
    added = 0
    for raw in cookies:
        norm = normalize_cookie(raw)
        if not norm:
            continue
        try:
            driver.add_cookie(norm)
            added += 1
        except Exception as exc:  # noqa: BLE001
            log(f"  cookie '{norm.get('name')}' skipped: {exc}")
    return added
