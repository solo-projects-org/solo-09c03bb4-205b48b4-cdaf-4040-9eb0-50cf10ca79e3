"""Errata (勘误): moving a segment and its failures to an equal-length run."""

from __future__ import annotations

import io
import random
import threading

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app
from app.service import (
    CONCL_FAIL,
    CONCL_PASS,
    CONCL_PENDING,
    ConflictError,
    NotFoundError,
    ValidationError,
    build_report,
    correct_report,
    create_report,
    query_range,
    report_detail,
    retract_report,
)


def add(conn, no, first, last, failures=()):
    return create_report(conn, build_report(no, first, last, failures))


def segs(conn, first, last, **kw):
    return [
        (s.lo, s.hi, s.conclusion, s.report_no)
        for s in query_range(conn, first, last, **kw).segments
    ]


def test_move_segment_and_failures_leaves_origin_pending(db):
    add(db, 1, 100, 199, ["150", "151", "160"])
    result = correct_report(db, 1, 150, 160, 400)
    assert result.created is True
    assert result.correction == {
        "orig_first": 150,
        "orig_last": 160,
        "new_first": 400,
        "new_last": 410,
        "created_at": result.correction["created_at"],
    }
    # Original uploaded range and content hash are untouched.
    assert result.report["serial_first"] == 100
    assert result.report["serial_last"] == 199
    assert result.report["failed_count"] == 3
    # The vacated segment has no covering report -> pending.
    assert segs(db, 145, 170) == [
        (145, 150, CONCL_PASS, 1),
        (150, 161, CONCL_PENDING, None),
        (161, 171, CONCL_PASS, 1),
    ]
    # Failures moved in order: 150->400, 151->401, 160->410.
    assert segs(db, 395, 415) == [
        (395, 400, CONCL_PENDING, None),
        (400, 402, CONCL_FAIL, 1),
        (402, 410, CONCL_PASS, 1),
        (410, 411, CONCL_FAIL, 1),
        (411, 416, CONCL_PENDING, None),
    ]
    assert segs(db, 410, 410) == [(410, 411, CONCL_FAIL, 1)]
    assert segs(db, 400, 401) == [(400, 402, CONCL_FAIL, 1)]


def test_moved_failures_do_not_duplicate_and_unmoved_ones_stay(db):
    add(db, 1, 1, 100, ["10", "50", "90"])
    correct_report(db, 1, 40, 60, 200)
    detail = report_detail(db, 1)
    assert detail["failures"] == [10, 90, 210]  # 50 -> 210, others untouched
    assert detail["original_failures"] == [10, 50, 90]
    assert segs(db, 50, 50) == [(50, 51, CONCL_PENDING, None)]
    assert segs(db, 210, 210) == [(210, 211, CONCL_FAIL, 1)]
    assert segs(db, 10, 10) == [(10, 11, CONCL_FAIL, 1)]


def test_new_run_may_overlap_the_moved_segment(db):
    # 90-100 moves to 95-105.  95-100 overlap the moved segment; 101-105 lies
    # beyond the uploaded range, so no unmoved serial of this report is taken.
    add(db, 1, 1, 100, ["92", "99"])
    correct_report(db, 1, 90, 100, 95)
    assert report_detail(db, 1)["failures"] == [97, 104]  # 92->97, 99->104
    assert report_detail(db, 1)["coverage"] == [
        {"serial_first": 1, "serial_last": 89, "count": 89},
        {"serial_first": 95, "serial_last": 105, "count": 11},
    ]
    assert segs(db, 85, 110) == [
        (85, 90, CONCL_PASS, 1),
        (90, 95, CONCL_PENDING, None),
        (95, 97, CONCL_PASS, 1),
        (97, 98, CONCL_FAIL, 1),
        (98, 104, CONCL_PASS, 1),
        (104, 105, CONCL_FAIL, 1),
        (105, 106, CONCL_PASS, 1),
        (106, 111, CONCL_PENDING, None),
    ]


