"""Montreal Parking web app: login, map UI and JSON API, plus the daily data refresh.

One process: the FastAPI app serves requests while etl.daemon runs in a background thread and
publishes new data under DATA_DIR/current, which the store reloads when it changes.
"""
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from html import escape

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import etl
from parking import auth
from parking.store import Store, local_time

DATA = os.environ.get("DATA_DIR", "/data")
APP_USER = os.environ.get("APP_USER", "")
PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH", "")
SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
COOKIE = "mp_session"
HERE = os.path.dirname(os.path.abspath(__file__))

store = Store(os.path.join(DATA, "current", "curbs.pkl"))
failures = {}  # ip -> (count, first_failure_time)


@asynccontextmanager
async def lifespan(app):
    if not os.environ.get("SESSION_SECRET"):
        print("WARNING: SESSION_SECRET is not set; logins will be lost on restart", flush=True)
    if os.environ.get("DISABLE_ETL") != "1":
        threading.Thread(target=etl.daemon, daemon=True).start()
    store.refresh(force=True)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def user_of(request):
    c = request.cookies.get(COOKIE)
    return auth.read_cookie(c, SECRET) if c else None


def need_user(request):
    if not user_of(request):
        raise HTTPException(401, "login required")


# --- public ----------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    try:
        age = time.time() - os.path.getmtime(os.path.join(DATA, "current", ".ok"))
    except OSError:
        return Response("no data yet", status_code=503, media_type="text/plain")
    ok = age < 36 * 3600
    return Response("ok" if ok else "stale data", status_code=200 if ok else 503, media_type="text/plain")


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Montreal Parking</title>
<style>
:root{--bg:#f5f6f8;--fg:#1c2230;--card:#fff;--line:#d9dde5;--accent:#1d5fd1}
@media(prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e8ebf2;--card:#1b2030;--line:#2e3548;--accent:#6aa0ff}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
form{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px;width:min(340px,calc(100vw - 32px))}
h1{font-size:20px;margin:0 0 18px}label{display:block;font-size:13px;margin:12px 0 4px}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit}
button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600}
.err{color:#c0392b;font-size:14px;margin-top:12px}
</style></head><body><form method="post" action="/login">
<h1>Montreal Parking</h1>
<label for="u">User</label><input id="u" name="username" autocomplete="username" autofocus required>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button>Sign in</button>__ERR__</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if user_of(request):
        return RedirectResponse("/", status_code=303)
    return LOGIN_PAGE.replace("__ERR__", "")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "?"
    count, first = failures.get(ip, (0, time.time()))
    if time.time() - first > 600:
        count, first = 0, time.time()
    if count >= 5:
        return HTMLResponse(LOGIN_PAGE.replace("__ERR__", '<div class="err">Too many attempts. Try again in a few minutes.</div>'), status_code=429)
    ok = bool(APP_USER and PASSWORD_HASH) and secrets.compare_digest(username, APP_USER) \
        and auth.check_password(password, PASSWORD_HASH)
    if not ok:
        failures[ip] = (count + 1, first)
        time.sleep(1)  # slows down guessing
        return HTMLResponse(LOGIN_PAGE.replace("__ERR__", '<div class="err">Wrong user or password.</div>'), status_code=401)
    failures.pop(ip, None)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, auth.make_cookie(username, SECRET), max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --- app (login required) --------------------------------------------------------------
@app.get("/")
def index(request: Request):
    if not user_of(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(os.path.join(HERE, "static", "index.html"), headers={"Cache-Control": "no-cache"})


def _ready():
    store.refresh()
    if not store.ready:
        raise HTTPException(503, "The data has not finished downloading yet. Try again in a few minutes.")


@app.get("/api/meta")
def meta(request: Request):
    need_user(request)
    store.refresh()
    info = {"ready": store.ready, "segments": len(store.portions)}
    try:
        with open(os.path.join(DATA, "current", "manifest.json")) as f:
            info.update(json.load(f))
    except (OSError, ValueError):
        pass
    return info


@app.get("/api/curbs")
def curbs(request: Request, west: float, south: float, east: float, north: float, t: str | None = None):
    need_user(request)
    _ready()
    if east - west > 0.12 or north - south > 0.09:
        raise HTTPException(400, "Zoom in to see street sides.")
    fc = store.viewport(west, south, east, north, local_time(t))
    return Response(json.dumps(fc, separators=(",", ":")), media_type="application/json")


@app.get("/api/curb/{i}")
def curb(request: Request, i: int, t: str | None = None):
    need_user(request)
    _ready()
    if not 0 <= i < len(store.portions):
        raise HTTPException(404)
    return store.describe(i, local_time(t))


@app.get("/api/here")
def here(request: Request, lat: float, lon: float, heading: float | None = Query(None), t: str | None = None):
    need_user(request)
    _ready()
    return store.here(lat, lon, heading, local_time(t))
