"""Docker health reporting for preflight and the /health/docker endpoint."""

from __future__ import annotations

from typing import Dict

from . import docker_runner as dr


def docker_health() -> Dict[str, object]:
    installed = _binary_present()
    daemon = dr.docker_available() if installed else False
    image = dr.image_present() if daemon else False
    state = dr.container_state() if daemon else "unknown"
    return {
        "docker_installed": installed,
        "daemon_running": daemon,
        "image_present": image,
        "container_state": state,
        "image": dr.settings.docker_image,
        "ready": daemon,  # generation/collection require only a reachable daemon
    }


def _binary_present() -> bool:
    import shutil

    return shutil.which("docker") is not None
