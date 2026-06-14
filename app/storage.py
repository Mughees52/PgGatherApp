"""Filesystem blob storage for per-report artifacts.

Layout: ``storage/reports/<report_id>/{raw.tsv|raw.tsv.gz, report.html, job.log}``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import settings


def report_dir(report_id: str) -> Path:
    d = settings.reports_dir / report_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def tsv_path(report_id: str, gzip: bool) -> Path:
    name = "raw.tsv.gz" if gzip else "raw.tsv"
    return report_dir(report_id) / name


def html_path(report_id: str) -> Path:
    return report_dir(report_id) / "report.html"


def log_path(report_id: str) -> Path:
    return report_dir(report_id) / "job.log"


def append_log(report_id: str, message: str) -> None:
    with log_path(report_id).open("a", encoding="utf-8") as fh:
        fh.write(message.rstrip("\n") + "\n")


def delete_report_dir(report_id: str) -> None:
    d = settings.reports_dir / report_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
