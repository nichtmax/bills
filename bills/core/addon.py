"""Base class shared by every bill addon."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from .browser import create_driver
from .mailer import Mailer
from .manifest import Manifest


@dataclass
class RunResult:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    new_files: list[Path] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"downloaded={self.downloaded} skipped={self.skipped} "
            f"failed={self.failed}"
        )


_INVALID = re.compile(r'[\\/:*?"<>|]+')


def safe_filename(name: str) -> str:
    return _INVALID.sub("_", name).strip()


class Addon:
    """Subclasses set ``name``/``provider`` and implement ``run``."""

    name: str = "addon"
    provider: str = "Addon"
    #: pull managed downloads back from the Selenium Grid
    needs_grid_downloads: bool = False

    def __init__(self, config: Config) -> None:
        self.config = config
        self.download_dir = Path(config.download_root) / self.name
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = Manifest(self.download_dir / ".manifest.json")
        self.mailer = Mailer(config.mail_for(self.name))

    # -- helpers shared by addons -----------------------------------------
    def make_driver(self):
        return create_driver(
            download_path=str(self.download_dir),
            headless=self.config.headless(self.name),
            enable_downloads=self.needs_grid_downloads,
        )

    def target_filename(self, date: str | None, number: str | None) -> str:
        parts = [date or "unknown-date", self.provider]
        if number:
            parts.append(number)
        return safe_filename(" ".join(parts)) + ".pdf"

    def target_path(self, date: str | None, number: str | None) -> Path:
        return self.download_dir / self.target_filename(date, number)

    def already_known(self, key: str, target: Path) -> bool:
        """Skip-if-exists: known in manifest OR the file is already on disk."""
        return self.manifest.has(key) or target.exists()

    def record(self, key: str, path: Path, extra: dict | None = None) -> None:
        self.manifest.add(key, path.name, extra)

    def email(self, path: Path) -> None:
        self.mailer.send_pdf(
            str(path),
            subject=f"{self.provider} invoice: {path.name}",
            body=f"Attached is the latest {self.provider} invoice: {path.name}",
        )

    def log(self, msg: str) -> None:
        print(f"[{self.name}] {msg}", flush=True)

    # -- to implement -----------------------------------------------------
    def run(self) -> RunResult:  # pragma: no cover - interface
        raise NotImplementedError
