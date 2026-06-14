"""Docker orchestration: one long-lived ``postgres:17`` container serves both as
the report-generation server and as the psql client used for collection.

All access is serialized by the single background worker (see ``app/jobs.py``),
so reusing one named container across jobs is safe.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from ..config import settings

LogFn = Callable[[str], None]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


class DockerError(RuntimeError):
    pass


@dataclass
class TargetConn:
    host: str
    port: int
    dbname: str
    username: str
    sslmode: str = "prefer"

    def container_host(self) -> str:
        """Rewrite host-local addresses so the container can reach the host."""
        return "host.docker.internal" if self.host in LOCAL_HOSTS else self.host

    def conninfo(self) -> str:
        return (
            f"host={self.container_host()} port={self.port} "
            f"dbname={self.dbname} user={self.username} sslmode={self.sslmode}"
        )


def _run(cmd: List[str], *, input_bytes: Optional[bytes] = None,
         stdout_path: Optional[Path] = None, env_extra: Optional[dict] = None,
         timeout: Optional[int] = None) -> Tuple[int, str]:
    """Run a command, optionally streaming stdin and capturing stdout to a file.

    Returns (returncode, stderr_text). When ``stdout_path`` is given, stdout is
    written there; otherwise stdout is merged into the returned text after stderr.
    """
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    stdout_target = open(stdout_path, "wb") if stdout_path else subprocess.PIPE
    try:
        proc = subprocess.run(
            cmd,
            input=input_bytes,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
        )
        err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        out = ""
        if stdout_path is None and proc.stdout:
            out = proc.stdout.decode("utf-8", "replace")
        return proc.returncode, (err + out).strip()
    finally:
        if stdout_path:
            stdout_target.close()


# --- Engine container lifecycle --------------------------------------------

def docker_available() -> bool:
    try:
        rc, _ = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
        return rc == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def image_present() -> bool:
    rc, out = _run(["docker", "image", "inspect", settings.docker_image], timeout=30)
    return rc == 0


def container_state() -> str:
    """Return 'running', 'exited', 'missing', or 'unknown'."""
    rc, out = _run(
        ["docker", "inspect", "-f", "{{.State.Status}}", settings.container_name],
        timeout=30,
    )
    if rc != 0:
        return "missing"
    state = out.strip().splitlines()[0] if out.strip() else "unknown"
    return state or "unknown"


def _wait_ready(log: LogFn) -> None:
    deadline = time.time() + settings.container_ready_timeout
    while time.time() < deadline:
        rc, _ = _run(
            ["docker", "exec", "--user", "postgres", settings.container_name,
             "psql", "-tAc", "select 1"],
            timeout=15,
        )
        if rc == 0:
            return
        time.sleep(1)
    raise DockerError("engine container did not become ready in time")


def ensure_container(log: LogFn) -> None:
    """Make sure the engine container exists and is accepting connections."""
    if not docker_available():
        raise DockerError("Docker is not available or the daemon is not running")
    state = container_state()
    if state == "missing":
        if not image_present():
            log(f"Pulling image {settings.docker_image} (first run, may take a while)...")
            rc, out = _run(["docker", "pull", settings.docker_image], timeout=1800)
            if rc != 0:
                raise DockerError(f"failed to pull image: {out}")
        log(f"Creating engine container '{settings.container_name}'...")
        rc, out = _run([
            "docker", "run", "-d",
            "--name", settings.container_name,
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "--add-host", "host.docker.internal:host-gateway",
            settings.docker_image,
        ], timeout=120)
        if rc != 0:
            raise DockerError(f"failed to start container: {out}")
    elif state != "running":
        log(f"Starting engine container '{settings.container_name}' (was {state})...")
        rc, out = _run(["docker", "start", settings.container_name], timeout=60)
        if rc != 0:
            raise DockerError(f"failed to start container: {out}")
    _wait_ready(log)


def stop_container() -> None:
    _run(["docker", "stop", settings.container_name], timeout=60)


# --- Collection (psql out to the target DB) --------------------------------

def test_connection(target: TargetConn, password: Optional[str]) -> Tuple[bool, str]:
    """Quick connectivity check: SELECT 1 against the target DB via the container."""
    try:
        ensure_container(lambda _m: None)
    except DockerError as exc:
        return False, str(exc)
    env_extra, pass_flags = _password_env(password)
    cmd = ["docker", "exec", "-i", *pass_flags, settings.container_name,
           "psql", target.conninfo(), "-tAc", "select 1"]
    rc, out = _run(cmd, env_extra=env_extra, timeout=30)
    if rc == 0:
        return True, "ok"
    return False, _scrub(out, password)


def collect(target: TargetConn, password: Optional[str], out_path: Path,
            log: LogFn) -> None:
    """Run gather.sql against the target DB; write the TSV to ``out_path``."""
    ensure_container(log)
    gather_sql = settings.gather_sql.read_bytes()
    env_extra, pass_flags = _password_env(password)
    cmd = ["docker", "exec", "-i", *pass_flags, settings.container_name,
           "psql", target.conninfo(), "-X", "-f", "-"]
    log(f"Collecting from {target.host}:{target.port}/{target.dbname} ...")
    rc, err = _run(cmd, input_bytes=gather_sql, stdout_path=out_path,
                   env_extra=env_extra, timeout=settings.collect_timeout)
    if err:
        log(_scrub(err, password))
    # gather.sql is designed to keep going on permission errors, so a non-zero
    # rc with a produced file is acceptable; an empty file is a real failure.
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise DockerError(f"collection produced no data (rc={rc}): {_scrub(err, password)}")
    log(f"Collected {out_path.stat().st_size} bytes.")


# --- Report generation ------------------------------------------------------

def generate(tsv_bytes: bytes, html_path: Path, log: LogFn) -> None:
    """Import schema + TSV into the engine DB, then run the report to HTML."""
    ensure_container(log)
    schema = settings.gather_schema_sql.read_bytes()
    report_sql = settings.gather_report_sql.read_bytes()

    log("Importing gather data into engine database...")
    rc, err = _run(
        ["docker", "exec", "-i", "--user", "postgres", settings.container_name,
         "psql", "-f", "-", "-c", "ANALYZE"],
        input_bytes=schema + b"\n" + tsv_bytes,
        timeout=settings.generate_timeout,
    )
    if err:
        log(err)
    if rc != 0:
        raise DockerError(f"data import failed (rc={rc})")

    log("Generating report...")
    rc, err = _run(
        ["docker", "exec", "-i", "--user", "postgres", settings.container_name,
         "sh", "-c", "psql -X -f -"],
        input_bytes=report_sql,
        stdout_path=html_path,
        timeout=settings.generate_timeout,
    )
    if err:
        log(err)
    if not html_path.exists() or html_path.stat().st_size == 0:
        raise DockerError(f"report generation produced no output (rc={rc})")
    log(f"Report generated: {html_path.stat().st_size} bytes.")


def extract_detail(log: LogFn) -> Optional[str]:
    """Extract detailed per-section data from the engine DB as a JSON string.

    Called right after generation while the engine DB still holds the imported
    data. Each section mirrors the analysis from gather_report.sql but returns
    JSON instead of HTML. Returns None if extraction fails (non-fatal).
    """
    sections = {}

    def _q(key: str, sql: str) -> None:
        rc, out = _run(
            ["docker", "exec", "-i", "--user", "postgres", settings.container_name,
             "psql", "-tAc", sql],
            timeout=120,
        )
        if rc == 0 and out.strip():
            sections[key] = out.strip()

    log("Extracting detailed report data...")

    # --- Tables (with bloat, dead tuples, cache hit, vacuum, sizes) ---
    # Matches gather_report.sql: JOIN on relid=reloid, exclude TOAST and partitioned parents.
    _q("tables", """
