"""Map the stored pg_gather ``obj`` JSON into a friendly view model.

The report's ``obj`` object is built with unaliased SQL ``ROW()`` constructors,
so its nested fields are positional (``f1``, ``f2``, ...). The exact field
meanings below were decoded from ``gather_report.sql`` (the ``obj=`` SELECT).
Keeping the mapping here means templates never deal with ``fN`` keys.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _f(d: Any, key: str, default: Any = None) -> Any:
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# --- Metric cards (health overview) ----------------------------------------

def metric_cards(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    sess = _f(obj, "sess", {})
    tabs = _f(obj, "tabs", {})
    induse = _f(obj, "induse", {})
    meta = _f(obj, "meta", {})
    sumry = _f(obj, "sumry", {})

    cards: List[Dict[str, Any]] = []

    # Connections
    idle_txn = _int(_f(sess, "f2"))
    cards.append({
        "label": "Connections", "value": _int(_f(sess, "f6")), "unit": "total",
        "tip": "Total sessions including active, idle, background, and parallel workers",
        "tone": "warn" if idle_txn > 0 else "ok",
        "sub": [
            {"k": "active", "v": _int(_f(sess, "f1"))},
            {"k": "idle", "v": _int(_f(sess, "f3"))},
            {"k": "idle in txn", "v": idle_txn, "tone": "warn" if idle_txn else None,
             "tip": "Idle-in-transaction sessions hold locks and block vacuum. Fix application code" if idle_txn else ""},
        ],
    })

    # Tables
    never_vac = _int(_f(tabs, "f2"))
    never_anl = _int(_f(tabs, "f4"))
    cards.append({
        "label": "Tables", "value": _int(_f(tabs, "f1")), "unit": "user tables",
        "tip": "Total user tables in this database (excluding system catalogs and TOAST)",
        "tone": "warn" if (never_vac or never_anl) else "ok",
        "sub": [
            {"k": "never vacuumed", "v": never_vac, "tone": "warn" if never_vac else None,
             "tip": "Tables never vacuumed risk bloat and wraparound" if never_vac else ""},
            {"k": "never analyzed", "v": never_anl, "tone": "warn" if never_anl else None,
             "tip": "Tables never analyzed may have poor query plans" if never_anl else ""},
        ],
    })

    # Indexes
    unused = _int(_f(induse, "f2"))
    invalid = _int(_f(induse, "f1"))
    cards.append({
        "label": "Indexes", "value": _int(_f(induse, "f4")), "unit": "user indexes",
        "tip": "Total user indexes. Unused/invalid indexes waste space and slow writes",
        "tone": "bad" if invalid else ("warn" if unused else "ok"),
        "sub": [
            {"k": "unused", "v": unused, "tone": "warn" if unused else None,
             "tip": f"Unused indexes waste {fmt_bytes(_f(induse, 'f6'))} and slow writes. Consider dropping" if unused else ""},
            {"k": "invalid", "v": invalid, "tone": "bad" if invalid else None,
             "tip": "Invalid indexes must be recreated with REINDEX or dropped" if invalid else ""},
            {"k": "wasted", "v": fmt_bytes(_f(induse, "f6"))},
        ],
    })

    # Total size
    cards.append({
        "label": "Total Size", "value": fmt_bytes(_f(meta, "f2")), "unit": "tables + indexes",
        "tip": "Combined size of all tables, indexes, and TOAST data in this database",
        "tone": "ok",
        "sub": [
            {"k": "relations", "v": _int(_f(meta, "f3"))},
            {"k": "catalog", "v": fmt_bytes(_f(meta, "f1"))},
        ],
    })

    # WAL generation rate (long-term average since stats reset)
    cards.append({
        "label": "WAL / hour", "value": fmt_bytes(_f(sumry, "f3")), "unit": "avg since reset",
        "tip": "Long-term average Write-Ahead Log generation rate. Used to size max_wal_size and estimate storage I/O",
        "tone": "ok",
        "sub": [{"k": "collection window", "v": f"{_int(_f(sumry, 'f1'))}s"}],
    })

    return cards


# --- Findings ---------------------------------------------------------------
# Ported from gather_report.sql JavaScript: checkfindings(), checkdbs(),
# checkconns(), checkusers(), checkhba(), checkindex(), checksess(),
# checkstmnts(), checkreplstat(), checkchkpntbgwrtr(), checkextn().

def findings(obj: Dict[str, Any], detail: Optional[Dict[str, Any]] = None,
             params: Optional[Dict[str, str]] = None,
             meta: Optional[Dict[str, Any]] = None,
             engine_ver: Optional[str] = None) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    tabs = _f(obj, "tabs", {})
    induse = _f(obj, "induse", {})
    nokey = _f(obj, "nokey", {})
    sess = _f(obj, "sess", {})
    arcfail = _f(obj, "arcfail", {})
    sumry = _f(obj, "sumry", {})
    cn = _f(obj, "cn", {})
    clas = _f(obj, "clas", {})
    meta_obj = _f(obj, "meta", {})
    netdlay = _f(obj, "netdlay", {})
    obj_params = _f(obj, "params", {})
    is_primary = not bool(_f(obj, "clsr"))
    detail = detail or {}
    params = params or {}
    _DOC = "https://jobinau.github.io/pg_gather/"

    def add(sev: str, title: str, detail_text: str, doc: str = "") -> None:
        entry: Dict[str, str] = {"sev": sev, "title": title, "detail": detail_text}
        if doc:
            entry["doc"] = _DOC + doc
        out.append(entry)

    # --- Critical (red) findings ---

    # Privilege / data collection issue
    if _int(_f(sess, "f7")) < 4 and _int(_f(sess, "f7")) > 0:
        add("red", "Insufficient privileges or corrupt data",
            "Data was collected by a user without necessary privileges, or the TSV format was damaged.")

    # Crash detection
    if _f(obj, "crash") is not None:
        add("red", "Possible crash / unclean restart detected",
            f"Suspected unclean shutdown around: {_f(obj, 'crash')}. Check PostgreSQL logs.")

    # Blocking sessions
    if _f(obj, "blkrs") is not None:
        n = len(_f(obj, "blkrs") or [])
        add("red", f"{n} blocking session(s)", "Sessions are blocking others (lock contention).")

    # Blocked victims
    victims = _f(obj, "victims")
    if victims is not None and len(victims or []) > 0:
        add("red", f"{len(victims)} session(s) blocked",
            "Sessions are waiting on locks held by other sessions.")

    # Invalid indexes
    if _int(_f(induse, "f1")) > 0:
        add("red", f"{_int(_f(induse, 'f1'))} invalid index(es)",
            "Invalid indexes are not used by the planner and waste space. Recreate or drop them.",
            "InvalidIndexes.html")

    # Configuration file errors
    errparams = _f(obj, "errparams")
    if errparams is not None and len(errparams or []) > 0:
        details = "; ".join(
            f"{_f(e, 'f2', '?')} = {_f(e, 'f3', '?')} ({_f(e, 'f1', '')})"
            for e in (errparams or [])[:5]
        )
        add("red", f"{len(errparams)} parameter file error(s)", details)

    # No parameter values found
    if _f(obj_params, "f6") is None or _int(_f(obj_params, "f6")) == 0:
        add("red", "No parameter values found",
            "Data collection could be partial or the parameter file(s) are corrupt.")

    # Partial data collection
    if _int(_f(sess, "f7")) == 0:
        add("red", "Partial data collection",
            "Make sure to run gather.sql with an account that has the required permissions and wait for completion.")

    # seq_page_cost != 1
    if params.get("seq_page_cost") and params["seq_page_cost"] != "1":
        add("red", f"seq_page_cost = {params['seq_page_cost']}",
            "This should almost always be 1. Non-default values distort the query planner.")

    # zero_damaged_pages on
    if params.get("zero_damaged_pages") == "on":
        add("red", "zero_damaged_pages is ON",
            "This hides data corruption. Turn it off immediately unless actively recovering.")

    # autovacuum off
    if params.get("autovacuum") and params["autovacuum"] != "on":
        add("red", f"Autovacuum is {params['autovacuum']}",
            "Autovacuum must be enabled to prevent bloat and transaction ID wraparound.")

    # --- Warning (amber) findings ---

    # Idle-in-transaction
    if _int(_f(sess, "f2")) > 0:
        add("amber", f"{_int(_f(sess, 'f2'))} idle-in-transaction session(s)",
            "Long idle transactions hold locks and block vacuum. Consider improving application code.")

    # Connection pool analysis
    cn1, cn2 = _int(_f(cn, "f1")), _int(_f(cn, "f2"))
    if cn1 > 0 and (cn2 > 9 or (cn2 / max(cn1, 1)) > 0.7):
        add("amber", f"{cn2} / {cn1} connections are new",
            "High ratio of new connections suggests missing or misconfigured connection pooling.")

    # Tables without PK
    nopk = _int(_f(nokey, "f1"))
    nouk = _int(_f(nokey, "f2"))
    if nopk > 0:
        add("amber", f"{nopk} table(s) without a primary key",
            f"{nouk} without PK nor unique key. Tables without PK complicate replication and row identification.",
            "pkuk.html")

    # Unused indexes
    unused_idx = _int(_f(induse, "f2"))
    unused_toast = _int(_f(induse, "f3"))
    if unused_idx > 0:
        add("amber", f"{unused_idx} unused user index(es), {unused_toast} unused TOAST index(es)",
            f"Out of {_int(_f(induse, 'f4'))} user and {_int(_f(induse, 'f5'))} TOAST indexes. "
            f"Unused indexes waste {fmt_bytes(_f(induse, 'f6'))} and slow writes.",
            "unusedIndexes.html")

    # Never vacuumed (only on primary)
    if is_primary and _int(_f(tabs, "f2")) > 0:
        add("amber", f"{_int(_f(tabs, 'f2'))} table(s) never vacuumed",
            "Risk of bloat and transaction ID wraparound.",
            "bloat.html")

    # Never analyzed
    if _int(_f(tabs, "f4")) > 0:
        add("amber", f"{_int(_f(tabs, 'f4'))} table(s) never analyzed",
            "Query planner may use stale statistics, leading to poor plans.")

    # No statistics available
    if _int(_f(tabs, "f3")) > 0:
        add("amber", f"No statistics available for {_int(_f(tabs, 'f3'))} tables/objects",
            "Query planning can go wrong without statistics.",
            "missingstats.html")

    # WAL archiving
    arc_secs = _num(_f(arcfail, "f1"))
    if arc_secs is None and is_primary:
        add("amber", "No WAL archiving detected",
            "No working WAL archiving and backup detected. Point-in-time recovery may not be possible.")
    elif arc_secs is not None and arc_secs > 300:
        add("amber", f"WAL archiving stalled for {fmt_duration(arc_secs)}",
            "Archiving could be failing. Check PostgreSQL logs.")

    # WAL archive lag in bytes
    arc_lag = _num(_f(arcfail, "f2"))
    if arc_lag is not None and arc_lag > 0:
        add("amber", f"WAL archiving lagging by {fmt_bytes(arc_lag)}",
            f"Last archived WAL: {_f(arcfail, 'f3') or '?'} at {_f(arcfail, 'f4') or '?'}.",
            "walarchive.html")

    # Unlogged tables
    if _int(_f(clas, "f2")) > 0:
        add("amber", f"{_int(_f(clas, 'f2'))} unlogged table(s) found",
            "Unlogged tables and associated indexes are ephemeral — data is lost on crash.",
            "unloggedtables.html")

    # Natively partitioned tables
    if _int(_f(clas, "f1")) > 0:
        add("blue", f"{_int(_f(clas, 'f1'))} natively partitioned table(s)",
            "Tables section may contain individual partitions.")

    # High OID (temp tables / DDL churn)
    if _int(_f(clas, "f3")) > 50000:
        add("amber", f"pg_class OID at {_int(_f(clas, 'f3')):,}",
            "Indicates high use of temporary tables or heavy DDL activity.")

    # Data collection response quality
    coll_secs = _num(_f(sumry, "f1"))
    if coll_secs is not None:
        if coll_secs >= 28:
            add("amber", f"Data collection took {int(coll_secs)}s — system response appears poor",
                "Slow collection suggests high load or I/O contention.")
        elif coll_secs >= 23:
            add("amber", f"Data collection took {int(coll_secs)}s — below average response",
                "Collection normally takes ~20s. Slightly elevated.")

    # WAL generation rate
    wal_current = _num(_f(sumry, "f2"))
    wal_longterm = _num(_f(sumry, "f3"))
    if wal_current is not None:
        parts = [f"Current WAL generation: {fmt_bytes(wal_current)}/hour"]
        if wal_longterm is not None:
            parts.append(f"long-term average: {fmt_bytes(wal_longterm)}/hour")
        add("blue", "WAL generation rate", ". ".join(parts) + ".")

    # Network/scheduling delay
    nd1, nd2, nd3 = _num(_f(netdlay, "f1")), _num(_f(netdlay, "f2")), _int(_f(netdlay, "f3"))
    if nd1 is not None and nd2 and nd2 > 0:
        ratio = nd1 / nd2 * 100
        if ratio > 20:
            severity = "amber" if nd1 / nd2 > 1 else "amber"
            add(severity, f"{nd3} session(s) with considerable network/scheduling delay",
                f"Delay ratio: {ratio:.0f}%. This may indicate network latency or OS scheduling issues.")

    # Patroni/HA cluster detection
    if _int(_f(obj_params, "f3")) > 10:
        cluster_name = _f(obj_params, "f2") or "unknown"
        add("blue", f"Patroni/HA cluster detected: {cluster_name}",
            "High number of command-line parameters suggests automated HA management.")

    # Non-standard compile/init params
    compile_params = _f(obj_params, "f5")
    if compile_params and len(compile_params) > 0:
        names = ", ".join(f"{_f(p, 'f1', '?')}: {_f(p, 'f2', '?')}" for p in compile_params[:5])
        add("amber", "Non-standard compile/initialization parameters detected",
            f"{names}. Custom compilation is prone to bugs that are difficult to diagnose.")

    # Maintenance work_mem consumers
    wmemuse = _f(obj, "wmemuse")
    if wmemuse and len(wmemuse) > 0:
        tables_str = ", ".join(
            f"{_f(t, 'f1', '?')} ({fmt_bytes(_f(t, 'f2'))})"
            for t in wmemuse[:3]
        )
        add("blue", "Large maintenance_work_mem consumers",
            f"Biggest consumers: {tables_str}.")

    # Additional tablespaces
    tbsp = _f(obj, "tbsp")
    if tbsp and len(tbsp) > 0:
        ts_str = ", ".join(f"{_f(t, 'tsname')}: {_f(t, 'location', '?')}" for t in tbsp)
        add("blue", f"{len(tbsp)} additional tablespace(s)",
            f"Found: {ts_str}.")

    # Too many tables
    if _int(_f(tabs, "f1")) > 10000:
        add("amber", f"{_int(_f(tabs, 'f1')):,} tables/objects in the database",
            "Only the biggest 10,000 are displayed. Avoid too many objects in a single database.",
            "table_object.html")

    # Multixact ID age
    mxiddbs = _f(obj, "mxiddbs")
    if mxiddbs is not None and _f(mxiddbs, "f2"):
        add("amber", f"High multixact ID age: {_f(mxiddbs, 'f2')}",
            f"Affected databases: {_f(mxiddbs, 'f1') or '?'}.",
            "mxid.html")

    # Standby mode
    if not is_primary:
        add("blue", "PostgreSQL is in standby / recovery mode", "")

    # Schema count
    ns = _f(obj, "ns")
    if ns and len(ns) > 0:
        temp = sum(1 for n in ns if "pg_temp" in (_f(n, "nsname") or "") or "pg_toast_temp" in (_f(n, "nsname") or ""))
        regular = len(ns) - temp
        add("blue", f"{regular} regular schema(s) and {temp} temporary schema(s)",
            "")

    # Catalog metadata size (JS only emits when > 15MB)
    cat_size = _num(_f(meta_obj, "f1"))
    obj_count = _int(_f(meta_obj, "f3"))
    if cat_size is not None and cat_size > 15728640:
        add("blue", f"Catalog metadata: {fmt_bytes(cat_size)} for {obj_count} objects", "")

    # --- checkgather findings (server head info) ---
    _findings_from_head(out, detail, obj)

    # --- Detail-based findings (from detail_json) ---
    _findings_from_detail(out, detail, params, is_primary, obj)

    # --- Old pg_gather version check ---
    if engine_ver:
        try:
            ev = int(engine_ver)
            if ev < 33:
                add("amber", f"Old pg_gather script version (v{ev})",
                    "Please use the latest release (v33+) for accurate analysis.")
        except (ValueError, TypeError):
            pass

    # --- Parameter-specific warnings ---
    _findings_from_params(out, params, obj, meta or {})

    if not out:
        add("ok", "No major issues detected",
            "None of the standard pg_gather checks flagged a problem.")
    return out


def _findings_from_head(out: List[Dict[str, str]], detail: Dict[str, Any],
                        obj: Dict[str, Any]) -> None:
    """Findings from checkgather(): server head info checks."""
    head = (detail or {}).get("head_info")
    if not head or not isinstance(head, dict):
        return

    def add(sev: str, title: str, d: str) -> None:
        out.append({"sev": sev, "title": title, "detail": d})

    import re

    # Unusual binary directory
    bindir = head.get("bindir") or ""
    if bindir and not re.search(r"/usr/lib/postgresql/\d+|/usr/pgsql-\d+/", bindir):
        add("amber", f"Unusual PostgreSQL binary directory: {bindir}",
            "Could be due to custom build or portable binaries. Custom builds are prone to issues.")

    # Timeline / MTBF
    timeline = _int(head.get("timeline"))
    uptime_secs = _num(head.get("uptime_secs"))
    if timeline > 1 and uptime_secs:
        days = uptime_secs / 86400
        failovers = timeline - 1
        if days > 30 and failovers > 5:
            mtbf = days / failovers
            if mtbf < 180:
                add("amber", f"Poor MTBF / availability: {mtbf:.0f} days",
                    f"{failovers} failovers in {days:.0f} days.")


def _findings_from_detail(out: List[Dict[str, str]], detail: Dict[str, Any],
                          params: Dict[str, str], is_primary: bool,
                          obj: Optional[Dict[str, Any]] = None) -> None:
    """Generate findings from detail_json section data."""

    def add(sev: str, title: str, detail_text: str) -> None:
        out.append({"sev": sev, "title": title, "detail": detail_text})

    # --- Database-level findings (checkdbs) ---
    dbs = detail.get("databases") or []
    high_rollback_dbs = []
    high_temp_dbs = []
    for db in dbs:
        rollbacks = _num(db.get("xact_rollback")) or 0
        commits_day = _num(db.get("commits_per_day")) or 0
        # Approximate rollbacks/day: if commits_per_day is available, estimate
        if rollbacks > 0 and commits_day > 0:
            pass  # rollback rate is cumulative, not per-day in our extraction
        if rollbacks > 4000:
            high_rollback_dbs.append(db.get("datname", "?"))
        temp_bytes = _num(db.get("temp_bytes")) or 0
        if temp_bytes > 50_000_000_000:
            high_temp_dbs.append(db.get("datname", "?"))
    if high_rollback_dbs:
        add("amber", f"High transaction rollbacks in: {', '.join(high_rollback_dbs)}",
            "Inspect PostgreSQL logs for details on aborted transactions.")
    if high_temp_dbs:
        add("amber", f"High temp file generation in: {', '.join(high_temp_dbs)}",
            "Consider increasing work_mem and enabling log_temp_files.")

    # --- Connection findings (checkconns) ---
    conns = detail.get("connections_by_db") or []
    total_nonssl = sum(_int(c.get("non_ssl")) for c in conns)
    if total_nonssl > 10:
        add("amber", f"{total_nonssl} unencrypted connections",
            "Consider enabling SSL for all client connections.")

    # --- Extension findings (checkextn) ---
    exts = detail.get("extensions") or []
    if len(exts) > 3:
        add("blue", f"{len(exts)} extensions installed",
            "Extensions can cause considerable overhead and performance degradation.")

    # --- Role/user findings (checkusers) ---
    roles = detail.get("roles") or []
    superusers = sum(1 for r in roles if r.get("rolsuper"))
    if superusers > 2:
        add("amber", f"{superusers} superuser accounts",
            "Consider reducing superuser count from a security standpoint.")

    # --- HBA findings (checkhba) ---
    hba = detail.get("hba") or []
    hba_errors = sum(1 for h in hba if h.get("err"))
    md5_rules = sum(1 for h in hba
                    if h.get("method") == "md5" and h.get("typ") in ("host", "hostssl"))
    if hba_errors > 0:
        add("red", f"{hba_errors} erroneous HBA rule(s)",
            "Review pg_hba.conf and fix errors.")
    if md5_rules > 0:
        add("amber", f"{md5_rules} HBA rule(s) using MD5 authentication",
            "Switch to scram-sha-256 for better security.")
    shadowed = sum(1 for h in hba if h.get("shadowed_by"))
    if shadowed > 0:
        add("amber", f"{shadowed} shadowed HBA rule(s) detected",
            "These rules will never match because earlier rules cover the same scope. Review pg_hba.conf.")

    # --- Statement findings (checkstmnts) ---
    # JS: hwsql counts statements with avg_time > 60s OR any block column > 12800
    stmts = detail.get("statements") or []
    high_impact = 0
    for s in stmts:
        hw = 0
        if (_num(s.get("avg_time_ms")) or 0) > 60000:
            hw += 1
        if (_num(s.get("avg_reads")) or 0) > 12800:
            hw += 1
        if (_num(s.get("avg_dirtied")) or 0) > 12800:
            hw += 1
        if (_num(s.get("avg_temp_reads")) or 0) > 12800:
            hw += 1
        if (_num(s.get("avg_temp_writes")) or 0) > 12800:
            hw += 1
        if hw > 0:
            high_impact += 1
    if high_impact > 0:
        add("amber", f"{high_impact} high-impact SQL statement(s)",
            "Statements with high execution time or heavy I/O. Check the Top Statements section.")

    # --- BGWriter/Checkpoint findings (checkchkpntbgwrtr) ---
    bg = detail.get("bgwriter")
    if bg and isinstance(bg, dict):
        forced = _num(bg.get("forced_pct"))
        if forced is not None and forced > 10:
            add("amber", f"Forced checkpoint rate: {forced:.1f}%",
                "Too many forced checkpoints. Consider increasing max_wal_size or checkpoint_timeout.")

        avg_interval = _num(bg.get("avg_cp_interval_min"))
        if avg_interval is not None and avg_interval < 10:
            add("amber", f"Checkpoints every {avg_interval:.1f} minutes",
                "Too frequent. Increase checkpoint_timeout or max_wal_size.")

        # JS checks per-checkpoint sync time: sync_time_ms / (total_cp * 1000) > 0.04
        # Our sync_time_s is total_sync_time/1000; compute per-CP sync time
        sync_total = _num(bg.get("sync_time_s")) or 0
        total_cp = _int(bg.get("checkpoints_timed")) + _int(bg.get("checkpoints_req"))
        per_cp_sync = sync_total / max(total_cp, 1)
        if per_cp_sync > 0.04:
            add("amber", f"High checkpoint sync time: {per_cp_sync:.3f}s per checkpoint",
                "Suspected slow storage. Consider benchmarking storage performance.")

        buf_backend = _int(bg.get("buffers_backend"))
        buf_clean = _int(bg.get("buffers_clean"))
        if buf_backend > buf_clean and buf_backend > 20:
            add("amber", "Backends writing more dirty pages than bgwriter",
                f"Backend buffers: {buf_backend}, BGWriter: {buf_clean}. "
                "Consider tuning bgwriter_lru_maxpages.")
        if buf_backend > 30:
            add("amber", "High memory pressure",
                "Backends are doing significant buffer writes. Consider increasing RAM and shared_buffers.")

    # --- Replication findings (checkreplstat) ---
    repl = detail.get("replication") or []
    slots = detail.get("replication_slots") or []
    for s in slots:
        if not s.get("active") and is_primary:
            add("amber", f"Abandoned replication slot: {s.get('slot_name', '?')}",
                "Inactive slots cause unwanted WAL retention. Drop if no longer needed.")
    if repl and params.get("hot_standby_feedback") == "off":
        add("amber", "hot_standby_feedback is off with active replication",
            "High chance of query cancellation at standby. Consider enabling it.")
    if not repl and not is_primary and params.get("hot_standby_feedback") == "off":
        add("amber", "hot_standby_feedback is off on standby",
            "This can cause query cancellations due to cleanup conflicts.")

    # --- Replication lag thresholds (checkreplstat per-cell) ---
    for r in repl:
        for lag_key in ("sent_lag", "write_lag", "flush_lag", "replay_lag", "restart_lag"):
            lag = _num(r.get(lag_key))
            if lag is not None and lag > 104857600:  # 100MB
                add("amber", f"Replication lag > 100MB: {r.get('usename', '?')} {lag_key}",
                    f"{fmt_bytes(lag)} lag to {r.get('client_addr', '?')}.")
                break  # one finding per replica is enough

    # --- Database size mismatch / orphan files (checkdbs) ---
    obj = obj or {}
    meta_total_size = _num(_f(_f(obj, "meta", {}), "f2"))
    dbts_name = _f(_f(obj, "dbts", {}), "f1")
    if meta_total_size and dbts_name:
        for db in dbs:
            if db.get("datname") == dbts_name:
                db_size = _num(db.get("db_size")) or 0
                if db_size > meta_total_size:
                    add("amber",
                        f"Database '{dbts_name}' is {fmt_bytes(db_size)} but objects total {fmt_bytes(meta_total_size)}",
                        "Possibly indicates orphaned files. Check for unlinked data files.")
                break

    # --- Bloated tables count (computed from detail tables) ---
    tables = detail.get("tables") or []
    bloat_count = sum(1 for t in tables
                      if (_num(t.get("bloat_pct")) or 0) > 20
                      and (_num(t.get("tab_ind_size")) or 0) > 5242880)
    if bloat_count > 0:
        add("amber", f"{bloat_count} bloated table(s)",
            "Tables with >20% bloat detected. This could affect performance.")

    # --- BGWriter stats sufficiency ---
    if bg and isinstance(bg, dict):
        stats_reset = bg.get("stats_reset")
        if not stats_reset:
            add("amber", "Insufficient bgwriter/checkpoint statistics",
                "Stats may have been recently reset. At least 1 day of data is needed for meaningful analysis.")


def _findings_from_params(out: List[Dict[str, str]], params: Dict[str, str],
                          obj: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> None:
    """Generate findings from parameter-specific checks (paramDespatch port)."""
    if not params:
        return
    is_primary = not bool(_f(obj, "clsr"))

    def add(sev: str, title: str, detail: str) -> None:
        out.append({"sev": sev, "title": title, "detail": detail})

    def _pval(name: str) -> Optional[str]:
        return params.get(name)

    def _pnum(name: str) -> Optional[float]:
        return _num(params.get(name))

    # archive_mode
    if is_primary and _pval("archive_mode") == "off":
        add("amber", "archive_mode is off",
            "WAL archiving is disabled. Point-in-time recovery is not possible.")

    # archive_command
    ac = _pval("archive_command") or ""
    if is_primary and _pval("archive_mode") == "on" and len(ac) < 5:
        add("amber", "No valid archive_command configured",
            "archive_mode is on but archive_command is empty or too short.")
    if "cp " in ac or "rsync " in ac:
        add("amber", "Using cp/rsync in archive_command",
            "This is highly discouraged. Use reliable backup tools for WAL archiving.")

    # shared_preload_libraries
    spl = _pval("shared_preload_libraries") or ""
    if "repmgr" in spl:
        add("amber", "repmgr detected in shared_preload_libraries",
            "Consider a more reliable HA framework.")

    # checkpoint_completion_target
    cct = _pnum("checkpoint_completion_target")
    if cct is not None and cct < 0.7:
        add("amber", f"checkpoint_completion_target = {cct}",
            "Too low. Recommend 0.9 for smoother I/O distribution.")

    # checkpoint_timeout
    ct = _pnum("checkpoint_timeout")
    if ct is not None and ct < 1200:
        add("blue", f"checkpoint_timeout = {int(ct)}s",
            "Consider increasing to 1800s (30 min) to reduce checkpoint frequency.")

    # huge_pages
    hp = _pval("huge_pages")
    sb = _pnum("shared_buffers")
    if hp and hp != "on":
        if sb and sb > 2097152:  # > 16GB shared_buffers
            add("amber", "huge_pages is not enabled",
                f"With shared_buffers of {fmt_bytes(sb * 8192)}, huge_pages is essential for stability.")
        else:
            add("blue", f"huge_pages = {hp}",
                "Recommend enabling huge_pages for better memory management.")

    # huge_pages_status
    if _pval("huge_pages_status") == "off" and hp == "on":
        add("amber", "huge_pages is configured but not active",
            "The OS is not providing huge pages. Check vm.nr_hugepages.")

    # jit
    if _pval("jit") == "on":
        add("blue", "JIT compilation is on",
            "JIT can cause overhead for short queries. Consider disabling for OLTP workloads.")

    # idle_in_transaction_session_timeout
    iitst = _pnum("idle_in_transaction_session_timeout")
    if iitst is not None and iitst == 0:
        add("amber", "idle_in_transaction_session_timeout = 0",
            "No timeout for idle-in-transaction sessions. Recommend setting to 5 minutes.")

    # idle_session_timeout
    ist = _pnum("idle_session_timeout")
    if ist is not None and ist > 0:
        add("amber", f"idle_session_timeout = {int(ist)}ms",
            "This can be dangerous — it disconnects idle sessions including connection pool connections.")

    # track_io_timing
    if _pval("track_io_timing") == "off":
        add("blue", "track_io_timing is off",
            "Enable for better I/O diagnostics in pg_stat_statements and EXPLAIN.")

    # log_hostname
    if _pval("log_hostname") == "on":
        add("blue", "log_hostname is on",
            "DNS lookups for every connection can cause latency. Recommend off.")

    # log_lock_waits
    if _pval("log_lock_waits") == "off":
        add("blue", "log_lock_waits is off",
            "Enable to get visibility into lock contention in logs.")

    # lock_timeout
    if _pval("lock_timeout") == "0":
        add("blue", "lock_timeout = 0",
            "No lock acquisition timeout. Consider setting to 1 minute.")

    # statement_timeout
    if _pval("statement_timeout") == "0":
        add("blue", "statement_timeout = 0",
            "No query execution timeout. Consider setting a reasonable limit.")

    # work_mem
    wm = _pnum("work_mem")
    if wm is not None and wm > 98304:
        add("amber", f"work_mem = {fmt_bytes(wm * 1024)}",
            "Very high work_mem. Each sort/hash can consume this much memory per operation.")

    # max_connections
    mc = _pnum("max_connections")
    if mc is not None and mc > 500:
        add("amber", f"max_connections = {int(mc)}",
            "Very high connection limit. Use connection pooling instead.")

    # synchronous_commit with synchronous_standby_names
    sc = _pval("synchronous_commit")
    ssn = _pval("synchronous_standby_names")
    if sc == "on" and ssn:
        add("amber", "synchronous_commit=on with synchronous_standby_names set",
            "This impacts write performance. Consider synchronous_commit=local if replication lag is acceptable.")

    # wal_compression
    wc = _pval("wal_compression")
    if wc == "off":
        add("blue", "wal_compression is off",
            "Enabling WAL compression reduces I/O at the cost of slight CPU. Recommend lz4 on PG15+.")

    # wal_sync_method
    wsm = _pval("wal_sync_method")
    if wsm and wsm not in ("fdatasync", "open_datasync"):
        add("blue", f"wal_sync_method = {wsm}",
            "fdatasync is generally the best choice for Linux.")

    # wal_sender_timeout
    wst = _pnum("wal_sender_timeout")
    if wst is not None and wst == 0:
        add("blue", "wal_sender_timeout = 0",
            "No timeout for WAL senders. Consider setting to 60s.")

    # random_page_cost
    rpc = _pnum("random_page_cost")
    if rpc is not None and rpc > 1.5:
        add("blue", f"random_page_cost = {rpc}",
            "For SSD storage, recommend 1.1-1.2. Current value may bias planner away from index scans.")

    # default_toast_compression
    dtc = _pval("default_toast_compression")
    if dtc and dtc != "lz4":
        add("blue", f"default_toast_compression = {dtc}",
            "lz4 is faster than pglz. Consider switching if on PG14+.")

    # log_truncate_on_rotation
    if _pval("log_truncate_on_rotation") == "off":
        add("blue", "log_truncate_on_rotation is off",
            "Log files may grow indefinitely. Consider enabling.")

    # server_version EOL + minor version check
    sv = _pval("server_version")
    meta = meta or {}
    pgvers = meta.get("pgvers") or []
    if sv:
        try:
            major = int(sv.split(".")[0])
            if major < 14:
                add("red", f"PostgreSQL {major} is end-of-life",
                    "This version no longer receives security updates. Upgrade immediately.")
            elif major == 14:
                add("amber", f"PostgreSQL {major} is approaching end-of-life",
                    "Plan your upgrade to a supported version.")
            # Minor version staleness check against meta.pgvers
            if pgvers:
                for latest in pgvers:
                    try:
                        lmaj, lmin = latest.split(".")
                        if int(lmaj) == major:
                            smin = int(sv.split(".")[1]) if "." in sv else 0
                            if int(lmin) > smin:
                                add("amber",
                                    f"PostgreSQL {sv} has pending minor updates (latest: {latest})",
                                    "Minor updates contain security fixes and bug fixes. "
                                    "Non-compliance with security updates is a risk.")
                            break
                    except (ValueError, IndexError):
                        pass
        except (ValueError, IndexError):
            pass

    # barman detection in archive_command
    if "barman" in ac:
        add("blue", "Barman backup tool detected in archive_command",
            "Be aware of possible risks if rsync is used as backup_method.")

    # autovacuum_max_workers
    amw = _pnum("autovacuum_max_workers")
    if amw is not None and amw > 3:
        add("blue", f"autovacuum_max_workers = {int(amw)}",
            "Default is 3. Higher values increase parallel vacuum but also resource contention.")

    # autovacuum_vacuum_cost_limit
    avcl = _pnum("autovacuum_vacuum_cost_limit")
    if avcl is not None and (avcl > 800 or avcl == -1):
        add("amber", f"autovacuum_vacuum_cost_limit = {int(avcl)}",
            "High cost limit can cause I/O spikes during autovacuum.")

    # autovacuum_freeze_max_age
    afma = _pnum("autovacuum_freeze_max_age")
    if afma is not None and afma > 800_000_000:
        add("amber", f"autovacuum_freeze_max_age = {int(afma):,}",
            "Very high freeze age increases risk of transaction ID wraparound.")

    # client_connection_check_interval
    ccci = _pnum("client_connection_check_interval")
    if ccci is not None and ccci == 0:
        add("blue", "client_connection_check_interval = 0",
            "Consider setting to detect dead client connections faster.")

    # transaction_timeout
    tt = _pnum("transaction_timeout")
    if tt is not None and tt == 0:
        add("blue", "transaction_timeout = 0",
            "No transaction duration limit. Consider setting a reasonable timeout.")

    # max_standby_archive_delay
    msad = _pnum("max_standby_archive_delay")
    if msad is not None:
        if msad < 30000:
            add("amber", f"max_standby_archive_delay = {int(msad)}ms",
                "Too low. Frequent query cancellations on standby. Recommend 30000ms.")
        elif msad > 300000:
            add("blue", f"max_standby_archive_delay = {int(msad)}ms",
                "High value. Recommend 30000ms for balanced behavior.")

    # max_standby_streaming_delay
    mssd = _pnum("max_standby_streaming_delay")
    if mssd is not None:
        if mssd < 30000:
            add("amber", f"max_standby_streaming_delay = {int(mssd)}ms",
                "Too low. Frequent query cancellations on standby. Recommend 30000ms.")
        elif mssd > 300000:
            add("blue", f"max_standby_streaming_delay = {int(mssd)}ms",
                "High value. Recommend 30000ms for balanced behavior.")

    # max_wal_size vs WAL generation rate
    mws = _pnum("max_wal_size")
    sumry = _f(obj, "sumry", {})
    wal_rate_mb = max(_num(_f(sumry, "f2")) or 0, _num(_f(sumry, "f3")) or 0) / 1048576
    if mws is not None and wal_rate_mb > 0 and mws < wal_rate_mb:
        add("amber", f"max_wal_size = {int(mws)}MB, WAL rate = {wal_rate_mb:.0f}MB/hour",
            "max_wal_size is less than hourly WAL generation. Increase to reduce checkpoint frequency.")
    elif mws is not None and mws < 8192:
        add("blue", f"max_wal_size = {int(mws)}MB",
            "Consider increasing to at least 8GB for production workloads.")

    # min_wal_size
    minws = _pnum("min_wal_size")
    if minws is not None and wal_rate_mb > 0 and minws < wal_rate_mb:
        add("blue", f"min_wal_size = {int(minws)}MB, WAL rate = {wal_rate_mb:.0f}MB/hour",
            "min_wal_size is below WAL generation rate. Consider increasing.")

    # shared_preload_libraries count
    spl_parts = [x.strip() for x in spl.split(",") if x.strip()] if spl else []
    if len(spl_parts) > 2:
        add("blue", f"{len(spl_parts)} shared_preload_libraries loaded",
            f"Libraries: {spl}. Too many preloaded libraries increase startup time and memory.")

    # wal_init_zero / wal_recycle (filesystem-dependent)
    # Without knowing filesystem type, we can only flag non-default values
    wiz = _pval("wal_init_zero")
    if wiz == "off":
        add("blue", "wal_init_zero = off",
            "This is optimal for CoW filesystems (ZFS, Btrfs). If using ext4/xfs, set to on.")
    wr = _pval("wal_recycle")
    if wr == "off":
        add("blue", "wal_recycle = off",
            "This is optimal for CoW filesystems (ZFS, Btrfs). If using ext4/xfs, set to on.")


# --- Sessions breakdown (for the stacked bar) ------------------------------

def session_breakdown(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    sess = _f(obj, "sess", {})
    segs = [
        {"k": "active", "v": _int(_f(sess, "f1")), "cls": "seg-active"},
        {"k": "idle in txn", "v": _int(_f(sess, "f2")), "cls": "seg-idletxn"},
        {"k": "idle", "v": _int(_f(sess, "f3")), "cls": "seg-idle"},
        {"k": "background", "v": _int(_f(sess, "f4")), "cls": "seg-bg"},
        {"k": "parallel", "v": _int(_f(sess, "f5")), "cls": "seg-parallel"},
    ]
    return [s for s in segs if s["v"] > 0]


def database_overview(obj: Dict[str, Any]) -> Dict[str, Any]:
    dbts = _f(obj, "dbts", {})
    clas = _f(obj, "clas", {})
    ns = _f(obj, "ns", []) or []
    return {
        "db_name": _f(dbts, "f1"),
        "stats_reset": _f(dbts, "f2"),
        "days_since_reset": _int(_f(dbts, "f4")),
        "schemas": len(ns),
        "partitioned": _int(_f(clas, "f1")),
        "unlogged": _int(_f(clas, "f2")),
        "in_recovery": bool(_f(obj, "clsr")),
    }


# --- Parameter grouping -----------------------------------------------------

_GROUPS: List[tuple] = [
    ("Memory", ("shared_buffers", "work_mem", "maintenance_work_mem", "effective_cache_size",
                "temp_buffers", "huge_pages", "hash_mem_multiplier", "logical_decoding_work_mem")),
    ("WAL & Checkpoints", ("wal_", "checkpoint", "max_wal", "min_wal", "archive_", "synchronous_commit",
                           "fsync", "full_page_writes", "commit_delay", "commit_siblings")),
    ("Connections & Auth", ("max_connections", "superuser_reserved", "listen_addresses", "port",
                            "ssl", "password", "authentication", "tcp_", "unix_socket")),
    ("Autovacuum", ("autovacuum", "vacuum_", "track_counts")),
    ("Query Planner", ("random_page_cost", "seq_page_cost", "cpu_", "effective_io_concurrency",
                       "default_statistics_target", "enable_", "jit", "from_collapse", "join_collapse",
                       "parallel", "max_parallel", "plan_cache")),
    ("Replication", ("max_replication", "max_wal_senders", "hot_standby", "wal_sender", "wal_receiver",
                     "primary_", "recovery_", "promote_", "synchronous_standby")),
    ("Logging", ("log_", "logging_", "client_min_messages", "syslog", "event_source")),
    ("Background Writer", ("bgwriter_",)),
]

# Common defaults for the most-tuned parameters, used to flag overrides.
_DEFAULTS = {
    "shared_buffers": "16384", "work_mem": "4096", "maintenance_work_mem": "65536",
    "effective_cache_size": "524288", "max_connections": "100", "wal_buffers": "512",
    "max_wal_size": "1024", "min_wal_size": "80", "checkpoint_completion_target": "0.9",
    "random_page_cost": "4", "effective_io_concurrency": "1", "autovacuum": "on",
    "default_statistics_target": "100", "max_worker_processes": "8",
    "max_parallel_workers": "8", "max_parallel_workers_per_gather": "2",
    "synchronous_commit": "on", "log_min_duration_statement": "-1",
}


def _group_of(name: str) -> str:
    for group, prefixes in _GROUPS:
        for p in prefixes:
            if name == p or name.startswith(p):
                return group
    return "Other"


def param_groups(params: Dict[str, str]) -> List[Dict[str, Any]]:
    """Group ``{name: setting}`` into ordered categories with override flags."""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for name in sorted(params):
        setting = params[name]
        g = _group_of(name)
        overridden = name in _DEFAULTS and str(_DEFAULTS[name]) != str(setting)
        buckets.setdefault(g, []).append({
            "name": name, "setting": setting, "overridden": overridden,
        })
    order = [g for g, _ in _GROUPS] + ["Other"]
    return [{"name": g, "params": buckets[g]} for g in order if g in buckets]


# --- Formatting helpers (also exposed as Jinja filters) --------------------

def fmt_bytes(v: Any) -> str:
    n = _num(v)
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} EB"


def fmt_number(v: Any) -> str:
    n = _num(v)
    if n is None:
        return "—"
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.1f}"


def fmt_pct(v: Any) -> str:
    n = _num(v)
    if n is None:
        return "—"
    return f"{n:.1f}%"


def fmt_duration(seconds: Any) -> str:
    n = _num(seconds)
    if n is None:
        return "—"
    n = int(abs(n))
    if n < 60:
        return f"{n}s"
    if n < 3600:
        return f"{n // 60}m {n % 60}s"
    if n < 86400:
        return f"{n // 3600}h {(n % 3600) // 60}m"
    return f"{n // 86400}d {(n % 86400) // 3600}h"


# ===================================================================
# Detail section mappings (from detail_json extracted by docker_runner)
# ===================================================================

def detail_tables(detail: Dict[str, Any], days: float = 1) -> List[Dict[str, Any]]:
    """Map raw table rows into display-ready dicts with per-day rates and tooltips."""
    rows = detail.get("tables") or []
    days = max(days, 1)
    out = []
    for r in rows:
        bloat = _num(r.get("bloat_pct"))
        dead_ratio = _num(r.get("dead_ratio"))
        cache = _num(r.get("cache_hit_pct"))
        ins = _int(r.get("n_tup_ins"))
        upd = _int(r.get("n_tup_upd"))
        dele = _int(r.get("n_tup_del"))
        hot = _int(r.get("n_tup_hot_upd"))
        vac = _int(r.get("vac_nos"))
        idx_count = _int(r.get("idx_count"))
        idx_unused = _int(r.get("idx_unused"))
        has_pk = _int(r.get("has_pk")) > 0
        has_uk = _int(r.get("has_uk", r.get("has_pk", 0)))
        schema = r.get("schema", "")
        oid = r.get("oid", "")
        relfilenode = r.get("relfilenode", "")
        tblspc = _int(r.get("reltablespace"))
        reloptions = r.get("reloptions", "")

        # Build rich tooltip (matches original pg_gather hover)
        tip_parts = [
            f"OID : {oid}",
            f"Schema : {schema}",
            f"Total Indexes: {idx_count}",
            f"Unused Indexes: {idx_unused}",
            f"Primary key: {'Exists' if has_pk else 'MISSING'}",
        ]
        if vac > 0:
            tip_parts.append(f"Vacuums / day : {vac / days:.1f}")
        tip_parts.extend([
            f"Inserts / day : {round(ins / days)}",
            f"Updates / day : {round(upd / days)}",
            f"Deletes / day : {round(dele / days)}",
            f"HOT.updates / day : {round(hot / days)}",
            f"Rel.filename : {relfilenode}",
            f"Tablespace : {'pg_default' if (not tblspc or tblspc < 16384) else tblspc}",
        ])
        if reloptions and reloptions not in ("", "None", "null"):
            tip_parts.append(f"Current Settings : {reloptions}")

        # Recommendations (matching JS logic)
        recs = []
        if upd > 0:
            ff = round(100 - 20 * upd / max(upd + ins, 1) + 20 * upd * hot / max((upd + ins) * max(upd, 1), 1))
            ff = max(10, min(100, ff))
            recs.append(f"FILLFACTOR : {ff}")
        if vac / days > 50:
            threshold = max(500, round((round(upd / days) + round(dele / days)) / 48))
            recs.append(f"AUTOVACUUM : autovacuum_vacuum_threshold = {threshold}, autovacuum_analyze_threshold = {threshold}")
        if recs:
            tip_parts.append("")
            tip_parts.append("RECOMMENDATIONS:")
            tip_parts.extend(recs)

        tip = "\n".join(tip_parts)

        out.append({
            "name": r.get("relname", "?"),
            "schema": schema,
            "kind": {"r": "table", "p": "partitioned", "m": "materialized"}.get(
                r.get("relkind", "r"), "table"),
            "live_tup": fmt_number(r.get("n_live_tup")),
            "dead_tup": fmt_number(r.get("n_dead_tup")),
            "dead_ratio": fmt_pct(dead_ratio) if dead_ratio is not None else "—",
            "dead_warn": dead_ratio is not None and dead_ratio > 0.1,
            "bloat_pct": f"{int(bloat)}%" if bloat is not None else "—",
            "bloat_warn": bloat is not None and bloat > 20 and (_num(r.get("tab_ind_size")) or 0) > 5242880,
            "bloat_bad": bloat is not None and bloat > 50,
            "rel_size": fmt_bytes(r.get("rel_size")),
            "total_size": fmt_bytes(r.get("tab_ind_size")),
            "age": fmt_number(r.get("rel_age")),
            "age_warn": (_num(r.get("rel_age")) or 0) > 200_000_000,
            "cache_hit": fmt_pct(cache) if cache is not None else "—",
            "cache_warn": cache is not None and cache < 90,
            "last_vac": r.get("last_vac"),
            "last_anlyze": r.get("last_anlyze"),
            "vac_count": vac,
            "idx_count": idx_count,
            "idx_unused": idx_unused,
            "has_pk": has_pk,
            "ins": fmt_number(ins),
            "upd": fmt_number(upd),
            "del": fmt_number(dele),
            "large": (_num(r.get("tot_tab_size")) or 0) > 5_000_000_000,
            "idx_bloated": ((_num(r.get("tab_ind_size")) or 0) > 2 * ((_num(r.get("tot_tab_size")) or 1))
                           and (_num(r.get("tot_tab_size")) or 0) > 2_000_000),
            "tip": tip,
        })
    return out


def detail_indexes(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = detail.get("indexes") or []
    out = []
    for r in rows:
        scans = _int(r.get("numscans"))
        cache = _num(r.get("cache_hit_pct"))
        out.append({
            "name": r.get("index_name", "?"),
            "table": r.get("table_name", "?"),
            "schema": r.get("schema", ""),
            "unique": r.get("indisunique", False),
            "primary": r.get("indisprimary", False),
            "valid": r.get("indisvalid", True),
            "scans": fmt_number(scans),
            "unused": scans == 0 and not r.get("indisprimary", False),
            "invalid": not r.get("indisvalid", True),
            "size": fmt_bytes(r.get("size")),
            "size_raw": _int(r.get("size")),
            "large": (_num(r.get("size")) or 0) > 2_000_000_000,
            "cache_hit": fmt_pct(cache) if cache is not None else "—",
            "cache_warn": cache is not None and cache < 50,
            "lastuse": r.get("lastuse"),
        })
    return out


def detail_sessions(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = detail.get("sessions") or []
    out = []
    for r in rows:
        state = r.get("state") or "unknown"
        app = r.get("application_name", "")
        client = r.get("client_addr") or "local"
        ssl = r.get("ssl", False)
        leader = r.get("leader_pid")
        blocked = r.get("blocked_by")
        tip_parts = [f"PID: {r.get('pid')}"]
        tip_parts.append(f"Application: {app}" if app else "No application name")
        tip_parts.append(f"Client: {client}")
        tip_parts.append(f"SSL: {'Yes (' + (r.get('sslversion') or '?') + ')' if ssl else 'No'}")
        if leader:
            tip_parts.append(f"Worker of leader PID: {leader}")
        if r.get("backend_start"):
            tip_parts.append(f"Connected since: {r['backend_start'][:19]}")
        if r.get("xact_start"):
            tip_parts.append(f"Transaction since: {r['xact_start'][:19]}")
        if blocked:
            tip_parts.append(f"BLOCKED by PIDs: {blocked}")
        out.append({
            "pid": r.get("pid"),
            "state": state,
            "state_cls": {"active": "seg-active", "idle in transaction": "seg-idletxn",
                          "idle": "seg-idle"}.get(state, ""),
            "backend_type": r.get("backend_type", ""),
            "wait": f"{r.get('wait_event_type', '') or ''}: {r.get('wait_event', '') or ''}"
                   if r.get("wait_event") else "—",
            "query": r.get("query", ""),
            "client": client,
            "app": app,
            "backend_start": r.get("backend_start"),
            "xact_start": r.get("xact_start"),
            "xmin_age": r.get("xmin_age"),
            "ssl": ssl,
            "blocked_by": blocked,
            "is_blocked": bool(blocked),
            "is_worker": bool(leader),
            "xmin_warn": _num(r.get("xmin_age") if r.get("xmin_age") else None) is not None
                         and (_num(r.get("xmin_age")) or 0) > 20,
            "tip": "\n".join(tip_parts),
        })
    return out


def detail_statements(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = detail.get("statements") or []
    out = []
    for r in rows:
        cache = _num(r.get("cache_hit_pct"))
        out.append({
            "query": r.get("query", ""),
            "calls": fmt_number(r.get("calls")),
            "total_time": fmt_duration(_num(r.get("total_time")) / 1000)
                          if _num(r.get("total_time")) else "—",
            "avg_time": f"{r.get('avg_time_ms', 0):.1f} ms"
                        if r.get("avg_time_ms") is not None else "—",
            "pct_db_time": fmt_pct(r.get("pct_db_time")),
            "cache_hit": fmt_pct(cache) if cache is not None else "—",
            "cache_warn": cache is not None and cache < 50,
            "avg_reads": fmt_number(r.get("avg_reads")),
            "avg_reads_warn": (_num(r.get("avg_reads")) or 0) > 12800,
            "avg_temp_reads": fmt_number(r.get("avg_temp_reads")),
            "has_temp": (_num(r.get("avg_temp_reads")) or 0) > 0,
            "avg_time_warn": (_num(r.get("avg_time_ms")) or 0) > 60000,
            "avg_time_slow": (_num(r.get("avg_time_ms")) or 0) > 10000,
            "high_impact": ((_num(r.get("avg_time_ms")) or 0) > 60000
                           or (_num(r.get("avg_reads")) or 0) > 12800),
        })
    return out


def detail_wait_events(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Returns both the per-event list and the category aggregation."""
    rows = detail.get("wait_events") or []
    total = sum(_int(r.get("cnt")) for r in rows) or 1
    events = []
    categories: Dict[str, int] = {}
    cpu_count = 0
    for r in rows:
        cnt = _int(r.get("cnt"))
        cat = r.get("category") or "Unknown"
        # pg_pid_wait samples that have no wait_event_type are CPU
        if r.get("wait_event") == "CPU" or cat == "Unknown":
            cat = "CPU"
        events.append({
            "event": r.get("wait_event", "?"),
            "category": cat,
            "count": cnt,
            "pct": round(cnt * 100.0 / total, 1),
        })
        categories[cat] = categories.get(cat, 0) + cnt
        if cat == "CPU" and cnt > 100:
            cpu_count = cnt

    cat_list = [{"category": k, "count": v, "pct": round(v * 100.0 / total, 1)}
                for k, v in sorted(categories.items(), key=lambda x: -x[1])]

    # CPU core estimate: JS does count * 1.2 / 2000
    cpu_estimate = round(cpu_count * 1.2 / 2000, 1) if cpu_count > 0 else None

    return {
        "events": events,
        "categories": cat_list,
        "cpu_estimate": cpu_estimate,
        "total": total,
    }


