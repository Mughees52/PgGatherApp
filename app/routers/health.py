"""Liveness and Docker readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from ..pipeline.health import docker_health

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/health/docker")
def health_docker() -> dict:
    return docker_health()
