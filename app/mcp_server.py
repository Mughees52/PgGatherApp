"""MCP server exposing pg_gather report data to AI clients.

Provides resources (read-only report data) and tools (actions) so AI
assistants like Claude Desktop or Cursor can analyze PostgreSQL diagnostics,
cross-reference with live database data from postgres-mcp, and give
contextual tuning recommendations.

Mount at /mcp in the FastAPI app via streamable_http_app().
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import param_recommend
from . import report_view
from . import repository as repo

mcp = FastMCP(
    "PgGatherApp",
    instructions=(
        "You are a PostgreSQL diagnostics assistant. You have access to "
        "pg_gather report data — snapshots of PostgreSQL server health "
        "including sessions, tables, indexes, parameters, wait events, "
        "replication, checkpoints, HBA rules, and findings. "
        "Use this data to explain issues, recommend tuning, and compare "
        "snapshots over time. When the user asks about a server, find the "
        "relevant report first with list_reports, then drill into specifics."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(raw: Any) -> dict | list | None:
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None


def _report_or_error(report_id: str):
    r = repo.get_report(report_id)
    if r is None:
        return None, f"Report {report_id} not found"
    return r, None


# ---------------------------------------------------------------------------
# Resources — read-only data the AI can browse
# ---------------------------------------------------------------------------

@mcp.resource("pggather://servers")
def resource_servers() -> str:
    """List all PostgreSQL servers with their latest report status."""
    from .db import get_conn
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT server_key, srvr_host, srvr_db, pg_version_num,
                   max(collected_at) as latest, count(*) as report_count
            FROM reports WHERE status = 'done' AND server_key IS NOT NULL
            GROUP BY server_key, srvr_host, srvr_db, pg_version_num
            ORDER BY latest DESC
        """).fetchall()
    servers = [
        {"server_key": r["server_key"], "host": r["srvr_host"],
         "database": r["srvr_db"], "pg_version": r["pg_version_num"],
         "latest_collection": r["latest"], "report_count": r["report_count"]}
        for r in rows
    ]
    return json.dumps(servers, indent=2)


@mcp.resource("pggather://report/{report_id}/summary")
def resource_summary(report_id: str) -> str:
    """Get a structured overview of a pg_gather report including key metrics."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    obj = _load_json(r["report_json"]) or {}
    summary = {
        "id": r["id"], "status": r["status"], "source": r["source"],
        "server": {"host": r["srvr_host"], "port": r["srvr_port"],
                    "database": r["srvr_db"], "system_id": r["system_id"]},
        "pg_version": r["pg_version"], "engine_ver": r["engine_ver"],
        "collected_at": r["collected_at"],
        "metrics": {c["label"]: {"value": c["value"], "unit": c["unit"],
                                  "tone": c["tone"]}
                     for c in report_view.metric_cards(obj)},
    }
    return json.dumps(summary, indent=2)


@mcp.resource("pggather://report/{report_id}/findings")
def resource_findings(report_id: str) -> str:
    """Get all diagnostic findings for a report with severity levels."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    obj = _load_json(r["report_json"]) or {}
    detail = _load_json(r["detail_json"]) or {}
    params = _load_json(r["params_json"]) or {}
    meta = _load_json(r["meta_json"])
    fs = report_view.findings(obj, detail=detail, params=params,
                               meta=meta, engine_ver=r["engine_ver"])
    return json.dumps(fs, indent=2)


@mcp.resource("pggather://report/{report_id}/params")
def resource_params(report_id: str) -> str:
    """Get all PostgreSQL configuration parameters (name -> value)."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    return r["params_json"] or "{}"


@mcp.resource("pggather://report/{report_id}/tables")
def resource_tables(report_id: str) -> str:
    """Get table health data: bloat, dead tuples, cache hit, vacuum status."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    detail = _load_json(r["detail_json"]) or {}
    obj = _load_json(r["report_json"]) or {}
    days = max(float(report_view._int(
        report_view._f(report_view._f(obj, "dbts", {}), "f4", 1))), 1.0)
    tables = report_view.detail_tables(detail, days=days)
    return json.dumps(tables, indent=2)


