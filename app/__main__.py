"""CLI entry point.

    python -m app run      # daemon: check + deploy every CHECK_INTERVAL_HOURS
    python -m app once     # a single pass, exit code 1 on any failure
    python -m app deploy   # deploy existing certificates only (no ACME request)
    python -m app check    # validate the configuration and exit
    python -m app health   # used by the container HEALTHCHECK
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ConfigError, load_config
from .share import init_share
from .runner import State, build_deployers, run_cycle, run_daemon


def _health() -> int:
    data_dir = os.environ.get("DATA_DIR", "/data")
    try:
        status = json.loads((Path(data_dir) / "status.json").read_text())
        last = datetime.fromisoformat(status["last_cycle"])
        hours = float(os.environ.get("CHECK_INTERVAL_HOURS", "12"))
    except (OSError, ValueError, KeyError):
        print("no status yet")
        return 1
    # Healthy as long as the loop is alive; deploy failures are visible in the logs.
    if datetime.now(timezone.utc) - last > timedelta(hours=hours * 2 + 1):
        print("last cycle too old")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="certsync")
    parser.add_argument("command", choices=["run", "once", "deploy", "check", "health"])
    parser.add_argument("--cert", help="limit to one certificate name")
    parser.add_argument("--force", action="store_true", help="deploy even if unchanged")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.command == "health":
        return _health()

    if args.command == "run":
        # Create the share folders and example files first, so a fresh install
        # has something to edit even before a valid configuration exists.
        init_share(os.environ.get("SHARE_DIR") or "/share")
        run_daemon()
        return 0

    try:
        config = load_config()
        deployers = build_deployers(config)
    except ConfigError as exc:
        logging.getLogger("config").error("%s", exc)
        return 2

    if args.command == "check":
        for cert in config.certs:
            logging.info("cert %-12s %s -> %s", cert.name, ", ".join(cert.domains),
                         ", ".join(cert.targets) or "(no targets)")
        for name, t in config.targets.items():
            logging.info("target %-10s type=%s", name, t.type)
        logging.info("configuration OK")
        return 0

    init_share(config.settings.share_dir)
    state = State(config.settings.data_dir)
    ok = run_cycle(
        config, deployers, state,
        only=args.cert,
        force_deploy=args.force,
        skip_issue=args.command == "deploy",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
