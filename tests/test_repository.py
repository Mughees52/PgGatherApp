from app import repository as repo


def test_report_crud_and_tags():
    rid = repo.create_report(source="uploaded", status="queued",
                             original_filename="x.tsv")
    repo.update_report(rid, {"status": "done", "system_id": "12345",
                             "server_key": "12345", "srvr_host": "db1"})
    r = repo.get_report(rid)
    assert r["status"] == "done" and r["system_id"] == "12345"

    repo.set_report_tags(rid, ["Prod", "prod", " Primary "])  # dedupe + lowercase
    assert repo.get_report_tags(rid) == ["primary", "prod"]

    repo.update_notes(rid, "hello")
    assert repo.get_report(rid)["notes"] == "hello"


def test_search_and_filters():
    rid = repo.create_report(source="uploaded", status="done",
                             original_filename="searchme.tsv")
    repo.update_report(rid, {"srvr_host": "needle-host", "server_key": "k1"})
    repo.set_report_tags(rid, ["findtag"])

    assert any(r["id"] == rid for r in repo.list_reports(search="needle"))
    assert any(r["id"] == rid for r in repo.list_reports(tag="findtag"))
    assert any(r["id"] == rid for r in repo.list_reports(status="done"))
    assert any(r["id"] == rid for r in repo.list_reports(server_key="k1"))
    assert all(r["id"] != rid for r in repo.list_reports(search="no-such-string"))


def test_server_key_matching_groups_same_cluster():
    a = repo.create_report(source="collected", status="done")
    b = repo.create_report(source="collected", status="done")
    repo.update_report(a, {"server_key": "sys-9", "system_id": "9"})
    repo.update_report(b, {"server_key": "sys-9", "system_id": "9"})
    matches = {r["id"] for r in repo.list_reports(server_key="sys-9")}
    assert {a, b} <= matches


def test_reset_stuck_jobs():
    rid = repo.create_report(source="collected", status="collecting")
    repo.reset_stuck_jobs()
    assert repo.get_report(rid)["status"] == "failed"
