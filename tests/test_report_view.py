from app import report_view as rv

# A representative obj with the positional (fN) fields decoded from gather_report.sql.
OBJ = {
    "clsr": False,
    "sess": {"f1": 2, "f2": 1, "f3": 4, "f4": 5, "f5": 0, "f6": 12, "f7": 3},
    "tabs": {"f1": 10, "f2": 3, "f3": 0, "f4": 2},
    "induse": {"f1": 1, "f2": 4, "f3": 0, "f4": 20, "f5": 6, "f6": 327680},
    "meta": {"f1": 8700000, "f2": 9437184, "f3": 110},
    "sumry": {"f1": 25, "f2": 0.0, "f3": 290035704.0},
    "nokey": {"f1": 2, "f2": 1},
    "dbts": {"f1": "postgres", "f2": "2026-06-14T11:48:28+00:00", "f3": "x", "f4": 1},
    "clas": {"f1": 0, "f2": 0, "f3": "16388"},
    "ns": [{"nsname": "public"}, {"nsname": "pg_catalog"}],
    "arcfail": {"f1": None},
    "crash": None, "blkrs": None, "errparams": None, "mxiddbs": None,
}


def test_metric_cards_mapping():
    cards = {c["label"]: c for c in rv.metric_cards(OBJ)}
    assert cards["Connections"]["value"] == 12
    assert cards["Connections"]["tone"] == "warn"  # idle-in-txn > 0
    assert cards["Tables"]["value"] == 10
    assert cards["Indexes"]["value"] == 20          # induse.f4 = total user indexes
    assert cards["Indexes"]["tone"] == "bad"        # induse.f1 invalid > 0
    assert cards["Total Size"]["value"] == "9.0 MB"  # meta.f2
    assert cards["WAL / hour"]["value"] == "276.6 MB"


def test_findings_severity():
    fs = rv.findings(OBJ)
    titles = " | ".join(f["title"] for f in fs)
    assert "invalid index" in titles            # red
    assert "without a primary key" in titles    # amber
    assert "unused user index" in titles
    assert "idle-in-transaction" in titles
    assert any(f["sev"] == "red" for f in fs)


def test_findings_includes_obj_level_checks():
    """Test the expanded findings from obj fields beyond the basics."""
    obj = {
        **OBJ,
        "cn": {"f1": 10, "f2": 8},        # 80% new connections
        "clas": {"f1": 3, "f2": 2, "f3": "60000"},  # partitioned, unlogged, high OID
        "sumry": {"f1": 30, "f2": 500000.0, "f3": 290035704.0},  # slow collection
        "victims": [{"f1": 200, "f2": [100]}],
        "wmemuse": [{"f1": "big_table", "f2": 5000000000}],
        "tbsp": [{"tsname": "fast_disk", "location": "/mnt/ssd"}],
    }
    fs = rv.findings(obj)
    titles = " | ".join(f["title"] for f in fs)
    assert "connections are new" in titles
    assert "unlogged" in titles
    assert "blocked" in titles
    assert "system response" in titles.lower()
    assert "WAL generation rate" in titles
    assert "tablespace" in titles


def test_findings_param_warnings():
    """Test parameter-specific findings."""
    obj = {**OBJ, "clsr": False}
    params = {
        "autovacuum": "off",
        "huge_pages": "off",
        "shared_buffers": "4194304",  # > 2097152 → huge_pages essential
        "jit": "on",
        "track_io_timing": "off",
        "statement_timeout": "0",
        "work_mem": "131072",  # > 98304
    }
    fs = rv.findings(obj, params=params)
    titles = " | ".join(f["title"] for f in fs)
    assert "Autovacuum" in titles
    assert "huge_pages" in titles
    assert "JIT" in titles
    assert "work_mem" in titles


