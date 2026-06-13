"""Env-configurable scheduler that runs each addon on its own cron.

Each scheduled trigger does a ``git pull`` and then runs the addon in a fresh
subprocess (``python -m bills run <addon>``), so code is always up to date and
one addon failing never kills the loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime

from croniter import croniter

from .config import Config


def _now() -> datetime:
    return datetime.now()


def _git_pull(app_dir: str) -> None:
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
        print(f"[scheduler] git pull: {msg[-1] if msg else 'ok'}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] git pull failed: {exc}", flush=True)


def run_addon_subprocess(addon: str, app_dir: str) -> int:
    _git_pull(app_dir)
    print(f"[scheduler] running addon '{addon}'", flush=True)
    proc = subprocess.run([sys.executable, "-m", "bills", "run", addon], cwd=app_dir)
    print(f"[scheduler] addon '{addon}' exited with {proc.returncode}", flush=True)
    return proc.returncode


def schedule(config: Config | None = None) -> None:
    config = config or Config()
    if config.tz:
        os.environ["TZ"] = config.tz
        try:
            time.tzset()
        except AttributeError:
            pass

    addons = config.enabled_addons()
    if not addons:
        print("[scheduler] no addons enabled (BILLS_ADDONS empty)", flush=True)
        return

    base = _now()
    iterators = {a: croniter(config.cron(a), base) for a in addons}
    next_run = {a: iterators[a].get_next(datetime) for a in addons}

    print("[scheduler] starting. Schedule:", flush=True)
    for a in addons:
        print(f"  - {a}: cron '{config.cron(a)}' next {next_run[a]:%Y-%m-%d %H:%M}", flush=True)

    if config.run_on_start:
        print("[scheduler] BILLS_RUN_ON_START=true -> running all addons now", flush=True)
        for a in addons:
            run_addon_subprocess(a, config.app_dir)

    while True:
        due_addon = min(next_run, key=next_run.get)
        due_time = next_run[due_addon]
        sleep_for = max(0.0, (due_time - _now()).total_seconds())
        time.sleep(min(sleep_for, 3600))
        if _now() < due_time:
            continue
        run_addon_subprocess(due_addon, config.app_dir)
        next_run[due_addon] = iterators[due_addon].get_next(datetime)
        print(f"[scheduler] next {due_addon} run: {next_run[due_addon]:%Y-%m-%d %H:%M}", flush=True)
