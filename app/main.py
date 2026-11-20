"""FastAPI application: report intake, retraction and range rulings."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import service
from .db import connect, init_db
from .service import (
    CONCL_LABEL,
    NotFoundError,
    ServiceError,
    ValidationError,
    build_report,
    correct_report,
    create_report,
    decode_failure_csv,
    get_overview,
    list_reports,
    parse_failure_csv,
    query_range,
    report_detail,
    retract_report,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["CONCL_LABEL"] = CONCL_LABEL
templates.env.globals["MAX_SERIAL"] = service.MAX_SERIAL


def _db_path() -> Path:
    return Path(os.environ.get("PM_DB_PATH", "data/meter.db"))


def create_app(db_path: Path | str | None = None) -> FastAPI:
    resolved = Path(db_path) if db_path is not None else _db_path()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(resolved)
        yield

    app = FastAPI(title="电表检定台", version="1.0.0", lifespan=lifespan)
    app.state.db_path = resolved
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def get_db(request: Request) -> Iterator[sqlite3.Connection]:
        conn = connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _render(request: Request, name: str, context: dict, status_code: int = 200):
        ctx = {"request": request, **context}
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def _report_context(conn: sqlite3.Connection, report_no: int, **extra) -> dict:
        detail = report_detail(conn, report_no)
        return {
            "title": f"报告 {report_no}",
            "active_nav": "reports",
            "report": detail["report"],
            "coverage": detail["coverage"],
            "correction": detail["correction"],
            "failures": detail["failures"],
            "original_failures": detail["original_failures"],
            "corr_form": {
                "orig_first": "",
                "orig_last": "",
                "new_first": "",
            },
            "flash": None,
            "flash_kind": "ok",
            **extra,
        }

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"error": str(exc), "type": type(exc).__name__},
                status_code=exc.status,
            )
        return _render(
            request,
            "error.html",
            {"title": "操作未完成", "message": str(exc)},
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """Render missing/invalid form fields as a page, not raw JSON, in browsers."""
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {
                    "error": "请求参数不合法",
                    "type": "RequestValidationError",
                    "detail": exc.errors(),
                },
                status_code=422,
            )
        fields = "、".join(str(err.get("loc", ["?"])[-1]) for err in exc.errors()) or "表单"
        return _render(
            request,
            "error.html",
            {"title": "操作未完成", "message": f"请完整填写：{fields}"},
            status_code=422,
        )

    # -- pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index():
        return RedirectResponse("/reports", status_code=303)

    def _reports_context(conn: sqlite3.Connection, page: int = 1) -> dict:
        page = max(1, page)
        per_page = 25
        reports, total = list_reports(conn, limit=per_page, offset=(page - 1) * per_page)
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "title": "检定报告",
            "active_nav": "reports",
            "reports": reports,
            "overview": get_overview(conn),
            "page": page,
            "pages": pages,
            "total": total,
            "form": {"report_no": "", "serial_first": "", "serial_last": ""},
            "flash": None,
            "flash_kind": "ok",
        }

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(
        request: Request,
        conn: sqlite3.Connection = Depends(get_db),
        flash: str | None = None,
        flash_kind: str = "ok",
        page: int = 1,
    ):
        return _render(
            request,
            "reports.html",
            {
                **_reports_context(conn, page),
                "flash": flash,
                "flash_kind": flash_kind,
            },
        )

    @app.post("/reports")
    async def upload_report(
        request: Request,
        report_no: str = Form(...),
        serial_first: str = Form(...),
        serial_last: str = Form(...),
        failures_csv: UploadFile | None = File(None),
        conn: sqlite3.Connection = Depends(get_db),
    ):
        failures: list[int] = []
        if failures_csv is not None and failures_csv.filename:
            raw = await failures_csv.read(service.MAX_CSV_BYTES + 1)
            if len(raw) > service.MAX_CSV_BYTES:
                raise ValidationError(
                    f"名单文件超过 {service.MAX_CSV_BYTES // (1024 * 1024)} MB 上限"
                )
            text = decode_failure_csv(raw) if raw.strip() else ""
            failures = parse_failure_csv(text)
        try:
            report = build_report(report_no, serial_first, serial_last, failures)
        except ValidationError as exc:
            # Echo the form back so nothing the user typed has to be retyped.
            return _render(
                request,
                "reports.html",
                {
                    **_reports_context(conn),
                    "form": {
                        "report_no": report_no,
                        "serial_first": serial_first,
                        "serial_last": serial_last,
                    },
                    "flash": str(exc),
                    "flash_kind": "error",
                },
                status_code=exc.status,
            )
        result = create_report(conn, report)
        if result.created:
            message = (
                f"已接收报告 {report.report_no}："
                f"{report.serial_first}–{report.serial_last}，"
                f"不合格 {len(report.failures)} 个"
            )
            kind = "ok"
        else:
            state = "已撤回" if result.report["retracted"] else "有效"
            message = (
                f"报告 {report.report_no} 内容与已有记录一致，未重复写入，"
                f"保持原状态（{state}）"
            )
            kind = "info"
        return RedirectResponse(
            f"/reports?flash={_quote(message)}&flash_kind={kind}",
            status_code=303,
        )

    @app.get("/reports/{report_no}", response_class=HTMLResponse)
    def report_page(
        request: Request,
        report_no: int,
        flash: str | None = None,
        flash_kind: str = "ok",
        conn: sqlite3.Connection = Depends(get_db),
    ):
        return _render(
            request,
            "report.html",
            _report_context(conn, report_no, flash=flash, flash_kind=flash_kind),
        )

    @app.post("/reports/{report_no}/correct")
    def correct(
        request: Request,
        report_no: int,
        orig_first: str = Form(...),
        orig_last: str = Form(...),
        new_first: str = Form(...),
        conn: sqlite3.Connection = Depends(get_db),
    ):
        try:
            result = correct_report(conn, report_no, orig_first, orig_last, new_first)
        except NotFoundError:
            raise  # the shared handler renders the "report not found" page
        except ServiceError as exc:
            # Keep the typed values visible on the detail page.
            return _render(
                request,
                "report.html",
                _report_context(
                    conn,
                    report_no,
                    flash=str(exc),
                    flash_kind="error",
                    corr_form={
                        "orig_first": orig_first,
                        "orig_last": orig_last,
                        "new_first": new_first,
                    },
                ),
                status_code=exc.status,
            )
        corr = result.correction
        if result.created:
            message = (
                f"报告 {report_no} 已勘误：{corr['orig_first']}–{corr['orig_last']} "
                f"移至 {corr['new_first']}–{corr['new_last']}"
            )
            kind = "ok"
        else:
            message = (
                f"报告 {report_no} 已有相同勘误（{corr['orig_first']}–"
                f"{corr['orig_last']} → {corr['new_first']}–{corr['new_last']}），"
                f"未重复办理"
            )
            kind = "info"
        return RedirectResponse(
            f"/reports/{report_no}?flash={_quote(message)}&flash_kind={kind}",
            status_code=303,
        )

    @app.post("/reports/{report_no}/retract")
    def retract(
        report_no: int,
        conn: sqlite3.Connection = Depends(get_db),
    ):
        result = retract_report(conn, report_no)
        if result.changed:
            message = f"报告 {report_no} 已撤回，相关编号按剩余报告重判"
            kind = "ok"
        else:
            message = f"报告 {report_no} 此前已撤回，状态未变"
            kind = "info"
        return RedirectResponse(
            f"/reports?flash={_quote(message)}&flash_kind={kind}",
            status_code=303,
        )

    @app.get("/query", response_class=HTMLResponse)
    def query_page(
        request: Request,
        serial_first: str | None = None,
        serial_last: str | None = None,
        max_segments: int = service.DEFAULT_MAX_SEGMENTS,
        conn: sqlite3.Connection = Depends(get_db),
    ):
        result = None
        error = None
        if serial_first is not None and serial_last is not None:
            try:
                result = query_range(conn, serial_first, serial_last, max_segments)
            except ServiceError as exc:
                error = str(exc)
        elif serial_first is not None or serial_last is not None:
            error = "请同时填写起始编号和结束编号"
        return _render(
            request,
            "query.html",
            {
                "title": "放行查询",
                "active_nav": "query",
                "serial_first": serial_first or "",
                "serial_last": serial_last or "",
                "max_segments": max_segments,
                "result": result,
                "error": error,
            },
        )

    # -- JSON API ---------------------------------------------------------

    @app.get("/api/reports")
    def api_reports(conn: sqlite3.Connection = Depends(get_db)):
        reports, total = list_reports(conn, limit=100_000, offset=0)
        return {"total": total, "reports": reports}

    @app.get("/api/reports/{report_no}")
    def api_report(report_no: int, conn: sqlite3.Connection = Depends(get_db)):
        detail = report_detail(conn, report_no)
        # Keep the original flat fields and add the erratum view alongside.
        return {
            **detail["report"],
            "coverage": detail["coverage"],
            "correction": detail["correction"],
            "failures": detail["failures"],
            "original_failures": detail["original_failures"],
        }

    @app.get("/api/query")
    def api_query(
        serial_first: str,
        serial_last: str,
        max_segments: int = service.DEFAULT_MAX_SEGMENTS,
        conn: sqlite3.Connection = Depends(get_db),
    ):
        return query_range(conn, serial_first, serial_last, max_segments).as_dict()

    @app.get("/healthz")
    def healthz(conn: sqlite3.Connection = Depends(get_db)):
        conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/favicon.ico")
    def favicon():
        return RedirectResponse("/static/favicon.svg", status_code=307)

    return app


def _quote(message: str) -> str:
    return quote(message)


app = create_app()
