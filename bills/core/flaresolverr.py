"""Minimal FlareSolverr client used to preflight anti-bot challenges."""

from __future__ import annotations

import json
import os
import urllib.request


def flaresolverr_enabled() -> bool:
    raw = os.getenv("FLARESOLVERR_ENABLED", "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def default_url() -> str:
    return os.getenv("FLARESOLVERR_URL", "http://flaresolverr:8191").strip()


HUMAN_CHALLENGE_MARKERS = (
    "verify you are human",
    "cf-turnstile",
    "challenge-platform",
    "just a moment",
    "checking your browser",
)


def human_challenge_visible(page_source: str) -> bool:
    src = (page_source or "").lower()
    return any(marker in src for marker in HUMAN_CHALLENGE_MARKERS)


class FlareSolverrClient:
    def __init__(self, base_url: str | None = None, timeout: int = 120) -> None:
        self.base_url = (base_url or default_url()).rstrip("/")
        self.timeout = timeout
        self.session_id: str | None = None

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"FlareSolverr request failed: {exc}") from exc

    def start_session(self) -> "FlareSolverrClient":
        result = self._post({"cmd": "sessions.create"})
        self.session_id = result.get("session")
        return self

    def close(self) -> None:
        if self.session_id:
            try:
                self._post({"cmd": "sessions.destroy", "session": self.session_id})
            except RuntimeError:
                pass
            self.session_id = None

    def get(self, url: str) -> dict:
        """Solve a GET request; returns FlareSolverr's ``solution`` dict."""
        payload = {"cmd": "request.get", "url": url, "maxTimeout": self.timeout * 1000}
        if self.session_id:
            payload["session"] = self.session_id
        result = self._post(payload)
        return result.get("solution", {})

    def apply_to_driver(self, driver, solution: dict, log=print) -> int:
        """Push FlareSolverr cookies (and UA) onto a Selenium driver."""
        from .browser import inject_cookies

        cookies = solution.get("cookies", []) or []
        added = inject_cookies(driver, cookies, log=log)
        log(f"  FlareSolverr supplied {added} cookies")
        return added
