"""Concurrency: one writer at a time, and readers never see a torn commit."""

from __future__ import annotations

import threading

from app.db import connect
from app.service import build_report, create_report, query_range, retract_report


def test_concurrent_identical_uploads_create_one_row(tmp_path):
    db_path = tmp_path / "race.db"
    from app.db import init_db

    init_db(db_path)

    results = []
    errors = []
    barrier = threading.Barrier(8)

    def worker():
        conn = connect(db_path)
        try:
            barrier.wait()
            results.append(create_report(conn, build_report(1, 1, 1000, ["7"])))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert sum(1 for r in results if r.created) == 1
    check = connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM failed_serials").fetchone()[0] == 1
        assert query_range(check, 1, 1000).summary["fail"] == 1
    finally:
        check.close()


def test_concurrent_conflicting_uploads_only_one_wins(tmp_path):
    from app.db import init_db

    db_path = tmp_path / "race2.db"
    init_db(db_path)

    outcomes = []
    barrier = threading.Barrier(6)
    lock = threading.Lock()

    def worker(failures):
        conn = connect(db_path)
        try:
            barrier.wait()
            try:
                result = create_report(conn, build_report(1, 1, 100, failures))
                with lock:
                    outcomes.append(("ok", result.created))
            except Exception as exc:
                with lock:
                    outcomes.append(("err", type(exc).__name__))
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=([str(10)],))
        for _ in range(3)
    ] + [
        threading.Thread(target=worker, args=([str(20)],))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created = sum(1 for kind, value in outcomes if kind == "ok" and value)
    conflicts = sum(1 for kind, value in outcomes if kind == "err" and value == "ConflictError")
    assert created == 1, outcomes
    assert conflicts == 3, outcomes
    check = connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
        failed = [
            r[0] for r in check.execute("SELECT serial FROM failed_serials ORDER BY serial")
        ]
        assert failed in ([10], [20])
    finally:
        check.close()


def test_queries_are_consistent_under_concurrent_writes(tmp_path):
    from app.db import init_db

    db_path = tmp_path / "race3.db"
    init_db(db_path)
    writer = connect(db_path)
    create_report(writer, build_report(1, 1, 500_000, []))

    stop = threading.Event()
    anomalies = []
    errors = []
    lock = threading.Lock()

    def reader():
        conn = connect(db_path)
        try:
            while not stop.is_set():
                data = query_range(conn, 1, 500_000)
                counts = data.summary
                # Every serial is always ruled on by some report here, and the
                # segments must tile the query exactly.
                problems = []
                if counts["count"] != 500_000 or counts["pending"] != 0:
                    problems.append(counts)
                covered = 0
                for seg in data.segments:
                    covered += seg.count
                    if seg.report_no is None or seg.report_no < 1:
                        problems.append(seg)
                if covered != 500_000:
                    problems.append(("coverage", covered))
                if problems:
                    with lock:
                        anomalies.extend(problems[:3])
                    return
        except Exception as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        for no in range(2, 40):
            create_report(writer, build_report(no, 1 + no * 1000, 10_000 + no * 1000, []))
            if no % 7 == 0:
                retract_report(writer, no)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)
        writer.close()

    assert not errors, errors
    assert not anomalies, anomalies[:5]