def detail_databases(detail: Dict[str, Any], days: float = 1) -> List[Dict[str, Any]]:
    rows = detail.get("databases") or []
    days = max(days, 1)
    out = []
    for r in rows:
        cache = _num(r.get("cache_hit_pct"))
        age = _num(r.get("age"))
        ins = _int(r.get("tup_inserted"))
        upd = _int(r.get("tup_updated"))
        dele = _int(r.get("tup_deleted"))
        tip_parts = [
            f"Size: {fmt_bytes(r.get('db_size'))}",
            f"Cache hit: {fmt_pct(cache) if cache else '—'}",
            f"Age: {fmt_number(age)}",
            f"Commits: {fmt_number(r.get('xact_commit'))} total, {fmt_number(r.get('commits_per_day'))}/day",
            f"Rollbacks: {fmt_number(r.get('xact_rollback'))}",
            f"Inserts/day: {round(ins / days):,}",
            f"Updates/day: {round(upd / days):,}",
            f"Deletes/day: {round(dele / days):,}",
            f"Temp files: {fmt_number(r.get('temp_files'))} ({fmt_bytes(r.get('temp_bytes'))})",
            f"Stats reset: {r.get('stats_reset', '?')}",
        ]
        out.append({
            "name": r.get("datname", "?"),
            "size": fmt_bytes(r.get("db_size")),
            "encoding": r.get("encod", ""),
            "cache_hit": fmt_pct(cache) if cache is not None else "—",
            "cache_warn": cache is not None and cache < 95,
            "age": fmt_number(age),
            "age_warn": age is not None and age > 200_000_000,
            "mxid_age": fmt_number(r.get("mxidage")),
            "commits_day": fmt_number(r.get("commits_per_day")),
            "rollbacks": fmt_number(r.get("xact_rollback")),
            "temp_files": fmt_number(r.get("temp_files")),
            "temp_bytes": fmt_bytes(r.get("temp_bytes")),
            "tip": "\n".join(tip_parts),
        })
    return out