@mcp.resource("pggather://report/{report_id}/indexes")
def resource_indexes(report_id: str) -> str:
    """Get index health data: scans, size, unused, invalid status."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    detail = _load_json(r["detail_json"]) or {}
    return json.dumps(report_view.detail_indexes(detail), indent=2)


@mcp.resource("pggather://report/{report_id}/sessions")
def resource_sessions(report_id: str) -> str:
    """Get session details: state, queries, wait events, blocking info."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    detail = _load_json(r["detail_json"]) or {}
    return json.dumps(report_view.detail_sessions(detail), indent=2)


@mcp.resource("pggather://report/{report_id}/statements")
def resource_statements(report_id: str) -> str:
    """Get top SQL statements by database time, with cache hit and I/O stats."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    detail = _load_json(r["detail_json"]) or {}
    return json.dumps(report_view.detail_statements(detail), indent=2)


@mcp.resource("pggather://report/{report_id}/bgwriter")
def resource_bgwriter(report_id: str) -> str:
    """Get checkpoint and background writer statistics."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    detail = _load_json(r["detail_json"]) or {}
    return json.dumps(report_view.detail_bgwriter(detail), indent=2)


@mcp.resource("pggather://report/{report_id}/recommendations")
def resource_recommendations(report_id: str) -> str:
    """Get parameter tuning recommendations (default: 4 CPU, 8GB RAM, SSD, OLTP)."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    params = _load_json(r["params_json"]) or {}
    obj = _load_json(r["report_json"]) or {}
    sumry = report_view._f(obj, "sumry", {})
    wal_rate = max(float(report_view._f(sumry, "f2", 0) or 0),
                   float(report_view._f(sumry, "f3", 0) or 0))
    recs = param_recommend.compute_recommendations(
        params, cpus=4, memory_gb=8, storage="ssd",
        workload="oltp", wal_rate_bytes=wal_rate)
    return json.dumps(recs, indent=2)


@mcp.resource("pggather://timeline/{server_key}")
def resource_timeline(server_key: str) -> str:
    """Get time-series monitoring data (last 24h) from continuous collection."""
    sessions = [dict(r) for r in repo.get_history_sessions(server_key, 24)]
    connections = [dict(r) for r in repo.get_history_connections(server_key, 24)]
    return json.dumps({"sessions": sessions, "connections": connections}, indent=2)


# ---------------------------------------------------------------------------
# Tools — actions the AI can invoke
# ---------------------------------------------------------------------------

@mcp.tool()
def list_reports(server_key: str = "", status: str = "done") -> str:
    """List available pg_gather reports. Filter by server_key or status."""
    reports = repo.list_reports(
        server_key=server_key or None,
        status=status or None,
    )
    result = [
        {"id": r["id"], "host": r["srvr_host"], "database": r["srvr_db"],
         "pg_version": r["pg_version_num"], "collected_at": r["collected_at"],
         "source": r["source"], "status": r["status"],
         "server_key": r["server_key"]}
        for r in reports
    ]
    return json.dumps(result, indent=2)


@mcp.tool()
def get_report_summary(report_id: str) -> str:
    """Get a complete diagnostic summary of a report including all findings."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    obj = _load_json(r["report_json"]) or {}
    detail = _load_json(r["detail_json"]) or {}
    params = _load_json(r["params_json"]) or {}
    meta = _load_json(r["meta_json"])

    fs = report_view.findings(obj, detail=detail, params=params,
                               meta=meta, engine_ver=r["engine_ver"])
    metrics = {c["label"]: c["value"] for c in report_view.metric_cards(obj)}
    db = report_view.database_overview(obj)

    return json.dumps({
        "server": {"host": r["srvr_host"], "port": r["srvr_port"],
                    "database": r["srvr_db"], "system_id": r["system_id"]},
        "pg_version": r["pg_version"], "collected_at": r["collected_at"],
        "metrics": metrics,
        "database_overview": db,
        "findings_count": {"red": sum(1 for f in fs if f["sev"] == "red"),
                           "amber": sum(1 for f in fs if f["sev"] == "amber"),
                           "blue": sum(1 for f in fs if f["sev"] == "blue")},
        "findings": fs,
    }, indent=2)


