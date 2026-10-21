"""Logins: password + authenticator code (or one-time recovery code), server-side sessions.

Modelled on the HomeFinance server's login so both apps behave the same way:
- Passwords: Argon2id over an HMAC of the password with a secret pepper derived from APP_SECRET.
- Second factor: TOTP that can only be used once (never an older step than the last used), or a
  single-use recovery code.
- Failures are delayed (doubling), every attempt goes to a log (never the password or code), and
  after MAX_FAILED_LOGINS in a row sign-in stays locked for everyone until `cli.py unlock` is run on
  the NAS.
- A session is a random 256-bit token kept only as a hash; each browser/phone is its own revocable
  session. Sessions end after SESSION_IDLE_MINUTES of no use, or after 30 days.
- Logins are created on the NAS (`cli.py create-user`), never through the web.
"""
import base64
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from . import passkeys

MIN_PASSWORD_LENGTH = 12
ISSUER = "Montreal Parking"
SESSION_MAX_DAYS = 30
INVALID = "Wrong username, password or code."
LOCKED_MESSAGE = "Sign-in is locked after too many failed attempts. Unlock it from the NAS."
TOUCH_EVERY = 60  # seconds between last_seen writes
LOG_MAX_ROWS = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  totp_secret TEXT NOT NULL, totp_last_step INTEGER NOT NULL DEFAULT 0,
  failed_streak INTEGER NOT NULL DEFAULT 0, locked_at REAL, created_at REAL NOT NULL, last_sign_in REAL);
CREATE TABLE IF NOT EXISTS recovery_codes(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL, used_at REAL);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT UNIQUE NOT NULL, device TEXT NOT NULL, created_at REAL NOT NULL,
  last_seen REAL NOT NULL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS passkeys(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  credential_id TEXT NOT NULL UNIQUE, public_key BLOB NOT NULL, sign_count INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL, created_at REAL NOT NULL, last_used_at REAL);
CREATE TABLE IF NOT EXISTS login_log(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, username TEXT, ip TEXT, device TEXT,
  result TEXT NOT NULL, method TEXT);
