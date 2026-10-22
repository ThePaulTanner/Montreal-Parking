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
from pydantic import BaseModel

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


PASSKEY_JS = """<script>
const b64u = {
  enc: b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, ""),
  dec: s => { s = s.replace(/-/g, "+").replace(/_/g, "/"); s += "=".repeat((4 - s.length % 4) % 4); return Uint8Array.from(atob(s), c => c.charCodeAt(0)).buffer; },
};
const XHR = {"Content-Type": "application/json", "X-Requested-With": "MontrealParking"};
async function post(url, body) {
  const r = await fetch(url, {method: "POST", headers: XHR, body: JSON.stringify(body || {}), credentials: "same-origin"});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong");
  return data;
}
async function passkeySignIn(box) {
  box.textContent = "";
  try {
    const o = await post("/passkey/options"), k = o.publicKey;
    let c;
    try {
      c = await navigator.credentials.get({publicKey: {challenge: b64u.dec(k.challenge), rpId: k.rpId,
        userVerification: k.userVerification, timeout: k.timeout, allowCredentials: []}});
    } catch (e) {
      throw new Error(e.name === "NotAllowedError" ? "Cancelled, or this device has no passkey here yet. Sign in with your password once, then add one on the Account page." : "Passkey sign-in failed (" + e.name + ")");
    }
    const r = c.response;
    await post("/passkey/login", {handle: o.handle, credential_id: c.id, client_data: b64u.enc(r.clientDataJSON),
      authenticator_data: b64u.enc(r.authenticatorData), signature: b64u.enc(r.signature)});
    location.href = "/";
  } catch (e) { box.textContent = e.message; }
}
async function addPasskey(box, name, password) {
  box.textContent = "";
  try {
    const o = await post("/account/passkey/options"), k = o.publicKey;
    let c;
    try {
      c = await navigator.credentials.create({publicKey: {rp: k.rp,
        user: {id: b64u.dec(k.user.id), name: k.user.name, displayName: k.user.displayName},
        challenge: b64u.dec(k.challenge), pubKeyCredParams: k.pubKeyCredParams,
        authenticatorSelection: k.authenticatorSelection,
        excludeCredentials: k.excludeCredentials.map(x => ({type: x.type, id: b64u.dec(x.id)})),
        attestation: k.attestation, timeout: k.timeout}});
    } catch (e) {
      throw new Error(e.name === "InvalidStateError" ? "This device already has a passkey for this site." : e.name === "NotAllowedError" ? "Cancelled." : "Could not make a passkey (" + e.name + ")");
    }
    await post("/account/passkey", {handle: o.handle, name, password, client_data: b64u.enc(c.response.clientDataJSON),
      attestation: b64u.enc(c.response.attestationObject)});
    location.reload();
  } catch (e) { box.textContent = e.message; }
}
</script>"""

LOGIN_PAGE = PAGE_HEAD + """<main><form class="login" method="post" action="/login">
<h1>Montreal Parking</h1>
<label for="u">Username</label><input id="u" name="username" autocomplete="username" autocapitalize="none" autofocus required>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<label for="c">Authenticator code (or a recovery code)</label>
<input id="c" name="code" inputmode="numeric" autocomplete="one-time-code" required>
<button>Sign in</button>__MSG__</form>
<div id="pk" hidden><p class="note" style="text-align:center">or</p>
<button type="button" class="plain" id="pkBtn">Sign in with Face ID / passkey</button>
<div class="err" id="pkErr"></div></div></main>""" + PASSKEY_JS + """<script>
if (window.PublicKeyCredential && navigator.credentials) {
  document.getElementById("pk").hidden = false;
  document.getElementById("pkBtn").onclick = () => passkeySignIn(document.getElementById("pkErr"));
}
</script></body></html>"""


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



class AssertionBody(BaseModel):
    handle: str
    credential_id: str
    client_data: str
    authenticator_data: str
    signature: str


class PasskeyAddBody(BaseModel):
    handle: str
    name: str = ""
    password: str
    client_data: str
    attestation: str


def need_xhr(request):
    """The passkey JSON routes are only for this site's own pages."""
    if request.headers.get("x-requested-with") != "MontrealParking":
        raise HTTPException(403, "Not allowed from another site")


def host_of(request):
    return request.headers.get("host", "")


def scheme():
    return "https" if COOKIE_SECURE else "http"


def failing(e):
    raise HTTPException(e.status, e.detail, headers=e.headers)


@app.post("/passkey/options")
def passkey_options(request: Request):
    need_xhr(request)
    try:
        return accounts.passkey_login_options(host_of(request))
    except AuthError as e:
        failing(e)


@app.post("/passkey/login")
def passkey_login(body: AssertionBody, request: Request):
    need_xhr(request)
    ip = request.client.host if request.client else "-"
    try:
        token = accounts.passkey_login(body.handle, body.credential_id, body.client_data, body.authenticator_data,
                                       body.signature, host_of(request), scheme(), device_name(request), ip)
    except AuthError as e:
        failing(e)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.post("/account/passkey/options")
