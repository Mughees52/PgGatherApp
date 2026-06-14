"""Compare two reports from the same server (config-drift diff)."""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import repository as repo
from ..pipeline.health import docker_health
from ..templating import templates

router = APIRouter(prefix="/compare")


@router.get("", response_class=HTMLResponse)
def picker(request: Request):
    done = [r for r in repo.list_reports(status="done") if r["server_key"]]
    groups: Dict[str, List] = {}
    for r in done:
        groups.setdefault(r["server_key"], []).append(r)
    # Only servers with at least two reports can be compared.
    comparable = {k: v for k, v in groups.items() if len(v) >= 2}
    # Servers with only one report — show them so the user knows why they're missing.
    singles = [v[0] for k, v in groups.items() if len(v) == 1]
    return templates.TemplateResponse(request, "compare_picker.html", { "groups": comparable, "singles": singles,
        "nav": "compare", "docker": docker_health(),
    })


@router.get("/{id_a}/{id_b}", response_class=HTMLResponse)
def compare(id_a: str, id_b: str, request: Request):
    a = repo.get_report(id_a)
    b = repo.get_report(id_b)
    if a is None or b is None:
        return HTMLResponse("One or both reports not found.", status_code=404)
    if a["server_key"] != b["server_key"]:
        return HTMLResponse(
            "These reports are from different servers and cannot be compared.",
            status_code=400)

    # Order chronologically (older = left).
    if (a["collected_at"] or "") > (b["collected_at"] or ""):
        a, b = b, a

    rows = _diff_params(_load(a["params_json"]), _load(b["params_json"]))
    metrics = _diff_metrics(_load(a["report_json"]), _load(b["report_json"]))
    return templates.TemplateResponse(request, "compare.html", { "a": a, "b": b, "rows": rows, "metrics": metrics,
        "changed": sum(1 for r in rows if r["state"] == "changed"),
        "added": sum(1 for r in rows if r["state"] == "added"),
        "removed": sum(1 for r in rows if r["state"] == "removed"),
        "nav": "compare", "docker": docker_health(),
    })


def _load(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, default)
        else:
            return default
    return d


def _diff_metrics(a: dict, b: dict) -> List[dict]:
    """Diff high-signal structured data from report_json obj."""
    metrics = []

    def _add(label: str, va, vb, fmt=str):
        if va is None and vb is None:
            return
        sa, sb = fmt(va) if va is not None else "—", fmt(vb) if vb is not None else "—"
        state = "same" if sa == sb else "changed"
        metrics.append({"label": label, "a": sa, "b": sb, "state": state})

    def _fnum(v):
        try:
            n = float(v)
            if n >= 1_000_000:
                return f"{n/1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n/1_000:.1f}K"
            return f"{int(n):,}" if n == int(n) else f"{n:,.1f}"
        except (TypeError, ValueError):
            return str(v) if v is not None else "—"

    def _fbytes(v):
        try:
            n = float(v)
            for u in ("B", "KB", "MB", "GB", "TB"):
                if abs(n) < 1024:
                    return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
                n /= 1024
            return f"{n:.1f} PB"
        except (TypeError, ValueError):
            return "—"

    # Sessions
    _add("Total sessions", _get(a, "sess", "f6"), _get(b, "sess", "f6"), _fnum)
    _add("Active sessions", _get(a, "sess", "f1"), _get(b, "sess", "f1"), _fnum)
    _add("Idle in transaction", _get(a, "sess", "f2"), _get(b, "sess", "f2"), _fnum)

    # Tables
    _add("Total tables", _get(a, "tabs", "f1"), _get(b, "tabs", "f1"), _fnum)
    _add("Never vacuumed", _get(a, "tabs", "f2"), _get(b, "tabs", "f2"), _fnum)
    _add("Never analyzed", _get(a, "tabs", "f4"), _get(b, "tabs", "f4"), _fnum)

    # Indexes
    _add("Total indexes", _get(a, "induse", "f4"), _get(b, "induse", "f4"), _fnum)
    _add("Unused indexes", _get(a, "induse", "f2"), _get(b, "induse", "f2"), _fnum)
    _add("Invalid indexes", _get(a, "induse", "f1"), _get(b, "induse", "f1"), _fnum)
    _add("Wasted index space", _get(a, "induse", "f6"), _get(b, "induse", "f6"), _fbytes)

    # Connections
    _add("Connections in use", _get(a, "cn", "f1"), _get(b, "cn", "f1"), _fnum)
    _add("New connections", _get(a, "cn", "f2"), _get(b, "cn", "f2"), _fnum)

    # Size
    _add("Total object size", _get(a, "meta", "f2"), _get(b, "meta", "f2"), _fbytes)
    _add("Catalog size", _get(a, "meta", "f1"), _get(b, "meta", "f1"), _fbytes)
    _add("Relations count", _get(a, "meta", "f3"), _get(b, "meta", "f3"), _fnum)

    # WAL
    _add("WAL rate (long-term)", _get(a, "sumry", "f3"), _get(b, "sumry", "f3"), _fbytes)

    # Tables without PK
    _add("Tables without PK", _get(a, "nokey", "f1"), _get(b, "nokey", "f1"), _fnum)

    return [m for m in metrics if m["state"] != "same" or True]  # include all for context


def _diff_params(a: Dict[str, str], b: Dict[str, str]) -> List[dict]:
    rows = []
    for name in sorted(set(a) | set(b)):
        va, vb = a.get(name), b.get(name)
        if va == vb:
            state = "same"
        elif va is None:
            state = "added"
        elif vb is None:
            state = "removed"
        else:
            state = "changed"
        rows.append({"name": name, "a": va, "b": vb, "state": state})
    return rows
