"""Pipeline tests with a fake database: change detection and the uniqueness guard."""

from __future__ import annotations

import pytest

import app.main as main_mod
from app.config import Settings
from app.exceptions import VerificationError
from app.main import RunMode, RunOptions, SyncPipeline
from app.merge import SRC_GR, SRC_NON_US, SRC_UK, SRC_US, TARGET_COLUMNS


def row(asin, mp, **kw):
    r = {c: None for c in TARGET_COLUMNS}
    r.update({"Child ASIN": asin, "MarketPlace": mp, "Child": "c"}, **kw)
    return r


class FakeDB:
    def __init__(self, target, sources, dups=None):
        self.target, self.sources, self.dups = target, sources, dups or []
        self.applied = 0
        self.checksum = 1

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def check_schema(self): pass
    def fetch_sources(self): return self.sources
    def fetch_target(self): return list(self.target)  # same dicts, so apply() persists updates
    def duplicate_target_keys(self): return self.dups
    def fingerprint(self): return {"t": self.checksum}
    def backup_target(self, d): pass

    def apply(self, d):
        self.applied += 1
        for r in d.inserts:
            self.target.append({c: r.values.get(c) for c in TARGET_COLUMNS})
        for want, have, ch in d.updates:
            have.update(ch)
        self.checksum += 1
        return len(d.updates), len(d.inserts)


@pytest.fixture
def settings(tmp_path):
    return Settings(db_host="h", db_port=3306, db_user="u", db_password="p",
                    data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


def run(settings, db, monkeypatch, **opts):
    monkeypatch.setattr(main_mod, "PowerBIDatabase", lambda s: db)
    SyncPipeline(settings, RunOptions(mode=RunMode.SYNC, use_lock=False, **opts)).run()


def sources():
    return {SRC_US: [], SRC_GR: [], SRC_NON_US: [], SRC_UK: [row("B1", "UK")]}


def test_if_changed_skips_until_data_changes(settings, monkeypatch):
    db = FakeDB([], sources())
    run(settings, db, monkeypatch, if_changed=True)            # first run: no state -> syncs
    assert db.applied == 1 and len(db.target) == 1
    run(settings, db, monkeypatch, if_changed=True)            # nothing changed -> skipped
    assert db.applied == 1
    db.sources[SRC_UK].append(row("B2", "UK")); db.checksum += 1   # new source data
    run(settings, db, monkeypatch, if_changed=True)
    assert db.applied == 2 and len(db.target) == 2


def test_failed_verification_does_not_save_state(settings, monkeypatch):
    db = FakeDB([], sources())
    db.apply = lambda d: (0, 0)  # write "succeeds" but changes nothing
    with pytest.raises(VerificationError):
        run(settings, db, monkeypatch, if_changed=True)
    assert not (settings.data_dir / "last_sync_state.json").exists()  # retried at the next check


def test_duplicate_keys_in_target_are_left_alone_unique_keys_still_sync(settings, monkeypatch):
    srcs = sources()
    srcs[SRC_UK] = [row("B1", "UK", Child="new"), row("B2", "UK", Child="new"), row("B3", "UK")]
    target = [row("B1", "UK", Child="old"), row("b1 ", "UK", Child="old2"),   # duplicated key
              row("B2", "UK", Child="old")]                                    # unique key
    db = FakeDB(target, srcs, dups=[("UK", "B1", 2)])
    run(settings, db, monkeypatch)                                 # no exception: verified
    assert db.applied == 1
    by_asin = {r["Child ASIN"]: r["Child"] for r in db.target}
    assert by_asin["B2"] == "new" and by_asin["B3"] == "c"         # unique updated, missing inserted
    assert by_asin["B1"] == "old" and by_asin["b1 "] == "old2"     # duplicates untouched
    report = (settings.data_dir / "reports" / "validation_after_latest.csv").read_text(encoding="utf-8-sig")
    assert "SKIPPED_DUPLICATE_IN_TARGET,UK,B1" in report