@mcp.tool()
def get_report_detail(report_id: str, section: str = "all") -> str:
    """Get detailed section data from a report.

    Args:
        report_id: The report ID
        section: Which section to return. Options:
            'tables' - Table health (bloat, dead tuples, cache hit, DML rates, recommendations)
            'indexes' - Index health (scans, size, unused, invalid)
            'sessions' - Session details (PID, state, query text, wait events, blockers)
            'statements' - Top SQL statements (query text, execution time, calls, cache hit, I/O)
            'wait_events' - Wait event breakdown with categories
            'databases' - Per-database stats (size, cache hit, age, DML rates)
            'bgwriter' - Checkpoint and BGWriter statistics
            'replication' - Replication lag and slot status
            'hba' - HBA rules with method warnings and shadow detection
            'extensions' - Installed extensions
            'roles' - User/role details with connection counts
            'connections_by_db' - Connections per database (active, idle, SSL)
            'io_stats' - IO statistics per backend type
            'head_info' - Server info (version, uptime, WAL position)
            'all' - Returns all sections (large response)
    """
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    detail = _load_json(r["detail_json"])
    if not detail:
        return json.dumps({"error": "No detail data available for this report. "
                           "The report may need to be regenerated."})
    obj = _load_json(r["report_json"]) or {}
    days = max(float(report_view._int(
        report_view._f(report_view._f(obj, "dbts", {}), "f4", 1))), 1.0)

    if section == "all":
        view = report_view.build_detail_view(detail, obj=obj)
        return json.dumps(view, indent=2, default=str)

    section_map = {
        "tables": lambda: report_view.detail_tables(detail, days=days),
        "indexes": lambda: report_view.detail_indexes(detail),
        "sessions": lambda: report_view.detail_sessions(detail),
        "statements": lambda: report_view.detail_statements(detail),
        "wait_events": lambda: report_view.detail_wait_events(detail),
        "databases": lambda: report_view.detail_databases(detail, days=days),
        "bgwriter": lambda: report_view.detail_bgwriter(detail),
        "replication": lambda: report_view.detail_replication(detail),
        "hba": lambda: report_view.detail_hba(detail),
        "extensions": lambda: report_view.detail_extensions(detail),
        "roles": lambda: report_view.detail_roles(detail),
        "connections_by_db": lambda: report_view.detail_connections_by_db(detail),
        "io_stats": lambda: report_view.detail_io_stats(detail),
        "head_info": lambda: report_view.detail_head_info(detail),
    }

    if section not in section_map:
        return json.dumps({"error": f"Unknown section '{section}'. "
                           f"Available: {', '.join(section_map.keys())}"})

    data = section_map[section]()
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def get_findings(report_id: str) -> str:
    """Get diagnostic findings for a report, grouped by severity."""
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    obj = _load_json(r["report_json"]) or {}
    detail = _load_json(r["detail_json"]) or {}
    params = _load_json(r["params_json"]) or {}
    meta = _load_json(r["meta_json"])
    fs = report_view.findings(obj, detail=detail, params=params,
                               meta=meta, engine_ver=r["engine_ver"])
    grouped = {"critical": [f for f in fs if f["sev"] == "red"],
               "warning": [f for f in fs if f["sev"] == "amber"],
               "info": [f for f in fs if f["sev"] == "blue"],
               "ok": [f for f in fs if f["sev"] == "ok"]}
    return json.dumps(grouped, indent=2)


@mcp.tool()
def get_parameter_recommendations(
    report_id: str,
    cpus: int = 4,
    memory_gb: int = 8,
    storage: str = "ssd",
    workload: str = "oltp",
    filesystem: str = "rglr",
) -> str:
    """Get PostgreSQL parameter tuning recommendations based on hardware specs.

    Args:
        report_id: The report ID to analyze
        cpus: Number of CPU cores on the server
        memory_gb: Total RAM in GB
        storage: Storage type - 'ssd', 'san', or 'mag' (magnetic)
        workload: Workload type - 'oltp', 'olap', or 'mixed'
        filesystem: Filesystem type - 'rglr' (ext4/xfs) or 'cow' (zfs/btrfs)
    """
    r, err = _report_or_error(report_id)
    if err:
        return json.dumps({"error": err})
    params = _load_json(r["params_json"]) or {}
    obj = _load_json(r["report_json"]) or {}
    sumry = report_view._f(obj, "sumry", {})
    wal_rate = max(float(report_view._f(sumry, "f2", 0) or 0),
                   float(report_view._f(sumry, "f3", 0) or 0))
    recs = param_recommend.compute_recommendations(
        params, cpus=cpus, memory_gb=memory_gb, storage=storage,
        workload=workload, filesystem=filesystem, wal_rate_bytes=wal_rate)
    return json.dumps(recs, indent=2)