"""


class AuthError(Exception):
    def __init__(self, status, detail, headers=None):
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers or {}


@dataclass(frozen=True)
class Session:
    id: str
    user_id: int
    username: str


class Limiter:
    """Counts failures per key; after `max_failures` locks the key with a doubling delay."""

    def __init__(self, clock, max_failures=5, base_delay=30.0, max_delay=900.0):
        self.clock, self.max_failures = clock, max_failures
        self.base_delay, self.max_delay = base_delay, max_delay
        self._state = {}

    def check(self, key):
        _, until = self._state.get(key, (0, 0.0))
        now = self.clock()
        if until > now:
            raise AuthError(429, "Too many attempts. Try again later.", {"Retry-After": str(int(until - now) + 1)})

    def failure(self, key):
        n, _ = self._state.get(key, (0, 0.0))
        n += 1
        until = 0.0
        if n >= self.max_failures:
            until = self.clock() + min(self.base_delay * 2 ** (n - self.max_failures), self.max_delay)
        self._state[key] = (n, until)
        if len(self._state) > 10_000:
            self._state.clear()

    def success(self, key):
        self._state.pop(key, None)


def _sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _norm_recovery(code):
    return "".join(ch for ch in code.lower() if ch.isalnum())


class AuthService:
    def __init__(self, db_path, secret, *, idle_minutes=480, max_failed=10, clock=time.time):
        if not secret or len(secret) < 16:
            raise ValueError("APP_SECRET must be set to a random value of at least 16 characters")
        self.clock = clock
        self.idle_seconds = idle_minutes * 60
        self.max_failed = max_failed
        self._pepper = hashlib.sha256(b"pepper|" + secret.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"totp|" + secret.encode()).digest()))
        self._hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
        self._dummy = self._hasher.hash(self._prehash("dummy password"))
        self._lock = threading.RLock()
        self._user_limiter = Limiter(clock)
        self._ip_limiter = Limiter(clock, max_failures=20)
        self.challenges = passkeys.ChallengeStore(clock)
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    # -- helpers ----------------------------------------------------------------------
    def _one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def _run(self, sql, args=()):
        return self.db.execute(sql, args).rowcount

    def _prehash(self, password):
        return hmac.new(self._pepper, password.encode(), hashlib.sha256).hexdigest()

    def _verify_password(self, stored, password):
        try:
            ok = self._hasher.verify(stored or self._dummy, self._prehash(password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            ok = False
        return ok and stored is not None  # same work whether or not the user exists

    def _log(self, result, username=None, ip="-", device="", method=None):
        self._run("INSERT INTO login_log(ts, username, ip, device, result, method) VALUES (?,?,?,?,?,?)",
                  (self.clock(), username, ip, device[:80], result, method))
        self._run("DELETE FROM login_log WHERE id <= (SELECT MAX(id) FROM login_log) - ?", (LOG_MAX_ROWS,))

    # -- users ------------------------------------------------------------------------
    def has_user(self):
        return self._one("SELECT 1 FROM users LIMIT 1") is not None

    def users(self):
        return [dict(r) for r in self.db.execute(
            "SELECT id, username, created_at, last_sign_in, locked_at FROM users ORDER BY id")]

    def create_user(self, username, password):
        """Returns (totp_secret, otpauth_uri, recovery_codes). Shown once, never stored in clear."""
        username = username.strip().lower()
        if not username:
            raise ValueError("A username is required.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")
        if self._one("SELECT 1 FROM users WHERE username=?", (username,)):
            raise ValueError("That username is already taken.")
        secret = pyotp.random_base32()
        codes = ["-".join(secrets.token_hex(2) for _ in range(5)) for _ in range(10)]
        with self._lock:
            self.db.execute("BEGIN")
            try:
                uid = self.db.execute(
                    "INSERT INTO users(username, password_hash, totp_secret, created_at) VALUES (?,?,?,?)",
                    (username, self._hasher.hash(self._prehash(password)),
                     self._fernet.encrypt(secret.encode()).decode(), self.clock())).lastrowid
                for c in codes:
                    self.db.execute("INSERT INTO recovery_codes(user_id, code_hash) VALUES (?,?)",
                                    (uid, _sha256(_norm_recovery(c))))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return secret, pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER), codes

    def delete_user(self, username):
        return self._run("DELETE FROM users WHERE username=?", (username.strip().lower(),)) == 1

    def unlock(self):
        """Clear the hard lock and failure counts. Returns whether anything was locked."""
        with self._lock:
            was = self._one("SELECT 1 FROM users WHERE locked_at IS NOT NULL") is not None
            self._run("UPDATE users SET locked_at=NULL, failed_streak=0")
            self._user_limiter, self._ip_limiter = Limiter(self.clock), Limiter(self.clock, max_failures=20)
            if was:
                self._log("unlocked", ip="cli")
            return was

    # -- second factor ----------------------------------------------------------------
    def _check_totp(self, user, code):
        if not (code.isdigit() and len(code) == 6):
            return False
        try:
            secret = self._fernet.decrypt(user["totp_secret"].encode()).decode()
        except InvalidToken:
            return False
        totp = pyotp.TOTP(secret)
        step = int(self.clock() // 30)
        for cand in (step - 1, step, step + 1):
            if cand <= user["totp_last_step"]:
                continue  # one use only, and never an older code than the last one used
            if hmac.compare_digest(totp.at(cand * 30), code):
                return self._run("UPDATE users SET totp_last_step=? WHERE id=? AND totp_last_step<?",
                                 (cand, user["id"], cand)) == 1
        return False

    def _check_recovery(self, user, code):
        return self._run("UPDATE recovery_codes SET used_at=? WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                         (self.clock(), user["id"], _sha256(_norm_recovery(code)))) == 1

    # -- sign in / sessions -----------------------------------------------------------
    def login(self, username, password, code, device="", ip="-"):
        """Returns the raw session token. Raises AuthError (401 wrong, 423 locked, 429 slow down)."""
        key = username.strip().lower()
        with self._lock:
            user = self._one("SELECT * FROM users WHERE username=?", (key,))
            # One refusal for everyone while locked, so it does not reveal which names exist.
            if self._one("SELECT 1 FROM users WHERE locked_at IS NOT NULL"):
                self._log("locked", key, ip, device)
                raise AuthError(423, LOCKED_MESSAGE)
            try:
                self._user_limiter.check(key)
                self._ip_limiter.check(ip)
            except AuthError:
                self._log("rate_limited", key, ip, device)
                raise
            password_ok = self._verify_password(user["password_hash"] if user else None, password)
            code, method = code.strip(), None
            if password_ok and user is not None:
                if self._check_totp(user, code):
                    method = "totp"
                elif self._check_recovery(user, code):
                    method = "recovery"
            if method is None:
                result = "unknown_user" if user is None else "bad_password" if not password_ok else "bad_code"
                self._user_limiter.failure(key)
                self._ip_limiter.failure(ip)
                self._log(result, key, ip, device)
                if user is not None:
                    self._run("UPDATE users SET failed_streak=failed_streak+1 WHERE id=?", (user["id"],))
                    streak = self._one("SELECT failed_streak FROM users WHERE id=?", (user["id"],))[0]
                    if streak >= self.max_failed and self._run(
                            "UPDATE users SET locked_at=? WHERE id=? AND locked_at IS NULL",
                            (self.clock(), user["id"])):
                        self._log("lockout", key, ip, device)
                raise AuthError(401, INVALID)
            self._user_limiter.success(key)
            now = self.clock()
            self._run("UPDATE users SET failed_streak=0, last_sign_in=? WHERE id=?", (now, user["id"]))
            self._log("success", key, ip, device, method)
            return self._open_session(user["id"], device)

    def _open_session(self, user_id, device):
        now = self.clock()
        token = secrets.token_urlsafe(32)
        self._run("INSERT INTO sessions(id, user_id, token_hash, device, created_at, last_seen) VALUES (?,?,?,?,?,?)",
                  (secrets.token_hex(6), user_id, _sha256(token), device[:80], now, now))
        return token

    def authenticate(self, token):
        """The Session for a live cookie token, else None. Enforces idle timeout and 30-day maximum."""
        if not token:
            return None
        row = self._one(
            "SELECT s.*, u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (_sha256(token),))
        if row is None or row["revoked_at"] is not None:
            return None
        now = self.clock()
        if now - row["last_seen"] > self.idle_seconds or now - row["created_at"] > SESSION_MAX_DAYS * 86400:
            self._run("UPDATE sessions SET revoked_at=? WHERE id=?", (now, row["id"]))
            return None
        if now - row["last_seen"] > TOUCH_EVERY:
            self._run("UPDATE sessions SET last_seen=? WHERE id=?", (now, row["id"]))
        return Session(row["id"], row["user_id"], row["username"])

    def logout(self, token):
        if token:
            self._run("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                      (self.clock(), _sha256(token)))

    def sessions(self, user_id):
        now = self.clock()
        return [dict(r) for r in self.db.execute(
            "SELECT id, device, created_at, last_seen FROM sessions WHERE user_id=? AND revoked_at IS NULL "
            "AND ? - last_seen <= ? ORDER BY last_seen DESC", (user_id, now, self.idle_seconds))]

    def revoke(self, user_id, session_id):
        return self._run("UPDATE sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                         (self.clock(), session_id, user_id)) == 1

    def login_log(self, limit=50):
        return [dict(r) for r in self.db.execute("SELECT * FROM login_log ORDER BY id DESC LIMIT ?", (limit,))]

    # -- passkeys (Face ID) -----------------------------------------------------------
    def passkeys_for(self, user_id):
        return [dict(r) for r in self.db.execute(
            "SELECT id, name, created_at, last_used_at FROM passkeys WHERE user_id=? ORDER BY id", (user_id,))]

    def remove_passkey(self, user_id, passkey_id):
        return self._run("DELETE FROM passkeys WHERE id=? AND user_id=?", (passkey_id, user_id)) == 1

    def passkey_register_options(self, user_id, host):
        user = self._one("SELECT id, username FROM users WHERE id=?", (user_id,))
        if user is None:
            raise AuthError(401, INVALID)
        try:
            rp_id = passkeys.rp_id_for(host)
        except passkeys.PasskeyError as e:
            raise AuthError(400, str(e)) from None
        handle, challenge = self.challenges.issue("register", user_id)
        existing = [r["credential_id"] for r in self.db.execute(
            "SELECT credential_id FROM passkeys WHERE user_id=?", (user_id,))]
        return {"handle": handle, "publicKey": {
            "rp": {"id": rp_id, "name": ISSUER},
            "user": {"id": passkeys.b64u(f"user-{user['id']}".encode()), "name": user["username"],
                     "displayName": user["username"]},
            "challenge": passkeys.b64u(challenge),
            "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
            "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True,
                                       "userVerification": "required"},
            "excludeCredentials": [{"type": "public-key", "id": c} for c in existing],
            "attestation": "none", "timeout": 120000}}

    def passkey_register(self, user_id, handle, name, password, client_data, attestation, host, scheme="https"):
        """Add a passkey. Needs the password again: a passkey signs in without the authenticator code, so a
        borrowed browser session must not be enough to make one."""
        user = self._one("SELECT username, password_hash FROM users WHERE id=?", (user_id,))
        if user is None:
            raise AuthError(401, INVALID)
        with self._lock:
            self._user_limiter.check(user["username"])
            if not self._verify_password(user["password_hash"], password):
                self._user_limiter.failure(user["username"])
                raise AuthError(400, "Wrong password")
            self._user_limiter.success(user["username"])
        try:
            challenge = self.challenges.take(handle, "register", user_id)
            cred_id, public_key, count = passkeys.verify_registration(
                challenge, host, passkeys.unb64u(client_data), passkeys.unb64u(attestation), scheme)
        except passkeys.PasskeyError as e:
            raise AuthError(400, str(e)) from None
        key = passkeys.b64u(cred_id)
        if self._one("SELECT 1 FROM passkeys WHERE credential_id=?", (key,)):
            raise AuthError(400, "That passkey is already added")
        label = "".join(ch for ch in name if ch.isprintable()).strip()[:60] or "This device"
        now = self.clock()
        self._run("INSERT INTO passkeys(user_id, credential_id, public_key, sign_count, name, created_at) "
                  "VALUES (?,?,?,?,?,?)", (user_id, key, public_key, count, label, now))
        return {"name": label}

    def passkey_login_options(self, host):
        try:
            rp_id = passkeys.rp_id_for(host)
        except passkeys.PasskeyError as e:
            raise AuthError(400, str(e)) from None
        handle, challenge = self.challenges.issue("login")
        return {"handle": handle, "publicKey": {
            "rpId": rp_id, "challenge": passkeys.b64u(challenge), "userVerification": "required",
            "allowCredentials": [], "timeout": 120000}}

    def passkey_login(self, handle, credential_id, client_data, authenticator_data, signature, host,
                      scheme="https", device="", ip="-"):
        """Returns a session token. Held to the same lock and rate limit as the password path; a bad
        answer cannot be guessed at, so it is logged and counted by address but never locks a login."""
        with self._lock:
            if self._one("SELECT 1 FROM users WHERE locked_at IS NOT NULL"):
                self._log("locked", None, ip, device, "passkey")
                raise AuthError(423, LOCKED_MESSAGE)
            try:
                self._ip_limiter.check(ip)
            except AuthError:
                self._log("rate_limited", None, ip, device, "passkey")
                raise
            row = self._one("SELECT p.*, u.username FROM passkeys p JOIN users u ON u.id=p.user_id "
                            "WHERE p.credential_id=?", (credential_id,))
            try:
                if row is None:
                    raise passkeys.PasskeyError("This passkey is not registered here")
                challenge = self.challenges.take(handle, "login")
                count = passkeys.verify_assertion(
                    challenge, host, bytes(row["public_key"]), row["sign_count"], passkeys.unb64u(client_data),
                    passkeys.unb64u(authenticator_data), passkeys.unb64u(signature), scheme)
            except passkeys.PasskeyError as e:
                self._ip_limiter.failure(ip)
                self._log("bad_passkey", row["username"] if row else None, ip, device, "passkey")
                raise AuthError(401, str(e)) from None
            self._run("UPDATE passkeys SET sign_count=?, last_used_at=? WHERE id=?", (count, self.clock(), row["id"]))
            self._run("UPDATE users SET failed_streak=0, last_sign_in=? WHERE id=?", (self.clock(), row["user_id"]))
            self._log("success", row["username"], ip, device, "passkey")
            return self._open_session(row["user_id"], device)
