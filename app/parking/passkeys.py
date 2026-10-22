"""Passkeys (WebAuthn): sign in with Face ID or Touch ID instead of password + code.

Adapted from the HomeFinance server (same author). The one change: the expected scheme is a parameter
so a plain-http test on localhost works; production uses https.

Only what is needed: ES256 keys (what iPhones, Macs and Android make), no attestation (the server
keeps just the public key), user verification required (the biometric), discoverable credentials
(so no username is typed). The server never sees a biometric, only a signature from the device.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

CHALLENGE_TTL = 120.0
MAX_CHALLENGES = 200
FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


class PasskeyError(Exception):
    """The browser's answer was not acceptable. The message is safe to show."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise PasskeyError("Unreadable passkey data") from None


# -- a small CBOR reader (maps, arrays, byte/text strings, integers) ----------------------


def _cbor(data: bytes, at: int = 0, depth: int = 0) -> tuple[Any, int]:
    if depth > 8 or at >= len(data):
        raise PasskeyError("Unreadable passkey data")
    head = data[at]
    kind, info = head >> 5, head & 0x1F
    at += 1
    if info < 24:
        value = info
    elif info in (24, 25, 26, 27):
        size = {24: 1, 25: 2, 26: 4, 27: 8}[info]
        if at + size > len(data):
            raise PasskeyError("Unreadable passkey data")
        value = int.from_bytes(data[at : at + size], "big")
        at += size
    else:
        raise PasskeyError("Unsupported passkey data")
    if kind == 0:
        return value, at
    if kind == 1:
        return -1 - value, at
    if kind in (2, 3):
        if at + value > len(data):
            raise PasskeyError("Unreadable passkey data")
        raw = data[at : at + value]
        return (raw if kind == 2 else raw.decode("utf-8", "replace")), at + value
    if kind == 4:
        if value > 64:
            raise PasskeyError("Unreadable passkey data")
        items = []
        for _ in range(value):
            item, at = _cbor(data, at, depth + 1)
            items.append(item)
        return items, at
    if kind == 5:
        if value > 64:
            raise PasskeyError("Unreadable passkey data")
        out = {}
        for _ in range(value):
            key, at = _cbor(data, at, depth + 1)
            if not isinstance(key, (int, str, bytes)):
                raise PasskeyError("Unreadable passkey data")
            item, at = _cbor(data, at, depth + 1)
            out[key] = item
        return out, at
    raise PasskeyError("Unsupported passkey data")


def cbor_decode(data: bytes) -> Any:
    value, _ = _cbor(data)
    return value


# -- challenges ----------------------------------------------------------------------------


@dataclass
class Challenge:
    value: bytes
    kind: str  # "register" or "login"
    user_id: int | None
    expires: float


class ChallengeStore:
    """Each challenge is used once and expires. In memory: a restart just means trying again."""

    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self._items: dict[str, Challenge] = {}
        self._lock = threading.Lock()

    def issue(self, kind: str, user_id: int | None = None) -> tuple[str, bytes]:
        now = self.clock()
        with self._lock:
            for key in [k for k, c in self._items.items() if c.expires < now]:
                del self._items[key]
            if len(self._items) >= MAX_CHALLENGES:
                del self._items[min(self._items, key=lambda k: self._items[k].expires)]
            handle, value = secrets.token_urlsafe(16), secrets.token_bytes(32)
            self._items[handle] = Challenge(value, kind, user_id, now + CHALLENGE_TTL)
            return handle, value

    def take(self, handle: str, kind: str, user_id: int | None = None) -> bytes:
        with self._lock:
            found = self._items.pop(handle, None)
        if found is None or found.expires < self.clock() or found.kind != kind or found.user_id != user_id:
            raise PasskeyError("That request expired. Try again.")
        return found.value


# -- checking what the browser sends ------------------------------------------------------


def rp_id_for(host: str) -> str:
    """The relying-party id is the site's host name, without any port."""
    host = host.strip().lower()
    if host.startswith("["):
        raise PasskeyError("Passkeys need a host name, not an address")
    host = host.split(":")[0]
    if not host or host.replace(".", "").isdigit():
        raise PasskeyError("Passkeys need a host name, not an address")
    return host


