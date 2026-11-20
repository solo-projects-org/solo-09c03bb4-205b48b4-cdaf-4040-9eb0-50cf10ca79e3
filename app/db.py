"""SQLite storage.

The database is a single file.  WAL mode plus a write transaction that begins
with ``BEGIN IMMEDIATE`` gives us one serialised writer and snapshot reads, so a
query never observes a half-committed report.  All state that a ruling depends
on lives in ``reports``; nothing derived is cached, which is why a restart is
invisible to clients.
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
    serial_first  INTEGER NOT NULL,
    serial_last   INTEGER NOT NULL,
    serial_span   INTEGER NOT NULL,        -- serial_last - serial_first + 1
    failed_count  INTEGER NOT NULL,
    content_hash  TEXT    NOT NULL,        -- over (report_no, range, failures)
    retracted     INTEGER NOT NULL DEFAULT 0 CHECK (retracted IN (0, 1)),
    created_at    TEXT    NOT NULL,
    retracted_at  TEXT,
    CHECK (serial_first >= 0),
    CHECK (serial_last >= serial_first)
);

CREATE TABLE IF NOT EXISTS failed_serials (
    report_id  INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    serial     INTEGER NOT NULL,
    PRIMARY KEY (report_id, serial)
);

-- Covering index: a query for a point interval walks merged runs, and finding
-- the highest-numbered covering report is an index lookup, not a scan.
CREATE INDEX IF NOT EXISTS reports_range
    ON reports (serial_first, serial_last, report_no, retracted);
CREATE INDEX IF NOT EXISTS reports_no
    ON reports (report_no);
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
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
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