@mcp.tool()
def compare_reports(report_id_a: str, report_id_b: str) -> str:
    """Compare two reports from the same server showing what changed.

    Returns metric diffs (sessions, indexes, tables, WAL rate) and
    parameter changes between two snapshots.
    """
    a = repo.get_report(report_id_a)
    b = repo.get_report(report_id_b)
    if a is None or b is None:
        return json.dumps({"error": "One or both reports not found"})
    if a["server_key"] != b["server_key"]:
        return json.dumps({"error": "Reports are from different servers"})

    # Order chronologically
    if (a["collected_at"] or "") > (b["collected_at"] or ""):
        a, b = b, a

    # Params diff
    pa = _load_json(a["params_json"]) or {}
    pb = _load_json(b["params_json"]) or {}
    changed_params = []
    for name in sorted(set(pa) | set(pb)):
        va, vb = pa.get(name), pb.get(name)
        if va != vb:
            changed_params.append({"param": name, "before": va, "after": vb})

    # Metrics diff
    oa = _load_json(a["report_json"]) or {}
    ob = _load_json(b["report_json"]) or {}

    def _get(d, *keys):
        for k in keys:
            d = d.get(k, {}) if isinstance(d, dict) else {}
        return d if d != {} else None

    metrics_diff = {}
    comparisons = [
        ("total_sessions", ("sess", "f6")),
        ("active_sessions", ("sess", "f1")),
        ("idle_in_txn", ("sess", "f2")),
        ("total_tables", ("tabs", "f1")),
        ("unused_indexes", ("induse", "f2")),
        ("invalid_indexes", ("induse", "f1")),
    ]
    for label, keys in comparisons:
        va = _get(oa, *keys)
        vb = _get(ob, *keys)
        if va is not None or vb is not None:
            metrics_diff[label] = {"before": va, "after": vb}

    return json.dumps({
        "older": {"id": a["id"], "collected_at": a["collected_at"]},
        "newer": {"id": b["id"], "collected_at": b["collected_at"]},
        "changed_parameters": len(changed_params),
        "parameter_changes": changed_params,
        "metric_changes": metrics_diff,
    }, indent=2)


# ---------------------------------------------------------------------------
# Granular tools — expose underlying data behind findings counts
# ---------------------------------------------------------------------------

def _get_detail(report_id: str):
    """Helper: load report + detail_json, return (detail_dict, report_row, error_str)."""
    r, err = _report_or_error(report_id)
    if err:
        return None, None, err
    detail = _load_json(r["detail_json"])
    if not detail:
        return None, r, ("No detail data for this report. It may predate the detail "
                         "extraction feature — re-upload the TSV to regenerate.")
    return detail, r, None


