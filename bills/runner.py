"""Shared background run manager used by both the web UI and the scheduler.

Runs ``python -m bills run <addon>`` in a subprocess, streams its combined
output into an in-memory ring buffer (and a per-addon log file under
``/config/logs``), and tracks per-addon status. A per-addon lock prevents the
same addon from running twice concurrently.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from .config import config_dir


class RunStatus:
    def __init__(self, addon: str) -> None:
        self.addon = addon
        self.state = "idle"  # idle | running | success | failed
        self.started: str | None = None
        self.finished: str | None = None
        self.returncode: int | None = None
        self.trigger: str | None = None
        self.lines: deque[str] = deque(maxlen=600)

    def to_dict(self) -> dict:
        return {
            "addon": self.addon,
            "state": self.state,
            "started": self.started,
            "finished": self.finished,
            "returncode": self.returncode,
            "trigger": self.trigger,
            "log": "\n".join(self.lines),
        }


class RunManager:
    def __init__(self) -> None:
        self._status: dict[str, RunStatus] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def status(self, addon: str) -> RunStatus:
        with self._guard:
            return self._status.setdefault(addon, RunStatus(addon))

    def all_status(self) -> dict[str, dict]:
        with self._guard:
            return {a: s.to_dict() for a, s in self._status.items()}

    def _lock(self, addon: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(addon, threading.Lock())

    def is_running(self, addon: str) -> bool:
        return self._lock(addon).locked()

    def run(self, addon: str, *, trigger: str = "manual", pull: bool = True) -> bool:
        """Run an addon synchronously. Returns False if already running."""
        lock = self._lock(addon)
        if not lock.acquire(blocking=False):
            return False
        st = self.status(addon)
        try:
            st.state = "running"
            st.started = datetime.now().isoformat(timespec="seconds")
            st.finished = None
            st.returncode = None
            st.trigger = trigger
            st.lines.clear()

            app_dir = os.getenv("BILLS_APP_DIR", "/app")
            if pull:
                self._git_pull(app_dir, st)

            st.lines.append(f"$ python -m bills run {addon}")
            proc = subprocess.Popen(
                [sys.executable, "-m", "bills", "run", addon],
                cwd=app_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                st.lines.append(line.rstrip("\n"))
            proc.wait()
            st.returncode = proc.returncode
            st.state = "success" if proc.returncode == 0 else "failed"
        except Exception as exc:  # noqa: BLE001
            st.lines.append(f"runner error: {exc}")
            st.state = "failed"
            st.returncode = -1
        finally:
            st.finished = datetime.now().isoformat(timespec="seconds")
            self._write_log(addon, st)
            lock.release()
        return True

    def run_async(self, addon: str, *, trigger: str = "manual", pull: bool = True) -> bool:
        if self.is_running(addon):
            return False
        threading.Thread(
            target=self.run,
            args=(addon,),
            kwargs={"trigger": trigger, "pull": pull},
            daemon=True,
        ).start()
        return True

    def run_all_async(self, addons: list[str], *, trigger: str = "manual", pull: bool = True) -> None:
        def _seq() -> None:
            for addon in addons:
                self.run(addon, trigger=trigger, pull=pull)

        threading.Thread(target=_seq, daemon=True).start()

    def _git_pull(self, app_dir: str, st: RunStatus) -> None:
        if not os.path.isdir(os.path.join(app_dir, ".git")):
            return
        try:
            out = subprocess.run(
                ["git", "-C", app_dir, "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            msg = (out.stdout or out.stderr).strip().splitlines()
            st.lines.append(f"[git] {msg[-1] if msg else 'pull ok'}")
        except Exception as exc:  # noqa: BLE001
            st.lines.append(f"[git] pull failed: {exc}")

    def _write_log(self, addon: str, st: RunStatus) -> None:
        try:
            logdir = Path(config_dir()) / "logs"
            logdir.mkdir(parents=True, exist_ok=True)
            (logdir / f"{addon}-last.log").write_text("\n".join(st.lines), "utf-8")
        except OSError:
            pass


GLOBAL = RunManager()
