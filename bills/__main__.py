"""bills CLI.

Usage:
  python -m bills schedule              # run the built-in scheduler loop
  python -m bills run <addon> [...]     # run one or more addons once
  python -m bills run                   # run all enabled addons once
  python -m bills list                  # list registered addons
"""

from __future__ import annotations

import sys

from .addons import REGISTRY, get_addon
from .config import Config
from .scheduler import schedule


def _run(names: list[str]) -> int:
    config = Config()
    if not names:
        names = config.enabled_addons()
    overall = 0
    for name in names:
        try:
            addon_cls = get_addon(name)
        except KeyError as exc:
            print(f"ERROR: {exc}", file=sys.stderr, flush=True)
            overall = 1
            continue
        print(f"=== running addon: {name} ===", flush=True)
        result = addon_cls(config).run()
        print(f"=== {name} done: {result} ===", flush=True)
        if result.failed:
            overall = 2
    return overall


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "schedule"

    if cmd == "schedule":
        schedule()
        return 0
    if cmd == "run":
        return _run(argv[1:])
    if cmd == "list":
        for name in sorted(REGISTRY):
            print(name)
        return 0

    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