SELECT coalesce(json_agg(t ORDER BY t.tab_ind_size DESC NULLS LAST), '[]') FROM (
  SELECT c.relname, n.nsname AS schema,
    c.relkind, r.relid AS oid, c.relfilenode, c.reltablespace,
    c.reloptions::text AS reloptions,
    r.n_live_tup, r.n_dead_tup,
    CASE WHEN r.n_live_tup > 0 THEN round(r.n_dead_tup::numeric / r.n_live_tup, 2) END AS dead_ratio,
    CASE WHEN r.blks > 999 AND r.blks > tb.est_pages
         THEN (r.blks - tb.est_pages) * 100 / r.blks END AS bloat_pct,
    r.rel_size, r.tot_tab_size, r.tab_ind_size, r.rel_age,
    CASE WHEN c.blocks_fetched > 0
         THEN round((c.blocks_hit * 100.0 / c.blocks_fetched)::numeric, 1) END AS cache_hit_pct,
    r.last_vac, r.last_anlyze, r.vac_nos,
    r.n_tup_ins, r.n_tup_upd, r.n_tup_del, r.n_tup_hot_upd,
    r.lastuse, r.dpart,
    (SELECT count(*) FROM pg_get_index i WHERE i.indrelid = c.reloid) AS idx_count,
    (SELECT count(*) FROM pg_get_index i WHERE i.indrelid = c.reloid AND i.numscans = 0) AS idx_unused,
    (SELECT count(*) FROM pg_get_index i WHERE i.indrelid = c.reloid AND i.indisprimary) AS has_pk,
    (SELECT count(*) FROM pg_get_index i WHERE i.indrelid = c.reloid AND i.indisunique) AS has_uk
  FROM pg_get_rel r
  JOIN pg_get_class c ON r.relid = c.reloid AND c.relkind NOT IN ('t','p')
  LEFT JOIN pg_get_ns n ON n.nsoid = c.relnamespace
  LEFT JOIN pg_tab_bloat tb ON tb.table_oid = c.reloid
  ORDER BY r.tab_ind_size DESC NULLS LAST
  LIMIT 500
) t
""")

    # --- Indexes ---
    _q("indexes", """