def test_findings_detail_section_checks():
    """Test findings generated from detail_json."""
    detail = {
        "connections_by_db": [
            {"datname": "app", "active": 5, "idle_in_txn": 0, "idle": 3,
             "total": 20, "ssl": 5, "non_ssl": 15},
        ],
        "roles": [
            {"rolname": "a", "rolsuper": True}, {"rolname": "b", "rolsuper": True},
            {"rolname": "c", "rolsuper": True},
        ],
        "hba": [
            {"typ": "host", "method": "md5", "err": None},
            {"typ": "host", "method": "trust", "err": "bad"},
        ],
        "statements": [
            {"query": "SELECT 1", "avg_time_ms": 70000},  # > 60s
        ],
        "bgwriter": {
            "forced_pct": 15, "avg_cp_interval_min": 3,
            "sync_time_s": 15, "buffers_backend": 50, "buffers_clean": 10,
        },
        "replication_slots": [
            {"slot_name": "dead_slot", "active": False},
        ],
    }
    fs = rv.findings(OBJ, detail=detail)
    titles = " | ".join(f["title"] for f in fs)
    assert "unencrypted" in titles
    assert "superuser" in titles
    assert "HBA rule" in titles.lower() or "erroneous" in titles.lower()
    assert "MD5" in titles
    assert "high-impact" in titles.lower()
    assert "Forced checkpoint" in titles
    assert "Abandoned replication slot" in titles


def test_findings_clean_when_healthy():
    clean = {"sess": {"f6": 1, "f1": 1, "f2": 0, "f3": 0, "f7": 5},
             "tabs": {"f1": 1, "f2": 0, "f3": 0, "f4": 0},
             "induse": {"f1": 0, "f2": 0, "f3": 0, "f4": 1, "f5": 0, "f6": 0},
             "nokey": {"f1": 0, "f2": 0}, "arcfail": {"f1": 10},
             "cn": {"f1": 5, "f2": 1},
             "clas": {"f1": 0, "f2": 0, "f3": "100"},
             "meta": {"f1": 1000, "f2": 5000, "f3": 10},
             "params": {"f6": 100},
             "clsr": False}
    fs = rv.findings(clean)
    # Should have no red/amber findings, only blue informational ones
    sevs = {f["sev"] for f in fs}
    assert "red" not in sevs
    assert "amber" not in sevs


def test_session_breakdown_filters_zero():
    segs = rv.session_breakdown(OBJ)
    kinds = {s["k"]: s["v"] for s in segs}
    assert kinds == {"active": 2, "idle in txn": 1, "idle": 4, "background": 5}  # parallel f5=0 dropped


def test_param_groups_categorize():
    groups = {g["name"]: g for g in rv.param_groups(
        {"shared_buffers": "16384", "work_mem": "8192", "wal_level": "replica",
         "autovacuum": "on", "some_unknown": "x"})}
    assert "Memory" in groups and "WAL & Checkpoints" in groups and "Other" in groups
    mem = {p["name"]: p for p in groups["Memory"]["params"]}
    assert mem["work_mem"]["overridden"] is True   # 8192 != default 4096
    assert mem["shared_buffers"]["overridden"] is False


def test_formatters():
    assert rv.fmt_bytes(1024) == "1.0 KB"
    assert rv.fmt_bytes(None) == "—"
    assert rv.fmt_duration(90) == "1m 30s"
    assert rv.fmt_duration(3700).startswith("1h")
    assert rv.fmt_number(12345) == "12.3K"
    assert rv.fmt_number(42) == "42"
    assert rv.fmt_number(None) == "—"
    assert rv.fmt_pct(99.5) == "99.5%"
    assert rv.fmt_pct(None) == "—"


# ===================================================================
# Detail section mapping tests
# ===================================================================

