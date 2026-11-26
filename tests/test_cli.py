"""The NAS command-line tool: the way to create logins and to recover from a lost authenticator or password."""
import getpass
import os
import sys
import tempfile
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.environ.update(DATA_DIR=tempfile.mkdtemp(), APP_SECRET="c" * 32)

import cli  # noqa: E402
from parking.accounts import AuthService  # noqa: E402

PW = "an easy to remember passphrase"


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "DATA", str(tmp_path))
    monkeypatch.setenv("APP_SECRET", "c" * 32)                  # other test files set their own at import time
    answers = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": answers.pop(0))

    def go(*argv, passwords=()):
        answers[:] = list(passwords)
        try:
            cli.main(list(argv))
        except SystemExit as e:
            return capsys.readouterr().out, str(e.code or "")
        return capsys.readouterr().out, ""
    go.svc = lambda: AuthService(str(tmp_path / "auth.db"), "c" * 32)
    return go


def test_create_user_makes_a_password_login_with_no_authenticator(run):
    out, err = run("create-user", "--username", "Paul", passwords=[PW, PW])
    assert not err and "created" in out and "passkey" in out
    assert "Recovery codes" not in out
    assert run.svc().login("paul", PW, "")                       # signs in with the password alone


def test_create_user_can_still_add_an_authenticator(run):
    out, err = run("create-user", "--username", "paul", "--with-authenticator", passwords=[PW, PW])
    assert not err and "Recovery codes" in out and "otpauth://" in out
    assert run.svc().users()[0]["authenticator"] == 1


def test_mismatched_or_short_passwords_are_refused(run):
    assert "differ" in run("create-user", "--username", "paul", passwords=[PW, PW + "x"])[1]
    assert "at least 14" in run("create-user", "--username", "paul", passwords=["short", "short"])[1]
    assert run.svc().users() == []


def test_a_lost_authenticator_is_fixed_without_recreating_the_login(run):
    run("create-user", "--username", "paul", "--with-authenticator", passwords=[PW, PW])
    svc = run.svc()
    uid = svc.users()[0]["id"]
    svc.db.execute("INSERT INTO passkeys(user_id, credential_id, public_key, name, created_at) VALUES (?,?,?,?,?)",
                   (uid, "c", b"k", "iPhone", 1.0))
    out, err = run("remove-authenticator", "--username", "paul", passwords=["a fresh long passphrase", "a fresh long passphrase"])
    assert not err and "Authenticator removed" in out
    svc = run.svc()
    assert svc.login("paul", "a fresh long passphrase", "")      # no code needed any more
    assert svc.users()[0]["passkeys"] == 1                       # the passkey survived


def test_reset_password_and_unknown_logins(run):
    run("create-user", "--username", "paul", passwords=[PW, PW])
    out, err = run("reset-password", "--username", "paul", passwords=["a new long passphrase!", "a new long passphrase!"])
    assert "Password changed" in out
    assert run.svc().login("paul", "a new long passphrase!", "")
    assert "No such login" in run("reset-password", "--username", "nobody", passwords=["a new long passphrase!"] * 2)[0]
    assert "No such login" in run("add-authenticator", "--username", "nobody")[1]


def test_users_lists_the_sign_in_methods(run):
    run("create-user", "--username", "paul", passwords=[PW, PW])
    out, _ = run("users")
    assert "paul" in out and "authenticator off" in out and "passkeys 0" in out
    run("add-authenticator", "--username", "paul")
    assert "authenticator on" in run("users")[0]