def detail_replication(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = detail.get("replication") or []
    out = []
    for r in rows:
        out.append({
            "user": r.get("usename", ""),
            "client": r.get("client_addr", ""),
            "state": r.get("state", ""),
            "sync": r.get("sync_state", ""),
            "sent_lag": fmt_bytes(r.get("sent_lag")),
            "write_lag": fmt_bytes(r.get("write_lag")),
            "flush_lag": fmt_bytes(r.get("flush_lag")),
            "replay_lag": fmt_bytes(r.get("replay_lag")),
            "slot": r.get("slot_name", ""),
            "slot_type": r.get("slot_type", ""),
        })
    return out


def detail_bgwriter(detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = detail.get("bgwriter")
    if not raw or not isinstance(raw, dict):
        return None
    forced = _num(raw.get("forced_pct"))
    total_cp = _int(raw.get("checkpoints_timed")) + _int(raw.get("checkpoints_req"))
    avg_interval = _num(raw.get("avg_cp_interval_min"))
    sync_total = _num(raw.get("sync_time_s")) or 0
    write_total = _num(raw.get("write_time_s")) or 0
    per_cp_sync = sync_total / max(total_cp, 1)
    per_cp_write = write_total / max(total_cp, 1)
    buf_backend = _int(raw.get("buffers_backend"))
    buf_clean = _int(raw.get("buffers_clean"))
    buf_cp = _int(raw.get("buffers_checkpoint"))
    total_buf = buf_cp + buf_clean + buf_backend
    cp_pct = round(buf_cp * 100.0 / max(total_buf, 1), 1) if total_buf else 0
    clean_pct = round(buf_clean * 100.0 / max(total_buf, 1), 1) if total_buf else 0
    backend_pct = round(buf_backend * 100.0 / max(total_buf, 1), 1) if total_buf else 0
    return {
        "cp_timed": _int(raw.get("checkpoints_timed")),
        "cp_req": _int(raw.get("checkpoints_req")),
        "forced_pct": fmt_pct(forced) if forced is not None else "—",
        "forced_warn": forced is not None and forced > 10,
        "forced_tip": "More than 10% forced checkpoints. Increase max_wal_size" if forced and forced > 10 else "",
        "write_time": f"{per_cp_write:.3f}s",
        "sync_time": f"{per_cp_sync:.3f}s",
        "sync_warn": per_cp_sync > 0.04,
        "sync_tip": f"Avg sync time {per_cp_sync:.3f}s per checkpoint. Slow storage suspected" if per_cp_sync > 0.04 else "",
        "avg_interval": f"{raw.get('avg_cp_interval_min', '?')} min",
        "interval_warn": avg_interval is not None and avg_interval < 10,
        "interval_tip": f"Checkpoints every {avg_interval} min is too frequent. Increase checkpoint_timeout or max_wal_size" if avg_interval and avg_interval < 10 else "",
        "mb_per_cp": f"{raw.get('mb_per_checkpoint', '?')} MB",
        "buf_checkpoint": fmt_number(buf_cp),
        "cp_pct": f"{cp_pct}%",
        "cp_pct_warn": cp_pct > 50,
        "cp_pct_tip": f"Checkpointer cleaning {cp_pct}% of dirty buffers — taking high load" if cp_pct > 50 else "",
        "buf_clean": fmt_number(buf_clean),
        "clean_pct": f"{clean_pct}%",
        "buf_backend": fmt_number(buf_backend),
        "backend_pct": f"{backend_pct}%",
        "backend_warn": buf_backend > buf_clean and buf_backend > 20,
        "backend_tip": f"Backends cleaning {backend_pct}% of dirty buffers — more than bgwriter ({clean_pct}%). Tune bgwriter_lru_maxpages or increase shared_buffers" if buf_backend > buf_clean and buf_backend > 20 else "",
        "buf_alloc": fmt_number(raw.get("buffers_alloc")),
        "stats_reset": raw.get("stats_reset"),
    }


def detail_hba(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = detail.get("hba") or []
    out = []
    for r in rows:
        method = r.get("method", "")
        out.append({
            "seq": r.get("seq"),
            "type": r.get("typ", ""),
            "database": ", ".join(r.get("db") or []),
            "user": ", ".join(r.get("usr") or []),
            "address": r.get("addr") or "local",
            "addr_warn": r.get("addr") == "all" or r.get("addr") == "0.0.0.0/0",
            "method": method,
            "method_warn": method in ("trust", "password", "md5"),
            "error": r.get("err"),
            "shadowed_by": r.get("shadowed_by"),
            "is_shadowed": bool(r.get("shadowed_by")),
        })
    return out


def detail_extensions(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    _RISKY = {"citus", "tds_fdw", "pglogical"}
    rows = detail.get("extensions") or []
    out = []
    for r in rows:
        name = r.get("extname", "")
        out.append({
            "name": name,
            "version": r.get("extversion", ""),
            "schema": r.get("schema", ""),
            "owner": r.get("owner", ""),
            "risky": name in _RISKY,
        })
    return out


def detail_roles(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = detail.get("roles") or []
    out = []
    for r in rows:
        name = r.get("rolname", "")
        active = _int(r.get("active"))
        idle_txn = _int(r.get("idle_in_txn"))
        idle = _int(r.get("idle"))
        total = _int(r.get("total_conns"))
        tip_parts = [
            f"Role: {name}",
            f"Superuser: {'Yes' if r.get('rolsuper') else 'No'}",
            f"Replication: {'Yes' if r.get('rolreplication') else 'No'}",
            f"Auth: {r.get('auth_method', '?')}",
            f"Connections — Active: {active}, Idle in txn: {idle_txn}, Idle: {idle}, Total: {total}",
            f"Connection limit: {r.get('rolconnlimit', -1) if r.get('rolconnlimit', -1) >= 0 else 'unlimited'}",
        ]
        out.append({
            "name": name,
            "superuser": r.get("rolsuper", False),
            "replication": r.get("rolreplication", False),
            "conn_limit": r.get("rolconnlimit", -1),
            "auth": r.get("auth_method", "?"),
            "auth_warn": r.get("auth_method") == "MD5",
            "active": active,
            "idle_in_txn": idle_txn,
            "total": total,
            "tip": "\n".join(tip_parts),
        })
    return out


def detail_head_info(detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = detail.get("head_info")
    if not raw or not isinstance(raw, dict):
        return None
    uptime = _num(raw.get("uptime_secs"))
    return {
        "pg_version": raw.get("pg_version", ""),
        "started": raw.get("pg_start_ts"),
        "uptime": fmt_duration(uptime) if uptime else "—",
        "recovery": raw.get("recovery", False),
        "timeline": raw.get("timeline"),
        "system_id": raw.get("systemid"),
        "current_wal": raw.get("current_wal", ""),
        "reload_ts": raw.get("reload_ts"),
        "bindir": raw.get("bindir", ""),
        "collected": raw.get("collect_ts"),
        "connstr": raw.get("connstr", ""),
    }


def detail_connections_by_db(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = detail.get("connections_by_db") or []
    out = []
    for r in rows:
        total = _int(r.get("total"))
        ssl = _int(r.get("ssl"))
        non_ssl = _int(r.get("non_ssl"))
        tip_parts = [
            f"Database: {r.get('datname', '?')}",
            f"Active: {_int(r.get('active'))}",
            f"Idle in transaction: {_int(r.get('idle_in_txn'))}",
            f"Idle: {_int(r.get('idle'))}",
            f"Total: {total}",
            f"SSL: {ssl}, Non-SSL: {non_ssl}",
        ]
        if total > 0:
            tip_parts.append(f"SSL ratio: {ssl * 100 // total}%")
        out.append({
            "database": r.get("datname") or "?",
            "active": _int(r.get("active")),
            "idle_in_txn": _int(r.get("idle_in_txn")),
            "idle": _int(r.get("idle")),
            "total": total,
            "ssl": ssl,
            "non_ssl": non_ssl,
            "ssl_warn": (total > 20 and non_ssl / max(total, 1) > 0.5),
            "tip": "\n".join(tip_parts),
        })
    return out


def detail_io_stats(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = detail.get("io_stats") or []
    out = []
    for r in rows:
        out.append({
            "backend": r.get("backend_type", "?"),
            "reads": fmt_number(r.get("reads")),
            "read_bytes": fmt_bytes(r.get("read_bytes")),
            "writes": fmt_number(r.get("writes")),
            "write_bytes": fmt_bytes(r.get("write_bytes")),
            "hits": fmt_number(r.get("hits")),
            "evictions": fmt_number(r.get("evictions")),
            "fsyncs": fmt_number(r.get("fsyncs")),
        })
    return out


def detail_partitioned_tables(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = detail.get("partitioned_tables") or []
    out = []
    for r in rows:
        prune = _num(r.get("fetch_prune_pct"))
        out.append({
            "name": r.get("table_name", "?"),
            "type": r.get("partitioning_type", "?"),
            "partitions": _int(r.get("partition_count")),
            "part_warn": _int(r.get("partition_count")) == 0,
            "total_size": fmt_bytes(r.get("tot_tab_size")),
            "tab_ind_size": fmt_bytes(r.get("tab_ind_size")),
            "prune_pct": fmt_pct(prune) if prune is not None else "—",
            "tip": f"{r.get('table_name', '?')}\nType: {r.get('partitioning_type')}\nPartitions: {_int(r.get('partition_count'))}\nTotal size: {fmt_bytes(r.get('tot_tab_size'))}\nPrune effectiveness: {fmt_pct(prune) if prune else '—'}",
        })
    return out


def build_detail_view(detail: Dict[str, Any], obj: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the complete detail view model from the extracted detail_json."""
    # Get days since stats reset for per-day rate calculations
    days = 1.0
    if obj:
        days = max(float(_int(_f(_f(obj, "dbts", {}), "f4", 1))), 1.0)
    return {
        "head_info": detail_head_info(detail),
        "tables": detail_tables(detail, days=days),
        "partitioned_tables": detail_partitioned_tables(detail),
        "indexes": detail_indexes(detail),
        "sessions": detail_sessions(detail),
        "statements": detail_statements(detail),
        "wait_events": detail_wait_events(detail),
        "databases": detail_databases(detail, days=days),
        "connections_by_db": detail_connections_by_db(detail),
        "replication": detail_replication(detail),
        "bgwriter": detail_bgwriter(detail),
        "io_stats": detail_io_stats(detail),
        "hba": detail_hba(detail),
        "extensions": detail_extensions(detail),
        "roles": detail_roles(detail),
        "has_data": True,
    }