SELECT coalesce(json_agg(t ORDER BY t.size DESC NULLS LAST), '[]') FROM (
  SELECT ci.relname AS index_name, ct.relname AS table_name, n.nsname AS schema,
    i.indisunique, i.indisprimary, i.indisvalid, i.numscans, i.size,
    CASE WHEN ci.blocks_fetched > 0
         THEN round((ci.blocks_hit * 100.0 / ci.blocks_fetched)::numeric, 1) END AS cache_hit_pct,
    i.lastuse
  FROM pg_get_index i
  LEFT JOIN pg_get_class ci ON ci.reloid = i.indexrelid
  LEFT JOIN pg_get_class ct ON ct.reloid = i.indrelid
  LEFT JOIN pg_get_ns n ON n.nsoid = COALESCE(ct.relnamespace, ci.relnamespace)
  ORDER BY i.size DESC NULLS LAST
  LIMIT 1000
) t
""")

    # --- Sessions ---
    _q("sessions", """
SELECT coalesce(json_agg(t ORDER BY t.state, t.backend_start), '[]') FROM (
  SELECT a.pid, a.state, a.backend_type, a.wait_event_type, a.wait_event,
    left(a.query, 1000) AS query,
    a.client_addr::text, a.application_name,
    a.backend_start, a.xact_start, a.query_start, a.state_change,
    age(a.backend_xmin)::text AS xmin_age,
    a.ssl, a.sslversion,
    a.leader_pid,
    (SELECT array_agg(b.blocking_pids) FROM pg_get_pidblock b WHERE b.victim_pid = a.pid) AS blocked_by
  FROM pg_get_activity a
  WHERE a.pid IS NOT NULL
  LIMIT 500
) t
""")

    # --- Wait events (aggregated with category from sessions) ---
    _q("wait_events", """
SELECT coalesce(json_agg(t ORDER BY t.cnt DESC), '[]') FROM (
  SELECT w.wait_event, count(*) AS cnt,
    (SELECT a.wait_event_type FROM pg_get_activity a
     WHERE a.wait_event = w.wait_event LIMIT 1) AS category
  FROM pg_pid_wait w
  WHERE w.wait_event IS NOT NULL
  GROUP BY w.wait_event
) t
""")

    # --- Top statements ---
    _q("statements", """
