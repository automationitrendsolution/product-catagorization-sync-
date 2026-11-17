"""Check / sync pipeline."""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from .config import TARGET_TABLE, Settings
from .database import PowerBIDatabase
from .exceptions import OutOfSyncError, VerificationError
from .logging_config import get_logger
from .merge import SOURCES, Diff, MergeResult, build_desired, diff
from .runlock import run_lock

logger = get_logger("main")


class RunMode(Enum):
    CHECK = "check"
    SYNC = "sync"


@dataclass(frozen=True)
class RunOptions:
    mode: RunMode
    dry_run: bool = False
    use_lock: bool = True
    #: --sync only: skip the run when no source (or the target) changed since the last verified sync.
    if_changed: bool = False


class SyncPipeline:
    def __init__(self, settings: Settings, options: RunOptions) -> None:
        self.settings = settings
        self.options = options

    def run(self) -> None:
        started = time.monotonic()
        with run_lock(self.settings.lock_file, enabled=self.options.use_lock):
            with PowerBIDatabase(self.settings) as db:
                db.check_schema()
                if self.options.if_changed and self.options.mode is RunMode.SYNC:
                    fp = db.fingerprint()
                    last = self._load_state()
                    if last.get("fingerprint") == fp:
                        logger.info("No new data in the 4 sources or the target since the last verified sync "
                                    "(%s); nothing to do", last.get("synced_at"))
                        logger.info("status : SUCCESS (no change)")
                        return
                    changed = [t for t in fp if last.get("fingerprint", {}).get(t) != fp[t]]
                    logger.info("Data changed since the last verified sync in: %s", ", ".join(changed))
                self._run_sync(db, started)

    def _run_sync(self, db: PowerBIDatabase, started: float) -> None:
        merged = build_desired(db.fetch_sources(), marketplaces=self.settings.marketplaces,
                               ca_mirrors_us=self.settings.ca_mirrors_us)
        for w in merged.warnings:
            logger.warning(w)
        for reason, n in sorted(merged.skipped.items()):
            logger.info("Skipped %d row(s): %s", n, reason)
        d = diff(merged.rows, db.fetch_target())
        report = self._report(merged, d, "before")

        dups_before = set(d.duplicate_target_keys)
        self._log_duplicates(d, db)

        if self.options.mode is RunMode.CHECK:
            self._finish(started, "IN SYNC" if d.in_sync else "OUT OF SYNC", report)
            if not d.in_sync:
                raise OutOfSyncError(
                    f"{len(d.inserts)} source row(s) missing from {TARGET_TABLE}, "
                    f"{len(d.updates)} row(s) with different values (details: {report})")
            return
        if d.in_sync:
            if not self.options.dry_run:
                self._save_state(db.fingerprint())
            self._finish(started, "SUCCESS (already in sync, nothing written)", report)
            return
        if self.options.dry_run:
            self._finish(started, f"DRY RUN: would update {len(d.updates)} and insert "
                                  f"{len(d.inserts)} row(s); nothing written", report)
            return

        db.backup_target(self.settings.backups_dir)
        updated, inserted = db.apply(d)
        logger.info("Committed: %d row(s) updated, %d row(s) inserted", updated, inserted)

        after = diff(merged.rows, db.fetch_target())
        report_after = self._report(merged, after, "after")
        if not after.in_sync:
            raise VerificationError(
                f"after the sync {len(after.inserts)} row(s) are still missing and "
                f"{len(after.updates)} still differ (details: {report_after})")
        new_dups = set(after.duplicate_target_keys) - dups_before
        if new_dups:
            raise VerificationError(f"the sync created duplicate (MarketPlace, Child ASIN) keys: {sorted(new_dups)[:5]}")
        logger.info("Verified: every source (MarketPlace, Child ASIN) that is unique in %s holds the source values%s",
                    TARGET_TABLE, f" ({len(after.skipped_duplicates)} duplicated key(s) left as they are)"
                    if after.skipped_duplicates else "")
        self._save_state(db.fingerprint())
        self._prune_backups()
        self._finish(started, f"SUCCESS: {updated} updated, {inserted} inserted, verified", report_after)

    @staticmethod
    def _log_duplicates(d: Diff, db: PowerBIDatabase) -> None:
        """Duplicated keys no longer stop the run: they are left untouched and reported."""
        db_dups = db.duplicate_target_keys()
        if not d.duplicate_target_keys and not db_dups:
            logger.info("Unique: every (MarketPlace, Child ASIN) appears once in %s", TARGET_TABLE)
            return
        logger.warning("%d (MarketPlace, Child ASIN) key(s) appear more than once in %s; they are NOT updated "
                       "(%d of them come from a source): %s", len(d.duplicate_target_keys), TARGET_TABLE,
                       len(d.skipped_duplicates), sorted(d.duplicate_target_keys)[:10])
        logger.info("All other keys are unique and are updated / validated")

    # -- reporting ----------------------------------------------------------
    def _report(self, merged: MergeResult, d: Diff, stage: str) -> Path:
        missing = {r.key for r in d.inserts}
        changed = {w.key for w, _, _ in d.updates}
        skipped = {r.key for r in d.skipped_duplicates}
        logger.info("Validation (%s) by (MarketPlace, Child ASIN):", stage)
        logger.info("  %-40s %6s %8s %8s %9s %9s", "source", "keys", "in tgt", "missing", "differs", "dup-skip")
        for src in SOURCES:
            keys = merged.source_keys[src]
            miss = len(keys & missing)
            label = f"{src} (US + CA copy)" if src == "product_categorisation" and self.settings.ca_mirrors_us else src
            logger.info("  %-40s %6d %8d %8d %9d %9d", label, len(keys), len(keys) - miss, miss,
                        len(keys & changed), len(keys & skipped))
        logger.info("  target rows matching no source (kept, not deleted): %d", len(d.orphans))
        if d.duplicate_target_keys:
            logger.warning("  duplicate keys in target: %d", len(d.duplicate_target_keys))

        self.settings.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.reports_dir / f"validation_{stage}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["status", "MarketPlace", "Child ASIN", "sources", "column", "source_value", "target_value"])
            for r in d.inserts:
                w.writerow(["MISSING_IN_TARGET", *r.key, " + ".join(r.sources), "", "", ""])
            for want, have, ch in d.updates:
                for col, v in ch.items():
                    w.writerow(["VALUE_DIFFERS", *want.key, " + ".join(want.sources), col, v, have.get(col)])
            for t in d.orphans:
                w.writerow(["NOT_IN_ANY_SOURCE", t.get("MarketPlace"), t.get("Child ASIN"), "", "", "", ""])
            for k, n in d.duplicate_target_keys.items():
                w.writerow(["DUPLICATE_IN_TARGET", *k, "", "", n, ""])
            for r in d.skipped_duplicates:
                w.writerow(["SKIPPED_DUPLICATE_IN_TARGET", *r.key, " + ".join(r.sources), "",
                            "not updated: key is not unique in target", ""])
        latest = self.settings.reports_dir / f"validation_{stage}_latest.csv"
        latest.write_bytes(path.read_bytes())
        return path

    # -- change detection state ---------------------------------------------
    @property
    def _state_file(self) -> Path:
        return self.settings.data_dir / "last_sync_state.json"

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, fingerprint: dict) -> None:
        """Written only after a verified sync, so a failed run is retried at the next check."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"synced_at": datetime.now().isoformat(timespec="seconds"),
                                   "fingerprint": fingerprint}, indent=2), encoding="utf-8")
        tmp.replace(self._state_file)

    def _prune_backups(self) -> None:
        cutoff = datetime.now() - timedelta(days=self.settings.backup_retention_days)
        for directory in (self.settings.backups_dir, self.settings.reports_dir):
            for f in directory.glob("*_2*.csv"):
                if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink(missing_ok=True)

    @staticmethod
    def _finish(started: float, status: str, report: Path) -> None:
        logger.info("report : %s", report)
        logger.info("elapsed: %.1fs", time.monotonic() - started)
        logger.info("status : %s", status)