@mcp.tool()
def get_top_statements(
    report_id: str,
    order_by: str = "total_time",
    limit: int = 10,
) -> str:
    """Get top SQL statements with full per-query metrics.

    Returns query text, calls, execution times, shared/temp block I/O,
    and database time percentage. Block read/write times may be null
    if track_io_timing was OFF during capture.

    Args:
        report_id: The report ID
        order_by: Sort order — 'total_time' (default), 'avg_time', 'calls', 'reads'
        limit: Max results (default 10, max 50)
    """
    detail, r, err = _get_detail(report_id)
    if err:
        return json.dumps({"error": err})

    stmts = detail.get("statements") or []
    if not stmts:
        return json.dumps({"note": "No pg_stat_statements data in this capture. "
                           "Ensure pg_stat_statements extension is enabled.",
                           "statements": []})

    sort_keys = {
        "total_time": lambda s: float(s.get("total_time") or 0),
        "avg_time": lambda s: float(s.get("avg_time_ms") or 0),
        "calls": lambda s: int(s.get("calls") or 0),
        "reads": lambda s: int(s.get("shared_blks_read") or 0),
    }
    key_fn = sort_keys.get(order_by, sort_keys["total_time"])
    stmts.sort(key=key_fn, reverse=True)
    limit = min(max(limit, 1), 50)

    results = []
    for s in stmts[:limit]:
        calls = int(s.get("calls") or 0)
        results.append({
            "query": s.get("query"),
            "calls": calls,
            "total_exec_time_ms": s.get("total_time"),
            "mean_exec_time_ms": s.get("avg_time_ms"),
            "pct_of_total_db_time": s.get("pct_db_time"),
            "shared_blks_hit": s.get("shared_blks_hit"),
            "shared_blks_read": s.get("shared_blks_read"),
            "shared_blks_dirtied": s.get("shared_blks_dirtied"),
            "shared_blks_written": s.get("shared_blks_written"),
            "temp_blks_read": s.get("temp_blks_read"),
            "temp_blks_written": s.get("temp_blks_written"),
            "cache_hit_pct": s.get("cache_hit_pct"),
            "avg_reads_per_call": s.get("avg_reads"),
            "avg_dirtied_per_call": s.get("avg_dirtied"),
            "avg_temp_reads_per_call": s.get("avg_temp_reads"),
            "avg_temp_writes_per_call": s.get("avg_temp_writes"),
            "note": "blk_read_time/blk_write_time unavailable (track_io_timing was OFF)"
                    if s.get("shared_blks_read") and not s.get("blk_read_time") else None,
        })
    return json.dumps({"count": len(results), "order_by": order_by,
                        "statements": results}, indent=2)


@mcp.tool()
def get_wraparound(report_id: str) -> str:
    """Get transaction ID wraparound risk per database and per table.

    Shows age(relfrozenxid) and multixact age with autovacuum_freeze_max_age
    headroom. Sorted by closest to the freeze threshold.
    """
    detail, r, err = _get_detail(report_id)
    if err:
        return json.dumps({"error": err})

    params = _load_json(r["params_json"]) or {}
    freeze_max = int(params.get("autovacuum_freeze_max_age", 200000000))

    # Per-database
    dbs = detail.get("databases") or []
    db_risk = []
    for d in dbs:
        age = d.get("age")
        mxid = d.get("mxidage")
        if age is not None:
            headroom = freeze_max - int(age)
            db_risk.append({
                "database": d.get("datname"),
                "xid_age": int(age),
                "multixact_age": int(mxid) if mxid else None,
                "freeze_max_age": freeze_max,
                "headroom": headroom,
                "pct_consumed": round(int(age) * 100.0 / freeze_max, 1),
                "urgent": headroom < 0,
            })
    db_risk.sort(key=lambda x: x["headroom"])

    # Per-table
    tables = detail.get("tables") or []
    table_risk = []
    for t in tables:
        age = t.get("rel_age")
        if age is not None and int(age) > freeze_max * 0.5:
            headroom = freeze_max - int(age)
            table_risk.append({
                "table": t.get("relname"),
                "schema": t.get("schema"),
                "xid_age": int(age),
                "freeze_max_age": freeze_max,
                "headroom": headroom,
                "pct_consumed": round(int(age) * 100.0 / freeze_max, 1),
                "last_vacuum": t.get("last_vac"),
                "urgent": headroom < 0,
            })
    table_risk.sort(key=lambda x: x["headroom"])

    return json.dumps({
        "autovacuum_freeze_max_age": freeze_max,
        "databases": db_risk,
        "tables_above_50pct": table_risk[:50],
        "note": "Tables shown have age > 50% of autovacuum_freeze_max_age",
    }, indent=2)