SELECT coalesce(json_agg(t ORDER BY t.total_time DESC), '[]') FROM (
  SELECT left(query, 1000) AS query, calls, round(total_time::numeric, 2) AS total_time,
    CASE WHEN calls > 0 THEN round((total_time / calls)::numeric, 2) END AS avg_time_ms,
    CASE WHEN calls > 0 THEN round((shared_blks_read::numeric / calls), 1) END AS avg_reads,
    CASE WHEN (shared_blks_hit + shared_blks_read) > 0
         THEN round((shared_blks_hit * 100.0 / (shared_blks_hit + shared_blks_read))::numeric, 1) END AS cache_hit_pct,
    CASE WHEN calls > 0 THEN round((shared_blks_dirtied::numeric / calls), 1) END AS avg_dirtied,
    CASE WHEN calls > 0 THEN round((temp_blks_read::numeric / calls), 1) END AS avg_temp_reads,
    CASE WHEN calls > 0 THEN round((temp_blks_written::numeric / calls), 1) END AS avg_temp_writes,
    round((total_time * 100.0 / NULLIF((SELECT sum(total_time) FROM pg_get_statements), 0))::numeric, 1) AS pct_db_time
  FROM pg_get_statements
  WHERE total_time > 0
  ORDER BY total_time DESC
  LIMIT 30
) t
""")

    # --- Databases ---
    _q("databases", """
SELECT coalesce(json_agg(t ORDER BY t.db_size DESC NULLS LAST), '[]') FROM (
  SELECT datname, encod, colat, db_size, age, mxidage,
    xact_commit, xact_rollback,
    tup_inserted, tup_updated, tup_deleted,
    CASE WHEN blks_fetch > 0
         THEN round((blks_hit * 100.0 / blks_fetch)::numeric, 1) END AS cache_hit_pct,
    temp_files, temp_bytes,
    CASE WHEN extract(epoch FROM (g.collect_ts - d.stats_reset)) > 86400
         THEN round((xact_commit / (extract(epoch FROM (g.collect_ts - d.stats_reset)) / 86400.0))::numeric)
    END AS commits_per_day,
    d.stats_reset
  FROM pg_get_db d
  CROSS JOIN pg_gather g
  WHERE datname IS NOT NULL
) t
""")

    # --- Replication ---
    _q("replication", """
SELECT coalesce(json_agg(t), '[]') FROM (
  SELECT r.usename, r.client_addr, r.pid, r.state, r.sync_state,
    pg_wal_lsn_diff(g.current_wal, r.sent_lsn) AS sent_lag,
    pg_wal_lsn_diff(g.current_wal, r.write_lsn) AS write_lag,
    pg_wal_lsn_diff(g.current_wal, r.flush_lsn) AS flush_lag,
    pg_wal_lsn_diff(g.current_wal, r.replay_lsn) AS replay_lag,
    s.slot_name, s.plugin, s.slot_type, s.temporary, s.active,
    pg_wal_lsn_diff(g.current_wal, s.restart_lsn) AS restart_lag
  FROM pg_replication_stat r
  CROSS JOIN pg_gather g
  LEFT JOIN pg_get_slots s ON s.active_pid = r.pid
) t
""")

    # --- Replication slots (not tied to a walsender) ---
    _q("replication_slots", """
SELECT coalesce(json_agg(t), '[]') FROM (
  SELECT s.slot_name, s.plugin, s.slot_type, s.temporary, s.active, s.active_pid,
    pg_wal_lsn_diff(g.current_wal, s.restart_lsn) AS restart_lag,
    pg_wal_lsn_diff(g.current_wal, s.confirmed_flush_lsn) AS confirmed_lag
  FROM pg_get_slots s
  CROSS JOIN pg_gather g
) t
""")

    # --- Checkpoints / BGWriter ---
    _q("bgwriter", """
SELECT row_to_json(t) FROM (
  SELECT checkpoints_timed, checkpoints_req,
    CASE WHEN (checkpoints_timed + checkpoints_req) > 0
         THEN round((checkpoints_req * 100.0 / (checkpoints_timed + checkpoints_req))::numeric, 1)
    END AS forced_pct,
    round(checkpoint_write_time::numeric / 1000, 1) AS write_time_s,
    round(checkpoint_sync_time::numeric / 1000, 1) AS sync_time_s,
    buffers_checkpoint, buffers_clean, buffers_backend, buffers_alloc,
    maxwritten_clean,
    CASE WHEN (checkpoints_timed + checkpoints_req) > 0
         THEN round((
           extract(epoch FROM (g.collect_ts - b.stats_reset))
           / (checkpoints_timed + checkpoints_req) / 60.0)::numeric, 1)
    END AS avg_cp_interval_min,
    CASE WHEN (checkpoints_timed + checkpoints_req) > 0
         THEN round((buffers_checkpoint * 8.0 / 1024 / (checkpoints_timed + checkpoints_req))::numeric, 1)
    END AS mb_per_checkpoint,
    b.stats_reset
  FROM pg_get_bgwriter b
  CROSS JOIN pg_gather g
) t
""")

    # --- HBA rules (with basic shadow detection) ---
    _q("hba", """
