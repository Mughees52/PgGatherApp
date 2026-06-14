"""Shared Jinja2 templates instance and template helpers/filters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi.templating import Jinja2Templates

from .config import PROJECT_ROOT, engine_version
from .report_view import fmt_bytes, fmt_duration, fmt_number, fmt_pct

templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))


def _status_badge(status: str) -> str:
    return {
        "queued": "badge-queued",
        "collecting": "badge-running",
        "generating": "badge-running",
        "done": "badge-done",
        "failed": "badge-failed",
    }.get(status, "badge-queued")


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text[:19])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _relative_time(value: str) -> str:
    dt = _parse_ts(value)
    if dt is None:
        return "—"
    now = datetime.now(timezone.utc)
    secs = (now - dt).total_seconds()
    future = secs < 0
    secs = abs(int(secs))
    if secs < 45:
        return "just now"
    units = [(86400 * 365, "year"), (86400 * 30, "month"), (86400, "day"),
             (3600, "hour"), (60, "minute")]
    for size, name in units:
        if secs >= size:
            n = secs // size
            label = f"{n} {name}{'s' if n != 1 else ''}"
            return f"in {label}" if future else f"{label} ago"
    return "just now"


def _pg_badge(version_num) -> str:
    try:
        return f"PG {int(version_num)}"
    except (TypeError, ValueError):
        return "PG ?"


templates.env.globals["engine_ver"] = engine_version()

templates.env.filters["status_badge"] = _status_badge
templates.env.filters["format_bytes"] = fmt_bytes
templates.env.filters["format_duration"] = fmt_duration
templates.env.filters["format_number"] = fmt_number
templates.env.filters["format_pct"] = fmt_pct
templates.env.filters["relative_time"] = _relative_time
templates.env.filters["pg_version_badge"] = _pg_badge
