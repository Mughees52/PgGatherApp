from app.pipeline import extract


def test_parse_tsv_metadata(sample_tsv):
    meta = extract.parse_tsv_metadata(sample_tsv)
    assert meta.system_id and meta.system_id.isdigit()
    assert meta.pg_version and "PostgreSQL" in meta.pg_version
    assert meta.pg_version_num and meta.pg_version_num >= 10
    assert meta.engine_ver == "33"
    assert meta.collected_at  # timestamp present
    # srvr_* parsed from \conninfo (host may be the rewritten container host)
    assert meta.srvr_db


def test_parse_report_html_obj_meta():
    html = """
    <html><body><script>
    obj={"clsr": false, "sess": [1,2,3]}
    ver="33";
    meta={"pgvers":["16.13"],"commonExtn":["plpgsql"]};
    </script></body></html>
    """
    r = extract.parse_report_html(html)
    assert r.obj == {"clsr": False, "sess": [1, 2, 3]}
    assert r.meta["pgvers"] == ["16.13"]


def test_parse_report_html_handles_garbage():
    r = extract.parse_report_html("<html>no script here</html>")
    assert r.obj is None and r.meta is None