def account_passkey_options(request: Request):
    need_xhr(request)
    me = user_of(request)
    if not me:
        raise HTTPException(401, "login required")
    try:
        return accounts.passkey_register_options(me.user_id, host_of(request))
    except AuthError as e:
        failing(e)


@app.post("/account/passkey", status_code=201)
def account_passkey_add(body: PasskeyAddBody, request: Request):
    need_xhr(request)
    me = user_of(request)
    if not me:
        raise HTTPException(401, "login required")
    try:
        return accounts.passkey_register(me.user_id, body.handle, body.name, body.password, body.client_data,
                                         body.attestation, host_of(request), scheme())
    except AuthError as e:
        failing(e)


@app.post("/account/passkey/{passkey_id}/delete")
def account_passkey_delete(request: Request, passkey_id: int):
    me = user_of(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    accounts.remove_passkey(me.user_id, passkey_id)
    return RedirectResponse("/account", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    me = user_of(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    rows = "".join(
        f"<tr><td>{escape(s['device'])}{' (this device)' if s['id'] == me.id else ''}</td><td>{_when(s['last_seen'])}</td>"
        f"<td>{'' if s['id'] == me.id else f'<form method=post action=/account/revoke/{s['id']}><button class=\'small plain\'>Revoke</button></form>'}</td></tr>"
        for s in accounts.sessions(me.user_id))
    pk_rows = "".join(
        f"<tr><td>{escape(p['name'])}</td><td>{_when(p['last_used_at'])}</td>"
        f"<td><form method=post action=/account/passkey/{p['id']}/delete><button class='small plain'>Remove</button></form></td></tr>"
        for p in accounts.passkeys_for(me.user_id))
    log = "".join(
        f"<tr><td>{_when(r['ts'])}</td><td>{escape(r['result'])}</td><td>{escape(r['username'] or '-')}</td><td>{escape(r['device'] or '')}</td></tr>"
        for r in accounts.login_log(25))
    return HTMLResponse(PAGE_HEAD + f"""<main><h1>Account: {escape(me.username)}</h1>
<p><a href="/">Back to the map</a></p>
<h2>Signed-in devices</h2><table><tr><th>Device</th><th>Last used</th><th></th></tr>{rows}</table>
<h2>Passkeys (Face ID)</h2>
<table><tr><th>Name</th><th>Last used</th><th></th></tr>{pk_rows}</table>
<div id="pkAdd" hidden>
<label for="pkName">Name for this device</label><input id="pkName" value="">
<label for="pkPw">Your password (a passkey skips the authenticator code, so it is asked again)</label>
<input id="pkPw" type="password" autocomplete="current-password">
<button type="button" class="plain" id="pkBtn">Add a passkey on this device</button>
<div class="err" id="pkErr"></div></div>
<h2>Recent sign-in attempts</h2><table><tr><th>When</th><th>Result</th><th>User</th><th>Device</th></tr>{log}</table>
<form method="post" action="/logout"><button class="plain">Sign out</button></form>
<p class="note">Logins are added and unlocked on the NAS with <code>cli.py</code>.</p></main>""" + PASSKEY_JS + """<script>
if (window.PublicKeyCredential && navigator.credentials) {
  document.getElementById("pkAdd").hidden = false;
  const ua = navigator.userAgent;
  document.getElementById("pkName").value = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android phone" : /Macintosh/.test(ua) ? "Mac" : "This device";
  document.getElementById("pkBtn").onclick = () => addPasskey(document.getElementById("pkErr"),
    document.getElementById("pkName").value, document.getElementById("pkPw").value);
}
</script></body></html>""")


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
def curbs(request: Request, west: float, south: float, east: float, north: float, t: str | None = None,
          stay: int | None = Query(None, ge=5, le=1440), paid: bool = False):
    need_user(request)
    _ready()
    if east - west > 0.12 or north - south > 0.09:
        raise HTTPException(400, "Zoom in to see street sides.")
    fc = store.viewport(west, south, east, north, local_time(t), stay=stay, paid=paid)
    return Response(json.dumps(fc, separators=(",", ":")), media_type="application/json")


@app.get("/api/curb/{i}")
def curb(request: Request, i: int, t: str | None = None,
         stay: int | None = Query(None, ge=5, le=1440), paid: bool = False):
    need_user(request)
    _ready()
    if not 0 <= i < len(store.portions):
        raise HTTPException(404)
    return store.describe(i, local_time(t), stay, paid)


@app.get("/api/here")
def here(request: Request, lat: float, lon: float, heading: float | None = Query(None), t: str | None = None,
         stay: int | None = Query(None, ge=5, le=1440), paid: bool = False):
    need_user(request)
    _ready()
    return store.here(lat, lon, heading, local_time(t), stay=stay, paid=paid)