def test_overlap_may_not_spill_into_unmoved_serials(db):
    # 510-515 -> 512-517: 516-517 are unmoved serials of the same report.
    add(db, 1, 500, 599)
    with pytest.raises(ConflictError, match="未移动的编号 516–517"):
        correct_report(db, 1, 510, 515, 512)


def test_new_run_cannot_take_unmoved_serials_of_same_report(db):
    add(db, 1, 500, 599, ["510"])
    with pytest.raises(ConflictError, match="未移动的编号"):
        correct_report(db, 1, 510, 520, 505)  # 505-509 is not moving
    with pytest.raises(ConflictError, match="未移动的编号"):
        correct_report(db, 1, 510, 520, 515)  # 521-525 is not moving
    assert report_detail(db, 1)["coverage"] == [
        {"serial_first": 500, "serial_last": 599, "count": 100}
    ]


def test_new_run_may_point_into_another_reports_serials(db):
    """Reports overlap by design; the higher report number still rules."""
    add(db, 1, 1, 100, ["5"])
    add(db, 2, 200, 300)
    correct_report(db, 1, 1, 10, 250)  # report 1 now covers 250-259 too
    assert segs(db, 250, 259) == [(250, 260, CONCL_PASS, 2)]  # 2 > 1
    assert segs(db, 5, 5) == [(5, 6, CONCL_PENDING, None)]  # vacated
    retract_report(db, 2)
    # 5 - 1 + 250 = 254, so 250 passes and 254 fails once report 1 rules.
    assert segs(db, 250, 250) == [(250, 251, CONCL_PASS, 1)]
    assert segs(db, 254, 254) == [(254, 255, CONCL_FAIL, 1)]


def test_errata_do_not_reserve_serials_against_other_reports(db):
    add(db, 1, 1000, 1100)
    add(db, 2, 2000, 2100)
    correct_report(db, 1, 1000, 1009, 2005)  # 2 is newer, so it still rules
    correct_report(db, 2, 2000, 2000, 1000)
    # Report 2 (newer) covers 1000 and owns 2005 from its first erratum.
    assert segs(db, 1000, 1000) == [(1000, 1001, CONCL_PASS, 2)]
    assert segs(db, 2005, 2005) == [(2005, 2006, CONCL_PASS, 2)]
    # Report 1's moved block 2005-2014 is outruled by report 2 throughout.
    assert query_range(db, 2005, 2014).summary["pass"] == 10


def test_new_run_can_leave_the_original_range_and_must_stay_in_bounds(db):
    add(db, 1, 100, 200, ["150"])
    correct_report(db, 1, 150, 150, 0)  # serial 0 is legal
    assert segs(db, 0, 0) == [(0, 1, CONCL_FAIL, 1)]
    add(db, 2, 100, 200)
    with pytest.raises(ValidationError, match="非负整数"):
        correct_report(db, 2, 150, 150, -1)
    from app.service import MAX_SERIAL

    add(db, 3, 0, 10)
    with pytest.raises(ValidationError, match="超出可表示范围"):
        correct_report(db, 3, 9, 10, MAX_SERIAL)
    # A one-serial run ending exactly at MAX_SERIAL is representable.
    correct_report(db, 3, 10, 10, MAX_SERIAL)


def test_moving_segment_must_lie_inside_uploaded_range(db):
    add(db, 1, 100, 200)
    with pytest.raises(ValidationError, match="原报告号段"):
        correct_report(db, 1, 99, 150, 300)
    with pytest.raises(ValidationError, match="原报告号段"):
        correct_report(db, 1, 150, 201, 300)
    with pytest.raises(ValidationError, match="不能大于"):
        correct_report(db, 1, 160, 150, 300)


