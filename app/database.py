"""MySQL access for powerbidb.

Reads the four sources and the target, and writes the target in ONE
transaction: a staging TEMPORARY table + one joined UPDATE for changed cells,
then batched INSERTs for new keys. Any error rolls everything back.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import mysql.connector
from mysql.connector import Error as MySQLError

from .config import DB_NAME, TARGET_TABLE, Settings
from .exceptions import DatabaseError, SchemaError
from .logging_config import get_logger
from .merge import (GR_COLUMNS, INT_COLUMNS, NON_US_NEW_ROW, SRC_GR, SRC_NON_US, SRC_UK, SRC_US,
                    TARGET_COLUMNS, Diff)

logger = get_logger("db")

#: Columns each source must have (extra columns such as id/Season are ignored).
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    SRC_US: TARGET_COLUMNS,
    SRC_UK: TARGET_COLUMNS,
    SRC_GR: GR_COLUMNS,
    SRC_NON_US: ("id", *NON_US_NEW_ROW),
    TARGET_TABLE: TARGET_COLUMNS,
}
INSERT_CHUNK = 500


def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


class PowerBIDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn = None

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        s = self.settings
        try:
            self._conn = mysql.connector.connect(
                host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password,
                database=DB_NAME, charset="utf8mb4", collation="utf8mb4_0900_ai_ci",
                connection_timeout=s.db_connect_timeout, autocommit=False,
            )
        except MySQLError as exc:
            raise DatabaseError(f"cannot connect to {s.db_host}:{s.db_port}/{DB_NAME}: {exc.msg}") from exc
        logger.info("Connected to %s:%s/%s as %s", s.db_host, s.db_port, DB_NAME, s.db_user)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "PowerBIDatabase":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _cursor(self, **kw) -> Iterator[Any]:
        if self._conn is None:
            raise DatabaseError("not connected")
        cur = self._conn.cursor(**kw)
        try:
            yield cur
        finally:
            cur.close()

    # -- reads --------------------------------------------------------------
    def check_schema(self) -> None:
        """Every source and the target exist with the columns the merge uses."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
                "AND TABLE_NAME IN (" + ",".join(["%s"] * len(REQUIRED_COLUMNS)) + ")",
                (DB_NAME, *REQUIRED_COLUMNS),
            )
            have: dict[str, set[str]] = {}
            for table, column in cur.fetchall():
                have.setdefault(table, set()).add(column)
        problems = []
        for table, cols in REQUIRED_COLUMNS.items():
            if table not in have:
                problems.append(f"table {DB_NAME}.{table} not found")
                continue
            missing = [c for c in cols if c not in have[table]]
            if missing:
                problems.append(f"{table} lacks column(s): {', '.join(missing)}")
        if problems:
            raise SchemaError("; ".join(problems))
        logger.info("Schema OK: 4 sources + %s", TARGET_TABLE)

    def fetch(self, table: str) -> list[dict[str, Any]]:
        cols = REQUIRED_COLUMNS[table]
        try:
            with self._cursor(dictionary=True) as cur:
                cur.execute(f"SELECT {', '.join(q(c) for c in cols)} FROM {q(table)}")
                rows = cur.fetchall()
        except MySQLError as exc:
            raise DatabaseError(f"reading {table} failed: {exc.msg}") from exc
        logger.info("Read %5d rows from %s", len(rows), table)
        return rows

    def fetch_sources(self) -> dict[str, list[dict[str, Any]]]:
        return {t: self.fetch(t) for t in (SRC_US, SRC_GR, SRC_NON_US, SRC_UK)}

    def fetch_target(self) -> list[dict[str, Any]]:
        return self.fetch(TARGET_TABLE)

    def fingerprint(self) -> dict[str, int | None]:
        """CHECKSUM TABLE of the 4 sources and the target: changes whenever any row does."""
        tables = (SRC_US, SRC_GR, SRC_NON_US, SRC_UK, TARGET_TABLE)
        try:
            with self._cursor() as cur:
                cur.execute("CHECKSUM TABLE " + ", ".join(f"{q(DB_NAME)}.{q(t)}" for t in tables))
                rows = cur.fetchall()
        except MySQLError as exc:
            raise DatabaseError(f"CHECKSUM TABLE failed: {exc.msg}") from exc
        return {name.split(".", 1)[-1]: checksum for name, checksum in rows}

    def duplicate_target_keys(self) -> list[tuple[str, str, int]]:
        """(MarketPlace, Child ASIN) pairs stored more than once, counted by MySQL itself."""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT UPPER(TRIM(`MarketPlace`)) mp, UPPER(TRIM(`Child ASIN`)) asin, COUNT(*) n "
                f"FROM {q(TARGET_TABLE)} GROUP BY mp, asin HAVING n > 1 ORDER BY n DESC LIMIT 50")
            return [(mp, asin, int(n)) for mp, asin, n in cur.fetchall()]

    # -- backup -------------------------------------------------------------
    def backup_target(self, backups_dir: Path) -> Path:
        """Whole target table -> CSV, so a bad run can be undone by hand."""
        backups_dir.mkdir(parents=True, exist_ok=True)
        path = backups_dir / f"{TARGET_TABLE}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        with self._cursor() as cur:
            cur.execute(f"SELECT * FROM {q(TARGET_TABLE)}")
            header = [d[0] for d in cur.description]
            rows = cur.fetchall()
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
        logger.info("Backup of %d target rows: %s", len(rows), path)
        return path

    # -- write --------------------------------------------------------------
    def apply(self, d: Diff) -> tuple[int, int]:
        """Write the diff in one transaction. Returns (rows updated, rows inserted)."""
        if d.in_sync:
            return 0, 0
        updated = inserted = 0
        try:
            with self._cursor() as cur:
                if d.updates:
                    updated = self._apply_updates(cur, d)
                if d.inserts:
                    sql = (f"INSERT INTO {q(TARGET_TABLE)} ({', '.join(q(c) for c in TARGET_COLUMNS)}) "
                           f"VALUES ({', '.join(['%s'] * len(TARGET_COLUMNS))})")
                    params = [tuple(r.values.get(c) for c in TARGET_COLUMNS) for r in d.inserts]
                    for i in range(0, len(params), INSERT_CHUNK):
                        cur.executemany(sql, params[i:i + INSERT_CHUNK])
                        inserted += cur.rowcount
            self._conn.commit()
        except MySQLError as exc:
            self._conn.rollback()
            raise DatabaseError(f"write failed and was rolled back: {exc.msg}") from exc
        except Exception:
            self._conn.rollback()
            raise
        return updated, inserted

    def _apply_updates(self, cur, d: Diff) -> int:
        cols = sorted({c for _, _, ch in d.updates for c in ch}, key=TARGET_COLUMNS.index)
        defs = ["k_mp VARCHAR(64) NOT NULL", "k_asin VARCHAR(64) NOT NULL"]
        for i, c in enumerate(cols):
            defs.append(f"c{i} {'INT' if c in INT_COLUMNS else 'TEXT'} NULL")
            defs.append(f"f{i} TINYINT NOT NULL")
        cur.execute("DROP TEMPORARY TABLE IF EXISTS _pc_sync_stage")
        cur.execute(f"CREATE TEMPORARY TABLE _pc_sync_stage ({', '.join(defs)}, KEY (k_mp, k_asin)) "
                    "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci")
        sql = (f"INSERT INTO _pc_sync_stage (k_mp, k_asin, {', '.join(f'c{i}, f{i}' for i in range(len(cols)))}) "
               f"VALUES ({', '.join(['%s'] * (2 + 2 * len(cols)))})")
        params = []
        for _, have, ch in d.updates:
            row: list[Any] = [have["MarketPlace"], have["Child ASIN"]]  # stored values: exact match
            for c in cols:
                row += [ch.get(c), 1 if c in ch else 0]
            params.append(tuple(row))
        for i in range(0, len(params), INSERT_CHUNK):
            cur.executemany(sql, params[i:i + INSERT_CHUNK])
        sets = ", ".join(f"t.{q(c)} = IF(s.f{i} = 1, s.c{i}, t.{q(c)})" for i, c in enumerate(cols))
        # Byte-exact match on the stored key, so the UPDATE can only touch the one
        # row that was diffed, never a duplicated key that was left out on purpose.
        cur.execute(f"UPDATE {q(TARGET_TABLE)} t JOIN _pc_sync_stage s "
                    f"ON t.`MarketPlace` = s.k_mp AND t.`Child ASIN` = s.k_asin "
                    f"AND BINARY t.`MarketPlace` = BINARY s.k_mp AND BINARY t.`Child ASIN` = BINARY s.k_asin "
                    f"SET {sets}")
        n = cur.rowcount
        cur.execute("DROP TEMPORARY TABLE IF EXISTS _pc_sync_stage")
        if n != len(d.updates):
            logger.warning("UPDATE changed %d rows, %d were planned (the re-check below decides)", n, len(d.updates))
        return n
