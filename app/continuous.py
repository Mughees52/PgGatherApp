"""Continuous collection: lightweight periodic sampling via template1.

Runs gather.sql against template1 (triggers lightweight/partial mode),
parses the dynamic metrics (sessions, wait events, connections, WAL,
replication), and stores them as time-series in SQLite history tables.

This runs as a separate thread alongside the main job worker.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from . import repository as repo
from .crypto import decrypt
from .pipeline import docker_runner as dr
from .pipeline import extract

log = logging.getLogger("pggather.continuous")
_thread: Optional[threading.Thread] = None
_started = False


def start_scheduler() -> None:
    global _thread, _started
    if _started:
        return
    _started = True
    _thread = threading.Thread(target=_scheduler_loop, name="pggather-continuous",
                               daemon=True)
    _thread.start()
    log.info("Continuous collection scheduler started")


def _scheduler_loop() -> None:
    """Check for due schedules every 30 seconds."""
    while True:
        try:
            _check_schedules()
        except Exception:
            log.exception("Error in continuous collection scheduler")
        time.sleep(30)


def _check_schedules() -> None:
    schedules = repo.list_enabled_schedules()
    now = time.time()
    for s in schedules:
        last_run = s["last_run_at"]
        interval = s["interval_sec"] or 60
        if last_run:
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                elapsed = now - dt.timestamp()
            except (ValueError, TypeError):
                elapsed = interval + 1
        else:
            elapsed = interval + 1

        if elapsed >= interval:
            try:
                _run_lightweight_collection(s)
                repo.update_schedule_last_run(s["id"])
            except Exception:
                log.exception(f"Continuous collection failed for {s['name']}")


def _run_lightweight_collection(schedule) -> None:
    """Run gather.sql against template1 and parse dynamic metrics."""
    connection_id = schedule["connection_id"]
    conn = repo.get_connection(connection_id)
    if conn is None:
        return

    password = decrypt(conn["password_enc"]) if conn["password_enc"] else None

    # Connect to template1 for lightweight mode
    target = dr.TargetConn(
        host=conn["host"], port=conn["port"],
        dbname="template1",  # triggers lightweight/partial mode
        username=conn["username"], sslmode=conn["sslmode"],
    )

    # Quick connection test first
    ok, _ = dr.test_connection(target, password)
    if not ok:
        log.warning(f"Cannot reach {conn['host']}:{conn['port']} for continuous collection")
        return

    # Run gather.sql and capture output
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile(suffix=".tsv", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        dr.collect(target, password, tmp_path, lambda m: None)

        # Parse TSV metadata for server identity
        meta = extract.parse_tsv_metadata(tmp_path)
        server_key = meta.system_id or f"hostport:{conn['host']}:{conn['port']}"
        collected_at = meta.collected_at or repo._now()

        # Parse the dynamic sections from the TSV using the engine container
        # For lightweight mode, we extract directly from the TSV content
        _parse_and_store_lightweight(tmp_path, server_key, collected_at)

        log.info(f"Continuous collection: {conn['name']} -> {server_key} at {collected_at}")
    finally:
        tmp_path.unlink(missing_ok=True)


def _parse_and_store_lightweight(tsv_path, server_key: str, collected_at: str) -> None:
    """Parse lightweight TSV and store metrics in history tables.

    The lightweight TSV from template1 contains: pg_gather, pg_get_activity,
    pg_pid_wait, pg_get_db, pg_replication_stat, pg_get_slots, pg_get_wal,
    pg_get_io, pg_gather_end. We parse sessions and wait events directly
    from the TSV without importing into PostgreSQL.
    """
    active = idle_txn = idle = total = workers = 0
    wait_events: dict = {}
    total_conns = ssl_conns = non_ssl_conns = 0
    current_wal = ""

    in_activity = False
    in_pid_wait = False
    in_gather = False
    activity_cols = None
    gather_cols = None

    with open(tsv_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")

            if line.startswith("COPY pg_get_activity"):
                in_activity = True
                # Parse column names
                if "(" in line:
                    cols = line.split("(")[1].rstrip(") FROM stdin;").split(",")
                    activity_cols = [c.strip() for c in cols]
                continue
            if line.startswith("COPY pg_pid_wait"):
                in_pid_wait = True
                continue
            if line.startswith("COPY pg_gather ") or line.startswith("COPY pg_gather("):
                in_gather = True
                if "(" in line:
                    cols = line.split("(")[1].rstrip(") FROM stdin;").split(",")
                    gather_cols = [c.strip() for c in cols]
                continue

            if line.startswith("\\."):
                in_activity = in_pid_wait = in_gather = False
                continue

            if in_activity and activity_cols:
                vals = line.split("\t")
                row = dict(zip(activity_cols, vals))
                state = row.get("state", "").strip()
                if state == "active":
                    active += 1
                elif state == "idle in transaction":
                    idle_txn += 1
                elif state == "idle":
                    idle += 1
                bt = row.get("backend_type", "").strip()
                if state:
                    total += 1
                if row.get("leader_pid", "\\N").strip() not in ("", "\\N"):
                    workers += 1
                ssl_val = row.get("ssl", "").strip()
                if ssl_val == "t":
                    ssl_conns += 1
                elif ssl_val == "f":
                    non_ssl_conns += 1
                total_conns += 1

            elif in_pid_wait:
                parts = line.split("\t")
                we = parts[-1].strip() if len(parts) >= 2 else ""
                if we and we != "\\N":
                    wait_events[we] = wait_events.get(we, 0) + 1

            elif in_gather and gather_cols:
                vals = line.split("\t")
                row = dict(zip(gather_cols, vals))
                current_wal = row.get("current_wal", "").strip()

    # Store in history tables
    repo.insert_history_sessions(
        collected_at, server_key, active, idle_txn, idle, total, workers)

    if wait_events:
        events = [{"wait_event": k, "cnt": v} for k, v in wait_events.items()]
        repo.insert_history_wait_events(collected_at, server_key, events)

    repo.insert_history_connections(
        collected_at, server_key, total_conns, ssl_conns, non_ssl_conns)

    if current_wal:
        repo.insert_history_wal(collected_at, server_key, current_wal, None)
