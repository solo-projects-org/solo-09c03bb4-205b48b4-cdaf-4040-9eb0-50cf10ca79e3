"""Rules under the ruling engine: latest covering report wins, retraction
re-evaluates, and identical/conflicting re-uploads behave as specified."""

from __future__ import annotations

import pytest

from app.service import (
    CONCL_FAIL,
    CONCL_PASS,
    CONCL_PENDING,
    ConflictError,
    NotFoundError,
    ValidationError,
    build_report,
    create_report,
    parse_failure_csv,
    query_range,
    retract_report,
)


def add(conn, no, first, last, failures=()):
    return create_report(conn, build_report(no, first, last, failures))


def segs(conn, first, last, **kw):
    """Compact view: ``[(lo, hi_exclusive, conclusion, report_no)]``."""
    result = query_range(conn, first, last, **kw)
    return [(s.lo, s.hi, s.conclusion, s.report_no) for s in result.segments]


def test_empty_database_is_all_pending(db):
    assert segs(db, 1, 100) == [(1, 101, CONCL_PENDING, None)]


def test_failure_list_marks_only_listed_serials(db):
    add(db, 1, 1, 1000, ["5", "10", "11"])
    assert segs(db, 1, 1000) == [
        (1, 5, CONCL_PASS, 1),
        (5, 6, CONCL_FAIL, 1),
        (6, 10, CONCL_PASS, 1),
        (10, 12, CONCL_FAIL, 1),
        (12, 1001, CONCL_PASS, 1),
    ]


def test_highest_covering_report_wins_and_can_overturn(db):
    add(db, 1, 1, 100, ["50"])          # older report says 50 failed
    add(db, 2, 40, 60)                  # newer clean report overrides 40..60
    assert segs(db, 45, 55) == [(45, 56, CONCL_PASS, 2)]
    # 50 is inside report 2: the newer report says pass.
    assert segs(db, 50, 50) == [(50, 51, CONCL_PASS, 2)]
    # Outside report 2 the older failure still stands.
    assert segs(db, 61, 100) == [
        (61, 101, CONCL_PASS, 1),
    ]
    assert segs(db, 40, 70) == [
        (40, 61, CONCL_PASS, 2),
        (61, 70 + 1, CONCL_PASS, 1),
    ]
    retract_report(db, 2)
    assert segs(db, 45, 55) == [
        (45, 50, CONCL_PASS, 1),
        (50, 51, CONCL_FAIL, 1),
        (51, 56, CONCL_PASS, 1),
    ]


def test_older_failure_resurfaces_when_newer_report_lacks_the_serial(db):
    add(db, 1, 1, 100, ["50"])
    add(db, 2, 40, 60, ["45"])
    assert segs(db, 40, 60) == [
        (40, 45, CONCL_PASS, 2),
        (45, 46, CONCL_FAIL, 2),
        (46, 61, CONCL_PASS, 2),
    ]


def test_gap_between_reports_is_pending(db):
    add(db, 1, 1, 10)
    add(db, 2, 21, 30)
    assert segs(db, 5, 25) == [
        (5, 11, CONCL_PASS, 1),
        (11, 21, CONCL_PENDING, None),
        (21, 26, CONCL_PASS, 2),
    ]


def test_equal_report_numbers_are_impossible_via_upload(db):
    first = add(db, 7, 1, 10)
    again = add(db, 7, 1, 10)
    assert first.created is True
    assert again.created is False  # identical content: no second row
    assert db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
    with pytest.raises(ConflictError):
        add(db, 7, 1, 10, ["3"])  # different content under an existing number


def test_identical_reupload_is_idempotent_and_keeps_retracted_state(db):
    add(db, 1, 1, 10, ["3"])
    retract_report(db, 1)
    again = add(db, 1, 1, 10, ["3"])
    assert again.created is False
    assert again.report["retracted"] is True
    assert segs(db, 1, 10) == [(1, 11, CONCL_PENDING, None)]


def test_conflicting_reupload_rejected_and_nothing_written(db):
    add(db, 1, 1, 10, ["3"])
    with pytest.raises(ConflictError):
        add(db, 1, 1, 10, ["4"])
    with pytest.raises(ConflictError):
        add(db, 1, 1, 11)  # range differs -> different content
    assert segs(db, 1, 10) == [
        (1, 3, CONCL_PASS, 1),
        (3, 4, CONCL_FAIL, 1),
        (4, 11, CONCL_PASS, 1),
    ]


def test_retraction_reevaluates_to_next_highest_report(db):
    add(db, 1, 1, 100, ["50"])
    add(db, 2, 1, 50)
    assert segs(db, 50, 50) == [(50, 51, CONCL_PASS, 2)]
    retract_report(db, 2)
    assert segs(db, 50, 50) == [(50, 51, CONCL_FAIL, 1)]


def test_retracting_everything_makes_serials_pending(db):
    add(db, 1, 1, 100)
    retract_report(db, 1)
    assert segs(db, 1, 100) == [(1, 101, CONCL_PENDING, None)]


def test_retraction_is_idempotent_and_keeps_row(db):
    add(db, 1, 1, 10)
    assert retract_report(db, 1).changed is True
    assert retract_report(db, 1).changed is False
    assert db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1


def test_retract_missing_report_is_not_found(db):
    with pytest.raises(NotFoundError):
        retract_report(db, 99)


def test_out_of_range_serial_in_failure_list_is_rejected(db):
    with pytest.raises(ValidationError):
        build_report(1, 10, 20, ["9"])
    with pytest.raises(ValidationError):
        build_report(1, 10, 20, ["21"])


