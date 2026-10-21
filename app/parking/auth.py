"""Single-user login: scrypt password check and a signed session cookie. Standard library only."""
import base64
import hashlib
import hmac
import secrets
import time

SESSION_DAYS = 30


def hash_password(password, n=2 ** 14, r=8, p=1):
    """'scrypt:N:r:p:salt:hash' (no '$' so it survives docker compose interpolation)."""
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt:{n}:{r}:{p}:{salt.hex()}:{h.hex()}"


def check_password(password, stored):
    try:
        scheme, n, r, p, salt, h = stored.split(":")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, bytes.fromhex(h))
    except (ValueError, TypeError):
        return False


def make_cookie(user, secret):
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = base64.urlsafe_b64encode(f"{user}|{exp}".encode()).decode()
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_cookie(value, secret):
    """The user name if the cookie is genuine and unexpired, else None."""
    try:
        payload, sig = value.rsplit(".", 1)
        want = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None
        user, exp = base64.urlsafe_b64decode(payload.encode()).decode().rsplit("|", 1)
        return user if int(exp) > time.time() else None
    except (ValueError, TypeError):
        return None
