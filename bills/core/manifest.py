"""Per-addon dedup manifest stored next to the downloaded PDFs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class Manifest:
    """Tracks already-handled invoices by a stable key so we never re-email."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text("utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def has(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def add(self, key: str, filename: str, extra: dict | None = None) -> None:
        entry = {"filename": filename, "added": datetime.now().isoformat(timespec="seconds")}
        if extra:
            entry.update(extra)
        self._data[key] = entry
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")
