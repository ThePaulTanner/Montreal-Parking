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
    totp_secret, uri, codes = svc.create_user("Paul", PW, authenticator=True)
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


# --- passkey first, password as the fallback, authenticator optional ----------------------------------

@pytest.fixture
def plain(tmp_path):
    clock = Clock()
    svc = AuthService(str(tmp_path / "plain.db"), SECRET, idle_minutes=60, max_failed=4, clock=clock)
    assert svc.create_user("Paul", PW) == (None, None, [])           # no authenticator by default
    return svc, clock


def test_a_password_alone_signs_in_when_the_login_has_no_authenticator(plain):
    svc, _ = plain
    assert svc.authenticate(svc.login("paul", PW, "", "iPhone")).username == "paul"
    assert svc.authenticate(svc.login("paul", PW, "123456", "iPhone"))      # a code that is not needed is ignored
    assert any(r["result"] == "success" and r["method"] == "password" for r in svc.login_log())


def test_a_wrong_password_is_still_refused_and_counts_towards_the_lock(plain):
    svc, clock = plain
    statuses = []
    for _ in range(6):
        try:
            svc.login("paul", "not the password!!", "", "x")
        except AuthError as e:
            statuses.append(e.status)
        clock.now += 1000
    assert 423 in statuses
    with pytest.raises(AuthError) as e:
        svc.login("paul", PW, "", "x")                                 # locked even for the right password
    assert e.value.status == 423
    assert svc.unlock() is True and svc.login("paul", PW, "", "x")


def test_password_length_minimums(tmp_path):
    svc = AuthService(str(tmp_path / "len.db"), SECRET)
    with pytest.raises(ValueError):
        svc.create_user("a", "x" * 13)                                  # 14 needed without an authenticator
    svc.create_user("a", "x" * 14)
    with pytest.raises(ValueError):
        svc.create_user("b", "x" * 11, authenticator=True)             # 12 needed with one
    svc.create_user("b", "x" * 12, authenticator=True)


def test_errors_do_not_reveal_which_logins_have_an_authenticator(env, plain):
    svc, clock, totp, _ = env
    messages = set()
    for attempt in (lambda: svc.login("paul", PW, "", "x"),            # has an authenticator, no code given
                    lambda: svc.login("paul", "wrong password here", "", "x"),
                    lambda: svc.login("nobody", PW, "", "x")):
        clock.now += 1000
        with pytest.raises(AuthError) as e:
            attempt()
        messages.add((e.value.status, e.value.detail))
    assert len(messages) == 1


def test_remove_authenticator_needs_a_long_new_password_and_ends_old_sessions(env):
    svc, clock, totp, codes = env
    token = svc.login("paul", PW, code_at(clock, totp), "phone")
    with pytest.raises(ValueError):
        svc.remove_authenticator("paul", "too short")
    assert svc.remove_authenticator("paul", "a brand new long password") is True
    assert svc.authenticate(token) is None                                # signed out everywhere
    assert svc.login("paul", "a brand new long password", "", "phone")   # no code needed now
    with pytest.raises(AuthError):
        svc.login("paul", PW, "", "phone")                                # the old password is gone
    assert svc.remove_authenticator("nobody", "a brand new long password") is False
    assert svc.users()[0]["authenticator"] == 0


def test_old_recovery_codes_stop_working_once_the_authenticator_is_removed(env):
    svc, clock, totp, codes = env
    svc.remove_authenticator("paul", "a brand new long password")
    left = svc.db.execute("SELECT COUNT(*) FROM recovery_codes").fetchone()[0]
    assert left == 0


def test_add_authenticator_makes_a_code_required_and_replaces_recovery_codes(plain):
    svc, clock = plain
    token = svc.login("paul", PW, "", "phone")
    secret, uri, codes = svc.add_authenticator("Paul")
    assert svc.authenticate(token) is None and len(codes) == 10 and uri.startswith("otpauth://")
    with pytest.raises(AuthError):
        svc.login("paul", PW, "", "phone")                                # a code is now needed
    assert svc.login("paul", PW, pyotp.TOTP(secret).at(int(clock.now)), "phone")
    clock.now += 60
    assert svc.login("paul", PW, codes[0], "phone")                       # a recovery code works once
    _, _, newer = svc.add_authenticator("paul")
    clock.now += 60
    with pytest.raises(AuthError):
        svc.login("paul", PW, codes[1], "phone")                          # replaced codes are void
    assert svc.add_authenticator("nobody") is None


def test_reset_password_keeps_passkeys_and_signs_out_everywhere(plain):
    svc, clock = plain
    token = svc.login("paul", PW, "", "phone")
    uid = svc.authenticate(token).user_id
    svc.db.execute("INSERT INTO passkeys(user_id, credential_id, public_key, name, created_at) VALUES (?,?,?,?,?)",
                   (uid, "cred", b"k", "iPhone", clock.now))
    for _ in range(4):                                                    # lock it first
        try:
            svc.login("paul", "wrong password here!", "", "x")
        except AuthError:
            pass
        clock.now += 1000
    assert svc.set_password("paul", "a different long password") is True
    assert svc.authenticate(token) is None
    assert svc.login("paul", "a different long password", "", "x")        # the lock was cleared too
    with pytest.raises(AuthError):
        svc.login("paul", PW, "", "x")
    assert svc.passkeys_for(uid)[0]["name"] == "iPhone"                   # passkeys are kept
    with pytest.raises(ValueError):
        svc.set_password("paul", "short")
    assert svc.set_password("nobody", "a different long password") is False
    assert any(r["result"] == "password_reset" for r in svc.login_log())


def test_summaries_show_the_authenticator_and_passkey_counts(env, plain):
    svc, *_ = env
    uid = svc.users()[0]["id"]
    assert svc.user_summary(uid) == {"authenticator": True, "passkeys": 0}
    psvc, _ = plain
    assert psvc.users()[0]["authenticator"] == 0 and psvc.user_summary(psvc.users()[0]["id"])["authenticator"] is False
    assert psvc.user_summary(999) is None
