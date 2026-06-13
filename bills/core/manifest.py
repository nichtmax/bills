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

    @staticmethod
    def is_mailed(entry: dict | None) -> bool:
        """Legacy entries without ``mailed`` were emailed on download — treat as mailed."""
        if not entry:
            return False
        if "mailed" not in entry:
            return True
        return bool(entry.get("mailed"))

    @staticmethod
    def mailed_at(entry: dict | None) -> str:
        if not entry:
            return ""
        if entry.get("mailed_at"):
            return str(entry["mailed_at"])
        if "mailed" not in entry:
            return str(entry.get("added", ""))
        return ""

    @staticmethod
    def mailed_to(entry: dict | None) -> str:
        if not entry:
            return ""
        return str(entry.get("mailed_to", ""))

    def find_key_by_filename(self, filename: str) -> str | None:
        for key, entry in self._data.items():
            if entry.get("filename") == filename:
                return key
        return None

    def add(self, key: str, filename: str, extra: dict | None = None) -> None:
        entry = {"filename": filename, "added": datetime.now().isoformat(timespec="seconds")}
        if extra:
            entry.update(extra)
        self._data[key] = entry
        self.save()

    def ensure_entry(self, key: str, filename: str) -> None:
        if key not in self._data:
            self.add(key, filename)

    def mark_mailed(self, key: str, recipient: str) -> None:
        entry = self._data.setdefault(key, {})
        entry["mailed"] = True
        entry["mailed_at"] = datetime.now().isoformat(timespec="seconds")
        entry["mailed_to"] = recipient
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")
