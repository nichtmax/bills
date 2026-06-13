"""Proton VPN invoice downloader via the Proton payments API.

Invoices page: https://account.protonvpn.com/subscription#invoices

Authentication: username + password via Playwright (preferred when set), or
exported session cookies (same approach as Cursor). With a valid session the
addon lists invoices through ``GET /api/payments/v5/invoices`` and downloads
each PDF from ``GET /api/payments/v5/invoices/{id}`` (falling back to v4 for
legacy invoices). Falls back to clicking Download in the web UI if the API is
unavailable.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..core.addon import Addon, RunResult
from ..core.browser import USER_AGENT, inject_cookies

INVOICES_PAGE = "https://account.proton.me/u/0/vpn/subscription#invoices"
LOGIN_URL = "https://account.proton.me/login"
API_BASE = "https://account.proton.me/api"
INVOICE_OWNER_USER = 0
PM_ACCEPT = "application/vnd.protonmail.v1+json"
PM_APPVERSION_FALLBACK = "web-account@5.0.0"
PM_LOCALE_FALLBACK = "en_US"


class AuthenticationError(Exception):
    """Session cookies missing or invalid."""


class ProtonAddon(Addon):
    name = "proton"
    provider = "Proton"

    def run(self) -> RunResult:
        result = RunResult()
        username = self.config.get("PROTON_USERNAME")
        password = self.config.get("PROTON_PASSWORT")

        self.browser = self.make_browser()
        self.page = self.browser.page
        try:
            authenticated = False
            if username and password:
                self.log("trying username/password login")
                if self._login(username, password):
                    authenticated = True
                elif self._try_session_auth():
                    self.log("login failed; session cookies worked as fallback")
                    authenticated = True
                else:
                    self.log("login failed and no valid session cookies")
                    result.failed = 1
                    return result
            elif self._try_session_auth():
                authenticated = True
            else:
                self.log(
                    "ERROR: set PROTON_USERNAME / PROTON_PASSWORT or export session "
                    "cookies (PROTON_SESSION_COOKIES / PROTON_SESSION_COOKIES_FILE)"
                )
                result.failed = 1
                return result

            if not authenticated:
                result.failed = 1
                return result

            self._pm_headers: dict[str, str] | None = None
            self._captured_invoices: list[dict] = []
            self._prepare_invoices_session()

            if self._captured_invoices:
                invoices = self._dedupe_invoices(self._captured_invoices)
                self.log(f"using {len(invoices)} invoice(s) captured from page load")
            else:
                invoices = self._list_invoices_api()
                if invoices is None:
                    self.log("API listing failed, trying web UI")
                    invoices = self._list_invoices_ui()
            if not invoices:
                self.log("no invoices found")
                return result

            self.log(f"found {len(invoices)} invoice(s)")
            for inv in invoices:
                self._process_invoice(inv, result)
        finally:
            self.browser.close()
        return result

    def _cookies_file(self) -> Path:
        custom = self.config.get("PROTON_SESSION_COOKIES_FILE")
        if custom:
            return Path(custom)
        return Path(self.config.config_dir) / "proton-session-cookies.json"

    def _load_session_cookies(self) -> list[dict] | None:
        raw = self.config.get("PROTON_SESSION_COOKIES")
        source = "PROTON_SESSION_COOKIES"
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

    def _try_session_auth(self) -> bool:
        try:
            cookies = self._load_session_cookies()
        except AuthenticationError as exc:
            self.log(f"session cookies invalid: {exc}")
            return False
        if not cookies:
            return False
        self.log(f"trying session cookies ({len(cookies)} exported)")
        inject_cookies(self.browser.context, cookies, log=self.log)
        return self._session_reaches_invoices()

    def _session_reaches_invoices(self) -> bool:
        self.page.goto(INVOICES_PAGE, wait_until="domcontentloaded")
        time.sleep(3)
        url = self.page.url.lower()
        if "login" in url and "subscription" not in url:
            self.log("session cookies did not reach subscription page")
            return False
        self.log("session cookies valid")
        return True

    def _login(self, username: str, password: str) -> bool:
        try:
            self.log(f"opening Proton login for {username[:3]}***")
            self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
            time.sleep(2)

            username_input = self.page.locator(
                "input#username, input[name='username'], input[type='email']"
            ).first
            username_input.wait_for(state="visible", timeout=45000)
            username_input.fill(username)
            self._click_continue()
            time.sleep(2)

            if self._two_factor_required():
                self.log(
                    "ERROR: Proton 2FA required — complete login in a browser and "
                    "export session cookies instead"
                )
                return False

            password_input = self.page.locator(
                "input#password, input[name='password'], input[type='password']"
            ).first
            password_input.wait_for(state="visible", timeout=45000)
            password_input.fill(password)
            self._click_continue()
            return self._wait_for_login_success()
        except PlaywrightTimeout as exc:
            self.log(f"login form not found: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            self.log(f"login error: {exc}")
            return False

    def _click_continue(self) -> None:
        for selector in (
            "button[type='submit']",
            "button.button-solid-norm",
            "button.button-solid",
        ):
            try:
                btn = self.page.locator(selector).first
                if btn.is_visible():
                    btn.click(timeout=3000)
                    return
            except Exception:
                continue
        for btn in self.page.locator("button").all():
            label = (btn.text_content() or "").strip().lower()
            if label in {"sign in", "log in", "continue", "next"}:
                if btn.is_visible() and btn.is_enabled():
                    btn.click()
                    return

    def _two_factor_required(self) -> bool:
        src = self.page.content().lower()
        return any(
            token in src
            for token in (
                "two-factor",
                "two factor",
                "authenticator",
                "verification code",
                "enter the code",
            )
        )

    def _wait_for_login_success(self) -> bool:
        deadline = time.time() + int(os.getenv("PROTON_LOGIN_TIMEOUT", "120"))
        while time.time() < deadline:
            if self._two_factor_required():
                self.log(
                    "ERROR: Proton 2FA required — complete login in a browser and "
                    "export session cookies instead"
                )
                return False
            url = self.page.url.lower()
            if "login" not in url or "subscription" in url:
                if self._session_reaches_invoices():
                    self.log("login successful")
                    return True
            time.sleep(2)
        self.log("login timed out waiting for subscription page")
        return False

    def _auth_pairs_from_cookies(self) -> list[tuple[str, str, str]]:
        pairs: list[tuple[str, str, str]] = []
        for cookie in self.browser.context.cookies():
            name = cookie.get("name") or ""
            if not name.startswith("AUTH-"):
                continue
            uid = name[5:]
            token = cookie.get("value")
            domain = cookie.get("domain") or ""
            if uid and token:
                pairs.append((uid, token, domain))
        pairs.sort(
            key=lambda item: (
                0 if item[2] == "account.proton.me" else 1,
                0 if item[2].endswith("proton.me") else 1,
            )
        )
        return pairs

    def _auth_from_cookies(self) -> tuple[str | None, str | None]:
        pairs = self._auth_pairs_from_cookies()
        if not pairs:
            return None, None
        uid, token, _domain = pairs[0]
        return uid, token

    def _uid_from_cookies(self) -> str | None:
        uid, _token = self._auth_from_cookies()
        return uid

    def _auth_token_from_cookies(self) -> str | None:
        _uid, token = self._auth_from_cookies()
        return token

    def _prepare_invoices_session(self) -> None:
        """Open invoices page and capture Proton API headers from browser traffic."""
        captured_headers: dict[str, str] = {}
        self._captured_invoices = []

        def on_request(request) -> None:
            if "/api/" not in request.url:
                return
            for key in (
                "x-pm-appversion",
                "x-pm-uid",
                "x-pm-locale",
                "accept",
                "authorization",
            ):
                val = request.headers.get(key)
                if val:
                    captured_headers[key.lower()] = val

        def on_response(response) -> None:
            url = response.url
            if "/payments/" not in url or "/invoices" not in url:
                return
            if response.request.method != "GET":
                return
            try:
                if response.ok:
                    payload = response.json()
                    batch = payload.get("Invoices")
                    if isinstance(batch, list):
                        self._captured_invoices.extend(batch)
            except Exception:
                pass

        self.page.on("request", on_request)
        self.page.on("response", on_response)
        try:
            self.page.goto(INVOICES_PAGE, wait_until="domcontentloaded")
            time.sleep(5)
            try:
                self.page.wait_for_selector(
                    "[data-testid*='invoice' i], table, [class*='invoice' i]",
                    timeout=15000,
                )
            except PlaywrightTimeout:
                pass
        finally:
            self.page.remove_listener("request", on_request)
            self.page.remove_listener("response", on_response)

        uid, token = self._auth_from_cookies()
        if not uid:
            uid = captured_headers.get("x-pm-uid")
        auth: str | None = None
        if token:
            auth = f"Bearer {token}"
        elif captured_headers.get("authorization"):
            auth = captured_headers["authorization"]
        self._pm_headers = {
            "Accept": captured_headers.get("accept", PM_ACCEPT),
            "x-pm-appversion": captured_headers.get(
                "x-pm-appversion", PM_APPVERSION_FALLBACK
            ),
            "x-pm-locale": captured_headers.get("x-pm-locale", PM_LOCALE_FALLBACK),
            "User-Agent": USER_AGENT,
        }
        if uid:
            self._pm_headers["x-pm-uid"] = uid
        if auth:
            self._pm_headers["Authorization"] = auth

        self.log(
            "Proton API headers ready "
            f"(appversion={self._pm_headers['x-pm-appversion']}, "
            f"uid={'set' if uid else 'missing'}, "
            f"auth={'set' if auth else 'missing'})"
        )

    def _api_headers(self) -> dict[str, str]:
        if not self._pm_headers:
            self._prepare_invoices_session()
        return dict(self._pm_headers or {})

    @staticmethod
    def _dedupe_invoices(invoices: list[dict]) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for inv in invoices:
            invoice_id = str(inv.get("ID") or "").strip()
            if invoice_id and invoice_id not in seen:
                seen.add(invoice_id)
                out.append(inv)
        return out

    def _list_invoices_api(self) -> list[dict] | None:
        """Return invoice dicts from Proton payments API, or None on hard failure."""
        request = self.browser.context.request
        collected: list[dict] = []
        page = 0
        page_size = 50
        total: int | None = None

        while True:
            url = (
                f"{API_BASE}/payments/v5/invoices"
                f"?Page={page}&PageSize={page_size}&Owner={INVOICE_OWNER_USER}"
            )
            try:
                resp = request.get(url, headers=self._api_headers(), timeout=60000)
            except Exception as exc:  # noqa: BLE001
                self.log(f"invoice API request failed: {exc}")
                return None if page == 0 else collected

            if resp.status == 401 or resp.status == 403:
                self.log(f"invoice API auth failed ({resp.status})")
                return None
            if not resp.ok:
                self.log(f"invoice API HTTP {resp.status}: {resp.text()[:200]}")
                return None if page == 0 else collected

            try:
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                self.log(f"invoice API invalid JSON: {exc}")
                return None if page == 0 else collected

            batch = payload.get("Invoices") or []
            if total is None:
                total = int(payload.get("Total", len(batch)))
            collected.extend(batch)
            if len(batch) < page_size or len(collected) >= total:
                break
            page += 1

        return collected

    def _parse_invoice_ids_from_page(self) -> list[str]:
        """Extract invoice IDs from the loaded subscription#invoices page."""
        ids: list[str] = []
        seen: set[str] = set()

        def add_id(raw: str) -> None:
            token = raw.strip()
            if token.isdigit() and len(token) >= 4 and token not in seen:
                seen.add(token)
                ids.append(token)
            elif re.fullmatch(r"[A-F0-9]{8,32}", token, re.I):
                upper = token.upper()
                if upper not in seen:
                    seen.add(upper)
                    ids.append(upper)

        try:
            found = self.page.evaluate(
                """() => {
                    const ids = new Set();
                    const add = (value) => {
                        if (!value) return;
                        for (const match of String(value).matchAll(/\\b(\\d{4,})\\b/g)) {
                            ids.add(match[1]);
                        }
                    };
                    for (const el of document.querySelectorAll(
                        '[data-testid*="invoice" i], [data-id], a[href*="invoice" i], button'
                    )) {
                        add(el.getAttribute('data-id'));
                        add(el.getAttribute('data-testid'));
                        add(el.getAttribute('href'));
                        add(el.textContent);
                    }
                    for (const row of document.querySelectorAll('tr, li, [role="row"]')) {
                        const text = row.textContent || '';
                        if (/invoice/i.test(text)) add(text);
                    }
                    return [...ids];
                }"""
            )
            for token in found or []:
                add_id(str(token))
        except Exception:
            pass

        html = self.page.content()
        for pattern in (
            r"/invoices/(\d{4,})",
            r"/invoices/([A-F0-9]{8,32})",
            r'"ID"\s*:\s*"(\d{4,})"',
            r'"ID"\s*:\s*"([A-F0-9]{8,32})"',
            r'"InvoiceID"\s*:\s*"(\d{4,})"',
        ):
            for match in re.findall(pattern, html, re.I):
                add_id(match)

        return ids

    def _list_invoices_ui(self) -> list[dict]:
        """Scrape invoice IDs from the subscription page (fallback)."""
        if self._captured_invoices:
            return self._dedupe_invoices(self._captured_invoices)

        if "login" in self.page.url.lower() and "subscription" not in self.page.url.lower():
            self.page.goto(INVOICES_PAGE, wait_until="domcontentloaded")
            time.sleep(5)
        if "login" in self.page.url.lower() and "subscription" not in self.page.url.lower():
            self.log("not authenticated on subscription page")
            return []

        ids = self._parse_invoice_ids_from_page()
        if not ids:
            self.log("could not parse invoice IDs from UI")
        return [{"ID": i} for i in ids]

    @staticmethod
    def _invoice_date(inv: dict) -> str:
        ts = inv.get("CreateTime") or inv.get("ModifyTime")
        if ts:
            try:
                return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                pass
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _download_pdf_api(self, invoice_id: str) -> tuple[bytes | None, int | None]:
        """Fetch invoice PDF via payments API (v5 first, v4 for legacy invoices)."""
        last_status: int | None = None
        for version in ("v5", "v4"):
            url = f"{API_BASE}/payments/{version}/invoices/{invoice_id}"
            try:
                resp = self.browser.context.request.get(
                    url, headers=self._api_headers(), timeout=120000
                )
            except Exception as exc:  # noqa: BLE001
                self.log(f"PDF API error for {invoice_id} ({version}): {exc}")
                continue
            last_status = resp.status
            if not resp.ok:
                if resp.status == 404:
                    continue
                self.log(f"PDF API HTTP {resp.status} for {invoice_id} ({version})")
                return None, resp.status
            body = resp.body()
            if body.startswith(b"%PDF"):
                if version != "v5":
                    self.log(f"PDF fetched via payments/{version} for {invoice_id}")
                return body, resp.status
            self.log(f"PDF API returned non-PDF for {invoice_id} ({version})")
        if last_status == 404:
            self.log(f"PDF API HTTP 404 for {invoice_id} on v5/v4")
        return None, last_status

    def _save_captured_pdf(self, invoice_id: str, pdf_bytes: bytes) -> Path | None:
        tmp = self.download_dir / f".incoming-{invoice_id}.pdf"
        tmp.write_bytes(pdf_bytes)
        return tmp if tmp.is_file() else None

    def _download_pdf_ui(self, invoice_id: str) -> Path | None:
        """Last-resort: open row actions on the invoices page and capture the PDF."""
        self.page.goto(INVOICES_PAGE, wait_until="domcontentloaded")
        time.sleep(3)
        captured_pdf: list[bytes] = []

        def on_response(response) -> None:
            if f"/invoices/{invoice_id}" not in response.url:
                return
            try:
                if response.ok:
                    body = response.body()
                    if body.startswith(b"%PDF"):
                        captured_pdf.append(body)
            except Exception:
                pass

        self.page.on("response", on_response)
        try:
            row = self.page.locator(f"tr:has-text('ID{invoice_id}')").first
            if row.count() == 0:
                row = self.page.locator(f"text=ID{invoice_id}").first
            row.scroll_into_view_if_needed(timeout=15000)
            menu = row.locator("button").last
            if menu.count() == 0:
                self.log(f"UI download: no action menu for {invoice_id}")
                return None
            with self.page.expect_download(timeout=90000) as dl_info:
                menu.click()
                time.sleep(0.5)
                dl_btn = self.page.locator(
                    "[role='menuitem']:has-text('Download'), button:has-text('Download')"
                ).first
                dl_btn.click(timeout=10000)
            download = dl_info.value
            tmp = self.download_dir / f".incoming-{invoice_id}.pdf"
            download.save_as(str(tmp))
            return tmp if tmp.is_file() else None
        except PlaywrightTimeout:
            if captured_pdf:
                return self._save_captured_pdf(invoice_id, captured_pdf[0])
            self.log(f"UI download timeout for {invoice_id}")
            return None
        except Exception as exc:  # noqa: BLE001
            if captured_pdf:
                return self._save_captured_pdf(invoice_id, captured_pdf[0])
            self.log(f"UI download failed for {invoice_id}: {exc}")
            return None
        finally:
            self.page.remove_listener("response", on_response)

    def _process_invoice(self, inv: dict, result: RunResult) -> None:
        invoice_id = str(inv.get("ID") or "").strip()
        if not invoice_id:
            result.failed += 1
            return

        date = self._invoice_date(inv)
        target = self.target_path(date, invoice_id)
        if self.already_known(invoice_id, target):
            self.log(f"skip already downloaded: {invoice_id}")
            result.skipped += 1
            return

        pdf_bytes, _status = self._download_pdf_api(invoice_id)
        if pdf_bytes:
            target.write_bytes(pdf_bytes)
        else:
            self.log(f"API download failed for {invoice_id}, trying UI")
            tmp = self._download_pdf_ui(invoice_id)
            if not tmp:
                result.failed += 1
                return
            try:
                tmp.replace(target)
            except Exception:
                target.write_bytes(tmp.read_bytes())
                tmp.unlink(missing_ok=True)

        self.record(invoice_id, target, {"date": date, "number": invoice_id})
        self.email(target)
        result.downloaded += 1
        result.new_files.append(target)
        self.log(f"new invoice: {target.name}")
