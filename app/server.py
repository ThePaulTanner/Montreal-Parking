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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

import etl
from parking.accounts import AuthError, AuthService
from parking.spot import SpotStore
from parking.store import Store, local_time

DATA = os.environ.get("DATA_DIR", "/data")
APP_SECRET = os.environ.get("APP_SECRET", "")
IDLE_MINUTES = int(os.environ.get("SESSION_IDLE_MINUTES", "480"))
MAX_FAILED = int(os.environ.get("MAX_FAILED_LOGINS", "10"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
COOKIE = "mp_session"
HERE = os.path.dirname(os.path.abspath(__file__))


def _read_version():
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


def version_text(version, build=""):
    return f"Version {version}" + (f" (build {build[:7]})" if build else "")


VERSION = _read_version()
BUILD = os.environ.get("BUILD_COMMIT", "")[:7]  # the commit the image was built from, set by the image build
VERSION_TEXT = version_text(VERSION, BUILD)

store = Store(os.path.join(DATA, "current", "curbs.pkl"))
accounts: AuthService | None = None
spots: SpotStore | None = None


@asynccontextmanager
async def lifespan(app):
    global accounts, spots
    os.makedirs(DATA, exist_ok=True)
    accounts = AuthService(os.path.join(DATA, "auth.db"), APP_SECRET, idle_minutes=IDLE_MINUTES, max_failed=MAX_FAILED)
    spots = SpotStore(os.path.join(DATA, "auth.db"))  # same file as the logins: a spot goes when its login goes
    if os.environ.get("DISABLE_ETL") != "1":
        threading.Thread(target=etl.daemon, daemon=True).start()
    store.refresh(force=True)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def bad_input(request, exc):
    """A 422 that does not echo the rejected value back: a NaN in it cannot be written as JSON and turned the
    answer into a 500."""
    return JSONResponse({"detail": [{"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
                                    for e in exc.errors()]}, status_code=422)


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
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
main{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px;width:min(560px,calc(100vw - 32px));margin:16px 0}
form.login{width:100%}h1{font-size:20px;margin:0 0 18px}h2{font-size:15px;margin:22px 0 8px}
label{display:block;font-size:13px;margin:12px 0 4px}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit}
button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button.small{width:auto;margin:0;padding:5px 10px;font-size:13px;font-weight:500}
button.plain{background:transparent;color:var(--accent);border:1px solid var(--line)}
.err{color:#c0392b;font-size:14px;margin-top:12px}.ok{color:#1e9e44;font-size:14px;margin-top:12px}.note{color:var(--muted);font-size:13px;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px}a{color:var(--accent)}
main.col{display:flex;flex-direction:column}main.col h1{order:-1}
#pk{order:2;margin-top:14px}.pkfirst #pk{order:0;margin:0 0 14px}
button.big{margin-top:0;padding:16px;font-size:17px}
details{margin:0}summary{cursor:pointer;color:var(--accent);font-size:14px;margin:8px 0}
details.code{margin-top:6px}
</style></head><body>"""


PASSKEY_JS = """<script>
const b64u = {
  enc: b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, ""),
  dec: s => { s = s.replace(/-/g, "+").replace(/_/g, "/"); s += "=".repeat((4 - s.length % 4) % 4); return Uint8Array.from(atob(s), c => c.charCodeAt(0)).buffer; },
};
const XHR = {"Content-Type": "application/json", "X-Requested-With": "MontrealParking"};
const rememberPk = () => { try { localStorage.setItem("mp_pk", "1"); } catch (e) {} };
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
      throw new Error(e.name === "NotAllowedError" ? "Cancelled, or this device has no passkey here yet. Use your password below once, then set one up under Settings." : "Passkey sign-in failed (" + e.name + ")");
    }
    const r = c.response;
    await post("/passkey/login", {handle: o.handle, credential_id: c.id, client_data: b64u.enc(r.clientDataJSON),
      authenticator_data: b64u.enc(r.authenticatorData), signature: b64u.enc(r.signature)});
    rememberPk();
    location.href = "/";
  } catch (e) { box.textContent = e.message; }
}
async function addPasskey(box, name, password) {
  box.className = "err"; box.textContent = "";
  try {
    const o = await post("/settings/passkey/options"), k = o.publicKey;
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
    await post("/settings/passkey", {handle: o.handle, name, password, client_data: b64u.enc(c.response.clientDataJSON),
      attestation: b64u.enc(c.response.attestationObject)});
    rememberPk();
    box.className = "ok";
    box.textContent = "Passkey added. Next time, tap \\"Sign in with Face ID / passkey\\" on the sign-in page.";
    setTimeout(() => location.reload(), 1800);
  } catch (e) { box.className = "err"; box.textContent = e.message; }
}
</script>"""

LOGIN_PAGE = PAGE_HEAD + """<main class="col"><h1>Montreal Parking</h1>
<div id="pk" hidden>
<button type="button" class="plain" id="pkBtn">Sign in with Face ID / passkey</button>
<div class="err" id="pkErr"></div></div>
<details id="pwd" open><summary>Use your password</summary>
<form class="login" method="post" action="/login">
<label for="u">Username</label><input id="u" name="username" value="__USER__" autocomplete="username" autocapitalize="none" autofocus required>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<details class="code"><summary>My login also has an authenticator code</summary>
<label for="c">Authenticator code (or a recovery code)</label>
<input id="c" name="code" inputmode="numeric" autocomplete="one-time-code"></details>
<button>Sign in</button>__MSG__</form></details></main>""" + PASSKEY_JS + """<script>
const failed = document.querySelector("#pwd .err");          // a wrong sign-in keeps the form open
let hasCode = false;
try { hasCode = localStorage.getItem("mp_totp") === "1"; } catch (e) {}
if (failed || hasCode) document.querySelector("details.code").open = true;  // so a missing code is never hidden
if (window.PublicKeyCredential && navigator.credentials) {
  document.getElementById("pk").hidden = false;
  document.getElementById("pkBtn").onclick = () => passkeySignIn(document.getElementById("pkErr"));
  let known = false;
  try { known = localStorage.getItem("mp_pk") === "1"; } catch (e) {}
  if (known) {                                              // this browser has a passkey: lead with it
    document.querySelector("main").classList.add("pkfirst");
    document.getElementById("pkBtn").className = "big";
    if (!failed) document.getElementById("pwd").open = false;
  }
}
</script></body></html>"""


def login_page(msg="", status=200, username=""):
    return HTMLResponse(LOGIN_PAGE.replace("__USER__", escape(username, quote=True)).replace("__MSG__", msg),
                        status_code=status)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if user_of(request):
        return RedirectResponse("/", status_code=303)
    if not accounts.has_user():
        return login_page('<div class="note">No login exists yet. Create one on the NAS: '
                          '<code>docker exec -it mtl-parking-app python cli.py create-user --username NAME</code></div>')
    return login_page()


@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...), code: str = Form("")):
    ip = request.client.host if request.client else "-"
    try:
        token = accounts.login(username, password, code, device_name(request), ip)
    except AuthError as e:
        resp = login_page(f'<div class="err">{escape(e.detail)}</div>', e.status, username)
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


@app.post("/settings/passkey/options")
def settings_passkey_options(request: Request):
    need_xhr(request)
    me = user_of(request)
    if not me:
        raise HTTPException(401, "login required")
    try:
        return accounts.passkey_register_options(me.user_id, host_of(request))
    except AuthError as e:
        failing(e)


@app.post("/settings/passkey", status_code=201)
def settings_passkey_add(body: PasskeyAddBody, request: Request):
    need_xhr(request)
    me = user_of(request)
    if not me:
        raise HTTPException(401, "login required")
    try:
        return accounts.passkey_register(me.user_id, body.handle, body.name, body.password, body.client_data,
                                         body.attestation, host_of(request), scheme())
    except AuthError as e:
        failing(e)


@app.post("/settings/passkey/{passkey_id}/delete")
def settings_passkey_delete(request: Request, passkey_id: int):
    me = user_of(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    accounts.remove_passkey(me.user_id, passkey_id)
    return RedirectResponse("/settings", status_code=303)


@app.get("/account")
def account_moved():
    return RedirectResponse("/settings", status_code=303)


SETTINGS_SCRIPT = """<script>
const pkAdd = document.getElementById("pkAdd"), pkNo = document.getElementById("pkNo");
if (window.PublicKeyCredential && navigator.credentials && window.isSecureContext) {
  pkAdd.hidden = false;
  const ua = navigator.userAgent;
  document.getElementById("pkName").value = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android phone" : /Macintosh/.test(ua) ? "Mac" : "This device";
  document.getElementById("pkBtn").onclick = () => addPasskey(document.getElementById("pkMsg"),
    document.getElementById("pkName").value, document.getElementById("pkPw").value);
} else {
  pkNo.hidden = false;
  pkNo.textContent = window.isSecureContext
    ? "This browser cannot make passkeys."
    : "Passkeys need the secure (https) address. You opened this page over plain http, so open it through your Tailscale address instead.";
}
</script></body></html>"""


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    me = user_of(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    keys = accounts.passkeys_for(me.user_id)
    summary = accounts.user_summary(me.user_id)
    pk_table = ("<table><tr><th>Passkey</th><th>Last used</th><th></th></tr>" + "".join(
        f"<tr><td>{escape(p['name'])}</td><td>{_when(p['last_used_at'])}</td>"
        f"<td><form method=post action=/settings/passkey/{p['id']}/delete><button class='small plain'>Remove</button></form></td></tr>"
        for p in keys) + "</table>") if keys else '<p class="note">No passkeys yet.</p>'
    devices = "".join(
        f"<tr><td>{escape(s['device'])}{' (this device)' if s['id'] == me.id else ''}</td><td>{_when(s['last_seen'])}</td>"
        f"<td>{'' if s['id'] == me.id else f'<form method=post action=/settings/revoke/{s['id']}><button class=\'small plain\'>Revoke</button></form>'}</td></tr>"
        for s in accounts.sessions(me.user_id))
    log = "".join(
        f"<tr><td>{_when(r['ts'])}</td><td>{escape(r['result'])}</td><td>{escape(r['username'] or '-')}</td><td>{escape(r['device'] or '')}</td></tr>"
        for r in accounts.login_log(25))
    return HTMLResponse(PAGE_HEAD + f"""<main><h1>Settings</h1>
