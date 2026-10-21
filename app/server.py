"""Montreal Parking web app: login, map UI and JSON API, plus the daily data refresh.

One process: the FastAPI app serves requests while etl.daemon runs in a background thread and
publishes new data under DATA_DIR/current, which the store reloads when it changes.
"""
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from html import escape

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import etl
from parking.accounts import AuthError, AuthService
from parking.store import Store, local_time

DATA = os.environ.get("DATA_DIR", "/data")
APP_SECRET = os.environ.get("APP_SECRET", "")
IDLE_MINUTES = int(os.environ.get("SESSION_IDLE_MINUTES", "480"))
MAX_FAILED = int(os.environ.get("MAX_FAILED_LOGINS", "10"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
COOKIE = "mp_session"
HERE = os.path.dirname(os.path.abspath(__file__))

store = Store(os.path.join(DATA, "current", "curbs.pkl"))
accounts: AuthService | None = None


@asynccontextmanager
async def lifespan(app):
    global accounts
    os.makedirs(DATA, exist_ok=True)
    accounts = AuthService(os.path.join(DATA, "auth.db"), APP_SECRET, idle_minutes=IDLE_MINUTES, max_failed=MAX_FAILED)
    if os.environ.get("DISABLE_ETL") != "1":
        threading.Thread(target=etl.daemon, daemon=True).start()
    store.refresh(force=True)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def user_of(request):
    """The signed-in Session for this request's cookie, or None."""
    return accounts.authenticate(request.cookies.get(COOKIE))


def need_user(request):
    if not user_of(request):
        raise HTTPException(401, "login required")


def device_name(request):
    """A short readable device name from the User-Agent, for the device list."""
    ua = request.headers.get("user-agent", "")
    os_ = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                               ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux")) if k in ua), "Device")
    br = next((n for k, n in (("Edg/", "Edge"), ("Firefox", "Firefox"), ("Chrome", "Chrome"),
                              ("CriOS", "Chrome"), ("Safari", "Safari")) if k in ua), "browser")
    return f"{os_} {br}"


# --- public ----------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    try:
        age = time.time() - os.path.getmtime(os.path.join(DATA, "current", ".ok"))
    except OSError:
        return Response("no data yet", status_code=503, media_type="text/plain")
    ok = age < 36 * 3600
    return Response("ok" if ok else "stale data", status_code=200 if ok else 503, media_type="text/plain")


PAGE_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Montreal Parking</title>
<style>
:root{--bg:#f5f6f8;--fg:#1c2230;--muted:#5b6475;--card:#fff;--line:#d9dde5;--accent:#1d5fd1}
@media(prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e8ebf2;--muted:#9aa3b5;--card:#1b2030;--line:#2e3548;--accent:#6aa0ff}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
main{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px;width:min(560px,calc(100vw - 32px));margin:16px 0}
form.login{width:100%}h1{font-size:20px;margin:0 0 18px}h2{font-size:15px;margin:22px 0 8px}
label{display:block;font-size:13px;margin:12px 0 4px}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit}
button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button.small{width:auto;margin:0;padding:5px 10px;font-size:13px;font-weight:500}
button.plain{background:transparent;color:var(--accent);border:1px solid var(--line)}
.err{color:#c0392b;font-size:14px;margin-top:12px}.note{color:var(--muted);font-size:13px;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px}a{color:var(--accent)}
</style></head><body>"""

LOGIN_PAGE = PAGE_HEAD + """<main><form class="login" method="post" action="/login">
<h1>Montreal Parking</h1>
<label for="u">Username</label><input id="u" name="username" autocomplete="username" autocapitalize="none" autofocus required>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<label for="c">Authenticator code (or a recovery code)</label>
<input id="c" name="code" inputmode="numeric" autocomplete="one-time-code" required>
<button>Sign in</button>__MSG__</form></main></body></html>"""


def login_page(msg="", status=200):
    return HTMLResponse(LOGIN_PAGE.replace("__MSG__", msg), status_code=status)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if user_of(request):
        return RedirectResponse("/", status_code=303)
    if not accounts.has_user():
        return login_page('<div class="note">No login exists yet. Create one on the NAS: '
                          '<code>docker exec -it mtl-parking-app python cli.py create-user --username NAME</code></div>')
    return login_page()


@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...), code: str = Form(...)):
    ip = request.client.host if request.client else "-"
    try:
        token = accounts.login(username, password, code, device_name(request), ip)
    except AuthError as e:
        resp = login_page(f'<div class="err">{escape(e.detail)}</div>', e.status)
        resp.headers.update(e.headers)
        return resp
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.post("/logout")
def logout(request: Request):
    accounts.logout(request.cookies.get(COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    me = user_of(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    rows = "".join(
        f"<tr><td>{escape(s['device'])}{' (this device)' if s['id'] == me.id else ''}</td><td>{_when(s['last_seen'])}</td>"
        f"<td>{'' if s['id'] == me.id else f'<form method=post action=/account/revoke/{s['id']}><button class=\'small plain\'>Revoke</button></form>'}</td></tr>"
        for s in accounts.sessions(me.user_id))
    log = "".join(
        f"<tr><td>{_when(r['ts'])}</td><td>{escape(r['result'])}</td><td>{escape(r['username'] or '-')}</td><td>{escape(r['device'] or '')}</td></tr>"
        for r in accounts.login_log(25))
    return HTMLResponse(PAGE_HEAD + f"""<main><h1>Account: {escape(me.username)}</h1>
<p><a href="/">Back to the map</a></p>
<h2>Signed-in devices</h2><table><tr><th>Device</th><th>Last used</th><th></th></tr>{rows}</table>
<h2>Recent sign-in attempts</h2><table><tr><th>When</th><th>Result</th><th>User</th><th>Device</th></tr>{log}</table>
<form method="post" action="/logout"><button class="plain">Sign out</button></form>
<p class="note">Logins are added and unlocked on the NAS with <code>cli.py</code>.</p></main></body></html>""")


@app.post("/account/revoke/{session_id}")
def account_revoke(request: Request, session_id: str):
    me = user_of(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    accounts.revoke(me.user_id, session_id)
    return RedirectResponse("/account", status_code=303)


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
