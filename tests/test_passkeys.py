"""Passkeys: a software authenticator stands in for the phone (cases adapted from HomeFinance)."""
import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from parking import passkeys
from parking.accounts import AuthError, AuthService

HOST = "nas.example.ts.net:10000"
PW = "correct horse battery"


def cbor(value):
    def head(kind, n):
        if n < 24:
            return bytes([kind << 5 | n])
        if n < 256:
            return bytes([kind << 5 | 24, n])
        return bytes([kind << 5 | 25]) + n.to_bytes(2, "big")

    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(v) for v in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(type(value))


class Phone:
    """A device with a Face ID passkey: makes registration and sign-in answers."""

    def __init__(self, host=HOST):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b"cred-" + hashlib.sha256(str(id(self)).encode()).digest()[:12]
        self.host, self.counter = host, 0

    def _auth_data(self, flags, attested=False):
        rp = self.host.split(":")[0]
        data = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", self.counter)
        if attested:
            n = self.key.public_key().public_numbers()
            cose = {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
            data += bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + cbor(cose)
        return data

    def _client(self, kind, challenge, origin=None):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or f"https://{self.host}"}).encode()

    def register(self, options, *, flags=0x45, origin=None):
        client = self._client("webauthn.create", options["publicKey"]["challenge"], origin)
        att = cbor({"fmt": "none", "attStmt": {}, "authData": self._auth_data(flags, attested=True)})
        return dict(handle=options["handle"], client_data=passkeys.b64u(client), attestation=passkeys.b64u(att))

    def sign_in(self, options, *, flags=0x05, origin=None, counter=None, tamper=False):
        self.counter = self.counter + 1 if counter is None else counter
        client = self._client("webauthn.get", options["publicKey"]["challenge"], origin)
        auth = self._auth_data(flags)
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        if tamper:
            sig = sig[:-1] + bytes([sig[-1] ^ 1])
        return dict(handle=options["handle"], credential_id=passkeys.b64u(self.credential_id),
                    client_data=passkeys.b64u(client), authenticator_data=passkeys.b64u(auth),
                    signature=passkeys.b64u(sig))


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc = AuthService(str(tmp_path / "a.db"), "s" * 32, clock=clock, max_failed=4)
    svc.create_user("paul", PW)
    uid = svc.users()[0]["id"]
    return svc, uid, clock


def add(svc, uid, phone=None, password=PW, **kw):
    phone = phone or Phone()
    opts = svc.passkey_register_options(uid, HOST)
    svc.passkey_register(uid, name="iPhone", password=password, host=HOST, **phone.register(opts, **kw))
    return phone


def sign_in(svc, phone, **kw):
    opts = svc.passkey_login_options(HOST)
    return svc.passkey_login(host=HOST, device="iPhone", ip="1.2.3.4", **phone.sign_in(opts, **kw))


def test_register_then_sign_in_with_the_passkey(env):
    svc, uid, _ = env
    phone = add(svc, uid)
    assert [p["name"] for p in svc.passkeys_for(uid)] == ["iPhone"]
    token = sign_in(svc, phone)                      # no password, no authenticator code
    assert svc.authenticate(token).username == "paul"
    assert svc.passkeys_for(uid)[0]["last_used_at"] is not None
    assert any(r["result"] == "success" and r["method"] == "passkey" for r in svc.login_log())


def test_a_challenge_works_once(env):
    svc, uid, _ = env
    phone = add(svc, uid)
    opts = svc.passkey_login_options(HOST)
    answer = phone.sign_in(opts)
    assert svc.passkey_login(host=HOST, **answer)
    with pytest.raises(AuthError):
        svc.passkey_login(host=HOST, **answer)


@pytest.mark.parametrize("kw", [{"flags": 0x01}, {"flags": 0x04}, {"origin": "https://evil.example"}, {"tamper": True}],
                         ids=["no-face-id", "no-presence", "other-site", "bad-signature"])
def test_bad_answers_are_refused_and_logged_but_do_not_lock(env, kw):
    svc, uid, _ = env
    phone = add(svc, uid)
    with pytest.raises(AuthError) as e:
        sign_in(svc, phone, **kw)
    assert e.value.status == 401
    assert any(r["result"] == "bad_passkey" for r in svc.login_log())
    assert sign_in(svc, phone)                       # one bad try does not lock anything


def test_unknown_passkey_and_counter_rules(env):
    svc, uid, _ = env
    phone = add(svc, uid)
    with pytest.raises(AuthError):
        sign_in(svc, Phone())
    assert sign_in(svc, phone, counter=5)
    with pytest.raises(AuthError):
        sign_in(svc, phone, counter=5)               # a counter that does not go up means a cloned key
    assert sign_in(svc, phone, counter=6)


def test_synced_passkeys_that_always_report_zero_are_fine(env):
    svc, uid, _ = env
    phone = add(svc, uid)
    assert sign_in(svc, phone, counter=0) and sign_in(svc, phone, counter=0)


def test_registration_refuses_wrong_site_no_face_id_and_duplicates(env):
    svc, uid, _ = env
    for kw in ({"origin": "https://evil.example"}, {"flags": 0x41}):
        with pytest.raises(AuthError) as e:
            add(svc, uid, **kw)
        assert e.value.status == 400
    assert svc.passkeys_for(uid) == []
    phone = add(svc, uid)
    with pytest.raises(AuthError):
        add(svc, uid, phone)


def test_adding_a_passkey_needs_the_password_and_guessing_is_rate_limited(env):
    svc, uid, _ = env
    with pytest.raises(AuthError) as e:
        add(svc, uid, password="not the password")
    assert e.value.detail == "Wrong password"
    statuses = []
    for i in range(6):
        try:
            add(svc, uid, password=f"wrong guess {i}")
        except AuthError as err:
            statuses.append(err.status)
    assert statuses[-1] == 429
    assert svc.passkeys_for(uid) == []


def test_a_locked_sign_in_refuses_passkeys_too_and_unlock_restores_them(env):
    svc, uid, _ = env
    phone = add(svc, uid)
    svc.db.execute("UPDATE users SET locked_at=1")
    with pytest.raises(AuthError) as e:
        sign_in(svc, phone)
    assert e.value.status == 423
    svc.unlock()
    assert sign_in(svc, phone)


def test_passkeys_belong_to_their_login_and_can_be_removed(env):
    svc, uid, _ = env
    phone = add(svc, uid)
    svc.create_user("sam", "another long password")
    sam = [u["id"] for u in svc.users() if u["username"] == "sam"][0]
    assert svc.passkeys_for(sam) == []
    pk = svc.passkeys_for(uid)[0]["id"]
    assert svc.remove_passkey(sam, pk) is False
    assert svc.remove_passkey(uid, pk) is True
    with pytest.raises(AuthError):
        sign_in(svc, phone)


def test_cbor_reader_and_relying_party_rules():
    sample = {"a": [1, -7, b"xy", "hi"], 3: {-1: 2}}
    assert passkeys.cbor_decode(cbor(sample)) == sample
    for bad in (b"\xff", b"\x45ab", bytes([0xA1, 0x80, 0x01])):
        with pytest.raises(passkeys.PasskeyError):
            passkeys.cbor_decode(bad)
    with pytest.raises(passkeys.PasskeyError):
        passkeys.rp_id_for("192.168.1.5:8080")       # needs a name, not an address
    assert passkeys.rp_id_for("nas.example.ts.net:10000") == "nas.example.ts.net"
