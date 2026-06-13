"""Env/file-configurable scheduler.

Re-reads configuration (including SQLite ``schedules``) on every loop
iteration, so cron edits made via the web UI take effect within ~30s without a
container rebuild. Each due addon runs through the shared RunManager (the same
one the web UI uses), so status and logs are unified.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from croniter import croniter

from .config import Config
from .runner import GLOBAL as runner

POLL_SECONDS = 30


def _apply_tz(tz: str) -> None:
    if not tz:
        return
    os.environ["TZ"] = tz
    try:
        time.tzset()
    except AttributeError:
        pass


def next_runs(cfg: Config, base: datetime) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for addon in cfg.enabled_addons():
        try:
            out[addon] = croniter(cfg.cron(addon), base).get_next(datetime)
        except (ValueError, KeyError):
            continue
    return out


def schedule(config: Config | None = None) -> None:
    cfg = config or Config()
    _apply_tz(cfg.tz)

    print("[scheduler] starting. Schedule:", flush=True)
    for addon, when in next_runs(cfg, datetime.now()).items():
        print(f"  - {addon}: cron '{cfg.cron(addon)}' next {when:%Y-%m-%d %H:%M}", flush=True)

    if cfg.run_on_start:
        print("[scheduler] BILLS_RUN_ON_START=true -> running all enabled now", flush=True)
        for addon in cfg.enabled_addons():
            runner.run(addon, trigger="run-on-start")

    while True:
        cfg = Config()  # re-read settings + DB schedules each iteration
        _apply_tz(cfg.tz)
        now = datetime.now()
        upcoming = next_runs(cfg, now)
        if not upcoming:
            time.sleep(POLL_SECONDS)
            continue

        due = min(upcoming, key=upcoming.get)
        due_time = upcoming[due]
        wait = (due_time - now).total_seconds()
        if wait > POLL_SECONDS:
            time.sleep(POLL_SECONDS)
            continue
        if wait > 0:
            time.sleep(wait)
        if runner.run(due, trigger="schedule"):
            print(f"[scheduler] ran {due} (schedule)", flush=True)
        else:
            # Already running (e.g. manual trigger); avoid a tight loop.
            time.sleep(POLL_SECONDS)
