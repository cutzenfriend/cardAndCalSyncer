"""CaCs – cardAndCalSyncer. Web UI + scheduler around vdirsyncer."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import confgen

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
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# vdirsyncer uses CalDAV (calendar scope) and CardDAV (carddav scope) for Google.
GOOGLE_SCOPES = ("https://www.googleapis.com/auth/calendar "
                 "https://www.googleapis.com/auth/carddav")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cacs")

DATA_DIR = os.environ.get("CACS_DATA", os.environ.get("CALSYNC_DATA", "/data"))
DB_PATH = os.path.join(DATA_DIR, "cacs.db")
CONFIG_PATH = os.path.join(DATA_DIR, "cacs.json")
APP_DIR = os.path.dirname(os.path.abspath(__file__))

os.makedirs(DATA_DIR, exist_ok=True)
# Google returns the full granted scope on refresh; relax oauthlib's scope check
# for in-process fetches (enrichment, clear).
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
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
                await runner.run_sync_all(trigger="scheduled")
            except Exception:
                log.exception("Scheduled sync failed")
        sched.next_run_at = time.time() + interval
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # optional admin bootstrap via env (headless)
    if not auth.is_configured():
        u, p = os.environ.get("ADMIN_USERNAME"), os.environ.get("ADMIN_PASSWORD")
        if u and p:
            auth.set_admin(u, p)
            log.info("Admin created from ADMIN_USERNAME/ADMIN_PASSWORD")
    task = asyncio.create_task(_scheduler())
    _bg_tasks.add(task)
    log.info("%s started – data dir: %s", APP_NAME, DATA_DIR)
    yield
    task.cancel()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")


# --- Auth guards -----------------------------------------------------------
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
        raise HTTPException(403, "Setup required")
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Not signed in")
    return u


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _require_idle() -> None:
    if runner.busy:
        secs = int(time.time() - runner.busy_since) if runner.busy_since else 0
        raise HTTPException(409, f"Another operation is running ({secs}s). "
                                 "Use Stop (top right) to cancel it, then retry.")


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
    busy_for = int(time.time() - runner.busy_since) if runner.busy and runner.busy_since else None
    return {
        "busy": runner.busy,
        "busy_for": busy_for,
        "sync_enabled": cfg.get("sync_enabled", True),
        "interval_seconds": cfg.get("interval_seconds", 300),
        "next_run_in": eta,
        "ready": _ready_to_sync(cfg),
        "totals": db.stats(),
        "last_run": dict(last) if last else None,
    }


# --- Setup & login ---------------------------------------------------------
@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if auth.is_configured():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html")


@app.post("/api/setup")
async def api_setup(request: Request):
    if auth.is_configured():
        raise HTTPException(409, "Already configured")
    body = await request.json()
    u, p = (body.get("username") or "").strip(), body.get("password") or ""
    if len(u) < 2 or len(p) < 6:
        raise HTTPException(400, "Username min. 2, password min. 6 characters")
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
    return templates.TemplateResponse(request, "login.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    u, p = (body.get("username") or "").strip(), body.get("password") or ""
    if not auth.verify_password(u, p):
        raise HTTPException(401, "Wrong username or password")
    resp = JSONResponse({"ok": True})
    _set_session(resp, u, remember=bool(body.get("remember")))
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# --- Google OAuth (in-app, one click) --------------------------------------
def _google_redirect_uri(request: Request) -> str:
    base = (store.get().get("base_url") or "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return base + "/oauth/google/callback"


def _write_google_token(acc_id: str, token: dict[str, Any]) -> None:
    """Write a vdirsyncer-compatible token file for both calendar and contacts."""
    os.makedirs(confgen.TOKEN_DIR, exist_ok=True)
    blob = {
        "access_token": token.get("access_token"),
        "token_type": token.get("token_type", "Bearer"),
        "refresh_token": token.get("refresh_token"),
        "scope": token["scope"].split() if isinstance(token.get("scope"), str)
        else token.get("scope"),
        "expires_in": token.get("expires_in"),
        "expires_at": time.time() + float(token.get("expires_in", 3600)),
    }
    for svc in ("cal", "card"):
        path = os.path.join(confgen.TOKEN_DIR, f"acc_{acc_id}_{svc}.token")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, path)


@app.get("/oauth/google/start")
def google_oauth_start(account: str, request: Request, _: str = Depends(require_page)):
    acc = store.account(account)
    if not acc or acc.get("kind") != "google":
        raise HTTPException(400, "Unknown Google account")
    if not acc.get("client_id") or not acc.get("client_secret"):
        raise HTTPException(400, "Set Client ID and Client secret first")
    params = urllib.parse.urlencode({
        "client_id": acc["client_id"],
        "redirect_uri": _google_redirect_uri(request),
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": auth.sign({"acc": account}),
    })
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}", status_code=303)


def _google_complete(request: Request, acc_id: str, code: str) -> None:
    """Exchange an auth code for tokens, store them, mark the account connected.

    Raises ValueError('unknown_account'|'no_refresh_token') for handled cases.
    """
    acc = store.account(acc_id)
    if not acc or acc.get("kind") != "google":
        raise ValueError("unknown_account")
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": acc["client_id"],
        "client_secret": acc["client_secret"],
        "redirect_uri": _google_redirect_uri(request),
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        GOOGLE_TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        token = json.load(r)
    if not token.get("refresh_token"):
        raise ValueError("no_refresh_token")
    _write_google_token(acc_id, token)
    accounts = store.get()["accounts"]
    accounts[acc_id]["google_connected"] = True
    store.replace("accounts", accounts)
    log.info("Google account %s connected", acc_id)


def _parse_oauth_response(pasted: str) -> tuple[str | None, str | None]:
    """Extract (code, state) from a pasted full redirect URL or query string."""
    pasted = (pasted or "").strip()
    query = urllib.parse.urlparse(pasted).query or pasted
    params = urllib.parse.parse_qs(query)
    return (params.get("code", [None])[0], params.get("state", [None])[0])


@app.get("/oauth/google/callback")
def google_oauth_callback(request: Request, _: str = Depends(require_page),
                          code: str | None = None, state: str | None = None,
                          error: str | None = None):
    if error:
        return RedirectResponse(f"/config?google=error&msg={urllib.parse.quote(error)}", status_code=303)
    data = auth.unsign(state)
    if not data or "acc" not in data or not code:
        return RedirectResponse("/config?google=error&msg=invalid_state", status_code=303)
    try:
        _google_complete(request, data["acc"], code)
    except ValueError as e:
        return RedirectResponse(f"/config?google=error&msg={urllib.parse.quote(str(e))}", status_code=303)
    except Exception as exc:
        log.exception("Google OAuth exchange failed")
        return RedirectResponse(f"/config?google=error&msg={urllib.parse.quote(str(exc)[:120])}", status_code=303)
    return RedirectResponse("/config?google=connected", status_code=303)


@app.post("/api/google/finish")
async def google_finish(request: Request, _: str = Depends(require_api)):
    """Complete the connect by pasting the redirected URL (no-redirect-reachable case)."""
    body = await request.json()
    acc_id = body.get("account")
    if acc_id not in store.get()["accounts"]:
        raise HTTPException(400, "Unknown account")
    code, state = _parse_oauth_response(body.get("response", ""))
    if state:
        d = auth.unsign(state)
        if not d or d.get("acc") != acc_id:
            raise HTTPException(400, "State mismatch — start the connect again")
    if not code:
        raise HTTPException(400, "No 'code=' found in the pasted URL")
    try:
        _google_complete(request, acc_id, code)
    except ValueError as e:
        msg = {"no_refresh_token": "No refresh token returned — revoke CaCs under your "
               "Google account's third-party access, then connect again.",
               "unknown_account": "Unknown account"}.get(str(e), str(e))
        raise HTTPException(400, msg)
    except Exception as exc:
        raise HTTPException(502, f"Token exchange failed: {exc}")
    return {"ok": True}


# --- HTML pages ------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_page)):
    return templates.TemplateResponse(request, "dashboard.html", {
        "status": _status_payload(),
        "runs": [dict(r) for r in db.list_runs(limit=10, kind="sync")],
        "activities": [dict(a) for a in db.recent_activities(limit=15)],
    })


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, _: str = Depends(require_page)):
    return templates.TemplateResponse(request, "runs.html", {
        "runs": [dict(r) for r in db.list_runs(limit=100)]})


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, _: str = Depends(require_page)):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return templates.TemplateResponse(request, "run_detail.html", {
        "run": dict(run),
        "activities": [dict(a) for a in db.run_activities(run_id)]})


def _since(days: int | None) -> str | None:
    if not days or days <= 0:
        return None
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, action: str | None = None, pair: str | None = None,
                  days: int | None = None, _: str = Depends(require_page)):
    pairs = [p.get("name") or pid for pid, p in store.get()["pairs"].items()]
    return templates.TemplateResponse(request, "activity.html", {
        "activities": [dict(a) for a in db.recent_activities(
            limit=500, action=action, pair=pair, since=_since(days))],
        "f_action": action or "", "f_pair": pair or "", "f_days": days or 0, "pairs": pairs})


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, _: str = Depends(require_page)):
    return templates.TemplateResponse(request, "logs.html")


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request, _: str = Depends(require_page)):
    return templates.TemplateResponse(request, "config.html", {
        "config": store.public_view()})


# --- API: status/config ----------------------------------------------------
@app.get("/api/status")
def api_status(_: str = Depends(require_api)):
    return _status_payload()


@app.get("/api/config")
def api_get_config(_: str = Depends(require_api)):
    return store.public_view()


@app.post("/api/config")
async def api_save_config(request: Request, _: str = Depends(require_api)):
    body = await request.json()
    allowed = {k: body[k] for k in ("interval_seconds", "sync_enabled", "base_url", "alerts") if k in body}
    cfg = store.save(allowed)
    return {"ok": True, "ready": _ready_to_sync(cfg)}


# --- API: accounts ----------------------------------------------------------
def _apply_secrets(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    from store import SECRET_FIELDS
    for fld, val in incoming.items():
        if fld in SECRET_FIELDS and val == "__SET__":
            continue  # masked -> keep existing value
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
        raise HTTPException(409, f"Account is used by pair(s): {', '.join(used)}")
    accounts = cfg["accounts"]
    accounts.pop(acc_id, None)
    store.replace("accounts", accounts)
    return {"ok": True, "config": store.public_view()}


# --- API: pairs -------------------------------------------------------------
@app.post("/api/pairs")
async def api_save_pair(request: Request, _: str = Depends(require_api)):
    body = await request.json()
    cfg = store.get()
    a, b = body.get("a"), body.get("b")
    if a not in cfg["accounts"] or b not in cfg["accounts"]:
        raise HTTPException(400, "Unknown account")
    if a == b:
        raise HTTPException(400, "A and B must be different")
    direction = body.get("direction", "both")
    if direction not in ("both", "a_to_b", "b_to_a"):
        raise HTTPException(400, "Invalid direction")
    pairs = cfg["pairs"]
    pid = body.get("id") or secrets.token_hex(4)
    pairs[pid] = {
        "name": body.get("name") or pid,
        "service": body.get("service", "calendar"),
        "a": a, "b": b,
        "direction": direction,
        "conflict_resolution": body.get("conflict_resolution", "a wins"),
        "collections": body.get("collections", pairs.get(pid, {}).get("collections", [])),
        "labels": body.get("labels", pairs.get(pid, {}).get("labels", {})),
    }
    store.replace("pairs", pairs)
    if pairs[pid]["collections"]:
        _spawn(runner.discover(pid, list_all=False))  # register mappings
    return {"ok": True, "id": pid, "config": store.public_view()}


@app.delete("/api/pairs/{pair_id}")
def api_del_pair(pair_id: str, _: str = Depends(require_api)):
    pairs = store.get()["pairs"]
    pairs.pop(pair_id, None)
    store.replace("pairs", pairs)
    return {"ok": True, "config": store.public_view()}


# --- API: clear one side (destructive) -------------------------------------
async def _do_clear(request: Request, execute: bool) -> dict[str, Any]:
    body = await request.json()
    pair, side = body.get("pair"), body.get("side")
    months = int(body.get("months") or 0)
    collection = body.get("collection") or None
    if pair not in store.get()["pairs"]:
        raise HTTPException(400, "Unknown pair")
    if side not in ("a", "b"):
        raise HTTPException(400, "Invalid side")
    _require_idle()
    try:
        return await runner.clear(pair, side, months, execute=execute, collection=collection)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Clear failed: {exc}")


@app.post("/api/clear/preview")
async def api_clear_preview(request: Request, _: str = Depends(require_api)):
    return await _do_clear(request, execute=False)


@app.post("/api/clear")
async def api_clear(request: Request, _: str = Depends(require_api)):
    return await _do_clear(request, execute=True)


# --- API: fix over-long UIDs (rewrites items) ------------------------------
async def _do_fix(request: Request, execute: bool) -> dict[str, Any]:
    body = await request.json()
    pair, side = body.get("pair"), body.get("side")
    collection = body.get("collection") or None
    threshold = int(body.get("threshold") or 0)
    if pair not in store.get()["pairs"]:
        raise HTTPException(400, "Unknown pair")
    if side not in ("a", "b"):
        raise HTTPException(400, "Invalid side")
    _require_idle()
    try:
        return await runner.fix_uids(pair, side, collection, threshold, execute=execute)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Fix UIDs failed: {exc}")


@app.post("/api/repair/preview")
async def api_repair_preview(request: Request, _: str = Depends(require_api)):
    return await _do_fix(request, execute=False)


@app.post("/api/repair")
async def api_repair(request: Request, _: str = Depends(require_api)):
    return await _do_fix(request, execute=True)


# --- API: actions -----------------------------------------------------------
@app.post("/api/discover")
async def api_discover(request: Request, _: str = Depends(require_api)):
    body = await request.json()
    pair = body.get("pair")
    if pair not in store.get()["pairs"]:
        raise HTTPException(400, "Unknown pair")
    _require_idle()
    return await runner.discover(pair, list_all=True)


@app.post("/api/sync")
async def api_sync(request: Request, _: str = Depends(require_api)):
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    _require_idle()
    pair = body.get("pair") or None
    collection = body.get("collection") or None
    if pair:
        if pair not in store.get()["pairs"]:
            raise HTTPException(400, "Unknown pair")
        _spawn(runner.run_sync(pair=pair, collection=collection, trigger="manual"))
    else:
        _spawn(runner.run_sync_all(trigger="manual"))
    return {"ok": True, "started": True}


@app.post("/api/resolve")
async def api_resolve(request: Request, _: str = Depends(require_api)):
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    _require_idle()
    return await runner.resolve_names(pair=body.get("pair") or None,
                                      collection=body.get("collection") or None)


@app.post("/api/activity/clear")
async def api_clear_activity(request: Request, _: str = Depends(require_api)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    older = int(body.get("older_than_days") or 0)
    deleted = db.clear_activities(since=_since(older))
    return {"ok": True, "deleted": deleted}


@app.post("/api/cancel")
def api_cancel(_: str = Depends(require_api)):
    return {"ok": True, "cancelled": runner.cancel()}


@app.post("/api/toggle")
async def api_toggle(request: Request, _: str = Depends(require_api)):
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    store.save({"sync_enabled": enabled})
    return {"ok": True, "sync_enabled": enabled}


@app.get("/api/logs", response_class=PlainTextResponse)
def api_logs(lines: int = 400, _: str = Depends(require_api)):
    if not os.path.exists(ROLLING_LOG):
        return "No logs yet."
    with open(ROLLING_LOG, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-lines:])


@app.get("/health")
def health():
    return JSONResponse({"status": "ok"})
