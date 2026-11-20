"""Scale: a million-number span must not become a million rows or lozenges."""

from __future__ import annotations

import random
import time

from app.service import build_report, create_report, query_range, retract_report


def test_million_serials_single_report(db):
    create_report(db, build_report(1, 0, 4_999_999, ["17", "3_000_000".replace("_", "")]))
    result = query_range(db, 0, 4_999_999)
    assert result.summary == {"pass": 4_999_998, "fail": 2, "pending": 0, "count": 5_000_000}
    assert len(result.segments) == 5
    assert db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM failed_serials").fetchone()[0] == 2


def test_many_reports_still_bounded_query_time(db):
    """2,000 overlapping reports over 1,000,000 serials."""
    rng = random.Random(1)
    for no in range(1, 2001):
        first = rng.randint(0, 900_000)
        last = first + rng.randint(0, 120_000)
        create_report(db, build_report(no, first, last, [str(rng.randint(first, last))]))
    started = time.perf_counter()
    result = query_range(db, 0, 1_000_000)
    elapsed = time.perf_counter() - started
    assert result.summary["count"] == 1_000_001
    assert elapsed < 5.0, elapsed


def test_retraction_with_many_overlaps_is_still_correct(db):
    for no in range(1, 200):
        create_report(db, build_report(no, 0, 1_000_000, [str(no * 1000)]))
    # Only report 199 rules everywhere; its own failure list has one entry.
    before = query_range(db, 0, 1_000_000).summary
    assert before["fail"] == 1
    assert before["count"] == 1_000_001
    retract_report(db, 199)
    after = query_range(db, 0, 1_000_000).summary
    assert after["fail"] == 1  # now report 198's failure at 198000
    assert after["count"] == 1_000_001
    serial_199 = query_range(db, 199_000, 199_000).summary
    assert serial_199["fail"] == 0  # 199000 now ruled by 198 (pass)
    serial_198 = query_range(db, 198_000, 198_000).summary
    assert serial_198["fail"] == 1


def test_failure_dense_report_truncates_without_reading_everything(db):
    failures = [str(n) for n in range(0, 2_000_000, 1000)]
    create_report(db, build_report(1, 0, 1_999_999, failures))
    started = time.perf_counter()
    result = query_range(db, 0, 1_999_999, max_segments=50)
    elapsed = time.perf_counter() - started
    assert result.complete is False
    assert len(result.segments) <= 50
    assert elapsed < 2.0, elapsed
