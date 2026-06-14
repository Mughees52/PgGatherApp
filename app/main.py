"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from starlette.routing import Mount

from .config import PROJECT_ROOT
from .continuous import start_scheduler
from .db import init_db
from .jobs import start_worker
from .mcp_server import mcp as mcp_server
from .pipeline.health import docker_health
from .routers import compare, connections, health, reports, serve, timeline
from .templating import templates  # noqa: F401  (ensures env is configured)

log = logging.getLogger("pggather")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_worker()
    start_scheduler()
    h = docker_health()
    if not h["daemon_running"]:
        log.warning("Docker daemon not reachable — collection/generation disabled "
                    "until Docker is running.")
    async with mcp_server.session_manager.run():
        log.info("MCP server started at /mcp")
        yield


app = FastAPI(title="PgGatherApp", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "app" / "static")),
          name="static")

# Mount MCP server for AI clients (Claude Desktop, Cursor, etc.)
app.routes.append(Mount("/mcp", app=mcp_server.streamable_http_app()))

app.include_router(health.router)
app.include_router(connections.router)
app.include_router(reports.router)
app.include_router(serve.router)
app.include_router(compare.router)
app.include_router(timeline.router)


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/reports")
