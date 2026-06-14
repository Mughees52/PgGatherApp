"""Timeline view: time-series charts from continuous collection history."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import repository as repo
from ..pipeline.health import docker_health
from ..templating import templates

router = APIRouter(prefix="/timeline")


@router.get("", response_class=HTMLResponse)
def timeline_picker(request: Request):
    """List servers that have history data."""
    from ..db import get_conn
    with get_conn() as conn:
        servers = conn.execute(
            """SELECT server_key, count(*) as samples,
                      min(collected_at) as first, max(collected_at) as last
               FROM history_sessions
               GROUP BY server_key ORDER BY last DESC"""
        ).fetchall()
    # Enrich with server names from reports
    enriched = []
    for s in servers:
        report = None
        with get_conn() as conn:
            report = conn.execute(
                "SELECT srvr_host, srvr_db, pg_version_num FROM reports WHERE server_key = ? LIMIT 1",
                (s["server_key"],),
            ).fetchone()
        enriched.append({
            "server_key": s["server_key"],
            "samples": s["samples"],
            "first": s["first"],
            "last": s["last"],
            "host": report["srvr_host"] if report else s["server_key"],
            "db": report["srvr_db"] if report else "",
            "pg": report["pg_version_num"] if report else None,
        })
    return templates.TemplateResponse(request, "timeline.html", { "servers": enriched,
        "nav": "timeline", "docker": docker_health(),
    })


@router.get("/{server_key}", response_class=HTMLResponse)
def timeline_view(server_key: str, request: Request, hours: int = 24):
    """Show timeline charts for a specific server."""
    return templates.TemplateResponse(request, "timeline_detail.html", { "server_key": server_key, "hours": hours,
        "nav": "timeline", "docker": docker_health(),
    })


@router.get("/{server_key}/data")
def timeline_data(server_key: str, hours: int = 24) -> JSONResponse:
    """JSON API for chart data."""
    sessions = [dict(r) for r in repo.get_history_sessions(server_key, hours)]
    wait_events = [dict(r) for r in repo.get_history_wait_events(server_key, hours)]
    connections = [dict(r) for r in repo.get_history_connections(server_key, hours)]

    # Aggregate wait events by timestamp
    wait_by_ts: dict = {}
    for w in wait_events:
        ts = w["collected_at"]
        if ts not in wait_by_ts:
            wait_by_ts[ts] = {}
        wait_by_ts[ts][w["wait_event"]] = w["count"]

    return JSONResponse({
        "sessions": sessions,
        "wait_events": wait_by_ts,
        "connections": connections,
    })
