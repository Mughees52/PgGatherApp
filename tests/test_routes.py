"""Route-level tests using httpx TestClient.

Mocks docker_runner to avoid needing Docker. Tests cover:
- Library, detail, upload, connections CRUD
- CSP headers on /view
- Content-Disposition on downloads
- Compare rejects mismatched server_key
- Password never in /connections responses
- Recommendations endpoint
- Status polling
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# --- Helpers ----------------------------------------------------------------

def _create_report_via_upload(filename="test.tsv") -> str:
    """Upload a minimal TSV and return the report ID from the redirect URL."""
    # Minimal pg_gather TSV content (enough for extract to parse)
    tsv = (
        "COPY pg_srvr FROM stdin;\n"
        'You are connected to database "testdb" as user "postgres" on host "localhost" at port "5432".\n'
        "\\.\n"
        "COPY pg_gather (collect_ts,usr,db,ver,pg_start_ts,recovery,client,server,"
        "reload_ts,timeline,systemid) FROM stdin;\n"
        "2026-06-14 12:00:00+00\tpg_gather.V33\ttestdb\tPostgreSQL 16.3\t"
        "2026-06-14 10:00:00+00\tf\t127.0.0.1\t127.0.0.1\t"
        "2026-06-14 11:00:00+00\t1\t9999999999\n"
        "\\.\n"
    )
    with patch("app.jobs.enqueue"):
        resp = client.post("/reports", files={"file": (filename, tsv.encode(), "text/plain")},
                           follow_redirects=False)
    assert resp.status_code == 303
    rid = resp.headers["location"].split("/")[-1]
    return rid


def _set_report_done(rid: str) -> None:
    """Manually set a report to done with mock data for testing."""
    from app import repository as repo
    repo.update_report(rid, {
        "status": "done",
        "stored_html_path": "/dev/null",
        "stored_tsv_path": "/dev/null",
        "report_json": json.dumps({"sess": {"f1": 1, "f2": 0, "f3": 1, "f6": 2, "f7": 5},
                                    "tabs": {"f1": 5, "f2": 0, "f3": 0, "f4": 0},
                                    "induse": {"f1": 0, "f2": 0, "f4": 3, "f5": 0, "f6": 0},
                                    "nokey": {"f1": 0, "f2": 0},
                                    "arcfail": {"f1": 10},
                                    "cn": {"f1": 2, "f2": 0},
                                    "meta": {"f1": 1000, "f2": 5000, "f3": 5},
                                    "clas": {"f1": 0, "f2": 0, "f3": "100"},
                                    "params": {"f6": 50},
                                    "clsr": False}),
        "params_json": json.dumps({"shared_buffers": "16384", "max_connections": "100"}),
        "server_key": "9999999999",
        "system_id": "9999999999",
        "srvr_host": "localhost",
        "pg_version_num": 16,
    })


# --- Tests ------------------------------------------------------------------

def test_index_redirects_to_reports():
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307 or resp.status_code == 302 or resp.status_code == 303
    assert "/reports" in resp.headers.get("location", "")


def test_library_page():
    resp = client.get("/reports")
    assert resp.status_code == 200
    assert "Report Library" in resp.text


def test_connections_page():
    resp = client.get("/connections")
    assert resp.status_code == 200
    assert "Connections" in resp.text


def test_upload_page():
    resp = client.get("/reports/upload")
    assert resp.status_code == 200
    assert "Upload" in resp.text or "upload" in resp.text


def test_compare_picker():
    resp = client.get("/compare")
    assert resp.status_code == 200


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_upload_creates_report():
    rid = _create_report_via_upload()
    resp = client.get(f"/reports/{rid}")
    assert resp.status_code == 200


def test_report_status_endpoint():
    rid = _create_report_via_upload()
    resp = client.get(f"/reports/{rid}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data


def test_report_status_404():
    resp = client.get("/reports/nonexistent/status")
    assert resp.status_code == 404


def test_report_detail_with_view():
    rid = _create_report_via_upload()
    _set_report_done(rid)
    resp = client.get(f"/reports/{rid}")
    assert resp.status_code == 200
    assert "Findings" in resp.text


@patch("app.pipeline.docker_runner.docker_available", return_value=False)
def test_view_serves_html_with_csp(mock_docker):
    """The /view endpoint should set sandbox CSP headers."""
    rid = _create_report_via_upload()
    from app import repository as repo, storage
    # Write a minimal HTML file
    html_path = storage.html_path(rid)
    html_path.write_text("<html><body>test</body></html>")
    repo.update_report(rid, {"status": "done", "stored_html_path": str(html_path)})

    resp = client.get(f"/reports/{rid}/view")
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert "sandbox" in csp


def test_download_sets_content_disposition():
    rid = _create_report_via_upload()
    from app import repository as repo, storage
    html_path = storage.html_path(rid)
    html_path.write_text("<html>report</html>")
    repo.update_report(rid, {"status": "done", "stored_html_path": str(html_path)})

    resp = client.get(f"/reports/{rid}/download/html")
    assert resp.status_code == 200
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd or "GatherReport" in cd


def test_compare_rejects_mismatched_server_key():
    rid1 = _create_report_via_upload("a.tsv")
    rid2 = _create_report_via_upload("b.tsv")
    from app import repository as repo
    repo.update_report(rid1, {"status": "done", "server_key": "aaa"})
    repo.update_report(rid2, {"status": "done", "server_key": "bbb"})

    resp = client.get(f"/compare/{rid1}/{rid2}")
    assert resp.status_code == 400


def test_connection_create_and_password_not_exposed():
    # Create a connection
    resp = client.post("/connections", data={
        "name": "test_conn", "host": "localhost", "port": "5432",
        "dbname": "testdb", "username": "postgres",
        "password": "supersecret123", "sslmode": "prefer",
    }, follow_redirects=False)
    assert resp.status_code == 303

    # List connections — password must NOT appear
    resp = client.get("/connections")
    assert resp.status_code == 200
    assert "supersecret123" not in resp.text
    assert "password_enc" not in resp.text


def test_recommendations_endpoint():
    rid = _create_report_via_upload()
    _set_report_done(rid)
    resp = client.post(f"/reports/{rid}/recommend", data={
        "cpus": "8", "memory_gb": "32", "storage": "ssd",
        "workload": "oltp", "filesystem": "rglr",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "recommendations" in data
    assert isinstance(data["recommendations"], list)


def test_tags_and_notes():
    rid = _create_report_via_upload()
    _set_report_done(rid)
    # Set tags
    resp = client.post(f"/reports/{rid}/tags", data={"tags": "prod, primary"},
                       follow_redirects=False)
    assert resp.status_code == 303

    # Set notes
    resp = client.post(f"/reports/{rid}/notes", data={"notes": "Test note"},
                       follow_redirects=False)
    assert resp.status_code == 303

    # Verify
    resp = client.get(f"/reports/{rid}")
    assert "prod" in resp.text
    assert "Test note" in resp.text


def test_delete_report():
    rid = _create_report_via_upload()
    resp = client.post(f"/reports/{rid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    # Verify gone
    resp = client.get(f"/reports/{rid}", follow_redirects=False)
    assert resp.status_code == 303  # redirects to library


def test_server_key_filter_on_library():
    resp = client.get("/reports?server_key=nonexistent")
    assert resp.status_code == 200
