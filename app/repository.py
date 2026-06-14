"""Data-access functions over the SQLite database (no ORM)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .db import get_conn

_CONN_COLS = frozenset({
    "name", "host", "port", "dbname", "username", "password_enc",
    "sslmode", "last_used_at",
})
_REPORT_COLS = frozenset({
    "source", "connection_id", "original_filename", "stored_tsv_path",
    "stored_html_path", "status", "error", "tsv_size_bytes", "html_size_bytes",
    "input_was_gzip", "engine_ver", "pg_version", "pg_version_num",
    "collected_at", "system_id", "srvr_host", "srvr_port", "srvr_db",
    "server_key", "notes", "report_json", "meta_json", "params_json",
    "detail_json",
})


def _safe_cols(fields: Dict[str, Any], allowed: frozenset) -> Dict[str, Any]:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Disallowed column(s): {bad}")
    return fields


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


# --- Connections ------------------------------------------------------------

def create_connection(*, name: str, host: str, port: int, dbname: str,
                      username: str, password_enc: Optional[str],
                      sslmode: str = "prefer") -> str:
    cid = _new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO connections
               (id, name, host, port, dbname, username, password_enc, sslmode, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (cid, name, host, port, dbname, username, password_enc, sslmode, _now()),
        )
    return cid


def update_connection(cid: str, *, fields: Dict[str, Any]) -> None:
    if not fields:
        return
    fields = _safe_cols(fields, _CONN_COLS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE connections SET {cols} WHERE id = ?",
                     (*fields.values(), cid))


def touch_connection(cid: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE connections SET last_used_at = ? WHERE id = ?",
                     (_now(), cid))


def get_connection(cid: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM connections WHERE id = ?", (cid,)).fetchone()


def list_connections() -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM connections ORDER BY name").fetchall()


def delete_connection(cid: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM connections WHERE id = ?", (cid,))


# --- Reports ----------------------------------------------------------------

def create_report(*, source: str, status: str,
                  connection_id: Optional[str] = None,
                  original_filename: Optional[str] = None,
                  input_was_gzip: bool = False) -> str:
    rid = _new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO reports
               (id, source, connection_id, original_filename, status,
                input_was_gzip, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (rid, source, connection_id, original_filename, status,
             1 if input_was_gzip else 0, _now()),
        )
    return rid


def update_report(rid: str, fields: Dict[str, Any]) -> None:
    if not fields:
        return
    fields = _safe_cols(fields, _REPORT_COLS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE reports SET {cols} WHERE id = ?",
                     (*fields.values(), rid))


def set_status(rid: str, status: str, error: Optional[str] = None) -> None:
    update_report(rid, {"status": status, "error": error})


def get_report(rid: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()


def list_reports(*, search: Optional[str] = None, tag: Optional[str] = None,
                 status: Optional[str] = None,
                 server_key: Optional[str] = None) -> List[sqlite3.Row]:
    clauses: List[str] = []
    params: List[Any] = []
    sql = "SELECT DISTINCT r.* FROM reports r"
    if tag:
        sql += (" JOIN report_tags rt ON rt.report_id = r.id"
                " JOIN tags t ON t.id = rt.tag_id")
        clauses.append("t.name = ?")
        params.append(tag.strip().lower())
    if status:
        clauses.append("r.status = ?")
        params.append(status)
    if server_key:
        clauses.append("r.server_key = ?")
        params.append(server_key)
    if search:
        like = f"%{search.strip()}%"
        clauses.append("(r.original_filename LIKE ? OR r.pg_version LIKE ?"
                       " OR r.notes LIKE ? OR r.srvr_host LIKE ? OR r.system_id LIKE ?)")
        params.extend([like, like, like, like, like])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY r.created_at DESC"
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def delete_report(rid: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM reports WHERE id = ?", (rid,))


def reset_stuck_jobs() -> List[str]:
    """On startup, re-queue reports that were queued, fail those mid-flight."""
    with get_conn() as conn:
        # Jobs that were mid-flight (actively running) can't be safely resumed.
        conn.execute(
            """UPDATE reports SET status = 'failed',
               error = 'interrupted by app restart'
               WHERE status IN ('collecting', 'generating')""")
        # Jobs that were merely queued can be re-enqueued safely.
        rows = conn.execute(
            "SELECT id FROM reports WHERE status = 'queued'").fetchall()
        return [r[0] for r in rows]


# --- Tags & notes -----------------------------------------------------------

def set_report_tags(rid: str, names: List[str]) -> None:
    cleaned = sorted({n.strip().lower() for n in names if n.strip()})
    with get_conn() as conn:
        conn.execute("DELETE FROM report_tags WHERE report_id = ?", (rid,))
        for name in cleaned:
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO report_tags(report_id, tag_id) VALUES (?,?)",
                         (rid, tag_id))


def get_report_tags(rid: str) -> List[str]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT t.name FROM tags t
               JOIN report_tags rt ON rt.tag_id = t.id
               WHERE rt.report_id = ? ORDER BY t.name""", (rid,)).fetchall()
    return [r[0] for r in rows]


