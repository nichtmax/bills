"""SQLite persistence for bills — single source of truth under /config/bills.db."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import config_dir

SCHEMA_VERSION = 1
_db_path: Path | None = None
_lock = threading.Lock()


def db_path() -> Path:
    global _db_path
    if _db_path is None:
        _db_path = Path(config_dir()) / "bills.db"
    return _db_path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                addon TEXT NOT NULL,
                invoice_key TEXT NOT NULL,
                filename TEXT NOT NULL,
                date TEXT,
                number TEXT,
                file_path TEXT,
                sha256 TEXT,
                discovered_at TEXT,
                downloaded_at TEXT,
                UNIQUE(addon, invoice_key)
            );
            CREATE INDEX IF NOT EXISTS idx_invoices_addon ON invoices(addon);
            CREATE INDEX IF NOT EXISTS idx_invoices_filename ON invoices(addon, filename);

            CREATE TABLE IF NOT EXISTS mail_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                recipient TEXT,
                subject TEXT,
                sent_at TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mail_invoice ON mail_events(invoice_id);

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                addon TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                exit_code INTEGER,
                trigger TEXT NOT NULL,
                log_summary TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedules (
                addon TEXT PRIMARY KEY,
                cron TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        _migrate_schema(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(mail_events)").fetchall()}
    if "sender" not in cols:
        conn.execute("ALTER TABLE mail_events ADD COLUMN sender TEXT")
    if "protocol" not in cols:
        conn.execute("ALTER TABLE mail_events ADD COLUMN protocol TEXT")


def file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# -- invoices ----------------------------------------------------------------

def upsert_invoice(
    *,
    addon: str,
    invoice_key: str,
    filename: str,
    date: str | None = None,
    number: str | None = None,
    file_path: str | None = None,
    sha256: str | None = None,
    discovered_at: str | None = None,
    downloaded_at: str | None = None,
) -> int:
    now = _now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO invoices(addon, invoice_key, filename, date, number, file_path,
                                 sha256, discovered_at, downloaded_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(addon, invoice_key) DO UPDATE SET
                filename=excluded.filename,
                date=COALESCE(excluded.date, invoices.date),
                number=COALESCE(excluded.number, invoices.number),
                file_path=COALESCE(excluded.file_path, invoices.file_path),
                sha256=COALESCE(excluded.sha256, invoices.sha256),
                downloaded_at=COALESCE(excluded.downloaded_at, invoices.downloaded_at)
            """,
            (
                addon,
                invoice_key,
                filename,
                date,
                number,
                file_path,
                sha256,
                discovered_at or now,
                downloaded_at or now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM invoices WHERE addon=? AND invoice_key=?",
            (addon, invoice_key),
        ).fetchone()
        return int(row["id"])


def get_invoice_by_key(addon: str, invoice_key: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM invoices WHERE addon=? AND invoice_key=?",
            (addon, invoice_key),
        ).fetchone()


def get_invoice_by_filename(addon: str, filename: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM invoices WHERE addon=? AND filename=?",
            (addon, filename),
        ).fetchone()


def has_invoice(addon: str, invoice_key: str) -> bool:
    return get_invoice_by_key(addon, invoice_key) is not None


def list_invoices_db(addon: str | None = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if addon:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE addon=? ORDER BY date DESC, filename DESC",
                (addon,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM invoices ORDER BY date DESC, addon, filename DESC"
            ).fetchall()
        return list(rows)


def delete_invoice_by_filename(addon: str, filename: str) -> bool:
    """Delete invoice row; mail_events cascade via FK."""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM invoices WHERE addon=? AND filename=?",
            (addon, filename),
        )
        return cur.rowcount > 0


