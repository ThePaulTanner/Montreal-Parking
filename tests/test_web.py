"""The web pages: version under the title, the Settings page with passkey setup, and the guards around them."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pyotp
import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.environ.update(DATA_DIR=tempfile.mkdtemp(), APP_SECRET="w" * 32, DISABLE_ETL="1", COOKIE_SECURE="false",
                  BUILD_COMMIT="")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

PW = "correct horse battery"


@pytest.fixture(scope="module")
def client():
    with TestClient(server.app, follow_redirects=False) as c:
        server.accounts.create_user("paul", PW)                  # password only, like a default login
        token = server.accounts.login("paul", PW, "", "Test browser")
        c.cookies.set(server.COOKIE, token)
        yield c


@pytest.fixture
def anon():
    with TestClient(server.app, follow_redirects=False) as c:
        yield c


def test_version_text_with_and_without_a_build():
    assert server.version_text("1.3.0") == "Version 1.3.0"
    assert server.version_text("1.3.0", "abcdef1234567") == "Version 1.3.0 (build abcdef1)"


def test_the_version_file_is_read():
    assert re.fullmatch(r"\d+\.\d+\.\d+", server.VERSION), server.VERSION
    assert server.VERSION == (APP / "VERSION").read_text().strip()


def test_the_map_page_shows_the_version_under_the_title(client):
    html = client.get("/").text
    assert f'<h1>Montreal Parking</h1><div class="ver">Version {server.VERSION}' in html
    assert "__VERSION__" not in html and 'href="/settings"' in html


def test_settings_page_has_passkey_setup_and_the_version(client):
    r = client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert "<h1>Settings</h1>" in html and f"Version {server.VERSION}" in html
    assert "Passkey (Face ID)" in html and "Set up a passkey on this device" in html
    assert "No passkeys yet." in html and "<b>paul</b>" in html
    assert "Signed-in devices" in html and "Recent sign-in attempts" in html


def test_the_old_account_address_goes_to_settings(client):
    r = client.get("/account")
    assert r.status_code == 303 and r.headers["location"] == "/settings"


def test_pages_need_a_login(anon):
    for path in ("/", "/settings"):
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert anon.get("/api/version").status_code == 401
    # the version is not shown to people who are not signed in
    assert server.VERSION not in anon.get("/login").text


def test_version_api_for_a_signed_in_user(client):
    assert client.get("/api/version").json() == {"version": server.VERSION, "build": server.BUILD or None}


def test_passkey_routes_only_answer_this_sites_own_pages(client):
    assert client.post("/settings/passkey/options").status_code == 403           # no header
    ok = client.post("/settings/passkey/options", headers={"X-Requested-With": "MontrealParking"})
    assert ok.status_code == 200 and "challenge" in ok.json()["publicKey"]
    assert client.post("/account/passkey/options", headers={"X-Requested-With": "MontrealParking"}).status_code == 404


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to check the page scripts")
@pytest.mark.parametrize("path", ["/settings", "/login", "/"])
def test_every_page_script_is_valid_javascript(client, anon, path):
    html = (anon if path == "/login" else client).get(path).text
    for i, src in enumerate(re.findall(r"<script>(.*?)</script>", html, flags=re.S)):
        out = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0,'utf8'))"],
                             input=src, text=True, capture_output=True)
        assert out.returncode == 0, f"{path} script {i}: {out.stderr[:300]}"


def test_the_map_page_is_a_full_screen_map_with_floating_controls(client):
    html = client.get("/").text
    assert "<header" not in html                                     # no bar above the map any more
    assert html.index('<div id="map">') < html.index('<div id="top">')  # the map is the page; controls come on top
    for ident in ("menuBtn", "timeChip", "findBtn", "chips", "findLegend", "findCtl", "driveBtn", "sheet", "hint"):
        assert f'id="{ident}"' in html, ident
    menu = html[html.index('id="menu"'):html.index('id="fabs"')]
    assert 'href="/settings"' in menu and 'action="/logout"' in menu    # Settings and Sign out live in the menu
    assert html.index('id="driveBtn"') > html.index('id="fabs"')       # the drive button floats bottom-right


def test_every_element_the_script_looks_up_exists(client):
    html = client.get("/").text
    script = html.split("<script>")[-1]
    wanted = set(re.findall(r'getElementById\("([^"]+)"\)', script))
    missing = [w for w in wanted if f'id="{w}"' not in html]
    assert not missing, missing


# --- where I parked -------------------------------------------------------------------
XHR = {"X-Requested-With": "MontrealParking"}


def test_the_parked_spot_needs_a_login_and_the_own_site_header(client, anon):
    assert anon.get("/api/parked").status_code == 401
    assert anon.post("/api/parked", json={"lat": 45.5, "lon": -73.5}, headers=XHR).status_code == 401
    assert client.post("/api/parked", json={"lat": 45.5, "lon": -73.5}).status_code == 403     # no header
    assert client.delete("/api/parked").status_code == 403


def test_save_read_note_and_clear_the_parked_spot(client):
    assert client.get("/api/parked").json() == {"parked": None}
    saved = client.post("/api/parked", json={"lat": 45.5255, "lon": -73.576, "accuracy": 8.0}, headers=XHR)
    assert saved.status_code == 200
    spot = saved.json()["parked"]
    assert (spot["lat"], spot["lon"], spot["accuracy_m"]) == (45.5255, -73.576, 8.0)
    assert spot["parked_at"].endswith("Z") and spot["note"] == ""
    assert client.get("/api/parked").json()["parked"]["lat"] == 45.5255

    noted = client.patch("/api/parked", json={"note": "level 2, by the lift"}, headers=XHR)
    assert noted.json()["parked"]["note"] == "level 2, by the lift"

    assert client.delete("/api/parked", headers=XHR).json() == {"parked": None}
    assert client.get("/api/parked").json() == {"parked": None}
    assert client.patch("/api/parked", json={"note": "x"}, headers=XHR).status_code == 404   # nothing saved


def test_an_impossible_position_is_refused(client):
    assert client.post("/api/parked", json={"lat": 123.0, "lon": -73.5}, headers=XHR).status_code == 400
    assert client.get("/api/parked").json() == {"parked": None}


NAV = subprocess.run(["node", "-v"], capture_output=True, text=True).returncode == 0 if shutil.which("node") else False


def _nav_url(lat, lon, label, ua, touch):
    page = (APP / "static" / "index.html").read_text(encoding="utf-8")
    code = page[page.index("// <navurl>"):page.index("// </navurl>")]
    script = code + f"\nconsole.log(navUrl({lat}, {lon}, {label!r}, {ua!r}, {touch}));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.mark.skipif(not NAV, reason="node is needed to run the page's navigation code")
def test_the_navigate_button_opens_the_phones_own_maps_app():
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
    ipad_desktop_mode = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15"
    android = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36"
    windows = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
    mac = ipad_desktop_mode

    assert _nav_url(45.5255, -73.576, "My car", iphone, 5) == \
        "https://maps.apple.com/?daddr=45.525500,-73.576000&dirflg=w&q=My%20car"           # Apple Maps, walking
    assert _nav_url(45.5255, -73.576, "My car", ipad_desktop_mode, 5).startswith("https://maps.apple.com/")
    assert _nav_url(45.5255, -73.576, "My car", android, 5) == "geo:45.525500,-73.576000?q=45.525500,-73.576000(My%20car)"
    assert _nav_url(45.5255, -73.576, "My car", windows, 0) == \
        "https://www.google.com/maps/dir/?api=1&destination=45.525500,-73.576000&travelmode=walking"
    assert _nav_url(45.5255, -73.576, "My car", mac, 0).startswith("https://www.google.com/maps/dir/")   # a real Mac
    assert "rue%20Boyer" in _nav_url(45.5, -73.5, "My car on rue Boyer", iphone, 5)                       # names are escaped


def test_the_map_page_has_the_park_here_button_and_a_toast(client):
    html = client.get("/").text
    for ident in ("carBtn", "carLbl", "toast"):
        assert f'id="{ident}"' in html, ident
    assert html.index('id="carBtn"') > html.index('id="fabs"')          # floats with the Drive button


# --- sign-in page and settings for passkey-first login ------------------------------------

def test_the_sign_in_page_leads_with_the_passkey_and_hides_the_code_field(anon):
    html = anon.get("/login").text
    assert 'id="pkBtn"' in html and "Sign in with Face ID / passkey" in html
    assert html.index('id="pk"') < html.index('id="pwd"')                         # passkey section comes first in the page
    assert '<details id="pwd" open>' in html                                      # the password form is open by default
    code_input = html[html.index('name="code"'):].split(">")[0]
    assert "required" not in code_input                                           # the code is optional
    assert "My login also has an authenticator code" in html


def test_a_password_only_login_signs_in_through_the_form_without_a_code(anon):
    ok = anon.post("/login", data={"username": "paul", "password": PW})
    assert ok.status_code == 303 and server.COOKIE in ok.headers.get("set-cookie", "")
    bad = anon.post("/login", data={"username": "paul", "password": "not the password at all"})
    assert bad.status_code == 401 and "Wrong username, password or code." in bad.text
    assert 'class="err"' in bad.text                                              # so the form stays open on the page


def test_settings_explains_how_you_sign_in(client):
    html = client.get("/settings").text
    assert "How you sign in" in html and "Passkeys:</b> 0 set up" in html
    assert "Authenticator code:</b> off" in html and "reset-password" in html


def test_me_endpoint_reports_the_sign_in_methods(client, anon):
    assert client.get("/api/me").json() == {"username": "paul", "authenticator": False, "passkeys": 0}
    assert anon.get("/api/me").status_code == 401


def test_the_map_page_offers_to_set_up_a_passkey(client):
    html = client.get("/").text
    assert 'id="pkNudge"' in html and "/settings#passkey" in html and 'id="pkLater"' in html
