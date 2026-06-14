"""Connection profile management and collection triggering."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import jobs
from .. import repository as repo
from ..crypto import encrypt
from ..pipeline import docker_runner as dr
from ..pipeline.health import docker_health
from ..templating import templates

router = APIRouter(prefix="/connections")


@router.get("", response_class=HTMLResponse)
def list_connections(request: Request):
    connections = repo.list_connections()
    schedules = {s["connection_id"]: s for s in repo.list_enabled_schedules()}
    # Also get disabled schedules
    for c in connections:
        sched = repo.get_schedule_for_connection(c["id"])
        if sched:
            schedules[c["id"]] = sched
    return templates.TemplateResponse(request, "connections.html", {
        "connections": connections,
        "schedules": schedules,
        "docker": docker_health(), "nav": "connections",
    })


@router.get("/new", response_class=HTMLResponse)
def new_connection(request: Request):
    return templates.TemplateResponse(request, "connection_form.html", { "conn": None, "nav": "connections",
        "docker": docker_health(),
    })


@router.post("")
def create_connection(
    name: str = Form(...), host: str = Form(...), port: int = Form(5432),
    dbname: str = Form(...), username: str = Form(...),
    password: str = Form(""), sslmode: str = Form("prefer"),
):
    repo.create_connection(
        name=name, host=host, port=port, dbname=dbname, username=username,
        password_enc=encrypt(password) if password else None, sslmode=sslmode,
    )
    return RedirectResponse(url="/connections", status_code=303)


@router.get("/{cid}/edit", response_class=HTMLResponse)
def edit_connection(cid: str, request: Request):
    conn = repo.get_connection(cid)
    return templates.TemplateResponse(request, "connection_form.html", { "conn": conn, "nav": "connections",
        "docker": docker_health(),
    })


@router.post("/{cid}/edit")
def update_connection(
    cid: str,
    name: str = Form(...), host: str = Form(...), port: int = Form(5432),
    dbname: str = Form(...), username: str = Form(...),
    password: str = Form(""), sslmode: str = Form("prefer"),
):
    fields = {"name": name, "host": host, "port": port, "dbname": dbname,
              "username": username, "sslmode": sslmode}
    if password:  # blank = keep existing password
        fields["password_enc"] = encrypt(password)
    repo.update_connection(cid, fields=fields)
    return RedirectResponse(url="/connections", status_code=303)


@router.post("/{cid}/delete")
def delete_connection(cid: str):
    repo.delete_connection(cid)
    return RedirectResponse(url="/connections", status_code=303)


@router.post("/{cid}/test")
def test_connection(cid: str) -> JSONResponse:
    from ..crypto import decrypt

    conn = repo.get_connection(cid)
    if conn is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    password = decrypt(conn["password_enc"]) if conn["password_enc"] else None
    target = dr.TargetConn(host=conn["host"], port=conn["port"],
                           dbname=conn["dbname"], username=conn["username"],
                           sslmode=conn["sslmode"])
    ok, msg = dr.test_connection(target, password)
    return JSONResponse({"ok": ok, "error": None if ok else msg})


@router.post("/{cid}/schedule")
def schedule(cid: str, interval: int = Form(60), action: str = Form("enable")):
    if action == "disable":
        repo.toggle_schedule(cid, False)
    elif action == "delete":
        repo.delete_schedule(cid)
    elif action == "enable":
        existing = repo.get_schedule_for_connection(cid)
        if existing:
            repo.toggle_schedule(cid, True)
        else:
            repo.create_schedule(cid, interval_sec=interval)
    return RedirectResponse(url="/connections", status_code=303)


@router.post("/{cid}/collect")
def collect(cid: str):
    conn = repo.get_connection(cid)
    if conn is None:
        return RedirectResponse(url="/connections", status_code=303)
    rid = repo.create_report(source="collected", status="queued", connection_id=cid)
    jobs.enqueue(rid)
    return RedirectResponse(url=f"/reports/{rid}", status_code=303)