@mcp.tool()
def get_objects(
    report_id: str,
    kind: str = "bloated_tables",
    limit: int = 50,
) -> str:
    """Get filtered lists of database objects by problem type.

    Args:
        report_id: The report ID
        kind: Object filter. Options:
            'bloated_tables' — tables with bloat > 20%, sorted by wasted space
            'unused_indexes' — indexes with zero scans (excluding PKs), sorted by size
            'invalid_indexes' — broken indexes needing REINDEX
            'vacuum_needed' — tables sorted by dead tuple count
            'never_vacuumed' — tables that have never been vacuumed
            'never_analyzed' — tables that have never been analyzed
            'no_primary_key' — tables without a primary key
            'largest_tables' — tables sorted by total size
        limit: Max results (default 50)
    """
    detail, r, err = _get_detail(report_id)
    if err:
        return json.dumps({"error": err})

    limit = min(max(limit, 1), 500)
    tables = detail.get("tables") or []
    indexes = detail.get("indexes") or []

    if kind == "bloated_tables":
        items = [{"table": t["relname"], "schema": t.get("schema"),
                  "bloat_pct": t.get("bloat_pct"),
                  "wasted_bytes": int((t.get("bloat_pct") or 0) * (t.get("rel_size") or 0) / 100),
                  "rel_size": t.get("rel_size"), "tab_ind_size": t.get("tab_ind_size"),
                  "n_live_tup": t.get("n_live_tup"), "n_dead_tup": t.get("n_dead_tup")}
                 for t in tables if (t.get("bloat_pct") or 0) > 20
                 and (t.get("tab_ind_size") or 0) > 5242880]
        items.sort(key=lambda x: x["wasted_bytes"], reverse=True)

    elif kind == "unused_indexes":
        items = [{"index": i["index_name"], "table": i["table_name"],
                  "schema": i.get("schema"), "size": i.get("size"),
                  "scans": i.get("numscans"), "is_unique": i.get("indisunique"),
                  "last_used": i.get("lastuse")}
                 for i in indexes
                 if (i.get("numscans") or 0) == 0
                 and not i.get("indisprimary")
                 and i.get("indisvalid", True)]
        items.sort(key=lambda x: x["size"] or 0, reverse=True)

    elif kind == "invalid_indexes":
        items = [{"index": i["index_name"], "table": i["table_name"],
                  "schema": i.get("schema"), "size": i.get("size")}
                 for i in indexes if not i.get("indisvalid", True)]

    elif kind == "vacuum_needed":
        items = [{"table": t["relname"], "schema": t.get("schema"),
                  "n_dead_tup": t.get("n_dead_tup"), "n_live_tup": t.get("n_live_tup"),
                  "dead_ratio": t.get("dead_ratio"),
                  "last_vacuum": t.get("last_vac"), "last_autovacuum": t.get("last_vac"),
                  "vacuum_count": t.get("vac_nos")}
                 for t in tables if (t.get("n_dead_tup") or 0) > 0]
        items.sort(key=lambda x: x["n_dead_tup"] or 0, reverse=True)

    elif kind == "never_vacuumed":
        items = [{"table": t["relname"], "schema": t.get("schema"),
                  "n_dead_tup": t.get("n_dead_tup"), "n_live_tup": t.get("n_live_tup"),
                  "rel_size": t.get("rel_size")}
                 for t in tables if not t.get("last_vac")]

    elif kind == "never_analyzed":
        items = [{"table": t["relname"], "schema": t.get("schema"),
                  "n_live_tup": t.get("n_live_tup"), "rel_size": t.get("rel_size")}
                 for t in tables if not t.get("last_anlyze")]

    elif kind == "no_primary_key":
        items = [{"table": t["relname"], "schema": t.get("schema"),
                  "n_live_tup": t.get("n_live_tup"),
                  "tab_ind_size": t.get("tab_ind_size"),
                  "has_unique_key": (t.get("has_uk") or 0) > 0}
                 for t in tables if (t.get("has_pk") or 0) == 0]

    elif kind == "largest_tables":
        items = [{"table": t["relname"], "schema": t.get("schema"),
                  "tab_ind_size": t.get("tab_ind_size"),
                  "rel_size": t.get("rel_size"), "tot_tab_size": t.get("tot_tab_size"),
                  "n_live_tup": t.get("n_live_tup"), "bloat_pct": t.get("bloat_pct"),
                  "idx_count": t.get("idx_count")}
                 for t in tables]
        items.sort(key=lambda x: x["tab_ind_size"] or 0, reverse=True)

    else:
        return json.dumps({"error": f"Unknown kind '{kind}'. Options: bloated_tables, "
                           "unused_indexes, invalid_indexes, vacuum_needed, never_vacuumed, "
                           "never_analyzed, no_primary_key, largest_tables"})

    return json.dumps({"kind": kind, "count": len(items[:limit]),
                        "total_matching": len(items),
                        "objects": items[:limit]}, indent=2)


