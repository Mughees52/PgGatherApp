"""Single-threaded background worker that runs collect → generate jobs.

A dedicated daemon thread drains a FIFO queue, so the shared Docker engine
container is never touched by two jobs at once. Status transitions and logs are
persisted to the database and per-report ``job.log`` files.
"""

from __future__ import annotations

import gzip
import json
import queue
import threading
import traceback
from pathlib import Path
from typing import Optional

from . import repository as repo
from . import storage
from .crypto import decrypt
from .pipeline import docker_runner as dr
from .pipeline import extract

_queue: "queue.Queue[str]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_started = False
_current_password: Optional[str] = None  # set during collect, used to scrub tracebacks


def start_worker() -> None:
    global _worker, _started
    if _started:
        return
    _started = True
    queued_ids = repo.reset_stuck_jobs()
    _worker = threading.Thread(target=_run_loop, name="pggather-worker", daemon=True)
    _worker.start()
    for rid in queued_ids:
        _queue.put(rid)


def enqueue(report_id: str) -> None:
    _queue.put(report_id)


def _run_loop() -> None:
    global _current_password
    while True:
        report_id = _queue.get()
        try:
            _process(report_id)
        except Exception:  # never let the worker die
            _fail(report_id, traceback.format_exc())
        finally:
            _current_password = None
            _queue.task_done()


def _log(report_id: str, msg: str) -> None:
    storage.append_log(report_id, msg)


def _scrub_password(text: str) -> str:
    if _current_password and _current_password in text:
        text = text.replace(_current_password, "***")
    return text


def _fail(report_id: str, detail: str) -> None:
    detail = _scrub_password(detail)
    storage.append_log(report_id, "ERROR: " + detail)
    short = detail.strip().splitlines()[-1] if detail.strip() else "failed"
    repo.set_status(report_id, "failed", error=_scrub_password(short[:500]))


def _read_tsv_bytes(path: Path) -> bytes:
    with path.open("rb") as fh:
        magic = fh.read(2)
    if magic == b"\x1f\x8b":
        with gzip.open(path, "rb") as gz:
            return gz.read()
    return path.read_bytes()


def _server_key(system_id: Optional[str], host: Optional[str],
                port: Optional[str], report_id: str) -> str:
    if system_id:
        return system_id
    if host:
        return f"hostport:{host}:{port or ''}"
    return f"orphan:{report_id}"


def _process(report_id: str) -> None:
    report = repo.get_report(report_id)
    if report is None:
        return

    if report["source"] == "collected":
        repo.set_status(report_id, "collecting")
        tsv_path = _do_collect(report_id, report["connection_id"])
    else:
        tsv_path = Path(report["stored_tsv_path"])

    # Extract identity from the TSV header (works even if generation fails).
    meta = extract.parse_tsv_metadata(tsv_path)
    server_key = _server_key(meta.system_id, meta.srvr_host, meta.srvr_port, report_id)
    repo.update_report(report_id, {
        "stored_tsv_path": str(tsv_path),
        "tsv_size_bytes": tsv_path.stat().st_size,
        "collected_at": meta.collected_at,
        "pg_version": meta.pg_version,
        "pg_version_num": meta.pg_version_num,
        "engine_ver": meta.engine_ver,
        "system_id": meta.system_id,
        "srvr_host": meta.srvr_host,
        "srvr_port": meta.srvr_port,
        "srvr_db": meta.srvr_db,
        "server_key": server_key,
    })

    repo.set_status(report_id, "generating")
    html_path = storage.html_path(report_id)
    tsv_bytes = _read_tsv_bytes(tsv_path)
    dr.generate(tsv_bytes, html_path, lambda m: _log(report_id, m))

    # Capture the config (still loaded) and the report's embedded JSON for compare.
    # Also extract detailed per-section data while the engine DB has the data.
    params_json = dr.dump_settings()
    detail_json = dr.extract_detail(lambda m: _log(report_id, m))
    rjson = extract.parse_report_html(html_path.read_text("utf-8", "replace"))
    repo.update_report(report_id, {
        "stored_html_path": str(html_path),
        "html_size_bytes": html_path.stat().st_size,
        "report_json": json.dumps(rjson.obj) if rjson.obj is not None else None,
        "meta_json": json.dumps(rjson.meta) if rjson.meta is not None else None,
        "params_json": params_json,
        "detail_json": detail_json,
    })
    repo.set_status(report_id, "done")
    _log(report_id, "Done.")


def _do_collect(report_id: str, connection_id: Optional[str]) -> Path:
    global _current_password
    conn = repo.get_connection(connection_id) if connection_id else None
    if conn is None:
        raise RuntimeError("connection profile not found for collection")
    password = decrypt(conn["password_enc"]) if conn["password_enc"] else None
    _current_password = password
    target = dr.TargetConn(
        host=conn["host"], port=conn["port"], dbname=conn["dbname"],
        username=conn["username"], sslmode=conn["sslmode"],
    )
    out_path = storage.tsv_path(report_id, gzip=False)
    dr.collect(target, password, out_path, lambda m: _log(report_id, m))
    repo.touch_connection(connection_id)
    return out_path
