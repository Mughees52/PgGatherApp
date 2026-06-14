"""Parameter recommendations engine.

Ported from gather_report.sql's paramDespatch handlers and getreccomendation().
Given hardware specs + current parameter values, computes optimal settings.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


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


def compute_recommendations(
    params: Dict[str, str],
    cpus: int = 4,
    memory_gb: int = 8,
    storage: str = "ssd",    # ssd | san | mag
    workload: str = "oltp",  # oltp | olap | mixed
    filesystem: str = "rglr",  # rglr | cow
    wal_rate_bytes: float = 0,
    max_connections_val: Optional[int] = None,
) -> List[Dict[str, str]]:
    """Compute parameter recommendations matching pg_gather's JS logic.

    Returns a list of {param, current, suggest, reason} dicts.
    """
    recs: List[Dict[str, str]] = []

    def _pval(name: str) -> Optional[str]:
        return params.get(name)

    def _pnum(name: str) -> Optional[float]:
        return _num(params.get(name))

    def suggest(name: str, value: str, reason: str) -> None:
        current = params.get(name, "?")
        if str(current) != str(value):
            recs.append({"param": name, "current": current,
                         "suggest": value, "reason": reason})

    # Transaction timeout based on workload
    trns_timeout = {"oltp": 900, "olap": 18000, "mixed": 3600}.get(workload, 900)

    # --- autovacuum ---
    if _pval("autovacuum") != "on":
        suggest("autovacuum", "on", "Autovacuum must be enabled")

    if _pnum("autovacuum_max_workers") and _pnum("autovacuum_max_workers") > 3:
        suggest("autovacuum_max_workers", "3",
                "Default is optimal for most workloads")

    # --- checkpoint_completion_target ---
    cct = _pnum("checkpoint_completion_target")
    if cct is not None and cct < 0.9:
        suggest("checkpoint_completion_target", "0.9",
                "Spreads checkpoint I/O more evenly")

    # --- checkpoint_timeout ---
    ct = _pnum("checkpoint_timeout")
    if ct is not None and ct < 1800:
        suggest("checkpoint_timeout", "1800",
                "30 minutes reduces checkpoint frequency")

    # --- client_connection_check_interval ---
    if _pval("client_connection_check_interval") == "0":
        ccci_val = f"'{trns_timeout // 60}s'"
        suggest("client_connection_check_interval", ccci_val,
                "Detect dead client connections faster")

    # --- default_toast_compression ---
    if _pval("default_toast_compression") and _pval("default_toast_compression") != "lz4":
        suggest("default_toast_compression", "lz4",
                "lz4 is faster than pglz with minimal compression loss")

    # --- huge_pages ---
    if _pval("huge_pages") != "on":
        suggest("huge_pages", "on",
                "Essential for stability with large shared_buffers")

    # --- idle_in_transaction_session_timeout ---
    if _pval("idle_in_transaction_session_timeout") == "0":
        suggest("idle_in_transaction_session_timeout", "'5min'",
                "Prevent sessions from holding locks indefinitely")

    # --- jit ---
    if _pval("jit") == "on":
        suggest("jit", "off",
                "JIT adds overhead for OLTP workloads with short queries")

    # --- log_hostname ---
    if _pval("log_hostname") == "on":
        suggest("log_hostname", "off",
                "DNS lookups on every connection cause latency")

    # --- log_lock_waits ---
    if _pval("log_lock_waits") == "off":
        suggest("log_lock_waits", "on",
                "Visibility into lock contention in logs")

    # --- log_truncate_on_rotation ---
    if _pval("log_truncate_on_rotation") == "off":
        suggest("log_truncate_on_rotation", "on",
                "Prevent log files from growing indefinitely")

    # --- lock_timeout ---
    if _pval("lock_timeout") == "0":
        suggest("lock_timeout", "'1min'",
                "Prevent queries from waiting on locks forever")

    # --- max_connections (CPU-based) ---
    mc = _pnum("max_connections")
    if mc is not None and cpus > 0:
        ideal = 10 * cpus
        if mc > ideal:
            suggest("max_connections", str(ideal),
                    f"Based on {cpus} CPUs. Use connection pooling for more")

    # --- max_standby_archive_delay ---
    msad = _pnum("max_standby_archive_delay")
    if msad is not None and (msad > 300000 or msad < 30000):
        suggest("max_standby_archive_delay", "30000",
                "Balance between replication lag and query cancellation")

    # --- max_standby_streaming_delay ---
    mssd = _pnum("max_standby_streaming_delay")
    if mssd is not None and (mssd > 300000 or mssd < 30000):
        suggest("max_standby_streaming_delay", "30000",
                "Balance between replication lag and query cancellation")

    # --- max_wal_size ---
    mws = _pnum("max_wal_size")
    if mws is not None and wal_rate_bytes > 0:
        ideal_gb = math.ceil(wal_rate_bytes / 1073741824 / 10) * 10
        if ideal_gb > 0 and mws < ideal_gb * 1024:
            suggest("max_wal_size", f"'{ideal_gb}GB'",
                    f"Based on WAL generation rate of {wal_rate_bytes / 1073741824:.1f} GB/hour")
    elif mws is not None and mws < 8192:
        suggest("max_wal_size", "8192",
                "8GB minimum recommended for production")

    # --- min_wal_size ---
    minws = _pnum("min_wal_size")
    if minws is not None and wal_rate_bytes > 0:
        ideal_gb = math.ceil(wal_rate_bytes / 1073741824 / 10) * 10 / 2
        if ideal_gb > 0 and minws < ideal_gb * 1024:
            suggest("min_wal_size", f"'{ideal_gb}GB'",
                    "Half of max_wal_size based on WAL rate")
    elif minws is not None and minws < 2048:
        suggest("min_wal_size", "'2GB'",
                "2GB minimum recommended")

    # --- parallel_leader_participation ---
    plp = _pval("parallel_leader_participation")
    if plp is not None:
        if workload == "oltp" and plp == "off":
            suggest("parallel_leader_participation", "on",
                    "Leader should participate in OLTP for better latency")
        elif workload == "olap" and plp == "on":
            suggest("parallel_leader_participation", "off",
                    "Leader can coordinate workers in OLAP for throughput")

    # --- random_page_cost ---
    rpc = _pnum("random_page_cost")
    if rpc is not None:
        if storage == "ssd" and rpc > 1.2:
            suggest("random_page_cost", "1.1",
                    "SSD storage has near-sequential random read performance")
        elif storage == "san" and rpc > 1.5:
            suggest("random_page_cost", "1.5",
                    "SAN storage typically has good random read")
        elif storage == "mag" and rpc < 4:
            suggest("random_page_cost", "4",
                    "Magnetic storage has high random read latency")

    # --- seq_page_cost ---
    if _pval("seq_page_cost") and _pval("seq_page_cost") != "1":
        suggest("seq_page_cost", "1",
                "Should almost always be 1")

    # --- shared_buffers (25% of RAM) ---
    sb = _pnum("shared_buffers")
    if sb is not None and memory_gb > 0:
        ideal_pages = int(memory_gb * 0.25 * 1024 * 1024 / 8)  # in 8KB pages
        if sb < ideal_pages * 0.8 or sb > ideal_pages * 1.3:
            suggest("shared_buffers", f"'{memory_gb * 0.25}GB'",
                    f"25% of {memory_gb}GB RAM")

    # --- statement_timeout ---
    if _pval("statement_timeout") == "0":
        suggest("statement_timeout",
                f"'{trns_timeout // 2}s'",
                "Half of transaction_timeout as safety net")

    # --- synchronous_commit ---
    sc = _pval("synchronous_commit")
    ssn = _pval("synchronous_standby_names")
    if sc == "on" and ssn:
        suggest("synchronous_commit", "local",
                "Avoids replication lag impacting write latency")

    # --- track_io_timing ---
    if _pval("track_io_timing") == "off":
        suggest("track_io_timing", "on",
                "Essential for I/O diagnostics in EXPLAIN and pg_stat_statements")

    # --- transaction_timeout ---
    if _pval("transaction_timeout") == "0":
        suggest("transaction_timeout", f"'{trns_timeout}s'",
                f"Based on {workload} workload type")

    # --- wal_compression ---
    wc = _pval("wal_compression")
    if wc == "off":
        if cpus > 3:
            suggest("wal_compression", "lz4",
                    "Reduces WAL I/O with minimal CPU cost")
        else:
            suggest("wal_compression", "'on'",
                    "Reduces WAL I/O")

    # --- wal_init_zero / wal_recycle (filesystem dependent) ---
    wiz = _pval("wal_init_zero")
    wr = _pval("wal_recycle")
    if filesystem == "cow":
        if wiz == "on":
            suggest("wal_init_zero", "off",
                    "CoW filesystems (ZFS/Btrfs) don't benefit from pre-zeroing")
        if wr == "on":
            suggest("wal_recycle", "off",
                    "CoW filesystems should create new WAL files, not recycle")
    else:
        if wiz == "off":
            suggest("wal_init_zero", "on",
                    "Regular filesystems benefit from pre-zeroed WAL files")
        if wr == "off":
            suggest("wal_recycle", "on",
                    "Regular filesystems should recycle WAL files")

    # --- wal_sender_timeout ---
    if _pval("wal_sender_timeout") == "0":
        suggest("wal_sender_timeout", "'1min'",
                "Detect dead replication connections")

    # --- wal_sync_method ---
    wsm = _pval("wal_sync_method")
    if wsm and wsm not in ("fdatasync", "open_datasync"):
        suggest("wal_sync_method", "fdatasync",
                "Most reliable sync method for Linux")

    # --- work_mem ---
    wm = _pnum("work_mem")
    conns = max_connections_val or _int(_pnum("max_connections"), 100)
    if wm is not None and memory_gb > 0.2 and conns > 1:
        ideal_kb = min(int(memory_gb * 1024 / (5 * conns) + 4), 64) * 1024
        if wm > 98304:
            suggest("work_mem", f"'{ideal_kb // 1024}MB'",
                    f"Based on {memory_gb}GB RAM / {conns} connections")
        elif wm < ideal_kb * 0.5:
            suggest("work_mem", f"'{ideal_kb // 1024}MB'",
                    f"Calculated from available memory and connections")

    # --- zero_damaged_pages ---
    if _pval("zero_damaged_pages") == "on":
        suggest("zero_damaged_pages", "off",
                "Hides data corruption — must be off in production")

    return recs
