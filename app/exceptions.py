"""Exception hierarchy. Each failure type carries the process exit code."""

from __future__ import annotations


class SyncError(Exception):
    """Base class for every failure."""

    exit_code: int = 1


class ConfigurationError(SyncError):
    """Missing or invalid .env configuration."""

    exit_code = 2


class SchemaError(SyncError):
    """A source or the target table is missing, or lacks an expected column."""

    exit_code = 8


class DatabaseError(SyncError):
    """MySQL connection or transaction failure (the transaction is rolled back)."""

    exit_code = 9


class OutOfSyncError(SyncError):
    """--check found source rows missing from, or different in, the target."""

    exit_code = 10


class LockError(SyncError):
    """Another run already holds the execution lock."""

    exit_code = 11


class TargetKeyError(SyncError):
    """The target holds the same (MarketPlace, Child ASIN) more than once.

    The sync refuses to write: an update would change both rows and nobody
    could tell which one Power BI should use.
    """

    exit_code = 12


class VerificationError(SyncError):
    """After --sync, a re-read of the target still differs from the sources."""

    exit_code = 15