def test_identical_retry_returns_record_and_writes_nothing_new(db):
    add(db, 1, 1, 100, ["50"])
    first = correct_report(db, 1, 40, 60, 500)
    second = correct_report(db, 1, 40, 60, 500)
    assert first.created is True
    assert second.created is False
    assert second.correction == first.correction
    assert db.execute("SELECT COUNT(*) FROM report_corrections").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM failed_serials").fetchone()[0] == 1
    with pytest.raises(ConflictError, match="不能再次勘误"):
        correct_report(db, 1, 40, 60, 501)
    with pytest.raises(ConflictError, match="不能再次勘误"):
        correct_report(db, 1, 41, 60, 500)


def test_retracted_report_rejects_first_erratum(db):
    add(db, 1, 1, 100)
    retract_report(db, 1)
    with pytest.raises(ConflictError, match="已撤回"):
        correct_report(db, 1, 1, 10, 500)
    assert db.execute("SELECT COUNT(*) FROM report_corrections").fetchone()[0] == 0


def test_retraction_invalidates_corrected_report_and_keeps_record(db):
    add(db, 1, 1, 100, ["50"])
    add(db, 2, 1, 100, ["60"])
    correct_report(db, 1, 1, 100, 1000)  # whole report moves; 50 -> 1049
    assert segs(db, 1000, 1000) == [(1000, 1001, CONCL_PASS, 1)]
    assert segs(db, 1049, 1049) == [(1049, 1050, CONCL_FAIL, 1)]
    retract_report(db, 1)
    # It no longer rules anywhere, including its new segment.
    assert segs(db, 1000, 1001) == [(1000, 1002, CONCL_PENDING, None)]
    assert segs(db, 1, 1) == [(1, 2, CONCL_PASS, 2)]  # report 2 still rules
    detail = report_detail(db, 1)
    assert detail["report"]["retracted"] is True
    assert detail["correction"] is not None  # record survives retraction


def test_reupload_original_after_correction_and_retraction_keeps_both(db):
    add(db, 1, 1, 100, ["50"])
    correct_report(db, 1, 40, 60, 500)
    retract_report(db, 1)
    again = add(db, 1, 1, 100, ["50"])
    assert again.created is False
    assert again.report["retracted"] is True
    assert again.report["correction"] is not None
    detail = report_detail(db, 1)
    assert detail["correction"]["new_first"] == 500
    assert detail["failures"] == [510]          # 50 moved to 500 + (50 - 40)
    assert detail["original_failures"] == [50]
    assert segs(db, 1, 100) == [(1, 101, CONCL_PENDING, None)]


def test_erratum_failure_leaves_report_untouched(db):
    add(db, 1, 1, 100, ["50"])
    add(db, 2, 200, 300)
    with pytest.raises(ValidationError):
        correct_report(db, 1, 0, 60, 500)  # starts before the uploaded range
    detail = report_detail(db, 1)
    assert detail["correction"] is None
    assert detail["coverage"] == [
        {"serial_first": 1, "serial_last": 100, "count": 100}
    ]
    assert detail["failures"] == [50]
    assert detail["report"]["failed_count"] == 1
    assert db.execute("SELECT COUNT(*) FROM report_corrections").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM original_failed_serials").fetchone()[0] == 0


def test_correction_of_missing_report_is_not_found(db):
    with pytest.raises(NotFoundError):
        correct_report(db, 42, 1, 10, 100)


# ---------------------------------------------------------------------------
# Randomised cross-check against a per-serial model


def _oracle_expand(model, lo, hi):
    out = []
    for serial in range(lo, hi + 1):
        winners = [
            r
            for r in model.values()
            if not r["retracted"] and serial in r["coverage"]
        ]
        if not winners:
            out.append((serial, CONCL_PENDING, None))
            continue
        best = max(winners, key=lambda r: r["no"])
        out.append(
            (serial, CONCL_FAIL if serial in best["failed"] else CONCL_PASS, best["no"])
        )
    return out


def _engine_expand(conn, lo, hi):
    out = []
    for seg in query_range(conn, lo, hi, max_segments=200_000).segments:
        for serial in range(seg.lo, seg.hi):
            out.append((serial, seg.conclusion, seg.report_no))
    return out


