"""Test fixtures: a throwaway SQLite database per test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect, init_db  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "meter.db"
    init_db(path)
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()