SELECT coalesce(json_agg(t ORDER BY t.seq), '[]') FROM (
  SELECT h.seq, h.typ, h.db, h.usr, h.addr, h.mask, h.method, h.err,
    (SELECT string_agg(h2.seq::text, ', ')
     FROM pg_get_hba_rules h2
     WHERE h2.seq < h.seq
       AND h2.typ = h.typ
       AND (h2.db @> h.db OR h2.db = ARRAY['all']::text[])
       AND (h2.usr @> h.usr OR h2.usr = ARRAY['all']::text[])
       AND (h2.addr = h.addr OR h2.addr IS NULL AND h.addr IS NULL)
    ) AS shadowed_by
  FROM pg_get_hba_rules h
) t
""")

    # --- Extensions ---
    _q("extensions", """
SELECT coalesce(json_agg(t ORDER BY t.extname), '[]') FROM (
  SELECT e.extname, e.extversion, e.extrelocatable,
    n.nsname AS schema, r.rolname AS owner
  FROM pg_get_extension e
  LEFT JOIN pg_get_ns n ON n.nsoid = e.extnamespace
  LEFT JOIN pg_get_roles r ON r.oid = e.extowner
) t
""")

    # --- Roles ---
    _q("roles", """
SELECT coalesce(json_agg(t ORDER BY t.rolname), '[]') FROM (
  SELECT ro.rolname, ro.rolsuper, ro.rolreplication, ro.rolconnlimit,
    CASE ro.enc_method WHEN 'S' THEN 'SCRAM' WHEN 'M' THEN 'MD5' ELSE '?' END AS auth_method,
    (SELECT count(*) FROM pg_get_activity a WHERE a.usesysid = ro.oid AND a.state = 'active') AS active,
    (SELECT count(*) FROM pg_get_activity a WHERE a.usesysid = ro.oid AND a.state = 'idle in transaction') AS idle_in_txn,
    (SELECT count(*) FROM pg_get_activity a WHERE a.usesysid = ro.oid AND a.state = 'idle') AS idle,
    (SELECT count(*) FROM pg_get_activity a WHERE a.usesysid = ro.oid) AS total_conns
  FROM pg_get_roles ro
) t
""")

    # --- Head info (server identity, uptime, WAL) ---
    _q("head_info", """
SELECT row_to_json(t) FROM (
  SELECT g.collect_ts, g.ver AS pg_version, g.pg_start_ts,
    CASE WHEN g.pg_start_ts IS NOT NULL
         THEN extract(epoch FROM (g.collect_ts - g.pg_start_ts)) END AS uptime_secs,
    g.recovery, g.timeline, g.systemid, g.current_wal::text,
    g.reload_ts, g.bindir, g.client::text, g.server::text,
    s.connstr,
    e.end_ts, e.end_lsn::text
  FROM pg_gather g
  LEFT JOIN pg_srvr s ON true
  LEFT JOIN pg_gather_end e ON true
) t
""")

    # --- Connections by database ---
    _q("connections_by_db", """
SELECT coalesce(json_agg(t ORDER BY t.total DESC), '[]') FROM (
  SELECT d.datname,
    count(*) FILTER (WHERE a.state = 'active') AS active,
    count(*) FILTER (WHERE a.state = 'idle in transaction') AS idle_in_txn,
    count(*) FILTER (WHERE a.state = 'idle') AS idle,
    count(*) AS total,
    count(*) FILTER (WHERE a.ssl) AS ssl,
    count(*) FILTER (WHERE NOT a.ssl) AS non_ssl
  FROM pg_get_activity a
  LEFT JOIN pg_get_db d ON d.datid = a.datid
  WHERE a.backend_type = 'client backend'
  GROUP BY d.datname
) t
""")

    # --- IO statistics ---
    _q("io_stats", """
