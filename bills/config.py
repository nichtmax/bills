"""Environment- and file-driven configuration for bills and its addons.

Resolution order for any setting: ``/config/settings.json`` (written by the web
UI) -> environment variable -> built-in default. Per-addon cron expressions are
stored in SQLite (``/config/bills.db`` schedules table); ``schedule.json`` is
migrated on first boot and no longer written.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

SETTINGS_FILENAME = "settings.json"
SCHEDULE_FILENAME = "schedule.json"

# Default cron cadence per addon (preserves the previous schedule).
DEFAULT_CRON = {
    "vodafone": "0 6 * * 1",   # weekly, Monday 06:00
    "cursor": "0 6 1 * *",     # monthly, 1st 06:00
}

# Keys whose values must never be rendered in plaintext in the web UI.
SECRET_KEYS = {
    "BILLS_EMAIL_PASSWORD",
    "VODAFONE_PASSWORT",
    "CURSOR_PASSWORD",
}

# Legacy per-addon SMTP keys (migrated to BILLS_*; still read as fallback from env).
_LEGACY_MAIL_KEYS = {
    "BILLS_SMTP_SERVER": ("VODAFONE_SMTP_SERVER", "CURSOR_SMTP_SERVER"),
    "BILLS_SMTP_PORT": ("VODAFONE_SMTP_PORT", "CURSOR_SMTP_PORT"),
    "BILLS_EMAIL_FROM": ("VODAFONE_EMAIL_FROM", "CURSOR_EMAIL_FROM"),
    "BILLS_EMAIL_PASSWORD": ("VODAFONE_EMAIL_PASSWORD", "CURSOR_EMAIL_PASSWORD"),
    "BILLS_EMAIL_TO": ("VODAFONE_EMAIL_TO", "CURSOR_EMAIL_TO"),
}

# Schema that drives the web config form. Each field maps 1:1 to a settings key.
SETTINGS_SCHEMA = [
    {
        "section": "Browser / FlareSolverr",
        "fields": [
            {"key": "BILLS_HEADLESS", "label": "Headless browser (Playwright Chromium)", "type": "bool"},
            {"key": "FLARESOLVERR_ENABLED", "label": "FlareSolverr enabled", "type": "bool"},
            {"key": "FLARESOLVERR_URL", "label": "FlareSolverr URL", "type": "text"},
        ],
    },
    {
        "section": "SMTP (all addons)",
        "fields": [
            {"key": "BILLS_SMTP_SERVER", "label": "SMTP server", "type": "text"},
            {"key": "BILLS_SMTP_PORT", "label": "SMTP port", "type": "text"},
            {"key": "BILLS_EMAIL_FROM", "label": "From address", "type": "text"},
            {"key": "BILLS_EMAIL_PASSWORD", "label": "Password", "type": "secret"},
            {"key": "BILLS_EMAIL_TO", "label": "Recipient", "type": "text"},
        ],
    },
    {
        "section": "Vodafone",
        "fields": [
            {"key": "VODAFONE_USERNAME", "label": "Username", "type": "text"},
            {"key": "VODAFONE_PASSWORT", "label": "Password", "type": "secret"},
        ],
    },
    {
        "section": "Cursor",
        "fields": [
            {"key": "CURSOR_EMAIL", "label": "Email", "type": "text"},
            {"key": "CURSOR_PASSWORD", "label": "Password", "type": "secret"},
            {"key": "CURSOR_STRIPE_PORTAL_URL", "label": "Stripe portal URL (optional)", "type": "text"},
        ],
    },
]


def config_dir() -> str:
    return os.getenv("BILLS_CONFIG_DIR", "/config").strip() or "/config"


def settings_path() -> Path:
    return Path(config_dir()) / SETTINGS_FILENAME


def schedule_path() -> Path:
    return Path(config_dir()) / SCHEDULE_FILENAME


def _load_json(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text("utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    tmp.replace(path)


def load_settings() -> dict:
    return _load_json(settings_path())


def save_settings(data: dict) -> None:
    _save_json(settings_path(), data)


def load_schedule() -> dict:
    return _load_json(schedule_path())


def save_schedule(data: dict) -> None:
    _save_json(schedule_path(), data)


@dataclass
class MailConfig:
    server: str
    port: int
    sender: str
    password: str
    recipient: str

    @property
    def usable(self) -> bool:
        return bool(self.server and self.sender and self.password and self.recipient)

    @property
    def protocol(self) -> str:
        """SMTP transport label stored with mail events."""
        return f"smtp+starttls://{self.server}:{self.port}"


_TRUE = ("1", "true", "yes", "on")


class Config:
    """Reads settings.json + env once and exposes typed accessors."""

    def __init__(self) -> None:
        self._settings = load_settings()
        self.config_dir = config_dir()
        self.download_root = self.get("BILLS_DOWNLOAD_DIR", "/downloads")
        self.tz = self.get("BILLS_TZ", "Europe/Berlin")
        self.run_on_start = self.get_bool("BILLS_RUN_ON_START", False)
        self.app_dir = self.get("BILLS_APP_DIR", "/app")
        self.flaresolverr_enabled = self.get_bool("FLARESOLVERR_ENABLED", False)
        self.flaresolverr_url = self.get("FLARESOLVERR_URL", "http://flaresolverr:8191")
        self.web_port = int(self.get("BILLS_WEB_PORT", "8080") or "8080")

    # -- generic resolution ----------------------------------------------
    def get(self, key: str, default: str = "") -> str:
        v = self._settings.get(key)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
        env = os.getenv(key)
        if env is not None and env.strip() != "":
            return env.strip()
        return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self._settings.get(key)
        if v is not None and str(v).strip() != "":
            return str(v).strip().lower() in _TRUE
        env = os.getenv(key)
        if env is not None and env.strip() != "":
            return env.strip().lower() in _TRUE
        return default

    def is_set(self, key: str) -> bool:
        """True if a (possibly secret) value exists from settings or env."""
        return bool(self.get(key))

    # -- addon selection / scheduling ------------------------------------
    def enabled_addons(self) -> list[str]:
        raw = self.get("BILLS_ADDONS", "vodafone,cursor")
        return [a.strip().lower() for a in raw.split(",") if a.strip()]

    def cron(self, addon: str) -> str:
        from . import db

        db_override = db.get_schedule(addon)
        if db_override:
            return db_override
        sched = load_schedule()
        override = (sched.get(addon) or "").strip()
        if override:
            return override
        return self.get(f"BILLS_{addon.upper()}_CRON", DEFAULT_CRON.get(addon, "0 6 * * *"))

    # -- per-addon behaviour ---------------------------------------------
    def headless(self, addon: str) -> bool:
        specific = self._settings.get(f"{addon.upper()}_HEADLESS") or os.getenv(
            f"{addon.upper()}_HEADLESS"
        )
        if specific is not None and str(specific).strip() != "":
            return str(specific).strip().lower() in _TRUE
        return self.get_bool("BILLS_HEADLESS", True)

    def mail_for(self, addon: str | None = None) -> MailConfig:
        """Shared SMTP settings for every addon (``addon`` is ignored)."""
        _ = addon
        server = self._mail_value("BILLS_SMTP_SERVER")
        port_raw = self._mail_value("BILLS_SMTP_PORT", default="587")
        sender = self._mail_value("BILLS_EMAIL_FROM")
        password = self._mail_value("BILLS_EMAIL_PASSWORD")
        recipient = self._mail_value("BILLS_EMAIL_TO")
        try:
            port = int(port_raw)
        except ValueError:
            port = 587
        return MailConfig(server, port, sender, password, recipient)

    def _mail_value(self, bills_key: str, default: str = "") -> str:
        value = self.get(bills_key)
        if value:
            return value
        for legacy in _LEGACY_MAIL_KEYS.get(bills_key, ()):
            value = self.get(legacy)
            if value:
                return value
        return default
