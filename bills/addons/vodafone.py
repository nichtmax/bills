"""Vodafone invoice downloader (Angular MeinVodafone portal).

Ported from the standalone ``vodafone`` repo. Login + navigation logic is
preserved; downloads now use the shared Selenium Grid's managed-download API
so finished PDFs are pulled back into ``/downloads/vodafone``, then parsed for
date + invoice number and renamed to ``YYYY-MM-DD Vodafone <number>.pdf``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pdfplumber
import PyPDF2
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

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
    # Downloads land in the shared host dataset mounted into both this
    # container and the Selenium Grid at the same path (/downloads/vodafone),
    # so the browser's download appears here directly.
    needs_grid_downloads = False

    def run(self) -> RunResult:
        result = RunResult()
        username = os.getenv("VODAFONE_USERNAME")
        password = os.getenv("VODAFONE_PASSWORT")
        if not username or not password:
            self.log("ERROR: VODAFONE_USERNAME / VODAFONE_PASSWORT not set")
            result.failed = 1
            return result

        self.driver = self.make_driver()
        self.wait = WebDriverWait(self.driver, 30)
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
            try:
                self.driver.quit()
            except Exception:
                pass
        return result

    # -- login / navigation (preserved) -----------------------------------
    def _navigate_to_login(self) -> bool:
        try:
            self.log("loading Vodafone login page...")
            self.driver.get("https://www.vodafone.de/meinvodafone/account/login")
            try:
                btn = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((By.ID, "dip-consent-summary-accept-all"))
                )
                btn.click()
                time.sleep(2)
            except Exception:
                pass
            return self._wait_for_angular()
        except Exception as exc:  # noqa: BLE001
            self.log(f"login page error: {exc}")
            return False

    def _wait_for_angular(self) -> bool:
        try:
            self.wait.until(EC.presence_of_element_located((By.TAG_NAME, "app-root")))
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "spinner"))
                )
                WebDriverWait(self.driver, 30).until(
                    EC.invisibility_of_element_located((By.CLASS_NAME, "spinner"))
                )
            except TimeoutException:
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
            for field in self.driver.find_elements(By.CSS_SELECTOR, selector):
                if field.is_displayed() and field.is_enabled():
                    username_field = field
                    break
            if username_field:
                break
        password_field = None
        for selector in (
            "app-root input[type='password']",
            "input[type='password']:not([style*='display: none'])",
        ):
            for field in self.driver.find_elements(By.CSS_SELECTOR, selector):
                if field.is_displayed() and field.is_enabled():
                    password_field = field
                    break
            if password_field:
                break
        return username_field, password_field

    def _find_login_button(self):
        for selector in (
            "app-root button[type='submit']",
            "//app-root//button[contains(text(), 'Login') or contains(text(), 'Anmelden')]",
            "//button[contains(text(), 'Login') or contains(text(), 'Anmelden')]",
        ):
            try:
                if selector.startswith("//"):
                    buttons = self.driver.find_elements(By.XPATH, selector)
                else:
                    buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for button in buttons:
                    if button.is_displayed() and button.is_enabled():
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
            username_field.clear()
            username_field.send_keys(username)
            time.sleep(1)
            password_field.clear()
            password_field.send_keys(password)
            time.sleep(1)
            button = self._find_login_button()
            if button:
                self.driver.execute_script("arguments[0].click();", button)
            else:
                password_field.send_keys(Keys.RETURN)
            return self._wait_for_login_success()
        except Exception as exc:  # noqa: BLE001
            self.log(f"login error: {exc}")
            return False

    def _wait_for_login_success(self) -> bool:
        for _ in range(15):
            url = self.driver.current_url
            src = self.driver.page_source.lower()
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
                "//a[.//div[contains(text(), 'Meine Rechnungen')]]",
                "//a[contains(.//div, 'Rechnungen')]",
                "//a[.//use[contains(@xlink:href, 'billing-lrg')]]",
                "a.btn.btn-alt.eqHeight",
                "//a[contains(text(), 'Rechnung')]",
            ]
            for selector in selectors:
                if selector.startswith("//"):
                    elements = self.driver.find_elements(By.XPATH, selector)
                else:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    if not element.is_displayed():
                        continue
                    text = (element.get_attribute("textContent") or "").lower()
                    if any(k in text for k in ("meine rechnungen", "rechnungen", "herunterladen")):
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});", element
                        )
                        time.sleep(2)
                        self.driver.execute_script("arguments[0].click();", element)
                        time.sleep(5)
                        return True
            self.log("'Meine Rechnungen' not found")
            return False
        except Exception as exc:  # noqa: BLE001
            self.log(f"navigation error: {exc}")
            return False

    # -- download (shared host dir between app + Grid) --------------------
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

                before = self._current_pdfs()
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", info["element"]
                )
                time.sleep(2)
                self.driver.execute_script("arguments[0].click();", info["element"])

                local = self._wait_for_new_download(before, timeout=60)
                if not local:
                    self.log(f"download {i + 1}: timeout, no file")
                    result.failed += 1
                    continue

                pdf_date, number = self._extract_invoice_data(local)
                target = self._finalize(local, pdf_date, number, year, month)
                if not target:
                    result.failed += 1
                    continue

                key = number or (f"{year}-{month}" if year and month else target.name)
                if self.manifest.has(key):
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

    def _find_invoice_buttons(self) -> list[dict]:
        selectors = [
            'button.ws10-button-link[aria-label*="Rechnung"][aria-label*="PDF"][aria-disabled="false"]',
            'button.ws10-button-link.ws10-button-link--color-monochrome-600[aria-disabled="false"]',
            '//button[@class="ws10-button-link ws10-button-link--color-monochrome-600" and contains(@aria-label, "Rechnung") and contains(@aria-label, "PDF")]',
            "button.ws10-button-link",
        ]
        found: list[dict] = []
        for selector in selectors:
            try:
                if selector.startswith("//"):
                    buttons = self.driver.find_elements(By.XPATH, selector)
                else:
                    buttons = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for button in buttons:
                try:
                    if not button.is_displayed() or not button.is_enabled():
                        continue
                    aria = button.get_attribute("aria-label") or ""
                    disabled = button.get_attribute("aria-disabled") or ""
                    text = button.get_attribute("textContent") or ""
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

    def _current_pdfs(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for f in self.download_dir.glob("*.pdf"):
            try:
                out[f.name] = f.stat().st_mtime
            except OSError:
                continue
        return out

    def _file_complete(self, path: Path, checks: int = 3) -> bool:
        last = -1
        for i in range(checks):
            try:
                size = path.stat().st_size
            except OSError:
                return False
            if size == 0:
                return False
            if size == last and i > 0:
                return True
            last = size
            time.sleep(1)
        return True

    def _wait_for_new_download(self, before: dict[str, float], timeout: int = 60):
        start = time.time()
        while time.time() - start < timeout:
            for name, mtime in self._current_pdfs().items():
                if name not in before or mtime > before.get(name, 0):
                    path = self.download_dir / name
                    if self._file_complete(path):
                        return path
            # account for chrome's partial download files
            if not any(self.download_dir.glob("*.crdownload")):
                time.sleep(1)
            else:
                time.sleep(1)
        return None

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
            target = self.download_dir / (
                f"{target.stem} ({counter}).pdf"
            )
            counter += 1
        try:
            local.replace(target)
        except Exception as exc:  # noqa: BLE001
            self.log(f"rename failed: {exc}")
            return None
        return target

    def _month_already_present(self, year: str, month: str) -> bool:
        prefix = f"{year}-{month}-"
        for f in self.download_dir.glob("*.pdf"):
            if f.name.startswith(prefix):
                return True
        return False

    # -- PDF parsing (preserved) ------------------------------------------
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
            r"[Aa]usstellungsdatum:?\s*(\d{1,2})\.\s*(\w+)\s*(\d{4})",
            r"(\d{1,2})\.(\d{1,2})\.(\d{4})",
            r"(\d{1,2})/(\d{1,2})/(\d{4})",
            r"(\d{1,2})-(\d{1,2})-(\d{4})",
            r"[Dd]atum:?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})",
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
            r"[Ii]nvoice\s+[Nn]umber:?\s*([A-Za-z0-9\-_]+)",
            r"[Bb]elegnummer:?\s*([A-Za-z0-9\-_]+)",
            r"[Rr]echnung-?[Nn]r\.?:?\s*([A-Za-z0-9\-_]+)",
            r"[Nn]r\.?:?\s*([A-Za-z0-9\-_]{6,})",
            r"[Nn]ummer:?\s*([A-Za-z0-9\-_]{6,})",
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
