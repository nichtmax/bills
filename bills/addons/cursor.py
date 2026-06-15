"""Cursor invoice downloader via the Stripe billing portal (Playwright)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..core.addon import Addon, RunResult
from ..core.browser import inject_cookies
from ..core.flaresolverr import (
    FlareSolverrClient,
    flaresolverr_enabled,
    human_challenge_visible,
)

BILLING_URL = "https://cursor.com/dashboard?tab=billing"
LOGIN_URL = "https://authenticator.cursor.sh/"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class AuthenticationError(Exception):
    """Login failed with a specific, actionable reason."""


class CursorAddon(Addon):
    name = "cursor"
    provider = "Cursor"

    def run(self) -> RunResult:
        result = RunResult()
        email = os.getenv("CURSOR_EMAIL")
        password = os.getenv("CURSOR_PASSWORD") or None

        self.browser = self.make_browser()
        self.page = self.browser.page
        self.context = self.browser.context
        self.fs: FlareSolverrClient | None = None
        try:
            portal_url = os.getenv("CURSOR_STRIPE_PORTAL_URL", "").strip()
            if portal_url:
                self.log("using CURSOR_STRIPE_PORTAL_URL; skipping login")
                self.page.goto(portal_url, wait_until="domcontentloaded")
                time.sleep(5)
                if "stripe.com" not in self.page.url.lower():
                    self.log(f"portal URL did not reach Stripe: {self.page.url}")
                    result.failed = 1
                    return result
            else:
                if not email:
                    self.log("ERROR: CURSOR_EMAIL not set")
                    result.failed = 1
                    return result
                try:
                    if not self._authenticate(email, password):
                        result.failed = 1
                        return result
                except AuthenticationError as exc:
                    self.log(f"ERROR: {exc}")
                    result.failed = 1
                    return result
                if not self._open_billing() or not self._open_stripe_portal():
                    result.failed = 1
                    return result

            self._download_invoices(result)
        finally:
            self.browser.close()
        return result

    def _cookies_file(self) -> Path:
        custom = os.getenv("CURSOR_SESSION_COOKIES_FILE", "").strip()
        if custom:
            return Path(custom)
        return Path(self.config.config_dir) / "cursor-session-cookies.json"

    def _load_session_cookies(self) -> list[dict] | None:
        raw = os.getenv("CURSOR_SESSION_COOKIES", "").strip()
        source = "CURSOR_SESSION_COOKIES"
        if not raw:
            path = self._cookies_file()
            if path.is_file():
                raw = path.read_text(encoding="utf-8").strip()
                source = str(path)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AuthenticationError(f"{source} is invalid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise AuthenticationError(f"{source} must be a JSON array of cookies")
        return data

    def _authenticate(self, email: str, password: str | None) -> bool:
        if self._try_session_auth():
            return True
        return self._login(email, password)

    def _try_session_auth(self) -> bool:
        cookies = self._load_session_cookies()
        if not cookies:
            return False
        self.log(f"trying session cookies ({len(cookies)} exported)")
        added = inject_cookies(self.context, cookies, log=self.log)
        self.log(f"injected {added} cookies")
        if flaresolverr_enabled():
            self._start_flaresolverr()
            try:
                self._flaresolverr_preflight(BILLING_URL)
            finally:
                self._stop_flaresolverr()
        self._safe_goto(BILLING_URL)
        time.sleep(5)
        if human_challenge_visible(self._page_source()):
            self.log("Cloudflare challenge after session cookies; trying FlareSolverr")
            self._start_flaresolverr()
            try:
                self._flaresolverr_preflight(BILLING_URL)
                self._safe_goto(BILLING_URL)
                time.sleep(5)
            finally:
                self._stop_flaresolverr()
        if self._on_dashboard():
            self.log("session cookies valid - skipping password login")
            return True
        self.log("session cookies did not reach billing dashboard")
        return False

    def _start_flaresolverr(self) -> None:
        if not flaresolverr_enabled():
            return
        try:
            self.fs = FlareSolverrClient(self.config.flaresolverr_url).start_session()
            self.log(f"FlareSolverr session started ({self.fs.base_url})")
        except RuntimeError as exc:
            self.log(f"FlareSolverr unavailable: {exc}")
            self.fs = None

    def _stop_flaresolverr(self) -> None:
        if self.fs:
            self.fs.close()
            self.fs = None

    def _flaresolverr_preflight(self, url: str) -> None:
        if not self.fs:
            return
        try:
            solution = self.fs.get(url)
            self.fs.apply_to_context(self.context, solution, log=self.log)
            time.sleep(2)
        except RuntimeError as exc:
            self.log(f"FlareSolverr preflight failed: {exc}")

    def _captcha_error(self) -> str:
        return (
            "Cursor CAPTCHA blocked login. Headless Playwright cannot solve Cloudflare "
            "Turnstile. Log in once in a normal browser, export session cookies, and place "
            "them at the cookies file (see README). Optionally enable FlareSolverr."
        )

    def _check_human_challenge(self, step: str) -> bool:
        if not human_challenge_visible(self._page_source()):
            return False
        self.log(f"human verification blocked login at {step}")
        if self.fs:
            try:
                solution = self.fs.get(self.page.url)
                self.fs.apply_to_context(self.context, solution, log=self.log)
                time.sleep(2)
                if not human_challenge_visible(self._page_source()):
                    self.log("FlareSolverr cleared challenge")
                    return False
            except RuntimeError as exc:
                self.log(f"FlareSolverr retry failed: {exc}")
        self.log(f"ERROR: {self._captcha_error()}")
        return True

    def _login(self, email: str, password: str | None) -> bool:
        self.log(f"opening Cursor login for {email[:3]}***")
        self._start_flaresolverr()
        try:
            self._flaresolverr_preflight(BILLING_URL)
            time.sleep(2)
            if self._on_dashboard():
                self.log("already logged in")
                return True
            if "authenticator.cursor" not in self.page.url.lower():
                self._flaresolverr_preflight(LOGIN_URL)
            try:
                email_input = self.page.locator(
                    "input[type='email'], input[name='email'], input[name='username']"
                ).first
                email_input.wait_for(state="visible", timeout=45000)
                email_input.fill(email)
                self._click_continue()
                time.sleep(3)
                if self._check_human_challenge("after-email"):
                    return False
                if password:
                    try:
                        password_input = self.page.locator(
                            "input[type='password'], input[name='password']"
                        ).first
                        password_input.wait_for(state="visible", timeout=45000)
                        password_input.fill(password)
                        self._click_continue()
                        time.sleep(3)
                        if self._check_human_challenge("after-password"):
                            return False
                    except PlaywrightTimeout:
                        self.log("no password field; waiting for magic-link/SSO")
                else:
                    self.log("no CURSOR_PASSWORD; waiting for magic-link/SSO")

                deadline = time.time() + int(os.getenv("CURSOR_LOGIN_TIMEOUT", "180"))
                while time.time() < deadline:
                    if self._on_dashboard():
                        self.log("login successful")
                        return True
                    if self._check_human_challenge("waiting-for-dashboard"):
                        return False
                    time.sleep(2)
                if human_challenge_visible(self._page_source()):
                    self._check_human_challenge("timeout")
                    return False
                self.log("login timed out waiting for dashboard redirect")
                return False
            except PlaywrightTimeout as exc:
                self.log(f"login form not found: {exc}")
                return False
        finally:
            self._stop_flaresolverr()

    def _click_continue(self) -> None:
        for selector in ("button[type='submit']", "input[type='submit']"):
            try:
                self.page.locator(selector).first.click(timeout=3000)
                return
            except Exception:
                continue
        for btn in self.page.locator("button").all():
            if (btn.text_content() or "").strip().lower() in {
                "continue", "sign in", "log in", "next",
            }:
                btn.click()
                return

    def _page_source(self) -> str:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PlaywrightTimeout:
            pass
        try:
            return self.page.content()
        except Exception:
            time.sleep(2)
            return self.page.content()

    def _on_dashboard(self) -> bool:
        url = self.page.url.lower()
        if human_challenge_visible(self._page_source()):
            return False
        return "cursor.com/dashboard" in url and "login" not in url and "auth" not in url

    def _safe_goto(self, url: str, *, wait_until: str = "domcontentloaded") -> None:
        try:
            self.page.goto(url, wait_until=wait_until)
        except Exception as exc:
            if "crash" not in str(exc).lower():
                raise
            self.log(f"page crashed navigating to {url}, opening fresh tab")
            self.page = self.context.new_page()
            self.page.goto(url, wait_until=wait_until)

    def _open_billing(self) -> bool:
        if self._on_dashboard():
            self.log("billing dashboard already open")
            time.sleep(5)
            return True
        try:
            self._safe_goto(BILLING_URL)
        except Exception as exc:
            self.log(f"billing dashboard navigation failed: {exc}")
            return False
        time.sleep(3)
        if not self._on_dashboard():
            self.log("billing dashboard unreachable")
            return False
        self.log("billing dashboard loaded")
        return True

    def _open_stripe_portal(self) -> bool:
        for href in self.page.locator("a[href*='billing.stripe.com']").all():
            url = href.get_attribute("href") or ""
            if url:
                self.log(f"opening Stripe portal link: {url[:80]}...")
                self._safe_goto(url)
                time.sleep(3)
                if "stripe.com" in self.page.url.lower():
                    break

        lower = "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
        clicked = False
        for needle in (
            "manage in stripe", "manage subscription", "manage billing",
            "billing portal", "view invoices", "invoices", "stripe",
        ):
            xpath = f"xpath=//*[self::a or self::button][contains({lower}, '{needle}')]"
            try:
                loc = self.page.locator(xpath)
                if loc.count() == 0:
                    continue
                el = loc.first
                if not el.is_visible():
                    continue
                try:
                    with self.context.expect_page(timeout=15000) as page_info:
                        el.click()
                    portal_page = page_info.value
                    portal_page.wait_for_load_state("domcontentloaded")
                    self.page = portal_page
                except PlaywrightTimeout:
                    el.click()
                    time.sleep(5)
                clicked = True
                self.log(f"clicked portal control matching '{needle}'")
                break
            except Exception:
                continue
        if not clicked and "stripe.com" not in self.page.url.lower():
            self.log("Stripe portal button not found")
            try:
                shot = Path(self.config.config_dir) / "cursor-billing-debug.png"
                self.page.screenshot(path=str(shot), full_page=True)
                self.log(f"saved debug screenshot: {shot}")
            except Exception as exc:
                self.log(f"could not save debug screenshot: {exc}")
            return False
        time.sleep(2)
        self.log(f"Stripe portal URL: {self.page.url}")
        if "stripe.com" not in self.page.url.lower():
            return False
        try:
            self.page.wait_for_selector(
                "a[href*='invoice.stripe.com/i/']", timeout=30000
            )
        except PlaywrightTimeout:
            self.log("no invoice links visible on Stripe portal yet")
        return True

    def _portal_invoice_links(self) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        seen: set[str] = set()
        for el in self.page.locator("a[href*='invoice.stripe.com/i/']").all():
            href = el.get_attribute("href") or ""
            label = (el.text_content() or "").strip()
            if href and href not in seen:
                seen.add(href)
                links.append((href, label))
        return links

    @staticmethod
    def _pdf_data_url(hosted_url: str) -> str | None:
        m = re.search(r"invoice\.stripe\.com/i/([^/]+)/([^/?#]+)", hosted_url)
        if not m:
            return None
        return (
            f"https://invoicedata.stripe.com/invoice_pdf_file_url/"
            f"{m.group(1)}/{m.group(2)}?locale=en-US"
        )

    @staticmethod
    def _http_get(url: str, timeout: int = 60) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    @staticmethod
    def _parse_date(text: str) -> str | None:
        m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", text)
        if not m:
            return None
        month = _MONTHS.get(m.group(1)[:3].lower())
        if not month:
            return None
        return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"

    @staticmethod
    def _invoice_number_from_url(file_url: str) -> str | None:
        m = re.search(r"filename%3D%22([^%]+?)\.pdf%22", file_url) or re.search(
            r'filename="?([^";]+?)\.pdf', file_url
        )
        if not m:
            return None
        return re.sub(r"^(invoice|receipt)[-_]", "", m.group(1), flags=re.I)

    def _download_invoices(self, result: RunResult) -> None:
        links = self._portal_invoice_links()
        self.log(f"found {len(links)} invoice link(s) on Stripe portal")
        for hosted_url, label in links:
            try:
                data_url = self._pdf_data_url(hosted_url)
                if not data_url:
                    self.log(f"cannot derive PDF endpoint from {hosted_url}")
                    result.failed += 1
                    continue
                info = json.loads(self._http_get(data_url).decode("utf-8"))
                file_url = info.get("file_url")
                if not file_url:
                    self.log("no file_url in invoice data")
                    result.failed += 1
                    continue
                invoice_no = self._invoice_number_from_url(file_url) or "unknown"
                date = self._parse_date(label) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                target = self.target_path(date, invoice_no)
                if self.already_known(invoice_no, target):
                    self.log(f"skip already downloaded: {invoice_no}")
                    result.skipped += 1
                    continue
                pdf_bytes = self._http_get(file_url)
                if not pdf_bytes.startswith(b"%PDF"):
                    self.log(f"downloaded data is not a PDF for {invoice_no}")
                    result.failed += 1
                    continue
                target.write_bytes(pdf_bytes)
                self.record(invoice_no, target, {"date": date})
                self.email(target)
                result.downloaded += 1
                result.new_files.append(target)
                self.log(f"new invoice: {target.name}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"failed {hosted_url}: {exc}")
                result.failed += 1