@mcp.tool()
def get_settings(report_id: str, category: str = "") -> str:
    """Get PostgreSQL configuration parameters with source and unit info.

    Returns the full pg_settings dump including where each setting comes from
    (default, config file, command line, etc.) and any file-level overrides.

    Args:
        report_id: The report ID
        category: Optional filter — prefix match on parameter name
                  (e.g., 'wal', 'autovacuum', 'shared', 'log')
    """
    detail, r, err = _get_detail(report_id)
    if err:
        return json.dumps({"error": err})

    params = detail.get("params_detail") or []
    if not params:
        # Fall back to params_json (name->setting only)
        flat = _load_json(r["params_json"]) or {}
        params = [{"name": k, "setting": v, "unit": None, "source": "unknown",
                   "file_confs": None} for k, v in sorted(flat.items())]

    if category:
        cat = category.lower()
        params = [p for p in params if p["name"].startswith(cat)]

    results = []
    for p in params:
        entry = {
            "name": p["name"],
            "setting": p["setting"],
            "unit": p.get("unit"),
            "source": p.get("source"),
        }
        fc = p.get("file_confs")
        if fc:
            entry["file_overrides"] = fc
        results.append(entry)

    return json.dumps({"count": len(results), "settings": results}, indent=2)


@mcp.tool()
def get_activity(report_id: str) -> str:
    """Get session activity snapshot: states, wait events, queries, and blocking chains.

    Returns the captured pg_stat_activity data including longest-running queries,
    idle-in-transaction sessions, and blocker/victim relationships.
    """
    detail, r, err = _get_detail(report_id)
    if err:
        return json.dumps({"error": err})

    sessions = detail.get("sessions") or []
    if not sessions:
        return json.dumps({"note": "No session data in this capture.",
                           "sessions": [], "summary": {}})

    # Summary
    states = {}
    blockers = set()
    blocked = []
    longest_active = None
    for s in sessions:
        st = s.get("state") or "unknown"
        states[st] = states.get(st, 0) + 1
        if s.get("blocked_by"):
            blocked.append({"pid": s["pid"], "blocked_by": s["blocked_by"],
                            "query": s.get("query", "")[:200],
                            "wait": f"{s.get('wait_event_type', '')}: {s.get('wait_event', '')}"})
        if st == "active" and s.get("query"):
            if longest_active is None or (s.get("query_start") or "") < (longest_active.get("query_start") or "z"):
                longest_active = s

    # Wait event summary
    wait_summary = {}
    for s in sessions:
        we = s.get("wait_event")
        if we:
            key = f"{s.get('wait_event_type', '?')}: {we}"
            wait_summary[key] = wait_summary.get(key, 0) + 1

    # Session list (with full query text)
    session_list = []
    for s in sessions:
        session_list.append({
            "pid": s.get("pid"),
            "state": s.get("state"),
            "backend_type": s.get("backend_type"),
            "wait_event_type": s.get("wait_event_type"),
            "wait_event": s.get("wait_event"),
            "query": s.get("query"),
            "client_addr": s.get("client_addr"),
            "application_name": s.get("application_name"),
            "backend_start": s.get("backend_start"),
            "xact_start": s.get("xact_start"),
            "query_start": s.get("query_start"),
            "state_change": s.get("state_change"),
            "xmin_age": s.get("xmin_age"),
            "ssl": s.get("ssl"),
            "blocked_by": s.get("blocked_by"),
        })

    return json.dumps({
        "summary": {
            "total_sessions": len(sessions),
            "by_state": states,
            "wait_events": wait_summary,
            "blocking_chains": blocked,
        },
        "longest_active_query": {
            "pid": longest_active["pid"],
            "query": longest_active.get("query"),
            "since": longest_active.get("query_start"),
        } if longest_active else None,
        "sessions": session_list,
    }, indent=2)
