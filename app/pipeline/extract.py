"""Extract metadata from a gather TSV and from a generated report HTML.

The TSV header is the primary, most reliable source for server identity (it works
even if report generation later fails). The report HTML's embedded ``obj``/``meta``
JSON is richer and used for the compare feature.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# psql \conninfo line, e.g.:
#   You are connected to database "postgres" as user "postgres" on host "db" (address "1.2.3.4") at port "5432".
_CONNINFO_DB = re.compile(r'database "([^"]+)"')
_CONNINFO_HOST = re.compile(r'on host "([^"]+)"')
_CONNINFO_PORT = re.compile(r'at port "([^"]+)"')

_PG_MAJOR = re.compile(r"PostgreSQL (\d+)")
_ENGINE_VER = re.compile(r"pg_gather\.V(\d+)")

_COPY_HEADER = re.compile(r"^COPY\s+(\w+)\s*\(([^)]*)\)\s+FROM stdin;", re.IGNORECASE)
_COPY_HEADER_NOCOLS = re.compile(r"^COPY\s+(\w+)\s+FROM stdin;", re.IGNORECASE)


@dataclass
class TsvMetadata:
    collected_at: Optional[str] = None
    pg_version: Optional[str] = None
    pg_version_num: Optional[int] = None
    engine_ver: Optional[str] = None
    system_id: Optional[str] = None
    srvr_host: Optional[str] = None
    srvr_port: Optional[str] = None
    srvr_db: Optional[str] = None
    raw: Dict[str, str] = field(default_factory=dict)


def _open_text(path: Path):
    """Open a possibly-gzipped TSV as a text stream."""
    with path.open("rb") as fh:
        magic = fh.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def _clean(value: str) -> Optional[str]:
    value = value.strip()
    if value in ("", "\\N"):
        return None
    return value


def parse_tsv_metadata(path: Path, max_lines: int = 400) -> TsvMetadata:
    """Parse server identity fields from the head of a gather TSV file."""
    meta = TsvMetadata()
    pg_cols: Optional[List[str]] = None
    expect_pg_row = False
    expect_srvr = False

    with _open_text(path) as stream:
        for i, line in enumerate(stream):
            if i >= max_lines:
                break
            line = line.rstrip("\n")

            # pg_srvr block holds the \conninfo line.
            if expect_srvr:
                if line.startswith("\\.") or not line.strip():
                    expect_srvr = False
                else:
                    meta.srvr_db = meta.srvr_db or _g(_CONNINFO_DB, line)
                    meta.srvr_host = meta.srvr_host or _g(_CONNINFO_HOST, line)
                    meta.srvr_port = meta.srvr_port or _g(_CONNINFO_PORT, line)
                    expect_srvr = False
                continue

            # The single pg_gather data row follows its COPY header.
            if expect_pg_row:
                expect_pg_row = False
                if pg_cols and not line.startswith("\\."):
                    values = line.split("\t")
                    row = dict(zip(pg_cols, values))
                    meta.raw = {k: v for k, v in row.items()}
                    meta.collected_at = _clean(row.get("collect_ts", ""))
                    meta.system_id = _clean(row.get("systemid", ""))
                    ver_str = _clean(row.get("ver", ""))  # full version() text
                    if ver_str:
                        meta.pg_version = ver_str
                        m = _PG_MAJOR.search(ver_str)
                        if m:
                            meta.pg_version_num = int(m.group(1))
                    usr = row.get("usr", "")
                    em = _ENGINE_VER.search(usr)
                    if em:
                        meta.engine_ver = em.group(1)
                continue

            m = _COPY_HEADER.match(line)
            if m:
                table, cols = m.group(1).lower(), m.group(2)
                if table == "pg_gather":
                    pg_cols = [c.strip() for c in cols.split(",")]
                    expect_pg_row = True
                continue

            mn = _COPY_HEADER_NOCOLS.match(line)
            if mn and mn.group(1).lower() == "pg_srvr":
                expect_srvr = True
                continue

    return meta


def _g(pattern: "re.Pattern[str]", text: str) -> Optional[str]:
    m = pattern.search(text)
    return m.group(1) if m else None


# --- Report HTML extraction -------------------------------------------------

_OBJ_LINE = re.compile(r"^\s*obj\s*=\s*(\{.*\})\s*;?\s*$")
_META_LINE = re.compile(r"^\s*meta\s*=\s*(\{.*\})\s*;?\s*$")


@dataclass
class ReportJson:
    obj: Optional[dict] = None
    meta: Optional[dict] = None


def parse_report_html(html: str) -> ReportJson:
    """Pull the embedded ``obj`` and ``meta`` JSON objects from a report's <script>."""
    result = ReportJson()
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        scripts = "\n".join(s.get_text() for s in soup.find_all("script"))
    except Exception:
        scripts = html  # fall back to scanning the whole document

    for line in scripts.splitlines():
        if result.obj is None:
            m = _OBJ_LINE.match(line)
            if m:
                result.obj = _try_json(m.group(1))
                continue
        if result.meta is None:
            m = _META_LINE.match(line)
            if m:
                result.meta = _try_json(m.group(1))
    return result


def _try_json(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