def delete_invoice_by_id(invoice_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
        return cur.rowcount > 0


# -- mail_events -------------------------------------------------------------

def add_mail_event(
    invoice_id: int,
    *,
    recipient: str,
    subject: str,
    success: bool = True,
    error: str | None = None,
    sent_at: str | None = None,
    sender: str | None = None,
    protocol: str | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO mail_events(
                invoice_id, recipient, subject, sent_at, success, error, sender, protocol
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                invoice_id,
                recipient,
                subject,
                sent_at or _now(),
                1 if success else 0,
                error,
                sender,
                protocol,
            ),
        )
        return int(cur.lastrowid)


def latest_mail_event(invoice_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM mail_events
            WHERE invoice_id=? AND success=1
            ORDER BY sent_at DESC LIMIT 1
            """,
            (invoice_id,),
        ).fetchone()


def mail_status(invoice_id: int) -> tuple[bool, str, str, str, str]:
    """Return (mailed, sent_at, recipient, sender, protocol) from latest successful send."""
    ev = latest_mail_event(invoice_id)
    if ev:
        return (
            True,
            ev["sent_at"],
            ev["recipient"] or "",
            ev["sender"] or "",
            ev["protocol"] or "",
        )
    return False, "", "", "", ""


def sync_pdfs_from_disk(download_root: str | Path) -> int:
    """Register any on-disk PDFs missing from the database."""
    return _scan_pdfs(Path(download_root))


# -- runs --------------------------------------------------------------------

def start_run(*, addon: str | None, trigger: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs(addon, started_at, trigger) VALUES(?,?,?)",
            (addon, _now(), trigger),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, *, exit_code: int, log_summary: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE runs SET finished_at=?, exit_code=?, log_summary=?
            WHERE id=?
            """,
            (_now(), exit_code, log_summary[:50000], run_id),
        )


def list_runs(limit: int = 50) -> list[sqlite3.Row]:
    with connect() as conn:
        return list(
            conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        )


# -- schedules ---------------------------------------------------------------

def get_schedule(addon: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT cron FROM schedules WHERE addon=?", (addon,)).fetchone()
        return row["cron"] if row else None


def set_schedule(addon: str, cron: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO schedules(addon, cron, updated_at) VALUES(?,?,?)
            ON CONFLICT(addon) DO UPDATE SET cron=excluded.cron, updated_at=excluded.updated_at
            """,
            (addon, cron, _now()),
        )


def delete_schedule(addon: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM schedules WHERE addon=?", (addon,))


def all_schedules() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT addon, cron FROM schedules").fetchall()
        return {r["addon"]: r["cron"] for r in rows}


# -- settings (non-secret operational keys; secrets stay in settings.json) ----

def get_setting(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, _now()),
        )


# -- migration ---------------------------------------------------------------

def _migrate_manifests(download_root: Path) -> tuple[int, int]:
    inv_count = mail_count = 0
    if not download_root.is_dir():
        return 0, 0
    for addon_dir in download_root.iterdir():
        if not addon_dir.is_dir():
            continue
        manifest_path = addon_dir / ".manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        addon = addon_dir.name
        for key, entry in data.items():
            fn = entry.get("filename", "")
            if not fn:
                continue
            fp = addon_dir / fn
            inv_id = upsert_invoice(
                addon=addon,
                invoice_key=key,
                filename=fn,
                date=entry.get("date"),
                number=entry.get("number") or key,
                file_path=str(fp) if fp.is_file() else None,
                sha256=file_sha256(fp) if fp.is_file() else None,
                discovered_at=entry.get("added"),
                downloaded_at=entry.get("added"),
            )
            inv_count += 1
            # Legacy manifest entries = already mailed
            mailed = entry.get("mailed", True)
            if mailed is not False and mailed != "false":
                sent_at = entry.get("mailed_at") or entry.get("added") or _now()
                recipient = entry.get("mailed_to") or ""
                if not latest_mail_event(inv_id):
                    add_mail_event(
                        inv_id,
                        recipient=recipient,
                        subject=f"{addon} invoice (migrated)",
                        success=True,
                        sent_at=sent_at,
                    )
                    mail_count += 1
    return inv_count, mail_count


def _scan_pdfs(download_root: Path) -> int:
    count = 0
    if not download_root.is_dir():
        return 0
    for addon_dir in download_root.iterdir():
        if not addon_dir.is_dir():
            continue
        addon = addon_dir.name
        for pdf in addon_dir.glob("*.pdf"):
            existing = get_invoice_by_filename(addon, pdf.name)
            if existing:
                continue
            key = pdf.stem
            mtime = datetime.fromtimestamp(pdf.stat().st_mtime).isoformat(timespec="seconds")
            upsert_invoice(
                addon=addon,
                invoice_key=key,
                filename=pdf.name,
                file_path=str(pdf),
                sha256=file_sha256(pdf),
                discovered_at=mtime,
                downloaded_at=mtime,
            )
            count += 1
    return count


def _migrate_schedules(config_path: Path) -> int:
    sched_file = config_path / "schedule.json"
    if not sched_file.is_file():
        return 0
    try:
        data = json.loads(sched_file.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    count = 0
    for addon, cron in data.items():
        if cron and not get_schedule(addon):
            set_schedule(addon, cron)
            count += 1
    return count


def _migrate_logs(config_path: Path) -> int:
    logdir = config_path / "logs"
    if not logdir.is_dir():
        return 0
    count = 0
    for logfile in logdir.glob("*-last.log"):
        addon = logfile.name.replace("-last.log", "")
        try:
            content = logfile.read_text("utf-8")
        except OSError:
            continue
        mtime = datetime.fromtimestamp(logfile.stat().st_mtime).isoformat(timespec="seconds")
        with connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM runs WHERE addon=? AND log_summary LIKE ? LIMIT 1",
                (addon, content[:80] + "%"),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO runs(addon, started_at, finished_at, exit_code, trigger, log_summary)
                VALUES(?,?,?,?,?,?)
                """,
                (addon, mtime, mtime, None, "migrated", content[:50000]),
            )
            count += 1
    return count


def migrate_all(download_root: str | Path, cfg_dir: str | Path | None = None) -> dict:
    """One-time migration from JSON files. Safe to call repeatedly."""
    with _lock:
        init_db()
        root = Path(download_root)
        cfg = Path(cfg_dir or config_dir())
        with connect() as conn:
            done = conn.execute(
                "SELECT value FROM schema_meta WHERE key='migrated_v1'"
            ).fetchone()
        if done:
            pdf_new = _scan_pdfs(root)
            return {"skipped": True, "pdfs_scanned": pdf_new}

        inv, mail = _migrate_manifests(root)
        pdfs = _scan_pdfs(root)
        sched = _migrate_schedules(cfg)
        logs = _migrate_logs(cfg)
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('migrated_v1', ?)",
                (_now(),),
            )
        return {
            "invoices_from_manifest": inv,
            "mail_events_inferred": mail,
            "pdfs_scanned": pdfs,
            "schedules_migrated": sched,
            "runs_migrated": logs,
        }
