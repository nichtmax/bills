"""Invoice persistence — SQLite-backed store replacing .manifest.json."""

from __future__ import annotations

from pathlib import Path

from . import db


class InvoiceStore:
    """Tracks invoices and mail status in SQLite (single source of truth)."""

    def __init__(self, addon: str, download_dir: Path) -> None:
        self.addon = addon
        self.download_dir = Path(download_dir)

    def has(self, key: str) -> bool:
        return db.has_invoice(self.addon, key)

    def get(self, key: str) -> dict | None:
        row = db.get_invoice_by_key(self.addon, key)
        return dict(row) if row else None

    def find_key_by_filename(self, filename: str) -> str | None:
        row = db.get_invoice_by_filename(self.addon, filename)
        return row["invoice_key"] if row else None

    def find_id_by_filename(self, filename: str) -> int | None:
        row = db.get_invoice_by_filename(self.addon, filename)
        return int(row["id"]) if row else None

    def record(
        self,
        key: str,
        path: Path,
        extra: dict | None = None,
    ) -> int:
        extra = extra or {}
        return db.upsert_invoice(
            addon=self.addon,
            invoice_key=key,
            filename=path.name,
            date=extra.get("date"),
            number=extra.get("number") or key,
            file_path=str(path),
            sha256=db.file_sha256(path),
            downloaded_at=db._now(),  # noqa: SLF001
        )

    def ensure_entry(self, key: str, filename: str) -> int:
        fp = self.download_dir / filename
        return db.upsert_invoice(
            addon=self.addon,
            invoice_key=key,
            filename=filename,
            file_path=str(fp) if fp.is_file() else None,
            sha256=db.file_sha256(fp) if fp.is_file() else None,
        )

    @staticmethod
    def is_mailed(invoice_id: int) -> bool:
        mailed, _, _, _, _ = db.mail_status(invoice_id)
        return mailed

    @staticmethod
    def mailed_at(invoice_id: int) -> str:
        _, at, _, _, _ = db.mail_status(invoice_id)
        return at

    @staticmethod
    def mailed_to(invoice_id: int) -> str:
        _, _, to, _, _ = db.mail_status(invoice_id)
        return to

    def mark_mailed(
        self,
        key: str,
        recipient: str,
        *,
        subject: str = "",
        success: bool = True,
        error: str | None = None,
        sender: str | None = None,
        protocol: str | None = None,
    ) -> None:
        row = db.get_invoice_by_key(self.addon, key)
        if not row:
            invoice_id = self.ensure_entry(key, key if key.endswith(".pdf") else f"{key}.pdf")
        else:
            invoice_id = int(row["id"])
        db.add_mail_event(
            invoice_id,
            recipient=recipient,
            subject=subject or f"{self.addon} invoice",
            success=success,
            error=error,
            sender=sender,
            protocol=protocol,
        )

    def mark_mailed_by_filename(
        self,
        filename: str,
        recipient: str,
        *,
        subject: str = "",
        success: bool = True,
        error: str | None = None,
        sender: str | None = None,
        protocol: str | None = None,
    ) -> None:
        row = db.get_invoice_by_filename(self.addon, filename)
        if row:
            db.add_mail_event(
                int(row["id"]),
                recipient=recipient,
                subject=subject or f"{self.addon} invoice: {filename}",
                success=success,
                error=error,
                sender=sender,
                protocol=protocol,
            )
