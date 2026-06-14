"""Serve stored report HTML inside a security sandbox.

The report contains inline JavaScript. Serving it with a ``sandbox`` CSP and
embedding via an ``allow-scripts`` (but not ``allow-same-origin``) iframe lets
the report run interactively in an opaque origin without access to the app's
cookies, storage, or DOM.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from .. import repository as repo

router = APIRouter(prefix="/reports")

_SECURITY_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts;",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
}


@router.get("/{rid}/view")
def view(rid: str):
    report = repo.get_report(rid)
    if report is None or not report["stored_html_path"]:
        return JSONResponse({"error": "report not ready"}, status_code=404)
    path = Path(report["stored_html_path"])
    if not path.exists():
        return JSONResponse({"error": "file missing"}, status_code=404)
    html = path.read_bytes()
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers=_SECURITY_HEADERS)
