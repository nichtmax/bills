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

# Words that mark a URL/response as billing-related for network capture.
_BILLING_KEYWORDS = (
    "invoice", "receipt", "billing", "payment", "order", "transaction",
    "history", "credit", "wallet", "finance", "recharge", "subscription",
    "purchase",
)
# Matches absolute PDF URLs embedded in JSON/text response bodies.
_PDF_URL_RE = re.compile(r'https?://[^\s"\'<>`]+?\.pdf[^\s"\'<>`]*', re.IGNORECASE)
# Matches JSON fields likely to hold a downloadable document URL.
_URL_FIELD_RE = re.compile(
    r'"(?:download|invoice|receipt|file|pdf|document|url)_?(?:url|link|href)"'
    r'\s*:\s*"(https?://[^"]+)"',
    re.IGNORECASE,
)
# Section/tab labels that reveal the invoice list on a SPA billing page.
_SECTION_LABELS = (
    "invoices", "invoice history", "billing history", "payment history",
    "receipts", "transactions", "payments", "bills", "orders",
)


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
        self._captured_responses: list = []
        self._all_api_calls: list[tuple[str, int, str]] = []
        self._auth_token: str | None = None
        self.page.on("response", self._on_response)
        try:
            # Try various auth methods (prefer session/token over password login)
            auth_worked = False
            
            # Try session cookies first (most reliable)
            if self._try_session_auth():
                self.log("session cookies valid")
                auth_worked = True
            # Bearer token / API key: validate against a protected endpoint before
            # trusting it, since chat.z.ai rejects inference-only API keys.
            elif token and self._try_token_auth(token):
                self.log("bearer token valid")
                self._auth_token = token
                self._set_bearer_token(token)
                auth_worked = True
            elif api_key and self._try_api_key(api_key):
                self.log("API key valid")
                self._auth_token = api_key
                self._set_bearer_token(api_key)
                auth_worked = True
            elif token or api_key:
                self.log(
                    "ERROR: provided ZAI_BEARER_TOKEN / ZAI_TOKEN / ZAI_API_KEY is not "
                    "a valid chat.z.ai web session token (rejected by /api/models). "
                    "Use the JWT from localStorage.token after a browser login, export "
                    "session cookies, or set ZAI_EMAIL/ZAI_PASSWORD."
                )
                result.failed = 1
                return result
            
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
        """Authenticate the browser SPA with the bearer token.

        OpenWebUI (which powers chat.z.ai) stores its session JWT in
        ``localStorage.token`` and only renders authenticated views — and fires
        the billing/invoice API calls — when that key is present and valid.
        A raw ``Authorization`` header alone authenticates individual XHRs but
        leaves the SPA in a logged-out shell, so no billing data is ever
        fetched. Injecting the token into localStorage via add_init_script
        (runs before page scripts on every navigation) lets the SPA authenticate
        itself end-to-end.
        """
        self.context.set_extra_http_headers({
            "Authorization": f"Bearer {token}",
            "X-API-Key": token,
        })
        self.context.add_init_script(
            "try { localStorage.setItem('token', " + json.dumps(token) + "); } catch (e) {}"
        )

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
        self._wait_for_render()
        return self._on_billing_page()

    def _wait_for_render(self) -> None:
        """Give the SPA time to render content beyond first paint."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            pass
        time.sleep(3)

    def _on_billing_page(self) -> bool:
        url = self.page.url.lower()
        src = self.page.content().lower()
        # Not logged in if bounced to the login page.
        if "login" in url and "billing" not in url:
            return False
        return "billing" in url or "invoice" in src or "receipt" in src or "payment" in src

    def _on_response(self, response) -> None:
        """Capture billing-related JSON/PDF responses for offline URL harvest."""
        try:
            url = response.url or ""
            status = response.status
            ctype = (response.headers.get("content-type") or "").lower()
        except Exception:  # noqa: BLE001
            return
        # Diagnostic record of every API/JSON call (reveals the real invoice API).
        if "/api/" in url or "json" in ctype:
            self._all_api_calls.append((url, status, ctype))
        low = url.lower()
        if not any(k in low for k in _BILLING_KEYWORDS) and not low.endswith(".pdf"):
            return
        if "json" in ctype or "pdf" in ctype or low.split("?")[0].endswith(".pdf"):
            self._captured_responses.append(response)

    def _harvest_pdf_urls(self) -> list[str]:
        """Extract downloadable PDF/document URLs from captured API responses."""
        urls: list[str] = []
        for resp in self._captured_responses:
            try:
                url = resp.url
                ctype = (resp.headers.get("content-type") or "").lower()
            except Exception:  # noqa: BLE001
                continue
            if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
                urls.append(url)
                continue
            if "json" not in ctype:
                continue
            try:
                body = resp.text()
            except Exception:  # noqa: BLE001
                continue
            urls.extend(m.group(0) for m in _PDF_URL_RE.finditer(body))
            urls.extend(m.group(1) for m in _URL_FIELD_RE.finditer(body))
        # Deduplicate, preserve order.
        return list(dict.fromkeys(urls))

    def _download_via_url(self, url: str) -> Path | None:
        """Download a document URL using the authenticated browser context."""
        headers = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
            headers["X-API-Key"] = self._auth_token
        try:
            api_resp = self.context.request.get(url, timeout=60000, headers=headers)
        except Exception as exc:  # noqa: BLE001
            self.log(f"fetch {url[:80]}: {exc}")
            return None
        if not api_resp.ok:
            self.log(f"fetch {url[:80]}: HTTP {api_resp.status}")
            return None
        body = api_resp.body()
        if not body or body[:4] != b"%PDF":
            self.log(f"fetch {url[:80]}: response is not a PDF")
            return None
        name = url.rsplit("/", 1)[-1].split("?")[0] or "invoice"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        local = self.download_dir / name
        local.write_bytes(body)
        return local if local.exists() and local.stat().st_size > 0 else None

    def _scroll_to_load(self, rounds: int = 6) -> None:
        """Scroll the page and inner scroll containers to trigger lazy rows."""
        for _ in range(rounds):
            try:
                self.page.mouse.wheel(0, 4000)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.page.evaluate(
                    "() => {"
                    " window.scrollTo(0, document.body.scrollHeight);"
                    " for (const el of document.querySelectorAll('main, [class*=scroll], [class*=list], [role=table], .ant-table-body')) {"
                    "  if (el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight;"
                    " }"
                    "}"
                )
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)

    def _open_invoices_section(self) -> None:
        """Click an invoices/billing-history tab if present (reveals the list)."""
        lower = (
            "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz')"
        )
        for needle in _SECTION_LABELS:
            xpath = (
                f"xpath=//*[self::a or self::button or @role='tab' or @role='button']"
                f"[contains({lower}, '{needle}')]"
            )
            try:
                loc = self.page.locator(xpath)
                if loc.count() == 0:
                    continue
            except Exception:  # noqa: BLE001
                continue
            try:
                el = loc.first
                if not el.is_visible():
                    continue
                el.scroll_into_view_if_needed()
                el.click()
                time.sleep(3)
                self.log(f"opened section matching '{needle}'")
                return
            except Exception:  # noqa: BLE001
                continue

    def _save_debug_artifacts(self, tag: str) -> None:
        """Persist a screenshot + HTML dump for offline inspection."""
        try:
            shot = Path(self.config.config_dir) / f"zai-billing-{tag}.png"
            self.page.screenshot(path=str(shot), full_page=True)
            self.log(f"saved debug screenshot: {shot}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"could not save screenshot: {exc}")
        try:
            html = Path(self.config.config_dir) / f"zai-billing-{tag}.html"
            html.write_text(self.page.content(), encoding="utf-8")
            self.log(f"saved debug HTML: {html}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"could not save HTML: {exc}")

    def _download_invoices(self, result: RunResult) -> None:
        self._wait_for_render()
        self._open_invoices_section()
        self._scroll_to_load()

        url = self.page.url
        title = self.page.title()
        self.log(f"page: {url} - title: {title}")

        downloaded_any = False

        # Strategy 1: harvest invoice PDF URLs straight from the billing API.
        pdf_urls = self._harvest_pdf_urls()
        if pdf_urls:
            self.log(f"captured {len(pdf_urls)} invoice URL(s) from network")
            for index, pdf_url in enumerate(pdf_urls, start=1):
                local = self._download_via_url(pdf_url)
                if not local:
                    result.failed += 1
                    continue
                if not self._finalize(local, result):
                    continue
                downloaded_any = True
            if downloaded_any:
                return

        # Strategy 2: click visible download controls.
        candidates = self._find_invoice_candidates()
        if candidates:
            self.log(f"found {len(candidates)} invoice candidate(s)")
            for index, candidate in enumerate(candidates, start=1):
                local = self._download_candidate(candidate)
                if not local:
                    self.log(f"invoice {index}: download failed or unsupported")
                    result.failed += 1
                    continue
                if not self._finalize(local, result, candidate.get("text")):
                    continue
                downloaded_any = True
            if downloaded_any:
                return

        # Nothing worked: leave breadcrumbs for diagnosis.
        self._log_diagnostics()

    def _log_diagnostics(self) -> None:
        """Emit network/HTML signals into the run log (readable via /api/runs)."""
        self.log(f"no invoices found; page content length: {len(self.page.content())}")
        try:
            final_url = self.page.url
        except Exception:  # noqa: BLE001
            final_url = "?"
        self.log(f"final url: {final_url}")
        try:
            visible = self.page.inner_text("body")[:600].replace("\n", " ")
        except Exception:  # noqa: BLE001
            visible = ""
        lowered = visible.lower()
        wall = any(
            w in lowered for w in ("sign in", "log in", "login", "sign-in", "please log")
        )
        self.log(f"login wall detected: {wall}")
        if visible:
            self.log(f"visible text: {visible[:300]}")
        # Show every API/JSON call the SPA made — reveals the real invoice endpoint.
        calls = self._all_api_calls or []
        self.log(f"api calls captured: {len(calls)}")
        for url, status, ctype in calls:
            self.log(f"  {status} {ctype.split(';')[0]} {url[:160]}")
        self._save_debug_artifacts("nodebug")

    def _finalize(self, local: Path, result: RunResult, text: str | None = None) -> bool:
        """Move a downloaded file to its target, record it, and email it.

        Returns True when the file was handled (recorded or skipped).
        """
        text = text or local.name
        date, number = self._extract_invoice_data(text)
        target = self.target_path(date, number)
        if target.exists():
            target.unlink(missing_ok=True)
        local.replace(target)

        key = number or local.stem
        if self.already_known(key, target):
            self.log(f"skip {target.name} (already known)")
            result.skipped += 1
            return True

        self.record(key, target, {"date": date, "number": number})
        self.email(target)
        result.downloaded += 1
        result.new_files.append(target)
        self.log(f"new invoice: {target.name}")
        return True

    def _find_invoice_candidates(self) -> list[dict]:
        selectors = [
            "a[href*='.pdf']",
            "a[download]",
            "a[href*='invoice']",
            "a[href*='receipt']",
            "a[href*='download']",
            "button[aria-label*='invoice' i]",
            "button[aria-label*='receipt' i]",
            "button[aria-label*='download' i]",
            "[role='button'][aria-label*='download' i]",
            "[role='link'][aria-label*='download' i]",
            # Icon-only download buttons rendered as clickable SVG glyphs.
            "button:has(svg)",
            "a:has(svg)",
            "[role='button']:has(svg)",
            "[role='link']:has(svg)",
            "td button",
            "td a",
            "tr button",
            "a",
            "button",
        ]

        candidates: list[dict] = []
        for selector in selectors:
            try:
                elements = self.page.locator(selector).all()
            except Exception:  # noqa: BLE001
                continue
            for element in elements:
                try:
                    if not element.is_visible():
                        continue
                    text = (element.text_content() or "").strip()
                    href = (element.get_attribute("href") or "").strip()
                    aria = (element.get_attribute("aria-label") or "").strip()
                    cls = (element.get_attribute("class") or "").lower()
                except Exception:  # noqa: BLE001
                    continue
                if not self._looks_like_download(text, href, aria, cls):
                    continue
                candidates.append(
                    {"element": element, "text": text, "href": href, "aria": aria}
                )
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for item in candidates:
            key = (item["text"], item["href"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _looks_like_download(text: str, href: str, aria: str, cls: str) -> bool:
        lowered = f"{text} {href} {aria}".lower()
        if any(token in lowered for token in ("invoice", "receipt", "download", ".pdf")):
            return True
        if any(token in cls for token in ("download", "invoice", "receipt")):
            return True
        return False

    def _download_candidate(self, candidate: dict) -> Path | None:
        element = candidate.get("element")
        href = candidate.get("href") or ""
        if not element:
            return None
        # Direct href to a PDF: fetch via the authenticated context.
        if href and href.lower().split("?")[0].endswith(".pdf"):
            local = self._download_via_url(href)
            if local:
                return local

        try:
            with self.page.expect_download(timeout=60000) as download_info:
                element.click()
            download = download_info.value
            local = self.download_dir / download.suggested_filename
            download.save_as(local)
            return local if local.exists() and local.stat().st_size > 0 else None
        except PlaywrightTimeout:
            pass
        except Exception as exc:  # noqa: BLE001
            self.log(f"download candidate error: {exc}")

        # Click may have triggered an XHR returning a PDF instead of a download;
        # re-harvest captured URLs after the click.
        time.sleep(2)
        for pdf_url in self._harvest_pdf_urls():
            local = self._download_via_url(pdf_url)
            if local:
                return local
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
