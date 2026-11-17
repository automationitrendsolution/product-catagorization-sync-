#!/usr/bin/env python3
"""Product categorisation sync: command-line entry point.

Merges product_categorisation (US, mirrored as CA), product_categorisation_uk (UK),
product_categorisation_gr (DE) and product_categorisation_non_us (CA/UK/IT/DE)
into powerbidb.product_categorisation_us_ca_uk_it, matching on
(MarketPlace, Child ASIN).

    python run.py --check            # read-only: is every source row in the target?
    python run.py --sync --dry-run   # what --sync would write; writes nothing
    python run.py --sync             # write, then re-check (what the 13:00 IST task runs)
    python run.py --sync --if-changed  # same, but only when a table changed (the 30-minute task)
    python run.py --check-config     # validate .env only (no DB)

Exit codes: 0 success / in sync, 2 config, 8 schema, 9 database,
10 out of sync (--check), 11 locked,
15 still out of sync after --sync.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import __version__
from app.config import load_settings
from app.exceptions import ConfigurationError, SyncError
from app.logging_config import configure_logging, get_logger
from app.main import RunMode, RunOptions, SyncPipeline


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="product-categorisation-sync",
        description="Sync the four product_categorisation source tables into "
                    "powerbidb.product_categorisation_us_ca_uk_it and validate by (MarketPlace, Child ASIN).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes: 0 ok, 2 config, 8 schema, 9 database, 10 out of sync (--check), "
               "11 locked, 15 verification failed after --sync",
    )
    p.add_argument("--version", action="version", version=f"product-categorisation-sync {__version__}")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Read-only validation report (no writes).")
    modes.add_argument("--sync", action="store_true", help="Insert missing / update changed rows, then verify (default).")
    modes.add_argument("--check-config", action="store_true", help="Validate .env only; no DB.")
    p.add_argument("--dry-run", action="store_true", help="With --sync: report what would change, write nothing.")
    p.add_argument("--if-changed", action="store_true",
                   help="With --sync: exit at once when no source (or the target) changed since the last "
                        "verified sync (CHECKSUM TABLE). What the 30-minute change check runs.")
    p.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    p.add_argument("--no-lock", action="store_true", help="Skip the run lock (supervised runs only).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return exc.exit_code

    configure_logging(settings.log_dir, level=args.log_level or settings.log_level,
                      retention_days=settings.log_retention_days)
    logger = get_logger("cli")
    if args.check_config:
        logger.info("Configuration OK: %r", settings)
        return 0

    options = RunOptions(mode=RunMode.CHECK if args.check else RunMode.SYNC,
                         dry_run=args.dry_run, use_lock=not args.no_lock, if_changed=args.if_changed)
    try:
        SyncPipeline(settings, options).run()
    except SyncError as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        logger.info("status : FAILED (exit %d)", exc.exit_code)
        return exc.exit_code
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130
    except Exception:
        logger.exception("Unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
