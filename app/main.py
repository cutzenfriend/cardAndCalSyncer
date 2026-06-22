"""CaCs – cardAndCalSyncer. Web-UI + Scheduler rund um vdirsyncer."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import COOKIE_NAME, REMEMBER_AGE, Auth
from db import Database
from runner import ROLLING_LOG, Runner
from store import ConfigStore

APP_NAME = "CaCs"
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cacs")

DATA_DIR = os.environ.get("CACS_DATA", os.environ.get("CALSYNC_DATA", "/data"))
DB_PATH = os.path.join(DATA_DIR, "cacs.db")
CONFIG_PATH = os.path.join(DATA_DIR, "cacs.json")
APP_DIR = os.path.dirname(os.path.abspath(__file__))

os.makedirs(DATA_DIR, exist_ok=True)
db = Database(DB_PATH)
store = ConfigStore(CONFIG_PATH)
auth = Auth(store)
runner = Runner(db, store)
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))
templates.env.globals["app_name"] = APP_NAME

_bg_tasks: set[asyncio.Task] = set()


class SchedulerState:
    next_run_at: float | None = None


sched = SchedulerState()


def _ready_to_sync(cfg: dict[str, Any]) -> bool:
    return any(p.get("collections") for p in cfg["pairs"].values())


async def _scheduler() -> None:
    await asyncio.sleep(5)
    while True:
        cfg = store.get()
        interval = max(30, int(cfg.get("interval_seconds", 300)))
        if cfg.get("sync_enabled", True) and _ready_to_sync(cfg):
            try:
                await runner.run_sync(trigger="scheduled")
            except Exception:
                log.exception("Scheduler-Sync fehlgeschlagen")
        sched.next_run_at = time.time() + interval
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # optionales Bootstrap eines Admins per Env (headless)
    if not auth.is_configured():
        u, p = os.environ.get("ADMIN_USERNAME"), os.environ.get("ADMIN_PASSWORD")
        if u and p:
            auth.set_admin(u, p)
            log.info("Admin aus ADMIN_USERNAME/ADMIN_PASSWORD angelegt")
    task = asyncio.create_task(_scheduler())
    _bg_tasks.add(task)
    log.info("%s gestartet – Daten: %s", APP_NAME, DATA_DIR)
    yield
    task.cancel()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")


# --- Auth-Guards -----------------------------------------------------------
class NeedsSetup(Exception): ...
class NeedsLogin(Exception): ...


@app.exception_handler(NeedsSetup)
async def _h_setup(request: Request, exc: NeedsSetup):
    return RedirectResponse("/setup", status_code=303)


@app.exception_handler(NeedsLogin)
async def _h_login(request: Request, exc: NeedsLogin):
    return RedirectResponse("/login", status_code=303)


def current_user(request: Request) -> str | None:
    return auth.verify_token(request.cookies.get(COOKIE_NAME))


def require_page(request: Request) -> str:
    if not auth.is_configured():
        raise NeedsSetup()
    u = current_user(request)
    if not u:
        raise NeedsLogin()
    return u


def require_api(request: Request) -> str:
    if not auth.is_configured():
        raise HTTPException(403, "Setup erforderlich")
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Nicht angemeldet")
    return u


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _set_session(resp: Response, username: str, remember: bool) -> None:
    token = auth.make_token(username, remember)
    kw: dict[str, Any] = dict(httponly=True, samesite="lax", path="/")
    if remember:
        kw["max_age"] = REMEMBER_AGE
    resp.set_cookie(COOKIE_NAME, token, **kw)


def _status_payload() -> dict[str, Any]:
    cfg = store.get()
    last = db.last_run("sync")
    eta = max(0, int(sched.next_run_at - time.time())) if sched.next_run_at else None
    return {
        "busy": runner.busy,
        "sync_enabled": cfg.get("sync_enabled", True),
        "interval_seconds": cfg.get("interval_seconds", 300),
        "next_run_in": eta,
        "ready": _ready_to_sync(cfg),
        "totals": db.stats(),
        "last_run": dict(last) if last else None,
    }


# --- Setup & Login ---------------------------------------------------------
@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if auth.is_configured():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("setup.html", {"request": request})


@app.post("/api/setup")
async def api_setup(request: Request):
    if auth.is_configured():
        raise HTTPException(409, "Bereits eingerichtet")
    body = await request.json()
    u, p = (body.get("username") or "").strip(), body.get("password") or ""
    if len(u) < 2 or len(p) < 6:
        raise HTTPException(400, "Benutzername min. 2, Passwort min. 6 Zeichen")
    auth.set_admin(u, p)
    resp = JSONResponse({"ok": True})
    _set_session(resp, u, remember=False)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not auth.is_configured():
        return RedirectResponse("/setup", status_code=303)
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    u, p = (body.get("username") or "").strip(), body.get("password") or ""
    if not auth.verify_password(u, p):
        raise HTTPException(401, "Benutzername oder Passwort falsch")
    resp = JSONResponse({"ok": True})
    _set_session(resp, u, remember=bool(body.get("remember")))
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# --- HTML-Seiten -----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_page)):
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "status": _status_payload(),
        "runs": [dict(r) for r in db.list_runs(limit=10, kind="sync")],
        "activities": [dict(a) for a in db.recent_activities(limit=15)],
    })


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, _: str = Depends(require_page)):
    return templates.TemplateResponse("runs.html", {
        "request": request, "runs": [dict(r) for r in db.list_runs(limit=100)]})


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, _: str = Depends(require_page)):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run nicht gefunden")
    return templates.TemplateResponse("run_detail.html", {
        "request": request, "run": dict(run),
        "activities": [dict(a) for a in db.run_activities(run_id)]})


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, action: str | None = None,
                  pair: str | None = None, _: str = Depends(require_page)):
    pairs = [p.get("name") or pid for pid, p in store.get()["pairs"].items()]
    return templates.TemplateResponse("activity.html", {
        "request": request,
        "activities": [dict(a) for a in db.recent_activities(limit=300, action=action, pair=pair)],
        "f_action": action or "", "f_pair": pair or "", "pairs": pairs})


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, _: str = Depends(require_page)):
    return templates.TemplateResponse("logs.html", {"request": request})


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request, _: str = Depends(require_page)):
    return templates.TemplateResponse("config.html", {
        "request": request, "config": store.public_view()})


# --- API: Status/Config ----------------------------------------------------
@app.get("/api/status")
def api_status(_: str = Depends(require_api)):
    return _status_payload()


@app.get("/api/config")
def api_get_config(_: str = Depends(require_api)):
    return store.public_view()


@app.post("/api/config")
async def api_save_config(request: Request, _: str = Depends(require_api)):
    body = await request.json()
    allowed = {k: body[k] for k in ("interval_seconds", "sync_enabled", "alerts") if k in body}
    cfg = store.save(allowed)
    return {"ok": True, "ready": _ready_to_sync(cfg)}


# --- API: Accounts ----------------------------------------------------------
def _apply_secrets(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    from store import SECRET_FIELDS
    for fld, val in incoming.items():
        if fld in SECRET_FIELDS and val == "__SET__":
            continue  # maskiert -> bestehenden Wert behalten
        target[fld] = val


@app.post("/api/accounts")
async def api_save_account(request: Request, _: str = Depends(require_api)):
    body = await request.json()
    accounts = store.get()["accounts"]
    acc_id = body.get("id") or secrets.token_hex(4)
    acc = accounts.get(acc_id, {})
    acc.setdefault("kind", body.get("kind", "caldav"))
    _apply_secrets(acc, {k: v for k, v in body.items() if k != "id"})
    accounts[acc_id] = acc
    store.replace("accounts", accounts)
    return {"ok": True, "id": acc_id, "config": store.public_view()}


@app.delete("/api/accounts/{acc_id}")
def api_del_account(acc_id: str, _: str = Depends(require_api)):
    cfg = store.get()
    used = [p.get("name") or pid for pid, p in cfg["pairs"].items()
            if acc_id in (p.get("a"), p.get("b"))]
    if used:
        raise HTTPException(409, f"Account wird von Paar(en) genutzt: {', '.join(used)}")
    accounts = cfg["accounts"]
    accounts.pop(acc_id, None)
    store.replace("accounts", accounts)
    return {"ok": True, "config": store.public_view()}


# --- API: Pairs -------------------------------------------------------------
@app.post("/api/pairs")
async def api_save_pair(request: Request, _: str = Depends(require_api)):
    body = await request.json()
    cfg = store.get()
    a, b = body.get("a"), body.get("b")
    if a not in cfg["accounts"] or b not in cfg["accounts"]:
        raise HTTPException(400, "Unbekannter Account")
    if a == b:
        raise HTTPException(400, "A und B müssen verschieden sein")
    pairs = cfg["pairs"]
    pid = body.get("id") or secrets.token_hex(4)
    pairs[pid] = {
        "name": body.get("name") or pid,
        "service": body.get("service", "calendar"),
        "a": a, "b": b,
        "conflict_resolution": body.get("conflict_resolution", "a wins"),
        "collections": body.get("collections", pairs.get(pid, {}).get("collections", [])),
    }
    store.replace("pairs", pairs)
    if pairs[pid]["collections"]:
        _spawn(runner.discover(pid, list_all=False))  # Mappings registrieren
    return {"ok": True, "id": pid, "config": store.public_view()}


@app.delete("/api/pairs/{pair_id}")
def api_del_pair(pair_id: str, _: str = Depends(require_api)):
    pairs = store.get()["pairs"]
    pairs.pop(pair_id, None)
    store.replace("pairs", pairs)
    return {"ok": True, "config": store.public_view()}


# --- API: Aktionen ----------------------------------------------------------
@app.post("/api/discover")
async def api_discover(request: Request, _: str = Depends(require_api)):
    body = await request.json()
    pair = body.get("pair")
    if pair not in store.get()["pairs"]:
        raise HTTPException(400, "Unbekanntes Paar")
    if runner.busy:
        raise HTTPException(409, "Es läuft gerade ein anderer Vorgang")
    return await runner.discover(pair, list_all=True)


@app.post("/api/sync")
async def api_sync(request: Request, _: str = Depends(require_api)):
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    if runner.busy:
        raise HTTPException(409, "Sync läuft bereits")
    _spawn(runner.run_sync(pair=body.get("pair") or None, trigger="manual"))
    return {"ok": True, "started": True}


@app.post("/api/toggle")
async def api_toggle(request: Request, _: str = Depends(require_api)):
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    store.save({"sync_enabled": enabled})
    return {"ok": True, "sync_enabled": enabled}


@app.get("/api/logs", response_class=PlainTextResponse)
def api_logs(lines: int = 400, _: str = Depends(require_api)):
    if not os.path.exists(ROLLING_LOG):
        return "Noch keine Logs."
    with open(ROLLING_LOG, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-lines:])


@app.get("/health")
def health():
    return JSONResponse({"status": "ok"})
