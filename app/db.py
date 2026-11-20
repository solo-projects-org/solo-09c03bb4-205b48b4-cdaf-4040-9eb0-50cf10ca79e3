"""SQLite storage.

The database is a single file.  WAL mode plus a write transaction that begins
with ``BEGIN IMMEDIATE`` gives us one serialised writer and snapshot reads, so a
query never observes a half-committed report.  All state that a ruling depends
on lives in the database (uploaded reports, their effective coverage and
failure lists); nothing derived is cached, which is why a restart is invisible
to clients.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("PM_DB_PATH", "data/meter.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_no     INTEGER NOT NULL UNIQUE,
    serial_first  INTEGER NOT NULL,        -- as uploaded; never rewritten
    serial_last   INTEGER NOT NULL,        -- as uploaded; never rewritten
    serial_span   INTEGER NOT NULL,        -- serial_last - serial_first + 1
    failed_count  INTEGER NOT NULL,
    content_hash  TEXT    NOT NULL,        -- over (report_no, range, failures)
    retracted     INTEGER NOT NULL DEFAULT 0 CHECK (retracted IN (0, 1)),
    created_at    TEXT    NOT NULL,
    retracted_at  TEXT,
    CHECK (serial_first >= 0),
    CHECK (serial_last >= serial_first)
);

-- Effective failure list.  For an uncorrected report this is the uploaded
-- list; for a corrected one the serials inside the moved segment have been
-- shifted to their new positions.  The uploaded list is archived in
-- original_failed_serials at correction time and is never overwritten.
CREATE TABLE IF NOT EXISTS failed_serials (
    report_id  INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    serial     INTEGER NOT NULL,
    PRIMARY KEY (report_id, serial)
);

-- Effective coverage, half-open [lo, hi).  One report may have several
-- disjoint pieces after an erratum (left remainder, right remainder, new
-- segment), so rulings read this table instead of reports.serial_first/last.
CREATE TABLE IF NOT EXISTS report_coverage (
    report_id  INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    lo         INTEGER NOT NULL,
    hi         INTEGER NOT NULL,
    PRIMARY KEY (report_id, lo),
    CHECK (lo >= 0),
    CHECK (hi > lo)
);

-- At most one erratum per report; the UNIQUE constraint enforces it even if
-- the application check were bypassed.
CREATE TABLE IF NOT EXISTS report_corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id   INTEGER NOT NULL UNIQUE REFERENCES reports(id) ON DELETE CASCADE,
    orig_first  INTEGER NOT NULL,
    orig_last   INTEGER NOT NULL,
    new_first   INTEGER NOT NULL,
    new_last    INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    CHECK (orig_last >= orig_first),
    CHECK (new_last >= new_first)
);

-- Uploaded failure list, kept verbatim when a correction rewrites the
-- effective list above.
CREATE TABLE IF NOT EXISTS original_failed_serials (
    report_id  INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    serial     INTEGER NOT NULL,
    PRIMARY KEY (report_id, serial)
);

-- Covering index: a query for a point interval walks merged runs, and finding
-- the highest-numbered covering report is an index lookup, not a scan.
CREATE INDEX IF NOT EXISTS report_coverage_range
    ON report_coverage (lo, hi, report_id);
CREATE INDEX IF NOT EXISTS reports_no
    ON reports (report_no);
"""

# Databases created before the erratum feature have reports but no coverage
# rows.  Backfill one interval per report; corrected reports already have their
# (possibly several) pieces and are left untouched.
BACKFILL_COVERAGE = """
INSERT INTO report_coverage (report_id, lo, hi)
SELECT r.id, r.serial_first, r.serial_last + 1
FROM reports r
WHERE NOT EXISTS (
    SELECT 1 FROM report_coverage c WHERE c.report_id = r.id
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # One connection per request.  FastAPI may run the dependency finaliser and
    # the sync endpoint on different threadpool threads, and within a request
    # the connection is used by one thread at a time.
    conn = sqlite3.connect(
        db_path, timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path | str | None = None) -> None:
    """Create or upgrade the schema in place; safe to call on every start."""
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        # Idempotent: only reports without any coverage row get the legacy
        # single interval.  A corrected report is never re-expanded to its
        # uploaded range.
        conn.execute(BACKFILL_COVERAGE)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialised write transaction; rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Snapshot read transaction: every statement sees the same commit."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
