"""Domain rules for meter verification reports.

Everything here is deliberately independent of FastAPI so the rules can be
tested directly.  Ranges are inclusive at the API boundary and half-open
``[lo, hi)`` inside, so a range spanning a million serials is one report row and
one query segment -- never a million rows.

Ruling for serial ``s``: among non-retracted reports whose inclusive range
covers ``s``, take the one with the greatest ``report_no``.  ``s`` fails iff it
appears in that report's failed list, otherwise it passes.  No covering report
=> pending (待检).
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .db import read_tx, write_tx

# Serial numbers are stored as SQLite integers; keep ``last + 1`` representable.
MAX_SERIAL = (1 << 63) - 2
MAX_REPORT_NO = (1 << 63) - 2
DEFAULT_MAX_SEGMENTS = 20_000
MAX_SEGMENTS_LIMIT = 200_000
MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_FAILURES = 1_000_000

CONCL_PASS = "pass"
CONCL_FAIL = "fail"
CONCL_PENDING = "pending"

CONCL_LABEL = {
    CONCL_PASS: "合格",
    CONCL_FAIL: "不合格",
    CONCL_PENDING: "待检",
}

_HEADER_TOKENS = {
    "serial",
    "serials",
    "serial_number",
    "serial_no",
    "number",
    "no",
    "编号",
    "序列号",
    "表号",
    "电表编号",
    "不合格编号",
    "不合格序列号",
}

_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９，　", "0123456789, "
)


class ServiceError(Exception):
    """Base class; ``status`` is the HTTP status the web layer should use."""

    status = 400


class ValidationError(ServiceError):
    status = 422


class ConflictError(ServiceError):
    status = 409


class NotFoundError(ServiceError):
    status = 404


# ---------------------------------------------------------------------------
# Parsing


def _digits(raw: object, label: str) -> int:
    text = str(raw).strip().translate(_FULLWIDTH)
    if not text:
        raise ValidationError(f"{label}不能为空")
    if not text.isascii() or not text.isdigit():
        raise ValidationError(f"{label}必须是非负整数，收到：{str(raw)!r}")
    value = int(text)
    if value > MAX_SERIAL:
        raise ValidationError(f"{label}超出可表示范围（最大 {MAX_SERIAL}）")
    return value


def parse_serial(raw: object) -> int:
    """Parse a single serial number. Leading zeros are normalised away."""
    return _digits(raw, "编号")


def parse_range_field(raw: object, label: str) -> int:
    """Form-field number: tolerates thousands separators and spaces."""
    text = str(raw).translate(_FULLWIDTH)
    text = re.sub(r"[,\s]", "", text)
    return _digits(text, label)


def parse_report_no(raw: object) -> int:
    value = parse_range_field(raw, "报告号")
    if value < 1:
        raise ValidationError("报告号必须是正整数")
    if value > MAX_REPORT_NO:
        raise ValidationError("报告号超出可表示范围")
    return value


def parse_serial_pair(first: object, last: object) -> tuple[int, int]:
    """Return inclusive ``(first, last)`` after validation."""
    lo = parse_range_field(first, "起始编号")
    hi = parse_range_field(last, "结束编号")
    if lo > hi:
        raise ValidationError("起始编号不能大于结束编号")
    return lo, hi


def decode_failure_csv(data: bytes) -> str:
    """Decode a Chinese-Excel-friendly CSV: UTF-8 (BOM ok), then GB18030."""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("CSV 编码无法识别，请使用 UTF-8 或 GB18030")


def parse_failure_csv(text: str) -> list[int]:
    """Parse a failure list.

    Accepts one or more serials per line separated by comma / semicolon /
    whitespace.  Blank lines are ignored.  A leading header such as ``serial``
    or ``编号`` is ignored.  Duplicates are collapsed.
    """
    tokens: list[str] = []
    for line in text.splitlines():
        for token in re.split(r"[,;\s]+", line.strip()):
            if token:
                tokens.append(token)
    if not tokens:
        return []
    if tokens[0].strip().lower() in _HEADER_TOKENS:
        tokens = tokens[1:]
    return failure_tokens_from_values(tokens)


def failure_tokens_from_values(values: Iterable[object]) -> list[int]:
    failed = {parse_serial(value) for value in values}
    if len(failed) > MAX_FAILURES:
        raise ValidationError(f"不合格名单超过 {MAX_FAILURES:,} 条上限")
    return sorted(failed)


# ---------------------------------------------------------------------------
# Report content


@dataclass(frozen=True)
class ReportInput:
    """A fully validated report, ready to persist."""

    report_no: int
    serial_first: int
    serial_last: int
    failures: tuple[int, ...]

    @property
    def serial_span(self) -> int:
        return self.serial_last - self.serial_first + 1

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [self.report_no, self.serial_first, self.serial_last, list(self.failures)],
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def build_report(
    report_no: object,
    serial_first: object,
    serial_last: object,
    failures: Sequence[object],
) -> ReportInput:
    no = parse_report_no(report_no)
    first, last = parse_serial_pair(serial_first, serial_last)
    failed = failure_tokens_from_values(failures)
    if failed:
        outside = [n for n in failed if n < first or n > last]
        if outside:
            sample = "、".join(str(n) for n in outside[:5])
            more = f" 等 {len(outside)} 个" if len(outside) > 5 else ""
            raise ValidationError(
                f"不合格名单中有编号不在报告范围内（{first}–{last}）：{sample}{more}"
            )
    return ReportInput(no, first, last, tuple(failed))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Persistence of reports


@dataclass
class CreateResult:
    created: bool
    report: dict


@dataclass
class RetractResult:
    changed: bool
    report: dict


def _row_to_report(row) -> dict:
    return {
        "report_no": row["report_no"],
        "serial_first": row["serial_first"],
        "serial_last": row["serial_last"],
        "serial_span": row["serial_span"],
        "failed_count": row["failed_count"],
        "content_hash": row["content_hash"],
        "retracted": bool(row["retracted"]),
        "created_at": row["created_at"],
        "retracted_at": row["retracted_at"],
    }


def _get_row(conn, report_no: int):
    return conn.execute(
        "SELECT * FROM reports WHERE report_no = ?", (report_no,)
    ).fetchone()


def create_report(conn, report: ReportInput) -> CreateResult:
    """Insert a report or recognise an identical re-upload.

    - same number + same content  -> no-op, original state (incl. retracted) kept
    - same number + other content -> ConflictError, nothing written
    """
    digest = report.content_hash
    with write_tx(conn):
        existing = _get_row(conn, report.report_no)
        if existing is not None:
            if existing["content_hash"] == digest:
                return CreateResult(False, _row_to_report(existing))
            raise ConflictError(
                f"报告号 {report.report_no} 已存在且内容不同，整份拒收"
            )
        cur = conn.execute(
            """
            INSERT INTO reports
                (report_no, serial_first, serial_last, serial_span,
                 failed_count, content_hash, retracted, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                report.report_no,
                report.serial_first,
                report.serial_last,
                report.serial_span,
                len(report.failures),
                digest,
                _now(),
            ),
        )
        report_id = cur.lastrowid
        if report.failures:
            conn.executemany(
                "INSERT INTO failed_serials (report_id, serial) VALUES (?, ?)",
                ((report_id, serial) for serial in report.failures),
            )
        row = conn.execute(
            "SELECT * FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        return CreateResult(True, _row_to_report(row))


def retract_report(conn, report_no: int) -> RetractResult:
    """Retract (撤回) a report.  Idempotent.  Never deletes data."""
    with write_tx(conn):
        row = _get_row(conn, report_no)
        if row is None:
            raise NotFoundError(f"报告号 {report_no} 不存在")
        if row["retracted"]:
            return RetractResult(False, _row_to_report(row))
        conn.execute(
            "UPDATE reports SET retracted = 1, retracted_at = ? WHERE id = ?",
            (_now(), row["id"]),
        )
        row = _get_row(conn, report_no)
        return RetractResult(True, _row_to_report(row))


def list_reports(conn, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    total = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM reports ORDER BY report_no DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_row_to_report(row) for row in rows], total


def get_report(conn, report_no: int) -> dict:
    row = _get_row(conn, report_no)
    if row is None:
        raise NotFoundError(f"报告号 {report_no} 不存在")
    return _row_to_report(row)


def list_failures(conn, report_no: int, limit: int = 500) -> list[int]:
    row = _get_row(conn, report_no)
    if row is None:
        raise NotFoundError(f"报告号 {report_no} 不存在")
    return [
        r[0]
        for r in conn.execute(
            "SELECT serial FROM failed_serials WHERE report_id = ? ORDER BY serial LIMIT ?",
            (row["id"], limit),
        )
    ]


def get_overview(conn) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*)                                   AS total,
               COALESCE(SUM(CASE WHEN retracted = 0 THEN 1 ELSE 0 END), 0) AS active,
               COALESCE(SUM(CASE WHEN retracted = 1 THEN 1 ELSE 0 END), 0) AS retracted,
               COALESCE(SUM(failed_count), 0)             AS failed_entries,
               MAX(CASE WHEN retracted = 0 THEN report_no END) AS max_active_no
        FROM reports
        """
    ).fetchone()
    return {
        "total": row["total"],
        "active": row["active"],
        "retracted": row["retracted"],
        "failed_entries": row["failed_entries"],
        "max_active_no": row["max_active_no"],
    }


# ---------------------------------------------------------------------------
# Rulings


@dataclass(frozen=True)
class Segment:
    lo: int  # inclusive
    hi: int  # exclusive
    conclusion: str
    report_no: int | None

    @property
    def serial_first(self) -> int:
        return self.lo

    @property
    def serial_last(self) -> int:
        return self.hi - 1

    @property
    def count(self) -> int:
        return self.hi - self.lo

    @property
    def conclusion_label(self) -> str:
        return CONCL_LABEL[self.conclusion]

    def as_dict(self) -> dict:
        return {
            "serial_first": self.serial_first,
            "serial_last": self.serial_last,
            "count": self.count,
            "conclusion": self.conclusion,
            "conclusion_label": CONCL_LABEL[self.conclusion],
            "report_no": self.report_no,
        }


@dataclass
class QueryResult:
    serial_first: int
    serial_last: int
    segments: list[Segment]
    complete: bool  # False: ``segments`` is only a prefix of the range
    max_segments: int
    covered_serial_last: int
    evidence: list[dict]
    summary: dict
    groups: list[dict]

    def as_dict(self) -> dict:
        return {
            "serial_first": self.serial_first,
            "serial_last": self.serial_last,
            "covered_serial_last": self.covered_serial_last,
            "complete": self.complete,
            "max_segments": self.max_segments,
            "summary": self.summary,
            "segments": [seg.as_dict() for seg in self.segments],
            "groups": self.groups,
            "evidence": self.evidence,
        }

def _merge(
    out: list[Segment],
    lo: int,
    hi: int,
    conclusion: str,
    report_no: int | None,
    max_segments: int,
) -> bool:
    """Append or extend a segment.  Returns False if the budget is full.

    Merging into the previous segment is free: it does not add a row.  Only a
    genuinely new segment can exhaust the budget, so ``len(out)`` never exceeds
    ``max_segments``.  Once the budget is exhausted the caller stops adding
    rows, but a later adjacent run may still merge into the last one, so the
    detail listing always remains a gap-free prefix.
    """
    if lo >= hi:
        return True
    if (
        out
        and out[-1].conclusion == conclusion
        and out[-1].report_no == report_no
        and out[-1].hi == lo
    ):
        last = out[-1]
        out[-1] = Segment(last.lo, hi, conclusion, report_no)
        return True
    if len(out) >= max_segments:
        return False
    out.append(Segment(lo, hi, conclusion, report_no))
    return True


class _Tally:
    """Always-complete totals, independent of the detail-segment budget."""

    def __init__(self):
        self.counts = {CONCL_PASS: 0, CONCL_FAIL: 0, CONCL_PENDING: 0}
        self.order: list[tuple[str, int | None]] = []
        self.groups: dict[tuple[str, int | None], dict] = {}

    def width(self, conclusion: str, report_no: int | None, width: int) -> None:
        """Add ``width`` serials with this ruling.  ``width`` must be >= 0."""
        if width <= 0:
            return
        self.counts[conclusion] += width
        key = (conclusion, report_no)
        group = self.groups.get(key)
        if group is None:
            group = {
                "conclusion": conclusion,
                "conclusion_label": CONCL_LABEL[conclusion],
                "report_no": report_no,
                "count": 0,
            }
            self.groups[key] = group
            self.order.append(key)
        group["count"] += width

    def segment(self, lo: int, hi: int, conclusion: str, report_no: int | None) -> None:
        self.width(conclusion, report_no, hi - lo)

    def summary(self) -> dict:
        return {**self.counts, "count": sum(self.counts.values())}

    def group_list(self) -> list[dict]:
        return [self.groups[key] for key in self.order]


def _query(conn, qlo: int, qhi: int, max_segments: int) -> QueryResult:
    rows = conn.execute(
        """
        SELECT id, report_no, serial_first, serial_last, failed_count, created_at
        FROM reports
        WHERE retracted = 0
          AND serial_first < ?
          AND serial_last >= ?
        ORDER BY serial_first, report_no
        """,
        (qhi, qlo),
    ).fetchall()

    # Clipped boundaries: every point where the winning report can change.
    boundaries = {qlo, qhi}
    for row in rows:
        boundaries.add(max(qlo, row["serial_first"]))
        boundaries.add(min(qhi, row["serial_last"] + 1))
    points = sorted(boundaries)

    heap: list[tuple[int, int, int]] = []  # (-report_no, report_id, hi_excl)
    failed_counts = {row["id"]: row["failed_count"] for row in rows}
    tally = _Tally()
    segments: list[Segment] = []
    detail_full = False
    cursor = 0  # index into rows, ordered by serial_first

    for idx, x in enumerate(points[:-1]):
        while cursor < len(rows) and rows[cursor]["serial_first"] <= x:
            row = rows[cursor]
            cursor += 1
            hi_excl = min(qhi, row["serial_last"] + 1)
            if hi_excl > x:
                heapq.heappush(heap, (-row["report_no"], row["id"], hi_excl))
        while heap and heap[0][2] <= x:
            heapq.heappop(heap)

        nxt = points[idx + 1]
        if not heap:
            tally.segment(x, nxt, CONCL_PENDING, None)
            if not detail_full:
                detail_full = not _merge(
                    segments, x, nxt, CONCL_PENDING, None, max_segments
                )
            continue

        neg_no, report_id, _ = heap[0]
        report_no = -neg_no
        if failed_counts.get(report_id, 0) == 0:
            # The report has no failures at all; skip the lookup entirely.
            tally.segment(x, nxt, CONCL_PASS, report_no)
            if not detail_full:
                detail_full = not _merge(
                    segments, x, nxt, CONCL_PASS, report_no, max_segments
                )
            continue

        # Complete totals for this interval: one indexed count, whatever the
        # size of the whole failure list.
        failed_here = conn.execute(
            """
            SELECT COUNT(*) FROM failed_serials
            WHERE report_id = ? AND serial >= ? AND serial < ?
            """,
            (report_id, x, nxt),
        ).fetchone()[0]
        tally.width(CONCL_PASS, report_no, (nxt - x) - failed_here)
        tally.width(CONCL_FAIL, report_no, failed_here)

        if detail_full:
            continue

        # Detail listing: streamed, and abandoned as soon as the budget is
        # full, so a huge failure list never has to be read in full.
        pos = x
        for (serial,) in conn.execute(
            """
            SELECT serial FROM failed_serials
            WHERE report_id = ? AND serial >= ? AND serial < ?
            ORDER BY serial
            """,
            (report_id, x, nxt),
        ):
            if not _merge(segments, pos, serial, CONCL_PASS, report_no, max_segments):
                detail_full = True
                break
            if not _merge(
                segments, serial, serial + 1, CONCL_FAIL, report_no, max_segments
            ):
                detail_full = True
                break
            pos = serial + 1
        if not detail_full:
            detail_full = not _merge(
                segments, pos, nxt, CONCL_PASS, report_no, max_segments
            )

    covered_last = segments[-1].hi - 1 if segments else qlo - 1
    used = {key[1] for key in tally.order if key[1] is not None}
    evidence = [
        {
            "report_no": row["report_no"],
            "serial_first": row["serial_first"],
            "serial_last": row["serial_last"],
            "failed_count": row["failed_count"],
            "created_at": row["created_at"],
        }
        for row in sorted(rows, key=lambda r: r["report_no"])
        if row["report_no"] in used
    ]

    return QueryResult(
        serial_first=qlo,
        serial_last=qhi - 1,
        segments=segments,
        complete=not detail_full,
        max_segments=max_segments,
        covered_serial_last=covered_last,
        evidence=evidence,
        summary=tally.summary(),
        groups=tally.group_list(),
    )


def query_range(
    conn,
    serial_first: object,
    serial_last: object,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
) -> QueryResult:
    """Ruling for an inclusive range, using one consistent snapshot.

    The read transaction is opened here (or reused if the caller already holds
    one) so the report set and the failure rows all come from the same commit.
    """
    lo, hi = parse_serial_pair(serial_first, serial_last)
    if max_segments < 1:
        raise ValidationError("max_segments 必须大于 0")
    if max_segments > MAX_SEGMENTS_LIMIT:
        raise ValidationError(f"max_segments 不能超过 {MAX_SEGMENTS_LIMIT}")
    if conn.in_transaction:
        return _query(conn, lo, hi + 1, max_segments)
    with read_tx(conn):
        return _query(conn, lo, hi + 1, max_segments)
