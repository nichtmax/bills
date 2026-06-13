"""Proton VPN invoice downloader via the Proton payments API.

Invoices page: https://account.protonvpn.com/subscription#invoices

Authentication uses exported session cookies (same approach as Cursor). With valid
cookies the addon lists invoices through ``GET /api/payments/v5/invoices`` and
downloads each PDF from ``GET /api/payments/v5/invoices/{id}``. Falls back to
clicking Download in the web UI if the API is unavailable.
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
from ..core.browser import inject_cookies

INVOICES_PAGE = "https://account.protonvpn.com/subscription#invoices"
API_BASE = "https://account.proton.me/api"
INVOICE_OWNER_USER = 0


class AuthenticationError(Exception):
    """Session cookies missing or invalid."""


class ProtonAddon(Addon):
    name = "proton"
    provider = "Proton"

    def run(self) -> RunResult:
        result = RunResult()
        try:
            cookies = self._load_session_cookies()
        except AuthenticationError as exc:
            self.log(f"ERROR: {exc}")
            result.failed = 1
            return result

        self.browser = self.make_browser()
        self.page = self.browser.page
        try:
            self.log(f"injecting {len(cookies)} session cookies")
            inject_cookies(self.browser.context, cookies, log=self.log)

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
        custom = os.getenv("PROTON_SESSION_COOKIES_FILE", "").strip()
        if custom:
            return Path(custom)
        return Path(self.config.config_dir) / "proton-session-cookies.json"

    def _load_session_cookies(self) -> list[dict]:
        raw = os.getenv("PROTON_SESSION_COOKIES", "").strip()
        source = "PROTON_SESSION_COOKIES"
        if not raw:
            path = self._cookies_file()
            if path.is_file():
                raw = path.read_text(encoding="utf-8").strip()
                source = str(path)
        if not raw:
            raise AuthenticationError(
                "session cookies required — export from a logged-in browser and set "
                "PROTON_SESSION_COOKIES or place JSON at "
                f"{self._cookies_file()}"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AuthenticationError(f"{source} is invalid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise AuthenticationError(f"{source} must be a JSON array of cookies")
        return data

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
                resp = request.get(url, timeout=60000)
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

    def _list_invoices_ui(self) -> list[dict]:
        """Scrape invoice IDs from the subscription page (fallback)."""
        self.page.goto(INVOICES_PAGE, wait_until="domcontentloaded")
        time.sleep(5)
        if "login" in self.page.url.lower() and "subscription" not in self.page.url.lower():
            self.log("session cookies did not reach subscription page")
            return []

        # Proton renders invoice rows with action menus; collect stable IDs from links/buttons.
        ids: list[str] = []
        seen: set[str] = set()
        for el in self.page.locator("[data-testid*='invoice'], tr, li").all():
            try:
                text = (el.text_content() or "").strip()
                for match in re.findall(r"\b([A-F0-9]{8,})\b", text):
                    if match not in seen and len(match) >= 8:
                        seen.add(match)
                        ids.append(match)
            except Exception:
                continue

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

    def _download_pdf_api(self, invoice_id: str) -> bytes | None:
        url = f"{API_BASE}/payments/v5/invoices/{invoice_id}"
        try:
            resp = self.browser.context.request.get(url, timeout=120000)
        except Exception as exc:  # noqa: BLE001
            self.log(f"PDF API error for {invoice_id}: {exc}")
            return None
        if not resp.ok:
            self.log(f"PDF API HTTP {resp.status} for {invoice_id}")
            return None
        body = resp.body()
        if body.startswith(b"%PDF"):
            return body
        self.log(f"PDF API returned non-PDF for {invoice_id}")
        return None

    def _download_pdf_ui(self, invoice_id: str) -> Path | None:
        """Last-resort: trigger browser download from the invoices page."""
        self.page.goto(INVOICES_PAGE, wait_until="domcontentloaded")
        time.sleep(3)
        try:
            row = self.page.locator(f"text={invoice_id}").first
            row.scroll_into_view_if_needed()
            # Open action menu then Download (Proton UI pattern).
            menu = row.locator("xpath=ancestor::tr//button").last
            if menu.count() == 0:
                menu = self.page.locator("button:has-text('Download')").first
            with self.page.expect_download(timeout=90000) as dl_info:
                if menu.count():
                    menu.click()
                    time.sleep(0.5)
                    dl = self.page.locator("text=Download").first
                    if dl.count():
                        dl.click()
                else:
                    return None
            download = dl_info.value
            tmp = self.download_dir / f".incoming-{invoice_id}.pdf"
            download.save_as(str(tmp))
            return tmp if tmp.is_file() else None
        except PlaywrightTimeout:
            self.log(f"UI download timeout for {invoice_id}")
            return None
        except Exception as exc:  # noqa: BLE001
            self.log(f"UI download failed for {invoice_id}: {exc}")
            return None

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

        pdf_bytes = self._download_pdf_api(invoice_id)
        if pdf_bytes:
            target.write_bytes(pdf_bytes)
        else:
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
