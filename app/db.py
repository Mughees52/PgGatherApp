"""SQLite access layer: connection helper and schema initialization.

Uses the stdlib ``sqlite3`` driver (no ORM) with ``Row`` factory so rows behave
like dicts. WAL mode + foreign keys are enabled on every connection.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    host         TEXT NOT NULL,
    port         INTEGER NOT NULL DEFAULT 5432,
    dbname       TEXT NOT NULL,
    username     TEXT NOT NULL,
    password_enc TEXT,
    sslmode      TEXT NOT NULL DEFAULT 'prefer',
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id                TEXT PRIMARY KEY,
    source            TEXT NOT NULL,              -- 'collected' | 'uploaded'
    connection_id     TEXT REFERENCES connections(id) ON DELETE SET NULL,
    original_filename TEXT,
    stored_tsv_path   TEXT,
    stored_html_path  TEXT,
    status            TEXT NOT NULL,              -- queued|collecting|generating|done|failed
    error             TEXT,
    tsv_size_bytes    INTEGER,
    html_size_bytes   INTEGER,
    input_was_gzip    INTEGER NOT NULL DEFAULT 0,
    engine_ver        TEXT,
    pg_version        TEXT,
    pg_version_num    INTEGER,
    collected_at      TEXT,
    created_at        TEXT NOT NULL,
    system_id         TEXT,
    srvr_host         TEXT,
    srvr_port         TEXT,
    srvr_db           TEXT,
    server_key        TEXT,
    notes             TEXT,
    report_json       TEXT,
    meta_json         TEXT,
    params_json       TEXT,
    detail_json       TEXT
);

CREATE INDEX IF NOT EXISTS idx_reports_server_key ON reports(server_key);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports(created_at);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS report_tags (
    report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (report_id, tag_id)
);

-- Continuous collection: schedule + time-series history
CREATE TABLE IF NOT EXISTS collection_schedules (
    id            TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    interval_sec  INTEGER NOT NULL DEFAULT 60,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_run_at   TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE(connection_id)
);

CREATE TABLE IF NOT EXISTS history_sessions (
    collected_at TEXT NOT NULL,
    server_key   TEXT NOT NULL,
    active       INTEGER, idle_in_txn INTEGER, idle INTEGER,
    total        INTEGER, workers     INTEGER,
    PRIMARY KEY (collected_at, server_key)
);

CREATE TABLE IF NOT EXISTS history_wait_events (
    collected_at TEXT NOT NULL,
    server_key   TEXT NOT NULL,
    wait_event   TEXT NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (collected_at, server_key, wait_event)
);

CREATE TABLE IF NOT EXISTS history_connections (
    collected_at TEXT NOT NULL,
    server_key   TEXT NOT NULL,
    total        INTEGER, ssl INTEGER, non_ssl INTEGER,
    PRIMARY KEY (collected_at, server_key)
);

CREATE TABLE IF NOT EXISTS history_wal (
    collected_at TEXT NOT NULL,
    server_key   TEXT NOT NULL,
    current_wal  TEXT,
    wal_bytes    REAL,
    PRIMARY KEY (collected_at, server_key)
);

CREATE TABLE IF NOT EXISTS history_replication (
    collected_at TEXT NOT NULL,
    server_key   TEXT NOT NULL,
    client_addr  TEXT,
    sent_lag     REAL, write_lag REAL, flush_lag REAL, replay_lag REAL,
    PRIMARY KEY (collected_at, server_key, client_addr)
);

CREATE INDEX IF NOT EXISTS idx_hist_sess_sk ON history_sessions(server_key, collected_at);
CREATE INDEX IF NOT EXISTS idx_hist_wait_sk ON history_wait_events(server_key, collected_at);
CREATE INDEX IF NOT EXISTS idx_hist_conn_sk ON history_connections(server_key, collected_at);
CREATE INDEX IF NOT EXISTS idx_hist_wal_sk ON history_wal(server_key, collected_at);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings.db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success and always closes."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    settings.ensure_dirs()
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migration: add detail_json if upgrading from an older schema.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reports)").fetchall()}
        if "detail_json" not in cols:
            conn.execute("ALTER TABLE reports ADD COLUMN detail_json TEXT")
