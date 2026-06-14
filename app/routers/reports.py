"""Report library, upload, detail, downloads, tags/notes, delete."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .. import jobs
from .. import param_recommend
from .. import report_view
from .. import repository as repo
from .. import storage
from ..pipeline.health import docker_health
from ..templating import templates

router = APIRouter(prefix="/reports")


@router.get("", response_class=HTMLResponse)
def library(request: Request, q: Optional[str] = None, tag: Optional[str] = None,
            status: Optional[str] = None, server_key: Optional[str] = None):
    reports = repo.list_reports(search=q, tag=tag, status=status, server_key=server_key)
    stats = repo.library_stats()
    tags_by_report = {r["id"]: repo.get_report_tags(r["id"]) for r in reports}
    return templates.TemplateResponse(request, "library.html", {
        "reports": reports,
        "tags": repo.list_all_tags(),
        "tags_by_report": tags_by_report,
        "stats": stats,
        "q": q or "", "active_tag": tag, "active_status": status,
        "docker": docker_health(), "nav": "library",
    })


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse(request, "upload.html", { "docker": docker_health(), "nav": "upload",
    })


@router.post("")
async def upload(file: UploadFile = File(...)):
    filename = file.filename or "upload.tsv"
    is_gzip = filename.endswith(".gz")
    rid = repo.create_report(source="uploaded", status="queued",
                             original_filename=filename, input_was_gzip=is_gzip)
    dest = storage.tsv_path(rid, gzip=is_gzip)
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    # If the name lied about gzip, detect by magic and rename.
    with dest.open("rb") as fh:
        magic = fh.read(2)
    if magic == b"\x1f\x8b" and not is_gzip:
        new_dest = storage.tsv_path(rid, gzip=True)
        dest.rename(new_dest)
        dest = new_dest
        repo.update_report(rid, {"input_was_gzip": 1})
    repo.update_report(rid, {"stored_tsv_path": str(dest)})
    jobs.enqueue(rid)
    return RedirectResponse(url=f"/reports/{rid}", status_code=303)


@router.get("/{rid}", response_class=HTMLResponse)
def detail(rid: str, request: Request):
    report = repo.get_report(rid)
    if report is None:
        return RedirectResponse(url="/reports", status_code=303)
    log_file = storage.log_path(rid)
    log_text = log_file.read_text("utf-8", "replace") if log_file.exists() else ""
    comparable = [r for r in repo.list_reports(server_key=report["server_key"])
                  if r["id"] != rid and r["status"] == "done"] if report["server_key"] else []

    # Build the native dashboard view model from the stored JSON, if available.
    view = None
    obj = _load_json(report["report_json"])
    params = _load_json(report["params_json"])
    detail = _load_json(report["detail_json"])
    if obj:
        view = {
            "metrics": report_view.metric_cards(obj),
            "findings": report_view.findings(
                obj, detail=detail, params=params,
                meta=_load_json(report["meta_json"]),
                engine_ver=report["engine_ver"]),
            "sessions": report_view.session_breakdown(obj),
            "db": report_view.database_overview(obj),
            "param_groups": report_view.param_groups(params or {}),
            "param_count": len(params or {}),
            "meta": _load_json(report["meta_json"]) or {},
        }
        if detail:
            view["detail"] = report_view.build_detail_view(detail, obj=obj)

    return templates.TemplateResponse(request, "detail.html", {
        "report": report,
        "tags": repo.get_report_tags(rid),
        "log": log_text,
        "comparable": comparable,
        "view": view,
        "docker": docker_health(), "nav": "library",
    })


def _load_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


@router.get("/{rid}/status")
def status(rid: str) -> JSONResponse:
    report = repo.get_report(rid)
    if report is None:
        return JSONResponse({"status": "missing"}, status_code=404)
    return JSONResponse({"status": report["status"], "error": report["error"]})


@router.post("/{rid}/recommend")
def recommend(rid: str, cpus: int = Form(4), memory_gb: int = Form(8),
              storage: str = Form("ssd"), workload: str = Form("oltp"),
              filesystem: str = Form("rglr")) -> JSONResponse:
    report = repo.get_report(rid)
    if report is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    params = _load_json(report["params_json"]) or {}
    obj = _load_json(report["report_json"]) or {}
    # Get WAL rate for max_wal_size recommendation
    sumry = obj.get("sumry", {}) if isinstance(obj, dict) else {}
    wal_rate = max(float(sumry.get("f2", 0) or 0), float(sumry.get("f3", 0) or 0))
    recs = param_recommend.compute_recommendations(
        params, cpus=cpus, memory_gb=memory_gb, storage=storage,
        workload=workload, filesystem=filesystem, wal_rate_bytes=wal_rate,
    )
    return JSONResponse({"recommendations": recs})


@router.get("/{rid}/download/tsv")
def download_tsv(rid: str):
    report = repo.get_report(rid)
    if report is None or not report["stored_tsv_path"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = Path(report["stored_tsv_path"])
    return FileResponse(str(path), filename=path.name, media_type="application/octet-stream")


@router.get("/{rid}/download/html")
def download_html(rid: str):
    report = repo.get_report(rid)
    if report is None or not report["stored_html_path"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(report["stored_html_path"], filename=f"GatherReport-{rid}.html",
                        media_type="text/html")


@router.post("/{rid}/tags")
def set_tags(rid: str, tags: str = Form("")):
    names = [t for t in tags.replace(",", " ").split()]
    repo.set_report_tags(rid, names)
    return RedirectResponse(url=f"/reports/{rid}", status_code=303)


@router.post("/{rid}/notes")
def set_notes(rid: str, notes: str = Form("")):
    repo.update_notes(rid, notes)
    return RedirectResponse(url=f"/reports/{rid}", status_code=303)


@router.post("/{rid}/retry")
def retry(rid: str):
    report = repo.get_report(rid)
    if report and report["status"] == "failed":
        repo.set_status(rid, "queued", error=None)
        jobs.enqueue(rid)
    return RedirectResponse(url=f"/reports/{rid}", status_code=303)


@router.post("/{rid}/delete")
def delete(rid: str):
    repo.delete_report(rid)
    storage.delete_report_dir(rid)
    return RedirectResponse(url="/reports", status_code=303)