def list_all_tags() -> List[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM tags ORDER BY name").fetchall()
    return [r[0] for r in rows]


def update_notes(rid: str, notes: str) -> None:
    update_report(rid, {"notes": notes})


# --- Collection schedules ---------------------------------------------------

def create_schedule(connection_id: str, interval_sec: int = 60) -> str:
    sid = _new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO collection_schedules
               (id, connection_id, interval_sec, enabled, created_at)
               VALUES (?, ?, ?, 1, ?)""",
            (sid, connection_id, interval_sec, _now()),
        )
    return sid


def get_schedule_for_connection(connection_id: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM collection_schedules WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()


def list_enabled_schedules() -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT s.*, c.* FROM collection_schedules s "
            "JOIN connections c ON c.id = s.connection_id "
            "WHERE s.enabled = 1"
        ).fetchall()


def update_schedule_last_run(schedule_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE collection_schedules SET last_run_at = ? WHERE id = ?",
            (_now(), schedule_id),
        )


def delete_schedule(connection_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM collection_schedules WHERE connection_id = ?",
            (connection_id,),
        )


def toggle_schedule(connection_id: str, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE collection_schedules SET enabled = ? WHERE connection_id = ?",
            (1 if enabled else 0, connection_id),
        )


# --- History data -----------------------------------------------------------

def insert_history_sessions(collected_at: str, server_key: str,
                            active: int, idle_in_txn: int, idle: int,
                            total: int, workers: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO history_sessions
               (collected_at, server_key, active, idle_in_txn, idle, total, workers)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (collected_at, server_key, active, idle_in_txn, idle, total, workers),
        )


def insert_history_wait_events(collected_at: str, server_key: str,
                               events: List[Dict[str, Any]]) -> None:
    with get_conn() as conn:
        for e in events:
            conn.execute(
                """INSERT OR REPLACE INTO history_wait_events
                   (collected_at, server_key, wait_event, count)
                   VALUES (?, ?, ?, ?)""",
                (collected_at, server_key, e.get("wait_event", "?"), e.get("cnt", 0)),
            )


def insert_history_connections(collected_at: str, server_key: str,
                               total: int, ssl: int, non_ssl: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO history_connections
               (collected_at, server_key, total, ssl, non_ssl)
               VALUES (?, ?, ?, ?, ?)""",
            (collected_at, server_key, total, ssl, non_ssl),
        )


def insert_history_wal(collected_at: str, server_key: str,
                       current_wal: str, wal_bytes: Optional[float]) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO history_wal
               (collected_at, server_key, current_wal, wal_bytes)
               VALUES (?, ?, ?, ?)""",
            (collected_at, server_key, current_wal, wal_bytes),
        )


def get_history_sessions(server_key: str, hours: int = 24) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM history_sessions
               WHERE server_key = ? AND collected_at > datetime('now', ?)
               ORDER BY collected_at""",
            (server_key, f"-{hours} hours"),
        ).fetchall()


def get_history_wait_events(server_key: str, hours: int = 24) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT collected_at, wait_event, count FROM history_wait_events
               WHERE server_key = ? AND collected_at > datetime('now', ?)
               ORDER BY collected_at, count DESC""",
            (server_key, f"-{hours} hours"),
        ).fetchall()


def get_history_connections(server_key: str, hours: int = 24) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM history_connections
               WHERE server_key = ? AND collected_at > datetime('now', ?)
               ORDER BY collected_at""",
            (server_key, f"-{hours} hours"),
        ).fetchall()


def library_stats() -> Dict[str, Any]:
    """Lightweight aggregate stats for the library page header."""
    with get_conn() as conn:
        total = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
        servers = conn.execute(
            "SELECT count(DISTINCT server_key) FROM reports WHERE server_key IS NOT NULL"
        ).fetchone()[0]
        latest = conn.execute(
            "SELECT max(coalesce(collected_at, created_at)) FROM reports WHERE status = 'done'"
        ).fetchone()[0]
    return {"total": total, "servers": servers, "latest": latest}