def _spec_legal(report, o_lo, o_hi, n_lo):
    """Spec: the new run may not touch this report's unmoved serials.

    Reports may overlap, so other reports' serials are not reserved.
    """
    span = o_hi - o_lo + 1
    n_hi = n_lo + span - 1
    if n_hi > 130 or n_lo < 0:
        return False
    if not set(range(o_lo, o_hi + 1)) <= report["coverage"]:
        return False
    return not (set(range(n_lo, n_hi + 1)) & (report["coverage"] - set(range(o_lo, o_hi + 1))))


def test_random_corrections_accept_exactly_the_spec_and_match_oracle(db):
    rng = random.Random(20261120)
    for _ in range(60):
        db.execute("DELETE FROM failed_serials")
        db.execute("DELETE FROM report_coverage")
        db.execute("DELETE FROM report_corrections")
        db.execute("DELETE FROM original_failed_serials")
        db.execute("DELETE FROM reports")

        model = {}
        for no in range(1, rng.randint(2, 6)):
            first = rng.randint(0, 60)
            last = first + rng.randint(4, 30)
            failures = rng.sample(range(first, last + 1), rng.randint(0, 3))
            add(db, no, first, last, [str(f) for f in failures])
            model[no] = {
                "no": no,
                "failed": set(failures),
                "coverage": set(range(first, last + 1)),
                "retracted": False,
            }
        for no in sorted(model):
            report = model[no]
            span = rng.randint(1, 8)
            o_lo = rng.randint(0, 90)
            o_hi = o_lo + span - 1
            if not set(range(o_lo, o_hi + 1)) <= report["coverage"]:
                continue
            candidates = [0, 95, 100, 120, o_lo, o_lo + 1, o_lo - span + 1]
            candidates += [rng.randint(0, 125) for _ in range(8)]
            applied = False
            for n_lo in candidates:
                legal = _spec_legal(report, o_lo, o_hi, n_lo)
                try:
                    correct_report(db, no, o_lo, o_hi, n_lo)
                except (ValidationError, ConflictError):
                    assert not legal, (no, o_lo, o_hi, n_lo, "spec allows but engine refused")
                    continue
                assert legal, (no, o_lo, o_hi, n_lo, "spec forbids but engine accepted")
                if applied:
                    raise AssertionError("second erratum was accepted")
                applied = True
                delta = n_lo - o_lo
                report["failed"] = {
                    s for s in report["failed"] if not (o_lo <= s <= o_hi)
                } | {
                    s + delta for s in report["failed"] if o_lo <= s <= o_hi
                }
                report["coverage"] = (
                    report["coverage"] - set(range(o_lo, o_hi + 1))
                ) | set(range(n_lo, n_lo + span))
        if rng.random() < 0.3 and model:
            victim = rng.choice(list(model.values()))
            retract_report(db, victim["no"])
            victim["retracted"] = True

        lo, hi = 0, 130
        assert _engine_expand(db, lo, hi) == _oracle_expand(model, lo, hi)
        result = query_range(db, lo, hi, max_segments=1)
        assert result.summary["count"] == 131


def test_query_during_correction_sees_a_committed_state(tmp_path):
    db_path = tmp_path / "race.db"
    init_db(db_path)
    writer = connect(db_path)
    create_report(writer, build_report(1, 1, 10_000, ["5000"]))

    errors = []
    stop = threading.Event()

    def reader():
        conn = connect(db_path)
        try:
            while not stop.is_set():
                counts = query_range(conn, 1, 10_000).summary
                # Either the pre- or post-correction commit, never a mixture:
                # 5000 is failed in the first and pending in the second, and
                # 2000 serials leave the report's coverage either way.
                if counts["count"] != 10_000 or counts["fail"] not in (0, 1):
                    errors.append(counts)
                    return
                if (counts["fail"], counts["pending"]) not in ((1, 0), (0, 2001)):
                    errors.append(counts)
                    return
        finally:
            conn.close()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        correct_report(writer, 1, 4000, 6000, 20_000)
    finally:
        stop.set()
        thread.join(timeout=30)
        writer.close()
    assert not errors, errors
    check = connect(db_path)
    try:
        assert query_range(check, 5000, 5000).summary["pending"] == 1
        assert query_range(check, 21_000, 21_000).summary["fail"] == 1
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Web pages


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as c:
        yield c