def expected_origin(host: str, scheme: str = "https") -> str:
    return f"{scheme}://{host.strip().lower()}"


def _client_data(raw: bytes, kind: str, challenge: bytes, host: str, scheme: str = "https") -> None:
    try:
        data = json.loads(raw)
    except ValueError:
        raise PasskeyError("Unreadable passkey data") from None
    if not isinstance(data, dict) or data.get("type") != kind:
        raise PasskeyError("Wrong kind of passkey answer")
    if unb64u(str(data.get("challenge", ""))) != challenge:
        raise PasskeyError("That request expired. Try again.")
    given = urlsplit(str(data.get("origin", "")))
    if f"{given.scheme}://{given.netloc}" != expected_origin(host, scheme):
        raise PasskeyError("This passkey belongs to a different address")
    if data.get("crossOrigin"):
        raise PasskeyError("Passkeys cannot be used from inside another page")


def _public_key(cose: dict) -> bytes:
    if cose.get(1) != 2 or cose.get(3) != -7 or cose.get(-1) != 1:
        raise PasskeyError("This device made a kind of passkey the server does not support")
    x, y = cose.get(-2), cose.get(-3)
    if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == len(y) == 32):
        raise PasskeyError("Unreadable passkey data")
    point = b"\x04" + x + y
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ValueError:
        raise PasskeyError("Unreadable passkey data") from None
    return point


def verify_registration(
    challenge: bytes, host: str, client_data_json: bytes, attestation_object: bytes, scheme: str = "https"
) -> tuple[bytes, bytes, int]:
    """Returns (credential_id, public_key_point, sign_count) for a new passkey."""
    _client_data(client_data_json, "webauthn.create", challenge, host, scheme)
    attestation = cbor_decode(attestation_object)
    auth = attestation.get("authData") if isinstance(attestation, dict) else None
    if not isinstance(auth, bytes) or len(auth) < 55:
        raise PasskeyError("Unreadable passkey data")
    if auth[:32] != hashlib.sha256(rp_id_for(host).encode()).digest():
        raise PasskeyError("This passkey belongs to a different address")
    flags = auth[32]
    if not flags & FLAG_UP or not flags & FLAG_UV:
        raise PasskeyError("Face ID (or the device's unlock) was not used")
    if not flags & FLAG_AT:
        raise PasskeyError("Unreadable passkey data")
    count = struct.unpack(">I", auth[33:37])[0]
    id_length = struct.unpack(">H", auth[53:55])[0]
    if id_length == 0 or id_length > 1023 or 55 + id_length > len(auth):
        raise PasskeyError("Unreadable passkey data")
    credential_id = auth[55 : 55 + id_length]
    cose, _ = _cbor(auth, 55 + id_length)
    if not isinstance(cose, dict):
        raise PasskeyError("Unreadable passkey data")
    return credential_id, _public_key(cose), count


def verify_assertion(
    challenge: bytes,
    host: str,
    public_key: bytes,
    stored_count: int,
    client_data_json: bytes,
    authenticator_data: bytes,
    signature: bytes,
    scheme: str = "https",
) -> int:
    """Checks a sign-in answer against the stored public key. Returns the new sign count."""
    _client_data(client_data_json, "webauthn.get", challenge, host, scheme)
    if len(authenticator_data) < 37 or authenticator_data[:32] != hashlib.sha256(rp_id_for(host).encode()).digest():
        raise PasskeyError("This passkey belongs to a different address")
    flags = authenticator_data[32]
    if not flags & FLAG_UP or not flags & FLAG_UV:
        raise PasskeyError("Face ID (or the device's unlock) was not used")
    count = struct.unpack(">I", authenticator_data[33:37])[0]
    # Many synced passkeys always report 0; a counter that goes backwards means a cloned key.
    if (count or stored_count) and count <= stored_count:
        raise PasskeyError("This passkey looks copied, so it was refused")
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key)
    try:
        key.verify(signature, authenticator_data + hashlib.sha256(client_data_json).digest(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise PasskeyError("The passkey signature was not valid") from None
    return count
