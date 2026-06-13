"""Vodafone invoice downloader (Angular MeinVodafone portal) via Playwright."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pdfplumber
import PyPDF2
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..core.addon import Addon, RunResult

MONTHS = {
    "januar": "01", "jänner": "01", "jan": "01",
    "februar": "02", "feb": "02",
    "märz": "03", "maerz": "03", "mär": "03", "mar": "03",
    "april": "04", "apr": "04",
    "mai": "05",
    "juni": "06", "jun": "06",
    "juli": "07", "jul": "07",
    "august": "08", "aug": "08",
    "september": "09", "sept": "09", "sep": "09",
    "oktober": "10", "okt": "10", "oct": "10",
    "november": "11", "nov": "11",
    "dezember": "12", "dez": "12", "dec": "12",
}


class VodafoneAddon(Addon):
    name = "vodafone"
    provider = "Vodafone"

    def run(self) -> RunResult:
        result = RunResult()
        username = os.getenv("VODAFONE_USERNAME")
        password = os.getenv("VODAFONE_PASSWORT")
        if not username or not password:
            self.log("ERROR: VODAFONE_USERNAME / VODAFONE_PASSWORT not set")
            result.failed = 1
            return result

        self.browser = self.make_browser()
        self.page = self.browser.page
        self.incoming = self.download_dir / ".incoming"
        self.incoming.mkdir(parents=True, exist_ok=True)
        try:
            if not self._navigate_to_login():
                self.log("could not load login page")
                result.failed = 1
                return result
            if not self._login(username, password):
                self.log("login failed")
                result.failed = 1
                return result
            if not self._navigate_to_my_bills():
                self.log("could not reach 'Meine Rechnungen'")
                result.failed = 1
                return result
            self._download_invoices(result)
        finally:
            self.browser.close()
        return result

    def _navigate_to_login(self) -> bool:
        try:
            self.log("loading Vodafone login page...")
            self.page.goto(
                "https://www.vodafone.de/meinvodafone/account/login",
                wait_until="domcontentloaded",
            )
            try:
                btn = self.page.locator("#dip-consent-summary-accept-all")
                btn.wait_for(state="visible", timeout=5000)
                btn.click()
                time.sleep(2)
            except PlaywrightTimeout:
                pass
            return self._wait_for_angular()
        except Exception as exc:  # noqa: BLE001
            self.log(f"login page error: {exc}")
            return False

    def _wait_for_angular(self) -> bool:
        try:
            self.page.wait_for_selector("app-root", timeout=30000)
            try:
                self.page.wait_for_selector(".spinner", timeout=15000)
                self.page.wait_for_selector(".spinner", state="hidden", timeout=30000)
            except PlaywrightTimeout:
                pass
            time.sleep(5)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"angular wait error: {exc}")
            return False

    def _find_login_fields(self):
        time.sleep(3)
        username_field = None
        for selector in (
            "app-root input[type='text']",
            "app-root input[type='email']",
            "input[type='text']:not([style*='display: none'])",
        ):
            for field in self.page.locator(selector).all():
                if field.is_visible() and field.is_enabled():
                    username_field = field
                    break
            if username_field:
                break
        password_field = None
        for selector in (
            "app-root input[type='password']",
            "input[type='password']:not([style*='display: none'])",
        ):
            for field in self.page.locator(selector).all():
                if field.is_visible() and field.is_enabled():
                    password_field = field
                    break
            if password_field:
                break
        return username_field, password_field

    def _find_login_button(self):
        for selector in (
            "app-root button[type='submit']",
            "xpath=//app-root//button[contains(text(), 'Login') or contains(text(), 'Anmelden')]",
            "xpath=//button[contains(text(), 'Login') or contains(text(), 'Anmelden')]",
        ):
            try:
                loc = self.page.locator(selector)
                for button in loc.all():
                    if button.is_visible() and button.is_enabled():
                        return button
            except Exception:
                continue
        return None

    def _login(self, username: str, password: str) -> bool:
        try:
            username_field, password_field = self._find_login_fields()
            if not username_field or not password_field:
                self.log("login fields not found")
                return False
            username_field.fill(username)
            time.sleep(1)
            password_field.fill(password)
            time.sleep(1)
            button = self._find_login_button()
            if button:
                button.click()
            else:
                password_field.press("Enter")
            return self._wait_for_login_success()
        except Exception as exc:  # noqa: BLE001
            self.log(f"login error: {exc}")
            return False

    def _wait_for_login_success(self) -> bool:
        for _ in range(15):
            url = self.page.url
            src = self.page.content().lower()
            if (
                "/meinvodafone/services" in url
                or "dashboard" in url
                or "meine rechnungen" in src
                or "logout" in src
            ):
                self.log("login successful")
                time.sleep(3)
                return True
            time.sleep(3)
        return True

    def _navigate_to_my_bills(self) -> bool:
        try:
            time.sleep(5)
            selectors = [
                "xpath=//a[.//div[contains(text(), 'Meine Rechnungen')]]",
                "xpath=//a[contains(.//div, 'Rechnungen')]",
                "xpath=//a[.//use[contains(@xlink:href, 'billing-lrg')]]",
                "a.btn.btn-alt.eqHeight",
                "xpath=//a[contains(text(), 'Rechnung')]",
            ]
            for selector in selectors:
                for element in self.page.locator(selector).all():
                    if not element.is_visible():
                        continue
                    text = (element.text_content() or "").lower()
                    if any(k in text for k in ("meine rechnungen", "rechnungen", "herunterladen")):
                        element.scroll_into_view_if_needed()
                        time.sleep(2)
                        element.click()
                        time.sleep(5)
                        return True
            self.log("'Meine Rechnungen' not found")
            return False
        except Exception as exc:  # noqa: BLE001
            self.log(f"navigation error: {exc}")
            return False

    def _download_invoices(self, result: RunResult) -> None:
        time.sleep(5)
        buttons = self._find_invoice_buttons()
        if not buttons:
            self.log("no invoice buttons found")
            return
        self.log(f"found {len(buttons)} invoice button(s)")

        for i, info in enumerate(buttons):
            try:
                year, month = info["year"], info["month"]
                if year and month and self._month_already_present(year, month):
                    self.log(f"skip {info['label']} (already present)")
                    result.skipped += 1
                    continue

                button = info["element"]
                button.scroll_into_view_if_needed()
                time.sleep(2)
                local = self._download_via_playwright(button)
                if not local:
                    self.log(f"download {i + 1}: timeout or failed")
                    result.failed += 1
                    continue

                pdf_date, number = self._extract_invoice_data(local)
                target = self._finalize(local, pdf_date, number, year, month)
                if not target:
                    result.failed += 1
                    continue

                key = number or (f"{year}-{month}" if year and month else target.name)
                if self.store.has(key):
                    self.log(f"already emailed {key}, keeping file only")
                    result.skipped += 1
                else:
                    self.record(key, target, {"date": pdf_date, "number": number})
                    self.email(target)
                    result.downloaded += 1
                    result.new_files.append(target)
                    self.log(f"new invoice: {target.name}")
                time.sleep(3)
            except Exception as exc:  # noqa: BLE001
                self.log(f"error on download {i + 1}: {exc}")
                result.failed += 1

    def _download_via_playwright(self, button) -> Path | None:
        try:
            with self.page.expect_download(timeout=60000) as dl_info:
                button.click()
            download = dl_info.value
            suggested = download.suggested_filename or "invoice.pdf"
            local = self.incoming / suggested
            download.save_as(local)
            if local.exists() and local.stat().st_size > 0:
                return local
        except PlaywrightTimeout:
            return None
        except Exception as exc:  # noqa: BLE001
            self.log(f"download error: {exc}")
        return None

    def _find_invoice_buttons(self) -> list[dict]:
        selectors = [
            'button.ws10-button-link[aria-label*="Rechnung"][aria-label*="PDF"][aria-disabled="false"]',
            'button.ws10-button-link.ws10-button-link--color-monochrome-600[aria-disabled="false"]',
            'xpath=//button[@class="ws10-button-link ws10-button-link--color-monochrome-600" and contains(@aria-label, "Rechnung") and contains(@aria-label, "PDF")]',
            "button.ws10-button-link",
        ]
        found: list[dict] = []
        for selector in selectors:
            try:
                buttons = self.page.locator(selector).all()
            except Exception:
                continue
            for button in buttons:
                try:
                    if not button.is_visible() or not button.is_enabled():
                        continue
                    aria = button.get_attribute("aria-label") or ""
                    disabled = button.get_attribute("aria-disabled") or ""
                    text = button.text_content() or ""
                    is_invoice = (
                        ("rechnung" in aria.lower() and "pdf" in aria.lower())
                        or ("rechnung (pdf)" in text.lower())
                    )
                    if is_invoice and disabled != "true":
                        year, month = self._year_month_from_aria(aria)
                        if not any(b["label"] == aria for b in found):
                            found.append(
                                {"element": button, "label": aria, "year": year, "month": month}
                            )
                except Exception:
                    continue
            if found:
                break
        return found

    def _finalize(self, local: Path, pdf_date, number, year, month) -> Path | None:
        if pdf_date:
            date_part = pdf_date
        elif year and month:
            date_part = f"{year}-{month}-01"
        else:
            self.log("no date available; cannot name file")
            return None
        target = self.target_path(date_part, number)
        counter = 1
        while target.exists():
            target = self.download_dir / f"{target.stem} ({counter}).pdf"
            counter += 1
        try:
            local.replace(target)
        except Exception as exc:  # noqa: BLE001
            self.log(f"rename failed: {exc}")
            return None
        return target

    def _month_already_present(self, year: str, month: str) -> bool:
        prefix = f"{year}-{month}-"
        return any(f.name.startswith(prefix) for f in self.download_dir.glob("*.pdf"))

    def _extract_invoice_data(self, pdf_path: Path):
        try:
            date, number = self._extract_with_pdfplumber(pdf_path)
            if date or number:
                return date, number
        except Exception:
            pass
        try:
            return self._extract_with_pypdf2(pdf_path)
        except Exception:
            return None, None

    def _extract_with_pdfplumber(self, pdf_path: Path):
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return None, None
            page = pdf.pages[0]
            text = page.extract_text() or ""
            date = self._find_date(text)
            number = self._find_number(text)
            if date or number:
                return date, number
            bbox = page.bbox
            width, height = bbox[2], bbox[3]
            for crop in (
                page.crop((0, 0, width, height * 0.5)),
                page.crop((width * 0.66, 0, width, height * 0.33)),
            ):
                t = crop.extract_text()
                if t:
                    date = date or self._find_date(t)
                    number = number or self._find_number(t)
            return date, number

    def _extract_with_pypdf2(self, pdf_path: Path):
        with open(pdf_path, "rb") as fh:
            reader = PyPDF2.PdfReader(fh)
            if not reader.pages:
                return None, None
            text = reader.pages[0].extract_text() or ""
            return self._find_date(text), self._find_number(text)

    def _find_date(self, text: str):
        patterns = [
            r"[Dd]atum:?\s*(\d{1,2})\.\s*(\w+)\s*(\d{4})",
            r"(\d{1,2})\.\s*(\w+)\s*(\d{4})",
            r"[Rr]echnungsdatum:?\s*(\d{1,2})\.\s*(\w+)\s*(\d{4})",
            r"(\d{1,2})\.(\d{1,2})\.(\d{4})",
        ]
        for pattern in patterns:
            for match in re.findall(pattern, text, re.IGNORECASE):
                if len(match) != 3:
                    continue
                day, month, year = match
                if month.lower() in MONTHS:
                    month_num = MONTHS[month.lower()]
                elif month.isdigit():
                    month_num = f"{int(month):02d}"
                else:
                    continue
                try:
                    d, y, m = int(day), int(year), int(month_num)
                except ValueError:
                    continue
                if 1 <= d <= 31 and 1 <= m <= 12 and 2020 <= y <= 2035:
                    return f"{y:04d}-{m:02d}-{d:02d}"
        return None

    def _find_number(self, text: str):
        patterns = [
            r"[Rr]echnungsnummer:?\s*([A-Za-z0-9\-_]+)",
            r"[Rr]echnung\s+[Nn]r\.?:?\s*([A-Za-z0-9\-_]+)",
            r"[Nn]r\.?:?\s*([A-Za-z0-9\-_]{6,})",
            r"(\d{8,})",
        ]
        for pattern in patterns:
            for match in re.findall(pattern, text, re.IGNORECASE):
                if isinstance(match, tuple):
                    match = match[0]
                cleaned = re.sub(r"[^A-Za-z0-9\-_]", "", str(match))
                if 6 <= len(cleaned) <= 20:
                    return cleaned
        return None

    def _year_month_from_aria(self, aria: str):
        match = re.search(r"rechnung\s+(\w+)\s+(\d{4})", aria.lower())
        if match:
            month = MONTHS.get(match.group(1).lower())
            if month:
                return match.group(2), month
        return None, None