def upload(client, no, first, last, csv_text=None):
    files = None
    if csv_text is not None:
        files = {"failures_csv": ("f.csv", io.BytesIO(csv_text.encode()), "text/csv")}
    return client.post(
        "/reports",
        data={"report_no": str(no), "serial_first": str(first), "serial_last": str(last)},
        files=files,
        follow_redirects=False,
    )


def correct(client, no, o_lo, o_hi, n_lo):
    return client.post(
        f"/reports/{no}/correct",
        data={
            "orig_first": str(o_lo),
            "orig_last": str(o_hi),
            "new_first": str(n_lo),
        },
        follow_redirects=False,
    )


def rows(client, first, last):
    data = client.get(f"/api/query?serial_first={first}&serial_last={last}").json()
    return [
        (s["serial_first"], s["serial_last"], s["conclusion"], s["report_no"])
        for s in data["segments"]
    ]


def test_detail_page_shows_erratum_before_and_after(client):
    upload(client, 7, 100, 200, "150\n151\n160\n")
    page = client.get("/reports/7").text
    assert "办理勘误" in page and "尚未勘误" in page
    assert 'action="/reports/7/correct"' in page
    response = correct(client, 7, 150, 160, 400)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "已勘误" in page
    assert "勘误前后号段" in page
    assert ">150<" in page and ">160<" in page and ">400<" in page and ">410<" in page
    assert "原上传名单" in page and ">151<" in page
    # Only one erratum: the form is gone.
    assert "办理勘误</h3>" not in page
    assert rows(client, 150, 160) == [(150, 160, "pending", None)]
    assert rows(client, 400, 401) == [(400, 401, "fail", 7)]


def test_correction_page_error_keeps_values_and_changes_nothing(client):
    upload(client, 7, 100, 200, "150\n")
    upload(client, 8, 300, 400)
    response = correct(client, 7, 150, 160, 120)  # 120-130 is not moving
    assert response.status_code == 409
    assert "未移动的编号" in response.text
    assert 'value="120"' in response.text
    assert rows(client, 150, 160) == [
        (150, 150, "fail", 7),
        (151, 160, "pass", 7),
    ]


def test_correction_blocks_retraction_and_retraction_keeps_record(client):
    upload(client, 7, 1, 100, "50\n")
    correct(client, 7, 1, 50, 1000)
    retract = client.post("/reports/7/retract", follow_redirects=False)
    assert retract.status_code == 303
    detail = client.get("/api/reports/7").json()
    assert detail["retracted"] is True
    assert detail["correction"]["new_first"] == 1000
    assert rows(client, 1000, 1000) == [(1000, 1000, "pending", None)]


def test_corrected_state_survives_restart(tmp_path):
    db = tmp_path / "restart.db"
    with TestClient(create_app(db)) as c:
        upload(c, 1, 1, 100, "50\n")
        assert correct(c, 1, 40, 60, 500).status_code == 303
        before = c.get("/api/query?serial_first=1&serial_last=600").json()
    with TestClient(create_app(db)) as c:
        after = c.get("/api/query?serial_first=1&serial_last=600").json()
        assert before == after
        detail = c.get("/api/reports/1").json()
        assert detail["correction"]["new_first"] == 500
        # Retry after restart is still the identical recorded erratum.
        assert correct(c, 1, 40, 60, 500).status_code == 303
        assert correct(c, 1, 40, 60, 501).status_code == 409


