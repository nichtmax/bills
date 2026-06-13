"""Environment-driven configuration for bills and its addons."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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


# Default cron cadence per addon (preserves the previous schedule).
DEFAULT_CRON = {
    "vodafone": "0 6 * * 1",   # weekly, Monday 06:00
    "cursor": "0 6 1 * *",     # monthly, 1st 06:00
}


class Config:
    """Reads env once and exposes typed accessors used across the app."""

    def __init__(self) -> None:
        self.download_root = _env("BILLS_DOWNLOAD_DIR", "/downloads")
        self.config_dir = _env("BILLS_CONFIG_DIR", "/config")
        self.tz = _env("BILLS_TZ", "Europe/Berlin")
        self.run_on_start = _bool("BILLS_RUN_ON_START", False)
        self.app_dir = _env("BILLS_APP_DIR", "/app")
        self.selenium_remote_url = _env("SELENIUM_REMOTE_URL")
        self.flaresolverr_enabled = _bool("FLARESOLVERR_ENABLED", False)
        self.flaresolverr_url = _env("FLARESOLVERR_URL", "http://flaresolverr:8191")

    # -- addon selection / scheduling -------------------------------------
    def enabled_addons(self) -> list[str]:
        raw = _env("BILLS_ADDONS", "vodafone,cursor")
        return [a.strip().lower() for a in raw.split(",") if a.strip()]

    def cron(self, addon: str) -> str:
        key = f"BILLS_{addon.upper()}_CRON"
        return _env(key, DEFAULT_CRON.get(addon, "0 6 * * *"))

    # -- per-addon behaviour ----------------------------------------------
    def headless(self, addon: str) -> bool:
        specific = os.getenv(f"{addon.upper()}_HEADLESS")
        if specific is not None and specific.strip() != "":
            return specific.strip().lower() in ("1", "true", "yes", "on")
        return _bool("BILLS_HEADLESS", True)

    def mail_for(self, addon: str) -> MailConfig:
        up = addon.upper()
        server = _env(f"{up}_SMTP_SERVER") or _env("BILLS_SMTP_SERVER")
        port_raw = _env(f"{up}_SMTP_PORT") or _env("BILLS_SMTP_PORT", "587")
        sender = _env(f"{up}_EMAIL_FROM") or _env("BILLS_EMAIL_FROM")
        password = _env(f"{up}_EMAIL_PASSWORD") or _env("BILLS_EMAIL_PASSWORD")
        recipient = _env(f"{up}_EMAIL_TO") or _env("BILLS_EMAIL_TO")
        try:
            port = int(port_raw)
        except ValueError:
            port = 587
        return MailConfig(server, port, sender, password, recipient)
