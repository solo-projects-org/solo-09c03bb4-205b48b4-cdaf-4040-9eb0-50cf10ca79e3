"""Live end-to-end check against a real uvicorn process (stdlib only).

Run from the repository root:

    python3 scripts/smoke_check.py

It starts the app on a free port with a throwaway database, exercises the
scenarios named in the delivery checklist (out-of-order uploads, overlapping
ranges, retraction, conflicting re-upload, restart), prints what it observed,
and exits non-zero on the first mismatch.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEP = str(uuid.uuid4())


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def multipart(fields: dict[str, str], file_text: str | None) -> tuple[bytes, str]:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{SEP}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    if file_text is not None:
        parts.append(
            (
                f'--{SEP}\r\nContent-Disposition: form-data; name="failures_csv";'
                f' filename="fail.csv"\r\nContent-Type: text/csv\r\n\r\n'
            ).encode()
            + file_text.encode("utf-8")
            + b"\r\n"
        )
    parts.append(f"--{SEP}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={SEP}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface the 303 instead of following it


class Client:
    def __init__(self, base: str):
        self.base = base
        self.opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, path, data=None, headers=None):
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers or {}, method=method
        )
        try:
            with self.opener.open(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def upload(self, no, first, last, csv_text=None):
        body, ctype = multipart(
            {
                "report_no": str(no),
                "serial_first": str(first),
                "serial_last": str(last),
            },
            csv_text,
        )
        return self.request("POST", "/reports", body, {"Content-Type": ctype})

    def retract(self, no):
        return self.request("POST", f"/reports/{no}/retract")

    def correct(self, no, orig_first, orig_last, new_first):
        body = (
            f"orig_first={orig_first}&orig_last={orig_last}&new_first={new_first}"
        ).encode()
        return self.request(
            "POST",
            f"/reports/{no}/correct",
            body,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )

    def query(self, first, last):
        status, body = self.request(
            "GET", f"/api/query?serial_first={first}&serial_last={last}"
        )
        assert status == 200, (status, body)
        return json.loads(body)

    def rows(self, first, last):
        return [
            (s["serial_first"], s["serial_last"], s["conclusion"], s["report_no"])
            for s in self.query(first, last)["segments"]
        ]


def start(db: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ, PM_DB_PATH=str(db))
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=1
            ) as resp:
                if resp.status == 200:
                    return proc
        except OSError:
            time.sleep(0.1)
    proc.terminate()
    out = proc.communicate(timeout=10)[0].decode(errors="replace")
    raise SystemExit(f"server failed to start:\n{out}")


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if proc.stdout:
        proc.stdout.close()


def check(label: str, got, want) -> None:
    if got != want:
        raise SystemExit(f"FAIL {label}\n  got:  {got!r}\n  want: {want!r}")
    print(f"  ok  {label}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "smoke.db"
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        proc = start(db, port)
        try:
            c = Client(base)

            print("1. 乱序上传：按 103 → 101 → 102 上传，报告号决定判定")
            check("upload 103", c.upload(103, 500, 560, "505\n")[0], 303)
            check("upload 101", c.upload(101, 1, 1000, "600\n")[0], 303)
            check("upload 102", c.upload(102, 550, 650, "600\n620\n")[0], 303)
            check(
                "数值最小但覆盖的 101 负责 400–410",
                c.rows(400, 410),
                [(400, 410, "pass", 101)],
            )
            check(
                "重叠区 590–630 由 102 判定（600、620 不合格）",
                c.rows(590, 630),
                [
                    (590, 599, "pass", 102),
                    (600, 600, "fail", 102),
                    (601, 619, "pass", 102),
                    (620, 620, "fail", 102),
                    (621, 630, "pass", 102),
                ],
            )
            check(
                "重叠区 550–561 由号码更大的 103 判定",
                c.rows(550, 561),
                [(550, 560, "pass", 103), (561, 561, "pass", 102)],
            )
            check(
                "101 覆盖全域，但 102 覆盖处 102 优先",
                c.rows(601, 700)[:1],
                [(601, 619, "pass", 102)],
            )

            print("1b. 网页渲染：明细行带出结论标签，依据报告可点开")
            status, html = c.request("GET", "/query?serial_first=590&serial_last=630")
            check("query page status", status, 200)
            check("no empty conclusion tag", '<span class="tag pass"></span>' in html, False)
            check("fail label rendered", '<span class="tag fail">不合格</span>' in html, True)
            check("evidence links to report page", 'href="/reports/102"' in html, True)
            status, detail = c.request("GET", "/reports/102")
            check("report detail status", status, 200)
            check("detail shows range", "550 – 650" in detail, True)
            check("detail lists failures", ">600<" in detail and ">620<" in detail, True)

            print("2. 同号同内容重传：保持原状态，不新增记录")
            before = json.loads(c.request("GET", "/api/reports")[1])
            status, _ = c.upload(101, 1, 1000, "600\n")
            after = json.loads(c.request("GET", "/api/reports")[1])
            check("re-upload accepted", status, 303)
            check("report count unchanged", len(after["reports"]), len(before["reports"]))
            check("total unchanged", after["total"], 3)

            print("3. 同号异文：整份拒收，原报告不受影响")
            status, body = c.upload(101, 1, 1000, "601\n")
            check("conflict status", status, 409)
            check("conflict message", "内容不同" in body, True)
            check("total still 3", json.loads(c.request("GET", "/api/reports")[1])["total"], 3)
            check("original ruling intact", c.rows(600, 600), [(600, 600, "fail", 102)])

            print("4. 撤回后按剩余报告重判，无报告覆盖则为待检")
            check("retract 102", c.retract(102)[0], 303)
            check(
                "600 回落到 101（不合格）",
                c.rows(590, 630),
                [
                    (590, 599, "pass", 101),
                    (600, 600, "fail", 101),
                    (601, 630, "pass", 101),
                ],
            )
            check("retract 103", c.retract(103)[0], 303)
            check(
                "560 号段回落到 101",
                c.rows(550, 561),
                [(550, 561, "pass", 101)],
            )
            check("retract 101", c.retract(101)[0], 303)
            check("全部撤回后为待检", c.rows(1, 10), [(1, 10, "pending", None)])

            print("5. 勘误：段内不合格号整体移到等长新号段")
            check("upload 300", c.upload(300, 3000, 3999, "3100\n3101\n3200\n")[0], 303)
            check("correct 300", c.correct(300, 3100, 3200, 7000)[0], 303)
            check(
                "原段空出、待检，其后编号仍由 300 判定",
                c.rows(3095, 3205),
                [
                    (3095, 3099, "pass", 300),
                    (3100, 3200, "pending", None),
                    (3201, 3205, "pass", 300),
                ],
            )
            check(
                "新段按原顺序带上不合格号（3100→7000、3101→7001、3200→7100）",
                c.rows(6995, 7105),
                [
                    (6995, 6999, "pending", None),
                    (7000, 7001, "fail", 300),
                    (7002, 7099, "pass", 300),
                    (7100, 7100, "fail", 300),
                    (7101, 7105, "pending", None),
                ],
            )
            status, detail = c.request("GET", "/reports/300")
            check("detail shows erratum section", "勘误前后号段" in detail, True)
            check("detail shows before-段", "3100" in detail and "3200" in detail, True)
            check("detail shows after-段", "7000" in detail and "7100" in detail, True)
            check("detail keeps original list", "原上传名单" in detail, True)
            check("detail has no second-erratum form", "办理勘误</h3>" in detail, False)

            print("5b. 重叠移号：新号段可压在被移动段上，但不能压本报告未移动编号")
            check("upload 301 (5000–5099, fail 5002)", c.upload(301, 5000, 5099, "5002\n")[0], 303)
            check("overlap move 5000-5010 -> 4995-5005", c.correct(301, 5000, 5010, 4995)[0], 303)
            check(
                "overlap 4995–5005 takes only moved serials; 5006–5010 vacated",
                c.rows(4990, 5020),
                [
                    (4990, 4994, "pending", None),
                    (4995, 4996, "pass", 301),
                    (4997, 4997, "fail", 301),      # 5002 moved to 4997
                    (4998, 5005, "pass", 301),
                    (5006, 5010, "pending", None),
                    (5011, 5020, "pass", 301),
                ],
            )
            check("upload 302 (6000–6199, fail 6001)", c.upload(302, 6000, 6199, "6001\n")[0], 303)
            check(
                "overlap into unmoved serials 6011–6015 is refused",
                c.correct(302, 6000, 6010, 6005)[0],
                409,
            )
            check(
                "new run may point into another report's serials (overlap is legal)",
                c.correct(302, 6000, 6010, 3000)[0],
                303,
            )
            check(
                "302 (higher number) now rules 3000–3010 inside 300's range;"
                " 6001 moved to 3001 and fails there",
                c.rows(3000, 3010),
                [
                    (3000, 3000, "pass", 302),
                    (3001, 3001, "fail", 302),
                    (3002, 3010, "pass", 302),
                ],
            )
            check(
                "302's vacated 6000–6010 is pending, rest of 302 still rules",
                c.rows(6000, 6012),
                [(6000, 6010, "pending", None), (6011, 6012, "pass", 302)],
            )

            print("5c. 同参重试 / 改参冲突 / 撤回后的勘误")
            check("same erratum retried", c.correct(300, 3100, 3200, 7000)[0], 303)
            check("different erratum rejected", c.correct(300, 3100, 3200, 7500)[0], 409)
            check("retract 301", c.retract(301)[0], 303)
            check(
                "retracted corrected report no longer rules",
                c.rows(4995, 5005)[0][2:],
                ("pending", None),
            )
            check("same erratum after retraction is a replay", c.correct(301, 5000, 5010, 4995)[0], 303)
            check("different erratum after retraction conflicts", c.correct(301, 5000, 5010, 4000)[0], 409)

            print("6. 撤回已勘误报告：整份失效，重传原件保留勘误与撤回")
            check("correct 300 retracted", c.retract(300)[0], 303)
            check("new segment pending after retraction", c.rows(7000, 7001), [(7000, 7001, "pending", None)])
            check("original re-upload accepted", c.upload(300, 3000, 3999, "3100\n3101\n3200\n")[0], 303)
            status, detail = c.request("GET", "/reports/300")
            check("re-upload keeps retracted state", "已撤回" in detail, True)
            check("re-upload keeps erratum record", "7000" in detail and "7100" in detail, True)
            check("still pending after re-upload", c.rows(7000, 7001), [(7000, 7001, "pending", None)])
            check("report count unchanged by replay", json.loads(c.request("GET", "/api/reports")[1])["total"], 6)

            print("7. 勘误后查询：依据报告快照展示生效覆盖")
            check("upload 303", c.upload(303, 8000, 8099, "8050\n")[0], 303)
            check("correct 303", c.correct(303, 8050, 8060, 9000)[0], 303)
            check(
                "vacated + moved segment ruled correctly",
                c.rows(8045, 9015),
                [
                    (8045, 8049, "pass", 303),
                    (8050, 8060, "pending", None),
                    (8061, 8099, "pass", 303),
                    (8100, 8999, "pending", None),
                    (9000, 9000, "fail", 303),
                    (9001, 9010, "pass", 303),
                    (9011, 9015, "pending", None),
                ],
            )
            status, html = c.request("GET", "/query?serial_first=8045&serial_last=9015")
            check("query page renders after erratum", status, 200)
            evidence = c.query(8061, 8061)["evidence"]
            check("evidence lists corrected report", [e["report_no"] for e in evidence], [303])
            check("evidence marks corrected report", evidence[0]["corrected"], True)
            check("evidence shows effective coverage", evidence[0]["coverage"], [
                {"serial_first": 8000, "serial_last": 8049},
                {"serial_first": 8061, "serial_last": 8099},
                {"serial_first": 9000, "serial_last": 9010},
            ])

            print("8. 百万级号段：入库一条记录，查询不逐号展开")
            check("retract 303 (its failure would otherwise add to the count)", c.retract(303)[0], 303)
            check("upload 200 (2,000,000 serials)", c.upload(200, 0, 1_999_999, "1234567\n")[0], 303)
            start_t = time.perf_counter()
            big = c.query(0, 1_999_999)
            elapsed = time.perf_counter() - start_t
            check("2M serials counted exactly", big["summary"]["count"], 2_000_000)
            check("the expected serial is the failure", c.query(1234567, 1234567)["summary"]["fail"], 1)
            check("302's own failure adds one more in the 2M span", big["summary"]["fail"], 2)
            check("segments stay tiny (a handful, not 2,000,000)", len(big["segments"]) <= 12, True)
            check("query under 1s", elapsed < 1.0, True)
            print(f"      (2,000,000-number query took {elapsed * 1000:.1f} ms)")

        finally:
            stop(proc)

        print("9. 重启后仍可查，结论与依据不变")
        proc = start(db, port)
        try:
            c = Client(base)
            after = c.query(0, 1_999_999)
            check("2M summary identical after restart", after["summary"], {
                "pass": 1_999_998, "fail": 2, "pending": 0, "count": 2_000_000,
            })
            check("failed serial survives restart", c.query(1234567, 1234567)["summary"]["fail"], 1)
            check("retraction states survive restart", [
                (r["report_no"], r["retracted"])
                for r in json.loads(c.request("GET", "/api/reports")[1])["reports"]
            ], [
                (303, True), (302, False), (301, True), (300, True),
                (200, False), (103, True), (102, True), (101, True),
            ])
            check(
                "retracted corrected report still ignored after restart",
                c.query(7000, 7001)["evidence"][0]["report_no"],
                200,
            )
            status, detail = c.request("GET", "/api/reports/300")
            payload = json.loads(detail)
            check("erratum record survives restart", payload["correction"]["new_first"], 7000)
            check("retracted+corrected report still replays its erratum", c.correct(300, 3100, 3200, 7000)[0], 303)
            check("changed erratum still conflicts after restart", c.correct(300, 3100, 3200, 7500)[0], 409)
        finally:
            stop(proc)

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
