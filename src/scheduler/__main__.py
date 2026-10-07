"""python -m scheduler            run continuously (startup sync, then every 6 h)
python -m scheduler --job NAME    run one job now (same Job as automatic runs)
python -m scheduler --status      execution history / current state
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scheduler.app import (
    EXIT_INTERRUPTED,
    EXIT_UNAVAILABLE,
    configure_logging,
    print_status,
    run_daemon,
    run_manual,
)
from scheduler.config import ALL_JOBS, DEFAULT_CONFIG_PATH, ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scheduler", description="Agente U Scheduler (MVP)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help=f"configuration file (default: {DEFAULT_CONFIG_PATH})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--job", choices=ALL_JOBS, help="run one job now (manual execution)")
    mode.add_argument("--status", action="store_true", help="show scheduler status and history")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    if args.status:
        return print_status(config)
    configure_logging(config)
    try:
        if args.job:
            return run_manual(config, args.job)
        return run_daemon(config)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
