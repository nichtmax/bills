"""Invoice listing: merge on-disk PDFs with per-addon manifest metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .addons import REGISTRY
from .config import Config
from .core.manifest import Manifest

_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<provider>[^/\\]+?)(?:\s+(?P<number>[^\s/\\]+))?\.pdf$",
    re.IGNORECASE,
)


@dataclass
class InvoiceRow:
    addon: str
    date: str
    provider: str
    number: str
    filename: str
    added: str
    status: str  # manifest | file-only

    def sort_key(self) -> tuple:
        return (self.date, self.addon, self.filename)


def _parse_filename(name: str) -> tuple[str, str, str]:
    m = _FILENAME_RE.match(name)
    if m:
        return m.group("date"), m.group("provider"), m.group("number") or ""
    return "", "", ""


def _fmt_time(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def list_invoices(cfg: Config, addon: str | None = None) -> list[InvoiceRow]:
    """Return invoice rows for one addon or all known addons."""
    addons = [addon] if addon else sorted(REGISTRY.keys())
    rows: list[InvoiceRow] = []

    for name in addons:
        ddir = Path(cfg.download_root) / name
        if not ddir.is_dir():
            continue
        manifest = Manifest(ddir / ".manifest.json")
        manifest_by_file: dict[str, dict] = {}
        for key, entry in manifest._data.items():  # noqa: SLF001 — read-only merge
            fn = entry.get("filename", "")
            if fn:
                manifest_by_file[fn] = {**entry, "_key": key}

        seen_files: set[str] = set()
        for pdf in sorted(ddir.glob("*.pdf"), key=lambda p: p.name, reverse=True):
            seen_files.add(pdf.name)
            m = manifest_by_file.get(pdf.name, {})
            date, provider, number = _parse_filename(pdf.name)
            if m.get("date") and not date:
                date = m["date"]
            if m.get("number") and not number:
                number = m["number"]
            if not provider:
                provider = REGISTRY[name].provider if name in REGISTRY else name
            added = m.get("added") or _fmt_time(pdf.stat().st_mtime)
            status = "manifest" if pdf.name in manifest_by_file else "file-only"
            if not number and m.get("_key"):
                number = str(m["_key"])
            rows.append(
                InvoiceRow(name, date or "—", provider, number or "—", pdf.name, added, status)
            )

        # manifest entries whose file was removed
        for fn, m in manifest_by_file.items():
            if fn in seen_files:
                continue
            date = m.get("date") or _parse_filename(fn)[0] or "—"
            provider = REGISTRY[name].provider if name in REGISTRY else name
            number = m.get("number") or m.get("_key") or "—"
            rows.append(
                InvoiceRow(
                    name, date, provider, str(number), fn, m.get("added", "—"), "manifest (missing file)"
                )
            )

    rows.sort(key=lambda r: r.sort_key(), reverse=True)
    return rows


def resolve_pdf_path(cfg: Config, addon: str, filename: str) -> Path | None:
    """Safe path resolution for PDF download — no directory traversal."""
    if addon not in REGISTRY:
        return None
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    if not filename.lower().endswith(".pdf"):
        return None
    base = (Path(cfg.download_root) / addon).resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target