SELECT coalesce(json_agg(t), '[]') FROM (
  SELECT
    CASE btype WHEN 'a' THEN 'Autovacuum' WHEN 'B' THEN 'BG Writer' WHEN 'K' THEN 'Checkpointer'
               WHEN 'b' THEN 'Client Backend' WHEN 'w' THEN 'WAL Writer' ELSE btype END AS backend_type,
    sum(reads) AS reads, sum(read_bytes) AS read_bytes,
    sum(writes) AS writes, sum(write_bytes) AS write_bytes,
    sum(hits) AS hits, sum(evictions) AS evictions, sum(fsyncs) AS fsyncs
  FROM pg_get_io
  GROUP BY btype
  HAVING sum(reads) > 0 OR sum(writes) > 0 OR sum(hits) > 0
) t
""")

    # --- Partitioned tables ---
    _q("partitioned_tables", """
SELECT coalesce(json_agg(t), '[]') FROM (
  WITH ptables AS (
    SELECT p.relname, p.relkind, i.inhparent, i.inhrelid
    FROM pg_get_class p LEFT JOIN pg_get_inherits i ON i.inhparent = p.reloid
    WHERE p.relkind IN ('p','r')
  )
  SELECT p.relname AS table_name,
    'Native-Declarative' AS partitioning_type,
    count(r.relid) AS partition_count,
    sum(r.tot_tab_size) AS tot_tab_size,
    sum(r.tab_ind_size) AS tab_ind_size,
    round((max(c.blocks_fetched)::numeric / NULLIF(sum(c.blocks_fetched), 0) * 100), 1) AS fetch_prune_pct
  FROM ptables p
  LEFT JOIN pg_get_rel r ON p.inhrelid = r.relid
  LEFT JOIN pg_get_class c ON p.inhrelid = c.reloid
  WHERE p.relkind = 'p'
  GROUP BY p.relname
  UNION ALL
  SELECT p.relname,
    'Inheritance',
    count(r.relid),
    sum(r.tot_tab_size),
    sum(r.tab_ind_size),
    round((max(c.blocks_fetched)::numeric / NULLIF(sum(c.blocks_fetched), 0) * 100), 1)
  FROM ptables p
  JOIN pg_get_rel r ON p.inhrelid = r.relid
  JOIN pg_get_class c ON p.inhrelid = c.reloid
  WHERE p.relkind = 'r'
  GROUP BY p.relname
) t
""")

    # --- Parameters (detailed: with unit, source, file overrides) ---
    _q("params_detail", """
SELECT coalesce(json_agg(t ORDER BY t.name), '[]') FROM (
  SELECT c.name, c.setting, c.unit, c.source,
    (SELECT json_agg(json_build_object('file', f.sourcefile, 'setting', f.setting, 'applied', f.applied, 'error', f.error))
     FROM pg_get_file_confs f WHERE f.name = c.name) AS file_confs
  FROM pg_get_confs c
) t
""")

    if not sections:
        log("Warning: detail extraction returned no data.")
        return None

    # Build combined JSON: parse each section's JSON string into the combined dict.
    import json
    combined = {}
    for key, raw in sections.items():
        try:
            combined[key] = json.loads(raw)
        except (ValueError, TypeError):
            pass

    result = json.dumps(combined)
    log(f"Extracted detail data: {len(combined)} sections, {len(result)} bytes.")
    return result


def dump_settings() -> Optional[str]:
    """Return the imported config as a JSON ``{name: setting}`` map (for compare).

    Called right after generation while the engine DB still holds the data.
    Returns None if the query fails (non-fatal).
    """
    rc, out = _run(
        ["docker", "exec", "-i", "--user", "postgres", settings.container_name,
         "psql", "-tAc",
         "SELECT coalesce(json_object_agg(name, setting), '{}') FROM pg_get_confs"],
        timeout=60,
    )
    if rc != 0 or not out.strip():
        return None
    return out.strip()


# --- helpers ---------------------------------------------------------------

def _password_env(password: Optional[str]):
    """Forward PGPASSWORD via the CLI environment (not argv) to avoid leaking it
    into the host process list. Returns (env_extra, docker_flags).

    ``-e PGPASSWORD`` (without ``=value``) tells docker to forward the var from
    the calling process's env into the container. The password is set in
    ``env_extra`` which subprocess.run merges into os.environ for the docker
    child process only — so the password never appears in ``/proc/.../cmdline``.
    """
    if not password:
        return {}, []
    return {"PGPASSWORD": password}, ["-e", "PGPASSWORD"]


def _scrub(text: str, password: Optional[str]) -> str:
    if password:
        text = text.replace(password, "***")
    return text
