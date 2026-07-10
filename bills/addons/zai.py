"""Z.ai invoice downloader via Playwright.

The addon opens the Z.ai billing page, looks for invoice or receipt download
controls, and saves any discovered PDFs under the addon download directory.
New invoices are recorded in SQLite and emailed through the shared mailer.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..core.addon import Addon, RunResult
from ..core.browser import inject_cookies

BILLING_URL = "https://chat.z.ai/billing"
LOGIN_URL = "https://chat.z.ai/login"


class ZaiAddon(Addon):
    name = "zai"
    provider = "Z.ai"

    def run(self) -> RunResult:
        result = RunResult()
        api_key = self.config.get("ZAI_API_KEY")
        token = self.config.get("ZAI_BEARER_TOKEN") or self.config.get("ZAI_TOKEN")
        email = self.config.get("ZAI_EMAIL") or self.config.get("ZAI_USERNAME")
        password = self.config.get("ZAI_PASSWORD")

        self.browser = self.make_browser()
        self.page = self.browser.page
        self.context = self.browser.context
        try:
            # Try various auth methods (prefer session/token over password login)
            auth_worked = False
            
            # Try session cookies first (most reliable)
            if self._try_session_auth():
                self.log("session cookies valid")
                auth_worked = True
            # If provided, set bearer token/API key as auth header for all requests
            elif token:
                self.log("using bearer token authentication")
                self._set_bearer_token(token)
                auth_worked = True
            elif api_key:
                self.log("using API key authentication")
                self._set_bearer_token(api_key)
                auth_worked = True
            
            # If no auto auth, try email/password login
            if not auth_worked:
                if not email or not password:
                    self.log(
                        "ERROR: set ZAI_API_KEY, ZAI_BEARER_TOKEN / ZAI_TOKEN, or "
                        "ZAI_EMAIL / ZAI_USERNAME and ZAI_PASSWORD, or export session cookies"
                    )
                    result.failed = 1
                    return result
                if not self._login(email, password):
                    self.log("login failed")
                    result.failed = 1
                    return result

            if not self._open_billing():
                self.log("could not reach the Z.ai billing page")
                result.failed = 1
                return result

            self._download_invoices(result)
        finally:
            self.browser.close()
        return result

    def _cookies_file(self) -> Path:
        custom = self.config.get("ZAI_SESSION_COOKIES_FILE")
        if custom:
            return Path(custom)
        return Path(self.config.config_dir) / "zai-session-cookies.json"

    def _load_session_cookies(self) -> list[dict] | None:
        raw = self.config.get("ZAI_SESSION_COOKIES")
        source = "ZAI_SESSION_COOKIES"
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
            raise ValueError(f"{source} is invalid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError(f"{source} must be a JSON array of cookies")
        return data

    def _api_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _token_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _set_bearer_token(self, token: str) -> None:
        """Set bearer token as Authorization header for all browser requests."""
        self.context.set_extra_http_headers({
            "Authorization": f"Bearer {token}",
            "X-API-Key": token,
        })

    def _try_api_key(self, api_key: str) -> bool:
        if not api_key:
            return False
        self.log("trying Z.ai API key authentication")
        try:
            req = urllib.request.Request(
                "https://chat.z.ai/api/v1/users/user/settings",
                headers=self._api_headers(api_key),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = response.read().decode("utf-8")
                if response.status == 200 and payload:
                    self.log("API key authentication succeeded")
                    return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"API key auth failed: {exc}")
        return False

    def _try_token_auth(self, token: str) -> bool:
        if not token:
            return False
        self.log("trying Z.ai bearer token authentication")
        try:
            req = urllib.request.Request(
                "https://chat.z.ai/api/v1/users/user/settings",
                headers=self._token_headers(token),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = response.read().decode("utf-8")
                if response.status == 200 and payload:
                    self.log("bearer token authentication succeeded")
                    return True
        except HTTPError as exc:  # noqa: BLE001
            self.log(f"bearer token auth failed: HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"bearer token auth failed: {exc}")
        return False

    def _try_session_auth(self) -> bool:
        try:
            cookies = self._load_session_cookies()
        except ValueError as exc:
            self.log(f"session cookies invalid: {exc}")
            return False
        if not cookies:
            return False
        self.log(f"trying session cookies ({len(cookies)} exported)")
        added = inject_cookies(self.context, cookies, log=self.log)
        self.log(f"injected {added} cookies")
        self.page.goto(BILLING_URL, wait_until="domcontentloaded")
        time.sleep(3)
        if self._on_billing_page():
            return True
        return False

    def _login(self, email: str, password: str) -> bool:
        self.log(f"opening Z.ai login for {email[:3]}***")
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        time.sleep(5)

        if self._try_magic_link_login(email, password):
            return True

        try:
            email_input = self.page.locator(
                "input[type='email'], input[name='email'], input[name='username'], input[type='text']"
            ).first
            email_input.wait_for(state="visible", timeout=45000)
            email_input.fill(email)
            password_input = self.page.locator(
                "input[type='password'], input[name='password']"
            ).first
            password_input.wait_for(state="visible", timeout=45000)
            password_input.fill(password)
            self._click_submit()
            time.sleep(5)
            if self._on_billing_page():
                self.log("login successful")
                return True
        except PlaywrightTimeout:
            self.log("login form not found")
            return False
        except Exception as exc:  # noqa: BLE001
            self.log(f"login error: {exc}")
            return False

        deadline = time.time() + 60
        while time.time() < deadline:
            if self._on_billing_page():
                return True
            time.sleep(2)
        return False

    def _try_magic_link_login(self, email: str, password: str) -> bool:
        try:
            for locator in self.page.locator("input").all():
                if not locator.is_visible():
                    continue
                name = (locator.get_attribute("name") or "").lower()
                kind = (locator.get_attribute("type") or "").lower()
                if kind in {"email", "text"} and "email" in name or "user" in name or "account" in name:
                    locator.fill(email)
                    break
            for locator in self.page.locator("input").all():
                if not locator.is_visible():
                    continue
                kind = (locator.get_attribute("type") or "").lower()
                if kind == "password":
                    locator.fill(password)
                    break
            self._click_submit()
            time.sleep(5)
            return self._on_billing_page()
        except Exception:  # noqa: BLE001
            return False

    def _click_submit(self) -> None:
        for selector in ("button[type='submit']", "button", "input[type='submit']"):
            try:
                candidates = self.page.locator(selector).all()
            except Exception:
                continue
            for button in candidates:
                if not button.is_visible() or not button.is_enabled():
                    continue
                label = (button.text_content() or "").strip().lower()
                if any(token in label for token in ("sign in", "log in", "continue", "next", "submit")):
                    button.click()
                    return
        self.page.keyboard.press("Enter")

    def _open_billing(self) -> bool:
        self.page.goto(BILLING_URL, wait_until="domcontentloaded")
        time.sleep(3)
        return self._on_billing_page()

    def _on_billing_page(self) -> bool:
        url = self.page.url.lower()
        src = self.page.content().lower()
        return "billing" in url or "invoice" in src or "receipt" in src or "payment" in src

    def _download_invoices(self, result: RunResult) -> None:
        time.sleep(3)
        
        # Debug: log page info
        url = self.page.url
        title = self.page.title()
        content_snippet = self.page.content()[:500]
        self.log(f"page: {url} - title: {title}")
        
        candidates = self._find_invoice_candidates()
        if not candidates:
            # Debug: log page content for inspection
            self.log(f"no invoice controls found; page content length: {len(self.page.content())}")
            content = self.page.content().lower()
            if "invoice" in content or "receipt" in content or "bill" in content:
                self.log("DEBUG: page contains billing-related content but no clickable controls found")
            return

        self.log(f"found {len(candidates)} invoice candidate(s)")
        for index, candidate in enumerate(candidates, start=1):
            try:
                local = self._download_candidate(candidate)
                if not local:
                    self.log(f"invoice {index}: download failed or unsupported")
                    result.failed += 1
                    continue

                text = candidate.get("text") or local.name
                date, number = self._extract_invoice_data(text)
                target = self.target_path(date, number)
                if target.exists():
                    target.unlink(missing_ok=True)
                local.replace(target)

                if self.already_known(number or local.stem, target):
                    self.log(f"skip {target.name} (already known)")
                    result.skipped += 1
                    continue

                key = number or local.stem
                self.record(key, target, {"date": date, "number": number})
                self.email(target)
                result.downloaded += 1
                result.new_files.append(target)
                self.log(f"new invoice: {target.name}")
            except Exception as exc:  # noqa: BLE001
                self.log(f"invoice {index} error: {exc}")
                result.failed += 1

    def _find_invoice_candidates(self) -> list[dict]:
        selectors = [
            "a[href*='.pdf']",
            "a[href*='invoice']",
            "a[href*='receipt']",
            "button[aria-label*='invoice']",
            "button[aria-label*='receipt']",
            "button[aria-label*='download']",
            "a",
            "button",
        ]

        candidates: list[dict] = []
        for selector in selectors:
            try:
                elements = self.page.locator(selector).all()
            except Exception:
                continue
            for element in elements:
                if not element.is_visible():
                    continue
                text = (element.text_content() or "").strip()
                href = (element.get_attribute("href") or "").strip()
                aria = (element.get_attribute("aria-label") or "").strip()
                if not text and not href and not aria:
                    continue
                lowered = f"{text} {href} {aria}".lower()
                if any(token in lowered for token in ("invoice", "receipt", "download", "bill", ".pdf")):
                    candidates.append({"element": element, "text": text, "href": href, "aria": aria})
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for item in candidates:
            key = (item["text"], item["href"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _download_candidate(self, candidate: dict) -> Path | None:
        element = candidate.get("element")
        href = candidate.get("href") or ""
        if not element:
            return None
        try:
            if href.endswith(".pdf"):
                with self.page.expect_download(timeout=60000) as download_info:
                    element.click()
                download = download_info.value
                local = self.download_dir / download.suggested_filename
                download.save_as(local)
                return local if local.exists() and local.stat().st_size > 0 else None

            with self.page.expect_download(timeout=60000) as download_info:
                element.click()
            download = download_info.value
            local = self.download_dir / download.suggested_filename
            download.save_as(local)
            return local if local.exists() and local.stat().st_size > 0 else None
        except PlaywrightTimeout:
            return None
        except Exception as exc:  # noqa: BLE001
            self.log(f"download candidate error: {exc}")
            return None

    def _extract_invoice_data(self, text: str) -> tuple[str | None, str | None]:
        text = text.strip()
        match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        date = match.group(1) if match else None
        number_match = re.search(r"(inv(?:oice)?[#\s:-]?\s*([A-Za-z0-9\-_/]+))", text, re.IGNORECASE)
        number = None
        if number_match:
            number = number_match.group(2).strip("# :-")
        elif re.search(r"\b(?:invoice|receipt|bill)\b", text, re.IGNORECASE):
            number = re.sub(r"\s+", "-", text)[:60]
        return date, number
