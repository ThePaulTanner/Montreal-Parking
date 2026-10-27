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
        secret, _, _ = server.accounts.create_user("paul", PW)
        token = server.accounts.login("paul", PW, pyotp.TOTP(secret).now(), "Test browser")
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