<div class="note">{escape(VERSION_TEXT)}</div>
<p><a href="/">Back to the map</a> &middot; signed in as <b>{escape(me.username)}</b></p>
<h2>How you sign in</h2>
<ul class="note"><li><b>Passkeys:</b> {summary['passkeys']} set up. This is the normal way in.</li>
<li><b>Password:</b> the fallback. Change it on the NAS with <code>cli.py reset-password</code>.</li>
<li><b>Authenticator code:</b> {'on, so signing in with the password also needs a code (a passkey replaces both). Remove it on the NAS with <code>cli.py remove-authenticator</code>.' if summary['authenticator'] else 'off. A password of 14 or more characters is all it takes, so keep it long.'}</li></ul>
<h2 id="passkey">Passkey (Face ID)</h2>
<p class="note">A passkey lets this phone or computer sign in with Face ID or Touch ID, with nothing to type.
Set one up on each device you use. Adding one asks for your password once, to confirm it is you.</p>
{pk_table}
<div id="pkAdd" hidden>
<label for="pkName">Name for this device</label><input id="pkName" value="">
<label for="pkPw">Your password</label><input id="pkPw" type="password" autocomplete="current-password">
<button type="button" id="pkBtn">Set up a passkey on this device</button></div>
<div class="note err" id="pkNo" hidden></div><div class="err" id="pkMsg"></div>
<h2>Signed-in devices</h2><table><tr><th>Device</th><th>Last used</th><th></th></tr>{devices}</table>
<h2>Recent sign-in attempts</h2><table><tr><th>When</th><th>Result</th><th>User</th><th>Device</th></tr>{log}</table>
<form method="post" action="/logout"><button class="plain">Sign out</button></form>
<p class="note">Logins are added and unlocked on the NAS with <code>cli.py</code>.</p></main>""" + PASSKEY_JS + SETTINGS_SCRIPT)


@app.post("/settings/revoke/{session_id}")
def settings_revoke(request: Request, session_id: str):
    me = user_of(request)
    if not me:
        return RedirectResponse("/login", status_code=303)
    accounts.revoke(me.user_id, session_id)
    return RedirectResponse("/settings", status_code=303)


# --- app (login required) --------------------------------------------------------------
@app.get("/")
def index(request: Request):
    if not user_of(request):
        return RedirectResponse("/login", status_code=303)
    with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__VERSION__", escape(VERSION_TEXT))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/me")
def me_info(request: Request):
    me = _me(request)
    return {"username": me.username, **accounts.user_summary(me.user_id)}


@app.get("/api/version")
def version(request: Request):
    need_user(request)
    return {"version": VERSION, "build": BUILD or None}


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
def curbs(request: Request, west: float = Query(allow_inf_nan=False), south: float = Query(allow_inf_nan=False),
          east: float = Query(allow_inf_nan=False), north: float = Query(allow_inf_nan=False), t: str | None = None,
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
def here(request: Request, lat: float = Query(ge=-85, le=85, allow_inf_nan=False), lon: float = Query(ge=-180, le=180, allow_inf_nan=False),
         heading: float | None = Query(None, allow_inf_nan=False), t: str | None = None,
         stay: int | None = Query(None, ge=5, le=1440), paid: bool = False):
    need_user(request)
    _ready()
    return store.here(lat, lon, heading, local_time(t), stay=stay, paid=paid)


# --- where I parked ---------------------------------------------------------------------
class ParkedBody(BaseModel):
    lat: float = Field(ge=-85, le=85, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    accuracy: float | None = Field(None, ge=0, le=100_000, allow_inf_nan=False)
    note: str = Field("", max_length=500)


class NoteBody(BaseModel):
    note: str = ""


def _me(request):
    me = user_of(request)
    if not me:
        raise HTTPException(401, "login required")
    return me


@app.get("/api/parked")
def parked_get(request: Request):
    return {"parked": spots.get(_me(request).user_id)}


@app.post("/api/parked")
def parked_set(body: ParkedBody, request: Request):
    need_xhr(request)
    me = _me(request)
    street = None
    if store.ready:  # name the street so the saved spot reads "rue Boyer", not just coordinates
        found = store.here(body.lat, body.lon, None, local_time(None), radius_m=40)
        street = found.get("street") if found.get("found") else None
    try:
        return {"parked": spots.set(me.user_id, body.lat, body.lon, body.accuracy, street, body.note)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/parked")
def parked_note(body: NoteBody, request: Request):
    need_xhr(request)
    saved = spots.set_note(_me(request).user_id, body.note)
    if saved is None:
        raise HTTPException(404, "Nothing is saved yet.")
    return {"parked": saved}


@app.delete("/api/parked")
def parked_clear(request: Request):
    need_xhr(request)
    spots.clear(_me(request).user_id)
    return {"parked": None}