DETAIL = {
    "tables": [
        {"relname": "orders", "schema": "public", "relkind": "r",
         "n_live_tup": 50000, "n_dead_tup": 8000, "dead_ratio": 0.16,
         "bloat_pct": 45, "rel_size": 8388608, "tab_ind_size": 16777216,
         "rel_age": 600000000, "cache_hit_pct": 88.5,
         "last_vac": "2026-06-10T12:00:00", "last_anlyze": "2026-06-10T12:00:00",
         "vac_nos": 12, "n_tup_ins": 1000, "n_tup_upd": 500, "n_tup_del": 100,
         "idx_count": 3, "idx_unused": 1, "has_pk": 1, "lastuse": None},
        {"relname": "logs", "schema": "public", "relkind": "r",
         "n_live_tup": 100, "n_dead_tup": 0, "dead_ratio": 0.0,
         "bloat_pct": None, "rel_size": 8192, "tab_ind_size": 16384,
         "rel_age": 1000, "cache_hit_pct": 100.0,
         "last_vac": None, "last_anlyze": None,
         "vac_nos": 0, "n_tup_ins": 10, "n_tup_upd": 0, "n_tup_del": 0,
         "idx_count": 0, "idx_unused": 0, "has_pk": 0, "lastuse": None},
    ],
    "indexes": [
        {"index_name": "orders_pkey", "table_name": "orders", "schema": "public",
         "indisunique": True, "indisprimary": True, "indisvalid": True,
         "numscans": 50000, "size": 2097152, "cache_hit_pct": 99.2, "lastuse": None},
        {"index_name": "idx_orders_date", "table_name": "orders", "schema": "public",
         "indisunique": False, "indisprimary": False, "indisvalid": True,
         "numscans": 0, "size": 1048576, "cache_hit_pct": None, "lastuse": None},
        {"index_name": "idx_broken", "table_name": "orders", "schema": "public",
         "indisunique": False, "indisprimary": False, "indisvalid": False,
         "numscans": 0, "size": 524288, "cache_hit_pct": None, "lastuse": None},
    ],
    "sessions": [
        {"pid": 100, "state": "active", "backend_type": "client backend",
         "wait_event_type": None, "wait_event": None, "query": "SELECT 1",
         "client_addr": "10.0.0.1", "application_name": "psql",
         "backend_start": "2026-06-14T10:00:00", "xact_start": None,
         "xmin_age": None, "ssl": True, "leader_pid": None, "blocked_by": None},
        {"pid": 200, "state": "idle in transaction", "backend_type": "client backend",
         "wait_event_type": "Lock", "wait_event": "relation",
         "query": "UPDATE orders SET x=1", "client_addr": "10.0.0.2",
         "application_name": "app", "backend_start": "2026-06-14T09:00:00",
         "xact_start": "2026-06-14T09:30:00", "xmin_age": "5000",
         "ssl": False, "leader_pid": None, "blocked_by": [[100]]},
    ],
    "statements": [
        {"query": "SELECT * FROM orders WHERE id=$1", "calls": 10000,
         "total_time": 5000.0, "avg_time_ms": 0.5, "pct_db_time": 45.2,
         "cache_hit_pct": 99.1, "avg_reads": 0.1,
         "avg_temp_reads": 0, "avg_temp_writes": 0, "avg_dirtied": 0},
    ],
    "wait_events": [
        {"wait_event": "ClientRead", "cnt": 500},
        {"wait_event": "WALWrite", "cnt": 100},
    ],
    "databases": [
        {"datname": "mydb", "encod": "UTF8", "colat": "en_US.UTF-8",
         "db_size": 1073741824, "age": 200000, "mxidage": 100,
         "xact_commit": 500000, "xact_rollback": 50, "blks_fetch": 100000,
         "blks_hit": 99000, "tup_inserted": 1000, "tup_updated": 500,
         "tup_deleted": 100, "temp_files": 5, "temp_bytes": 10485760,
         "cache_hit_pct": 99.0, "commits_per_day": 25000, "stats_reset": None},
    ],
    "bgwriter": {
        "checkpoints_timed": 100, "checkpoints_req": 5,
        "forced_pct": 4.8, "write_time_s": 12.3, "sync_time_s": 0.5,
        "avg_cp_interval_min": 5.2, "mb_per_checkpoint": 128.0,
        "buffers_checkpoint": 16000, "buffers_clean": 500,
        "buffers_backend": 200, "buffers_alloc": 50000, "stats_reset": None,
    },
    "hba": [
        {"seq": 1, "typ": "local", "db": ["all"], "usr": ["all"],
         "addr": None, "mask": None, "method": "trust", "err": None},
        {"seq": 2, "typ": "host", "db": ["all"], "usr": ["all"],
         "addr": "0.0.0.0/0", "mask": None, "method": "scram-sha-256", "err": None},
    ],
    "extensions": [
        {"extname": "plpgsql", "extversion": "1.0", "schema": "pg_catalog",
         "owner": "postgres", "extrelocatable": False},
        {"extname": "citus", "extversion": "12.0", "schema": "public",
         "owner": "postgres", "extrelocatable": True},
    ],
    "roles": [
        {"rolname": "postgres", "rolsuper": True, "rolreplication": True,
         "rolconnlimit": -1, "auth_method": "SCRAM",
         "active": 1, "idle_in_txn": 0, "idle": 2, "total_conns": 3},
        {"rolname": "app_user", "rolsuper": False, "rolreplication": False,
         "rolconnlimit": 50, "auth_method": "MD5",
         "active": 0, "idle_in_txn": 1, "idle": 0, "total_conns": 1},
    ],
}


