"""Docker integration tests — require a running Docker daemon and pg_src container.

Run with: pytest tests/test_docker_integration.py -m docker -v

These tests:
1. Ensure the pg_gather engine container works
2. Test real collection against a pg_src database
3. Verify report generation produces valid HTML
4. Verify detail_json extraction produces data
5. Test localhost rewrite for container connectivity
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

# Skip all tests in this module if Docker is not available
pytestmark = pytest.mark.docker


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _container_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name],
        capture_output=True, timeout=10,
    )
    return result.returncode == 0 and b"running" in result.stdout


if not _docker_available():
    pytest.skip("Docker not available", allow_module_level=True)


@pytest.fixture(scope="module")
def pg_src():
    """Ensure a pg_src container is running for collection tests."""
    name = "pg_src"
    if _container_running(name):
        yield name
        return

    # Start a fresh one
    subprocess.run(
        ["docker", "run", "--name", name, "-d",
         "-e", "POSTGRES_PASSWORD=secret",
         "--add-host", "host.docker.internal:host-gateway",
         "postgres:16"],
        capture_output=True, timeout=120,
    )

    # Wait for readiness
    deadline = time.time() + 30
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "exec", name, "psql", "-U", "postgres", "-tAc", "select 1"],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        pytest.skip("pg_src container did not become ready")

    yield name
    # Don't clean up — leave it for re-use


class TestEngineContainer:
    """Tests for the pg_gather engine container."""

    def test_ensure_container_starts(self):
        from app.pipeline.docker_runner import ensure_container
        ensure_container(lambda m: None)

    def test_container_state(self):
        from app.pipeline.docker_runner import container_state
        state = container_state()
        assert state == "running"


class TestCollection:
    """Tests for real data collection from pg_src."""

    def test_test_connection(self, pg_src):
        from app.pipeline.docker_runner import TargetConn, test_connection
        target = TargetConn(
            host="localhost", port=5432,
            dbname="postgres", username="postgres", sslmode="prefer",
        )
        ok, msg = test_connection(target, "secret")
        assert ok, f"Connection test failed: {msg}"

    def test_collect_produces_tsv(self, pg_src, tmp_path):
        from app.pipeline.docker_runner import TargetConn, collect
        target = TargetConn(
            host="localhost", port=5432,
            dbname="postgres", username="postgres", sslmode="prefer",
        )
        out_path = tmp_path / "out.tsv"
        collect(target, "secret", out_path, lambda m: None)
        assert out_path.exists()
        size = out_path.stat().st_size
        assert size > 1000, f"TSV too small: {size} bytes"

    def test_localhost_rewrite(self):
        from app.pipeline.docker_runner import TargetConn
        target = TargetConn(host="localhost", port=5432,
                            dbname="postgres", username="postgres")
        assert target.container_host() == "host.docker.internal"

        target2 = TargetConn(host="10.0.0.1", port=5432,
                             dbname="postgres", username="postgres")
        assert target2.container_host() == "10.0.0.1"


class TestGeneration:
    """Tests for report generation."""

    def test_generate_produces_html(self, pg_src, tmp_path):
        from app.pipeline.docker_runner import TargetConn, collect, generate
        target = TargetConn(
            host="localhost", port=5432,
            dbname="postgres", username="postgres", sslmode="prefer",
        )
        tsv_path = tmp_path / "out.tsv"
        collect(target, "secret", tsv_path, lambda m: None)
        tsv_bytes = tsv_path.read_bytes()

        html_path = tmp_path / "report.html"
        generate(tsv_bytes, html_path, lambda m: None)
        assert html_path.exists()
        size = html_path.stat().st_size
        assert size > 100_000, f"HTML too small: {size} bytes (expected >100KB)"
        content = html_path.read_text("utf-8", "replace")
        assert "obj=" in content, "HTML missing embedded obj JSON"

    def test_extract_detail_produces_data(self, pg_src, tmp_path):
        from app.pipeline.docker_runner import (
            TargetConn, collect, generate, extract_detail,
        )
        target = TargetConn(
            host="localhost", port=5432,
            dbname="postgres", username="postgres", sslmode="prefer",
        )
        tsv_path = tmp_path / "out.tsv"
        collect(target, "secret", tsv_path, lambda m: None)
        tsv_bytes = tsv_path.read_bytes()

        html_path = tmp_path / "report.html"
        generate(tsv_bytes, html_path, lambda m: None)

        import json
        result = extract_detail(lambda m: None)
        assert result is not None
        data = json.loads(result)
        assert "sessions" in data
        assert "wait_events" in data
        assert "bgwriter" in data
        assert "hba" in data
        assert "roles" in data
        assert "databases" in data
        assert len(data.get("sessions", [])) > 0
