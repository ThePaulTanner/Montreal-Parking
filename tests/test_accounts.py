import sys
from pathlib import Path

import pyotp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from parking.accounts import AuthError, AuthService

SECRET = "x" * 32
PW = "correct horse battery"


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc = AuthService(str(tmp_path / "auth.db"), SECRET, idle_minutes=60, max_failed=4, clock=clock)
    totp_secret, uri, codes = svc.create_user("Paul", PW)
    return svc, clock, pyotp.TOTP(totp_secret), codes


def code_at(clock, totp):
    return totp.at(int(clock.now))


def test_login_with_password_and_authenticator_code(env):
    svc, clock, totp, _ = env
    token = svc.login("paul", PW, code_at(clock, totp), "iPhone")
    s = svc.authenticate(token)
    assert s and s.username == "paul"


def test_username_is_case_insensitive_and_wrong_things_are_refused(env):
    svc, clock, totp, _ = env
    assert svc.authenticate(svc.login("PAUL", PW, code_at(clock, totp)))
    for user, pw, code in [("paul", "wrong password!", code_at(clock, totp)), ("paul", PW, "000000"), ("nobody", PW, "123456")]:
        clock.now += 31
        with pytest.raises(AuthError) as e:
            svc.login(user, pw, code if code != "000000" else "000000")
        assert e.value.status in (401, 429)


def test_a_code_cannot_be_used_twice(env):
    svc, clock, totp, _ = env
    code = code_at(clock, totp)
    svc.login("paul", PW, code)
    with pytest.raises(AuthError):
        svc.login("paul", PW, code)          # replay of the same code
    clock.now += 60
    assert svc.login("paul", PW, code_at(clock, totp))  # a fresh code works


def test_recovery_code_works_once(env):
    svc, _, _, codes = env
    assert svc.login("paul", PW, codes[0].upper())
    with pytest.raises(AuthError):
        svc.login("paul", PW, codes[0])


def test_failures_slow_down_then_hard_lock_until_unlocked(env):
    svc, clock, totp, _ = env
    statuses = []
    for _ in range(8):
        try:
            svc.login("paul", PW, "000000")
        except AuthError as e:
            statuses.append(e.status)
        clock.now += 1000                      # skip the temporary delays
    assert 423 in statuses                     # locked after max_failed (4) failures in a row
    with pytest.raises(AuthError) as e:        # even the right credentials are refused while locked
        svc.login("paul", PW, code_at(clock, totp))
    assert e.value.status == 423
    assert svc.unlock() is True
    assert svc.login("paul", PW, code_at(clock, totp))


def test_devices_can_be_revoked_and_logged_out(env):
    svc, clock, totp, _ = env
    t1 = svc.login("paul", PW, code_at(clock, totp), "phone")
    clock.now += 31
    t2 = svc.login("paul", PW, code_at(clock, totp), "laptop")
    me = svc.authenticate(t1)
    assert {d["device"] for d in svc.sessions(me.user_id)} == {"phone", "laptop"}
    svc.revoke(me.user_id, svc.authenticate(t2).id)
    assert svc.authenticate(t2) is None        # revoked from the device list
    svc.logout(t1)
    assert svc.authenticate(t1) is None


def test_session_ends_after_idle_time(env):
    svc, clock, totp, _ = env
    t = svc.login("paul", PW, code_at(clock, totp))
    clock.now += 59 * 60
    assert svc.authenticate(t)                  # still within the hour (and refreshes the idle clock)
    clock.now += 61 * 60
    assert svc.authenticate(t) is None


def test_log_never_contains_secrets_and_weak_passwords_are_rejected(env, tmp_path):
    svc, clock, totp, _ = env
    with pytest.raises(AuthError):
        svc.login("paul", "super secret pw!!", "123456")
    dump = str(svc.login_log())
    assert "super secret pw" not in dump and "123456" not in dump
    with pytest.raises(ValueError):
        svc.create_user("sam", "short")


def test_wrong_app_secret_cannot_read_the_authenticator_key(env, tmp_path):
    svc, clock, totp, _ = env
    other = AuthService(str(tmp_path / "auth.db"), "y" * 32, clock=clock)
    with pytest.raises(AuthError):
        other.login("paul", PW, code_at(clock, totp))
