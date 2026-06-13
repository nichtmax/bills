"""Invoice listing from SQLite."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import db
from .addons import REGISTRY
from .config import Config
from .core.mailer import Mailer
from .core.mail_template import invoice_mail_message
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
    number: str
    filename: str
    added: str
    mailed: bool
    mailed_at: str
    mailed_to: str
    mail_sender: str
    mail_protocol: str
    file_exists: bool

    def sort_key(self) -> tuple:
        return (self.date, self.addon, self.filename)


def _parse_filename(name: str) -> tuple[str, str, str]:
    m = _FILENAME_RE.match(name)
    if m:
        return m.group("date"), m.group("provider"), m.group("number") or ""
    return "", "", ""


def list_invoices(cfg: Config, addon: str | None = None) -> list[InvoiceRow]:
    db.sync_pdfs_from_disk(cfg.download_root)
    rows: list[InvoiceRow] = []
    db_rows = db.list_invoices_db(addon)

    for r in db_rows:
        fp = Path(r["file_path"]) if r["file_path"] else Path(cfg.download_root) / r["addon"] / r["filename"]
        exists = fp.is_file()
        date = r["date"] or _parse_filename(r["filename"])[0] or "—"
        number = r["number"] or _parse_filename(r["filename"])[2] or "—"
        mailed, mailed_at, mailed_to, mail_sender, mail_protocol = db.mail_status(int(r["id"]))
        rows.append(
            InvoiceRow(
                id=int(r["id"]),
                addon=r["addon"],
                date=date,
                number=number,
                filename=r["filename"],
                added=r["downloaded_at"] or r["discovered_at"] or "—",
                mailed=mailed,
                mailed_at=mailed_at,
                mailed_to=mailed_to,
                mail_sender=mail_sender,
                mail_protocol=mail_protocol,
                file_exists=exists,
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
    subject, body = invoice_mail_message(
        cfg, addon=addon, provider=provider, filename=filename
    )
    mailer = Mailer(cfg.mail_for(addon))
    sent = mailer.send_pdf(str(path), subject=subject, body=body)
    if not sent:
        key = store.find_key_by_filename(filename) or filename.replace(".pdf", "")
        store.mark_mailed(
            key,
            mailer.cfg.recipient,
            subject=subject,
            success=False,
            error="SMTP failed",
            sender=mailer.cfg.sender,
            protocol=mailer.cfg.protocol,
        )
        return False, "SMTP send failed (check mail settings)"

    key = store.find_key_by_filename(filename)
    if not key:
        _, _, number = _parse_filename(filename)
        key = number or filename.replace(".pdf", "")
        store.ensure_entry(key, filename)
    store.mark_mailed(
        key,
        mailer.cfg.recipient,
        subject=subject,
        success=True,
        sender=mailer.cfg.sender,
        protocol=mailer.cfg.protocol,
    )
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