def test_vacated_serials_fall_back_to_remaining_reports(db):
    add(db, 1, 1, 100, ["40", "50"])
    add(db, 2, 1, 100, ["45"])
    correct_report(db, 1, 40, 60, 500)
    # 40-60 vacated by report 1 -> report 2 rules; its 45 is failed.
    assert segs(db, 40, 60) == [
        (40, 45, CONCL_PASS, 2),
        (45, 46, CONCL_FAIL, 2),
        (46, 61, CONCL_PASS, 2),
    ]
    # 50 -> 510, and 40-60 as a block now covers 500-520.
    assert segs(db, 505, 515) == [
        (505, 510, CONCL_PASS, 1),
        (510, 511, CONCL_FAIL, 1),
        (511, 516, CONCL_PASS, 1),
    ]


def test_moved_failure_mapping_is_offset_by_moved_start(db):
    add(db, 1, 1, 100, ["50"])
    correct_report(db, 1, 40, 60, 500)
    # 50 - 40 + 500 = 510; the moved run is 500-520.
    assert segs(db, 510, 510) == [(510, 511, CONCL_FAIL, 1)]
    assert segs(db, 520, 520) == [(520, 521, CONCL_PASS, 1)]
    assert segs(db, 521, 521) == [(521, 522, CONCL_PENDING, None)]


def test_failed_erratum_mid_write_rolls_back_everything(db, monkeypatch):
    import app.service as svc

    add(db, 1, 1, 100, ["50"])
    before = report_detail(db, 1)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(svc, "_write_coverage", boom)
    with pytest.raises(RuntimeError):
        correct_report(db, 1, 40, 60, 500)
    monkeypatch.undo()

    after = report_detail(db, 1)
    assert after["correction"] is None
    assert after["report"] == before["report"]
    assert after["failures"] == before["failures"]
    assert after["coverage"] == before["coverage"]
    assert db.execute("SELECT COUNT(*) FROM report_corrections").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM original_failed_serials").fetchone()[0] == 0
    assert segs(db, 40, 60) == [
        (40, 50, CONCL_PASS, 1),
        (50, 51, CONCL_FAIL, 1),
        (51, 61, CONCL_PASS, 1),
    ]
    # And the report accepts a fresh erratum afterwards.
    assert correct_report(db, 1, 40, 60, 500).created is True


def test_report_list_marks_corrected_and_retracted(client):
    upload(client, 1, 1, 100, "50\n")
    upload(client, 2, 1, 100)
    correct(client, 1, 1, 10, 500)
    page = client.get("/reports").text
    assert "已勘误" in page
    assert "勘误 1–10 → 500–509" in page
    assert "撤回" in page


def test_segments_and_evidence_come_from_the_same_commit(tmp_path):
    """While uploads/errata/retractions interleave, every query's ruling and
    its evidence snapshot must describe the same committed state."""
    db_path = tmp_path / "mix.db"
    init_db(db_path)
    writer = connect(db_path)
    create_report(writer, build_report(1, 1, 1000, ["500"]))
    create_report(writer, build_report(2, 400, 600, []))

    stop = threading.Event()
    problems = []

    def reader():
        conn = connect(db_path)
        try:
            while not stop.is_set():
                data = query_range(conn, 1, 1200)
                by_no = {e["report_no"]: e for e in data.evidence}
                for seg in data.segments:
                    if seg.report_no is None:
                        continue
                    evidence = by_no.get(seg.report_no)
                    if evidence is None:
                        problems.append(("missing evidence", seg))
                        return
                    covered = any(
                        c["serial_first"] <= seg.lo
                        and seg.hi - 1 <= c["serial_last"]
                        for c in evidence["coverage"]
                    )
                    if not covered:
                        problems.append(("segment outside evidence", seg, evidence))
                        return
        finally:
            conn.close()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        correct_report(writer, 1, 1, 10, 5000)  # one erratum per report
        for i in range(2, 20):
            retract_report(writer, 2)
            create_report(writer, build_report(10 + i, 200, 300, []))
            create_report(writer, build_report(50 + i, 5001, 5005, []))
    finally:
        stop.set()
        thread.join(timeout=30)
        writer.close()
    assert not problems, problems[:3]
