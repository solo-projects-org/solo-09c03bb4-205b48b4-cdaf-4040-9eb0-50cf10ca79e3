"""Cross-check the interval engine against a naive per-serial oracle.

The oracle is the specification written directly: for each serial, pick the
covering non-retracted report with the greatest number and read its failed set.
It is only ever run on small ranges.
"""

from __future__ import annotations

import random

from app.service import (
    CONCL_FAIL,
    CONCL_PASS,
    CONCL_PENDING,
    build_report,
    create_report,
    query_range,
    retract_report,
)


def oracle(reports, lo, hi):
    """returns list of (start, end_excl, conclusion, report_no) with no merging"""
    out = []
    current = None
    for serial in range(lo, hi + 1):
        winners = [
            r for r in reports if not r["retracted"] and r["first"] <= serial <= r["last"]
        ]
        if not winners:
            state = (CONCL_PENDING, None)
        else:
            best = max(winners, key=lambda r: r["no"])
            state = (CONCL_FAIL if serial in best["failed"] else CONCL_PASS, best["no"])
        if current and current[2:] == state and current[1] == serial:
            current = (current[0], serial + 1, *state)
        else:
            if current:
                out.append(current)
            current = (serial, serial + 1, *state)
    if current:
        out.append(current)
    return out


def expand(segments):
    out = []
    for seg in segments:
        for serial in range(seg.lo, seg.hi):
            out.append((serial, seg.conclusion, seg.report_no))
    return out


def oracle_expanded(reports, lo, hi):
    out = []
    for start, end, conclusion, report_no in oracle(reports, lo, hi):
        for serial in range(start, end):
            out.append((serial, conclusion, report_no))
    return out


def random_case(rng, db, count):
    reports = []
    for no in range(1, count + 1):
        first = rng.randint(0, 40)
        last = first + rng.randint(0, 25)
        failures = rng.sample(range(first, last + 1), rng.randint(0, min(4, last - first + 1)))
        create_report(db, build_report(no, first, last, [str(f) for f in failures]))
        reports.append({"no": no, "first": first, "last": last, "failed": set(failures), "retracted": False})
        if rng.random() < 0.25:
            victim = rng.choice(reports)
            if not victim["retracted"]:
                retract_report(db, victim["no"])
                victim["retracted"] = True
    return reports


def test_random_cases_match_oracle(db):
    rng = random.Random(20261120)
    for _ in range(120):
        db.execute("DELETE FROM failed_serials")
        db.execute("DELETE FROM reports")
        reports = random_case(rng, db, rng.randint(1, 8))
        lo, hi = 0, 70
        result = query_range(db, lo, hi)
        assert expand(result.segments) == oracle_expanded(reports, lo, hi)
        # Summary must be exact even when the detail budget is tiny.
        tiny = query_range(db, lo, hi, max_segments=1)
        assert tiny.summary == result.summary
        assert sum(g["count"] for g in tiny.groups) == result.summary["count"]
        assert len(tiny.segments) <= 1
        # Group counts agree with the oracle, keyed by conclusion+report.
        oracle_groups = {}
        for _, conclusion, report_no in oracle_expanded(reports, lo, hi):
            oracle_groups[(conclusion, report_no)] = (
                oracle_groups.get((conclusion, report_no), 0) + 1
            )
        assert {(g["conclusion"], g["report_no"]): g["count"] for g in result.groups} == (
            oracle_groups
        )


def test_upload_order_does_not_change_result(db):
    rng = random.Random(7)
    specs = []
    for no in range(1, 9):
        first = rng.randint(0, 30)
        last = first + rng.randint(0, 20)
        failures = rng.sample(range(first, last + 1), rng.randint(0, 3))
        specs.append((no, first, last, failures))

    def run(order):
        db.execute("DELETE FROM failed_serials")
        db.execute("DELETE FROM reports")
        for idx in order:
            no, first, last, failures = specs[idx]
            create_report(db, build_report(no, first, last, [str(f) for f in failures]))
        return expand(query_range(db, 0, 60).segments)

    forward = run(range(len(specs)))
    backwards = run(reversed(range(len(specs))))
    shuffled = list(range(len(specs)))
    rng.shuffle(shuffled)
    assert forward == backwards == run(shuffled)


def test_segments_are_maximal_and_contiguous(db):
    rng = random.Random(99)
    for _ in range(40):
        db.execute("DELETE FROM failed_serials")
        db.execute("DELETE FROM reports")
        random_case(rng, db, rng.randint(1, 6))
        result = query_range(db, 0, 70)
        segments = result.segments
        assert segments[0].lo == 0 and segments[-1].hi == 71
        for a, b in zip(segments, segments[1:]):
            assert a.hi == b.lo
            assert (a.conclusion, a.report_no) != (b.conclusion, b.report_no)
        assert result.summary["count"] == 71
