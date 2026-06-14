"""Application configuration and resolved filesystem paths.

A single ``Settings`` instance is created at import time and shared across the
app. Paths are resolved relative to the project root so the app works no matter
which directory uvicorn is launched from.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = the directory containing the ``app`` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PGGATHER_", env_file=".env")

    # Storage / database locations (overridable via PGGATHER_* env vars).
    data_dir: Path = PROJECT_ROOT / "storage"
    db_path: Path = PROJECT_ROOT / "app.db"
    vendor_dir: Path = PROJECT_ROOT / "vendor" / "pg_gather"

    # Docker engine container that runs both collection psql and report generation.
    docker_image: str = "postgres:17"
    container_name: str = "pg_gather"

    # Timeouts (seconds).
    collect_timeout: int = 300
    generate_timeout: int = 600
    container_ready_timeout: int = 60

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def gather_sql(self) -> Path:
        return self.vendor_dir / "gather.sql"

    @property
    def gather_schema_sql(self) -> Path:
        return self.vendor_dir / "gather_schema.sql"

    @property
    def gather_report_sql(self) -> Path:
        return self.vendor_dir / "gather_report.sql"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


settings = get_settings()


def engine_version() -> str:
    """Read the vendored pg_gather engine version from VERSION file."""
    vfile = settings.vendor_dir / "VERSION"
    if vfile.exists():
        for line in vfile.read_text().splitlines():
            if line.startswith("engine_ver:"):
                return line.split(":", 1)[1].strip()
    return "?"
