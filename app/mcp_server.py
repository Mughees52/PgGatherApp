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