def test_detail_tables():
    tabs = rv.detail_tables(DETAIL)
    assert len(tabs) == 2
    orders = tabs[0]
    assert orders["name"] == "orders"
    assert orders["bloat_warn"] is True   # 45% > 20 and tab_ind_size > 5MB
    assert orders["dead_warn"] is True    # 0.16 > 0.1
    assert orders["cache_warn"] is True   # 88.5 < 90
    assert orders["age_warn"] is True     # 600M > 500M
    assert orders["has_pk"] is True
    assert orders["idx_unused"] == 1
    logs = tabs[1]
    assert logs["bloat_warn"] is False
    assert logs["has_pk"] is False


def test_detail_indexes():
    idxs = rv.detail_indexes(DETAIL)
    assert len(idxs) == 3
    pkey = next(i for i in idxs if i["name"] == "orders_pkey")
    assert pkey["primary"] is True
    assert pkey["unused"] is False  # primary keys are never flagged unused
    unused = next(i for i in idxs if i["name"] == "idx_orders_date")
    assert unused["unused"] is True
    broken = next(i for i in idxs if i["name"] == "idx_broken")
    assert broken["invalid"] is True


def test_detail_sessions():
    sess = rv.detail_sessions(DETAIL)
    assert len(sess) == 2
    blocked = next(s for s in sess if s["pid"] == 200)
    assert blocked["is_blocked"] is True
    assert blocked["state"] == "idle in transaction"
    active = next(s for s in sess if s["pid"] == 100)
    assert active["is_blocked"] is False
    assert active["ssl"] is True


def test_detail_statements():
    stmts = rv.detail_statements(DETAIL)
    assert len(stmts) == 1
    assert stmts[0]["pct_db_time"] == "45.2%"
    assert stmts[0]["cache_warn"] is False  # 99.1 >= 90


def test_detail_wait_events():
    we = rv.detail_wait_events(DETAIL)
    assert len(we["events"]) == 2
    cr = next(w for w in we["events"] if w["event"] == "ClientRead")
    assert cr["pct"] == round(500 * 100.0 / 600, 1)
    assert we["total"] == 600
    assert len(we["categories"]) >= 1


def test_detail_databases():
    dbs = rv.detail_databases(DETAIL)
    assert len(dbs) == 1
    assert dbs[0]["name"] == "mydb"
    assert dbs[0]["size"] == "1.0 GB"
    assert dbs[0]["cache_warn"] is False  # 99.0 >= 95


def test_detail_bgwriter():
    bg = rv.detail_bgwriter(DETAIL)
    assert bg is not None
    assert bg["forced_warn"] is False  # 4.8 <= 10
    assert bg["cp_timed"] == 100


def test_detail_hba():
    hba = rv.detail_hba(DETAIL)
    assert len(hba) == 2
    assert hba[0]["method_warn"] is True    # trust
    assert hba[1]["method_warn"] is False   # scram-sha-256 (not trust/password/md5)


def test_detail_extensions():
    exts = rv.detail_extensions(DETAIL)
    citus = next(e for e in exts if e["name"] == "citus")
    assert citus["risky"] is True
    plpgsql = next(e for e in exts if e["name"] == "plpgsql")
    assert plpgsql["risky"] is False


def test_detail_roles():
    roles = rv.detail_roles(DETAIL)
    app = next(r for r in roles if r["name"] == "app_user")
    assert app["auth_warn"] is True  # MD5
    assert app["superuser"] is False
    pg = next(r for r in roles if r["name"] == "postgres")
    assert pg["superuser"] is True
    assert pg["auth_warn"] is False


def test_build_detail_view():
    view = rv.build_detail_view(DETAIL)
    assert view["has_data"] is True
    assert len(view["tables"]) == 2
    assert len(view["indexes"]) == 3
    assert view["bgwriter"] is not None
