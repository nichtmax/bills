"""Invoice listing from SQLite."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import db
from .addons import REGISTRY
from .config import Config
from .core.mailer import Mailer
from .store import InvoiceStore

_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<provider>[^/\\]+?)(?:\s+(?P<number>[^\s/\\]+))?\.pdf$",
    re.IGNORECASE,
)


@dataclass
class InvoiceRow:
    id: int
    addon: str
    date: str
    provider: str
    number: str
    filename: str
    added: str
    status: str  # tracked | file-only
    mailed: bool
    mailed_at: str
    mailed_to: str
    file_exists: bool

    def sort_key(self) -> tuple:
        return (self.date, self.addon, self.filename)


def _parse_filename(name: str) -> tuple[str, str, str]:
    m = _FILENAME_RE.match(name)
    if m:
        return m.group("date"), m.group("provider"), m.group("number") or ""
    return "", "", ""


def list_invoices(cfg: Config, addon: str | None = None) -> list[InvoiceRow]:
    rows: list[InvoiceRow] = []
    db_rows = db.list_invoices_db(addon)
    seen_files: set[tuple[str, str]] = set()

    for r in db_rows:
        fp = Path(r["file_path"]) if r["file_path"] else Path(cfg.download_root) / r["addon"] / r["filename"]
        exists = fp.is_file()
        seen_files.add((r["addon"], r["filename"]))
        date = r["date"] or _parse_filename(r["filename"])[0] or "—"
        provider = REGISTRY[r["addon"]].provider if r["addon"] in REGISTRY else r["addon"]
        number = r["number"] or r["invoice_key"] or "—"
        mailed, mailed_at, mailed_to = db.mail_status(int(r["id"]))
        rows.append(
            InvoiceRow(
                id=int(r["id"]),
                addon=r["addon"],
                date=date,
                provider=provider,
                number=str(number),
                filename=r["filename"],
                added=r["downloaded_at"] or r["discovered_at"] or "—",
                status="tracked" if exists else "tracked (missing file)",
                mailed=mailed,
                mailed_at=mailed_at,
                mailed_to=mailed_to,
                file_exists=exists,
            )
        )

    # PDFs on disk not yet in DB
    addons = [addon] if addon else sorted(REGISTRY.keys())
    for name in addons:
        ddir = Path(cfg.download_root) / name
        if not ddir.is_dir():
            continue
        for pdf in ddir.glob("*.pdf"):
            if (name, pdf.name) in seen_files:
                continue
            date, provider, number = _parse_filename(pdf.name)
            if not provider:
                provider = REGISTRY[name].provider if name in REGISTRY else name
            mtime = datetime.now().isoformat(timespec="seconds")
            try:
                mtime = datetime.fromtimestamp(pdf.stat().st_mtime).isoformat(timespec="seconds")
            except OSError:
                pass
            rows.append(
                InvoiceRow(
                    id=0,
                    addon=name,
                    date=date or "—",
                    provider=provider,
                    number=number or "—",
                    filename=pdf.name,
                    added=mtime,
                    status="file-only",
                    mailed=False,
                    mailed_at="",
                    mailed_to="",
                    file_exists=True,
                )
            )

    rows.sort(key=lambda r: r.sort_key(), reverse=True)
    return rows


def resolve_pdf_path(cfg: Config, addon: str, filename: str) -> Path | None:
    path = _safe_invoice_path(cfg, addon, filename)
    if not path or not path.is_file():
        return None
    return path


def _safe_invoice_path(cfg: Config, addon: str, filename: str) -> Path | None:
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
    return target


def _remove_from_manifest(addon_dir: Path, filename: str) -> bool:
    manifest_path = addon_dir / ".manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        data = json.loads(manifest_path.read_text("utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return False
    keys = [k for k, entry in data.items() if entry.get("filename") == filename]
    if not keys:
        return False
    for key in keys:
        del data[key]
    manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    return True


def mail_invoice(cfg: Config, addon: str, filename: str) -> tuple[bool, str]:
    path = resolve_pdf_path(cfg, addon, filename)
    if not path:
        return False, "Invoice not found"

    provider = REGISTRY[addon].provider
    store = InvoiceStore(addon, path.parent)
    subject = f"{provider} invoice: {filename}"
    mailer = Mailer(cfg.mail_for(addon))
    sent = mailer.send_pdf(
        str(path),
        subject=subject,
        body=f"Attached {provider} invoice: {filename}",
    )
    if not sent:
        key = store.find_key_by_filename(filename) or filename.replace(".pdf", "")
        store.mark_mailed(key, mailer.cfg.recipient, subject=subject, success=False, error="SMTP failed")
        return False, "SMTP send failed (check mail settings)"

    key = store.find_key_by_filename(filename)
    if not key:
        _, _, number = _parse_filename(filename)
        key = number or filename.replace(".pdf", "")
        store.ensure_entry(key, filename)
    store.mark_mailed(key, mailer.cfg.recipient, subject=subject, success=True)
    return True, f"sent to {mailer.cfg.recipient}"


def delete_invoice(cfg: Config, addon: str, filename: str) -> tuple[bool, str]:
    """Remove PDF, SQLite row (+ mail_events), and legacy manifest entry."""
    path = _safe_invoice_path(cfg, addon, filename)
    if not path:
        return False, "Invalid invoice path"

    removed_file = False
    if path.is_file():
        try:
            path.unlink()
            removed_file = True
        except OSError as exc:
            return False, f"Could not delete file: {exc}"

    db_removed = db.delete_invoice_by_filename(addon, filename)
    manifest_removed = _remove_from_manifest(path.parent, filename)

    if not removed_file and not db_removed and not manifest_removed:
        return False, "Invoice not found"

    parts = []
    if removed_file:
        parts.append("file")
    if db_removed:
        parts.append("database")
    if manifest_removed:
        parts.append("manifest")
    return True, f"deleted ({', '.join(parts)})"