def test_reversed_range_and_bad_numbers_rejected(db):
    with pytest.raises(ValidationError):
        build_report(1, 20, 10, [])
    with pytest.raises(ValidationError):
        build_report("abc", 1, 10, [])
    with pytest.raises(ValidationError):
        build_report(0, 1, 10, [])
    with pytest.raises(ValidationError):
        build_report(1, "1.5", 10, [])


def test_csv_parsing_variants():
    assert parse_failure_csv("") == []
    assert parse_failure_csv("\n\n  \n") == []
    assert parse_failure_csv("serial\n3\n4\n") == [3, 4]
    assert parse_failure_csv("编号\n3\n4\n") == [3, 4]
    assert parse_failure_csv("3,4\n4 5;6") == [3, 4, 5, 6]
    assert parse_failure_csv("003\n") == [3]  # leading zeros normalised
    assert parse_failure_csv("７\n") == [7]  # full-width digit


def test_csv_decoding_utf8_bom_and_gb18030():
    from app.service import decode_failure_csv

    assert decode_failure_csv("编号\n5\n".encode("utf-8-sig")).startswith("编号")
    assert "编号" in decode_failure_csv("编号\n5\n".encode("gb18030"))
    with pytest.raises(ValidationError):
        decode_failure_csv(b"\xff\xfe\x00\x00not-text")


def test_content_hash_ignores_csv_layout_noise(db):
    """Reordered / duplicated / blank-padded CSVs are the same report."""
    first = add(db, 1, 1, 100, parse_failure_csv("5\n7\n"))
    again = add(db, 1, 1, 100, parse_failure_csv("7\n\n5\n5\n"))
    assert first.created is True
    assert again.created is False


def test_max_serial_boundary_is_representable(db):
    from app.service import MAX_SERIAL

    add(db, 1, 0, MAX_SERIAL, ["0", str(MAX_SERIAL)])
    result = query_range(db, 0, MAX_SERIAL)
    assert result.summary["fail"] == 2
    assert result.summary["count"] == MAX_SERIAL + 1
    assert result.segments[-1].hi == MAX_SERIAL + 1


def test_large_span_is_constant_size(db):
    add(db, 1, 0, 5_000_000, ["42", "4_000_000".replace("_", "")])
    result = query_range(db, 0, 5_000_000)
    assert result.summary["count"] == 5_000_001
    assert result.summary["fail"] == 2
    assert len(result.segments) == 5
    assert db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
    assert (
        db.execute("SELECT COUNT(*) FROM failed_serials").fetchone()[0] == 2
    )


def test_segment_cap_marks_result_incomplete(db):
    failures = list(range(10, 100, 10))  # 9 failures -> 18 segments per report
    add(db, 1, 1, 100, [str(n) for n in failures])
    result = query_range(db, 1, 100, max_segments=4)
    assert result.complete is False
    assert len(result.segments) <= 4
    assert result.covered_serial_last < 100
    # The prefix must be truthful; its last segment is fully covered.
    full = query_range(db, 1, result.covered_serial_last)
    assert [s.as_dict() for s in result.segments] == [
        s.as_dict() for s in full.segments
    ]
    assert result.covered_serial_last + 1 == result.segments[-1].hi
    # Crucially, the headline counts still describe the WHOLE range.
    assert result.summary == {"pass": 91, "fail": 9, "pending": 0, "count": 100}
    assert sum(g["count"] for g in result.groups) == 100


def test_summary_complete_even_with_tiny_detail_budget(db):
    add(db, 1, 1, 1_000_000, ["1", "500000", "999999"])
    result = query_range(db, 1, 1_000_000, max_segments=1)
    assert result.complete is False
    assert len(result.segments) <= 1
    assert result.summary == {
        "pass": 999_997,
        "fail": 3,
        "pending": 0,
        "count": 1_000_000,
    }


def test_segment_cap_exactly_sufficient_is_complete(db):
    add(db, 1, 1, 100, ["50"])  # 3 segments
    result = query_range(db, 1, 100, max_segments=3)
    assert result.complete is True
    assert result.covered_serial_last == 100


def test_query_bounds_are_inclusive(db):
    add(db, 1, 10, 20)
    assert segs(db, 10, 10) == [(10, 11, CONCL_PASS, 1)]
    assert segs(db, 20, 20) == [(20, 21, CONCL_PASS, 1)]
    assert segs(db, 21, 21) == [(21, 22, CONCL_PENDING, None)]
    assert segs(db, 9, 9) == [(9, 10, CONCL_PENDING, None)]


def test_evidence_contains_only_reports_that_ruled(db):
    add(db, 1, 1, 10)
    add(db, 2, 1, 5)
    add(db, 3, 100, 200)
    result = query_range(db, 1, 10)
    assert [e["report_no"] for e in result.evidence] == [1, 2]
    assert result.summary["count"] == 10


def test_successive_queries_on_one_connection_are_independent(db):
    """After a query the connection must be usable again, and not left with an
    open snapshot that would hide a later upload."""
    add(db, 1, 1, 10)
    first = query_range(db, 1, 10)
    assert first.summary["count"] == 10
    assert not db.in_transaction
    add(db, 2, 1, 10, ["5"])
    second = query_range(db, 1, 10)
    assert second.summary["fail"] == 1
    assert second.segments[-1].report_no == 2


def test_failed_query_rolls_back_its_read_transaction(db):
    from app.service import query_range as qr

    with pytest.raises(ValidationError):
        qr(db, 10, 1)
    assert not db.in_transaction
    add(db, 1, 1, 10)
    assert qr(db, 1, 10).summary["count"] == 10
