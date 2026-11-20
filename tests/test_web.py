"""End-to-end behaviour through the HTTP layer: intake, retraction, querying."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as c:
        yield c


def upload(client, no, first, last, csv_text=None, filename="fail.csv"):
    files = {}
    if csv_text is not None:
        files = {"failures_csv": (filename, io.BytesIO(csv_text.encode("utf-8")), "text/csv")}
    return client.post(
        "/reports",
        data={"report_no": str(no), "serial_first": str(first), "serial_last": str(last)},
        files=files or None,
        follow_redirects=False,
    )


def rows(client, first, last):
    data = client.get(f"/api/query?serial_first={first}&serial_last={last}").json()
    return [
        (s["serial_first"], s["serial_last"], s["conclusion"], s["report_no"])
        for s in data["segments"]
    ]


def test_upload_redirects_with_confirmation(client):
    response = upload(client, 1, 100, 200, "150\n151\n")
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "已接收报告 1" in page.text
    assert "100" in page.text and "200" in page.text
    assert rows(client, 150, 151) == [(150, 151, "fail", 1)]  # coalesced
    assert rows(client, 149, 152) == [
        (149, 149, "pass", 1),
        (150, 151, "fail", 1),
        (152, 152, "pass", 1),
    ]


def test_empty_csv_means_whole_range_passes(client):
    upload(client, 1, 1, 1_000_000, "")
    data = client.get("/api/query?serial_first=1&serial_last=1000000").json()
    assert data["summary"] == {"pass": 1000000, "fail": 0, "pending": 0, "count": 1000000}
    assert len(data["segments"]) == 1


def test_no_file_at_all_means_whole_range_passes(client):
    upload(client, 1, 1, 10)
    assert rows(client, 1, 10) == [(1, 10, "pass", 1)]


def test_same_number_same_content_keeps_state(client):
    upload(client, 5, 1, 100, "50\n")
    client.post("/reports/5/retract", follow_redirects=False)
    again = upload(client, 5, 1, 100, "50\n")
    assert again.status_code == 303
    page = client.get(again.headers["location"])
    assert "未重复写入" in page.text
    assert "已撤回" in page.text
    # Ruling still ignores it.
    assert rows(client, 50, 50) == [(50, 50, "pending", None)]
    assert client.get("/api/reports").json()["total"] == 1


def test_same_number_different_content_rejected_whole(client):
    upload(client, 5, 1, 100, "50\n")
    bad = upload(client, 5, 1, 100, "51\n")
    assert bad.status_code == 409
    assert "内容不同" in bad.text
    assert client.get("/api/reports").json()["total"] == 1
    assert rows(client, 50, 51) == [
        (50, 50, "fail", 5),
        (51, 51, "pass", 5),
    ]


def test_out_of_range_failure_list_is_rejected(client):
    bad = upload(client, 5, 100, 200, "99\n")
    assert bad.status_code == 422
    assert "不在报告范围内" in bad.text
    # The typed values come back in the form so they need not be retyped.
    assert 'value="100"' in bad.text and 'value="200"' in bad.text
    assert client.get("/api/reports").json()["total"] == 0


def test_missing_form_field_renders_page_not_json(client):
    response = client.post(
        "/reports",
        data={"report_no": "", "serial_first": "1", "serial_last": "10"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "请完整填写" in response.text


def test_out_of_order_uploads_and_overlap(client):
    # Uploaded in the order 30, 10, 20; report numbers decide the ruling.
    upload(client, 30, 500, 560, "505\n")
    upload(client, 10, 1, 1000, "600\n")
    upload(client, 20, 550, 650, "600\n620\n")
    assert rows(client, 590, 630) == [
        (590, 599, "pass", 20),
        (600, 600, "fail", 20),
        (601, 619, "pass", 20),
        (620, 620, "fail", 20),
        (621, 630, "pass", 20),
    ]
    # 550..560 is covered by both 20 and 30; the higher number wins, and
    # report 30's range ends at 560 so the ruling changes at 561.
    assert rows(client, 550, 561) == [
        (550, 560, "pass", 30),
        (561, 561, "pass", 20),
    ]
    # 600 belongs to report 20 and is failed there.
    assert rows(client, 600, 600) == [(600, 600, "fail", 20)]
    # Below report 30's range, report 10 rules.
    assert rows(client, 400, 410) == [(400, 410, "pass", 10)]


def test_retraction_recomputes_and_labels_pending(client):
    upload(client, 10, 1, 100, "50\n")
    upload(client, 20, 40, 60)
    assert rows(client, 50, 50) == [(50, 50, "pass", 20)]
    response = client.post("/reports/20/retract", follow_redirects=False)
    page = client.get(response.headers["location"])
    assert "已撤回" in page.text
    assert rows(client, 50, 50) == [(50, 50, "fail", 10)]
    client.post("/reports/10/retract", follow_redirects=False)
    assert rows(client, 50, 50) == [(50, 50, "pending", None)]


def test_retract_is_idempotent(client):
    upload(client, 10, 1, 100)
    assert client.post("/reports/10/retract", follow_redirects=False).status_code == 303
    second = client.post("/reports/10/retract", follow_redirects=False)
    assert second.status_code == 303
    assert "此前已撤回" in client.get(second.headers["location"]).text
    assert client.get("/api/reports").json()["reports"][0]["retracted"] is True
    assert client.get("/api/reports").json()["total"] == 1


def test_retract_missing_report_returns_404(client):
    response = client.post("/reports/123/retract", follow_redirects=False)
    assert response.status_code == 404


def test_query_page_renders_groups_and_segments(client):
    upload(client, 1, 1, 1000, "500\n")
    upload(client, 2, 400, 600)
    page = client.get("/query?serial_first=450&serial_last=550")
    assert page.status_code == 200
    assert "依据报告" in page.text
    assert "#2" in page.text
    assert "合格" in page.text and "不合格" in page.text
    # Every segment row carries its conclusion label, not an empty tag.
    assert '<span class="tag pass"></span>' not in page.text
    assert '<span class="tag fail">不合格</span>' not in page.text  # 2 overrides 1's failure
    assert '<span class="tag pass">合格</span>' in page.text


def test_query_page_shows_pending_for_uncovered(client):
    page = client.get("/query?serial_first=1&serial_last=10")
    assert page.status_code == 200
    assert "待检" in page.text


def test_bad_query_is_reported_not_crashed(client):
    page = client.get("/query?serial_first=10&serial_last=1")
    assert page.status_code == 200
    assert "起始编号不能大于结束编号" in page.text
    api = client.get("/api/query?serial_first=10&serial_last=1")
    assert api.status_code == 422
    assert "error" in api.json()


def test_gb18030_csv_is_accepted(client):
    payload = "编号\n50\n".encode("gb18030")
    response = client.post(
        "/reports",
        data={"report_no": "1", "serial_first": "1", "serial_last": "100"},
        files={"failures_csv": ("fail.csv", io.BytesIO(payload), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert rows(client, 50, 50) == [(50, 50, "fail", 1)]


def test_query_evidence_comes_from_one_snapshot(client):
    upload(client, 1, 1, 100, "50\n")
    data = client.get("/api/query?serial_first=1&serial_last=100").json()
    assert [e["report_no"] for e in data["evidence"]] == [1]
    manifest = client.get("/api/reports").json()["reports"]
    assert manifest[0]["content_hash"]


def test_state_survives_restart(tmp_path):
    db = tmp_path / "restart.db"
    with TestClient(create_app(db)) as c:
        upload(c, 1, 1, 1000, "500\n")
        upload(c, 2, 400, 600)
        before = c.get("/api/query?serial_first=1&serial_last=1000").json()
    with TestClient(create_app(db)) as c:
        after = c.get("/api/query?serial_first=1&serial_last=1000").json()
    assert before == after
    # 500 was failed by report 1 but report 2 (clean, higher number) covers it.
    assert after["summary"]["pass"] == 1000
    assert after["summary"]["fail"] == 0
    assert after["evidence"][-1]["report_no"] == 2


def test_report_detail_page(client):
    upload(client, 7, 100, 200, "150\n151\n160\n")
    page = client.get("/reports/7")
    assert page.status_code == 200
    assert "报告 #7" in page.text
    assert "100 – 200" in page.text
    assert ">150<" in page.text and ">160<" in page.text
    assert "撤回此报告" in page.text
    client.post("/reports/7/retract", follow_redirects=False)
    assert "已撤回" in client.get("/reports/7").text
    assert client.get("/reports/999").status_code == 404


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
