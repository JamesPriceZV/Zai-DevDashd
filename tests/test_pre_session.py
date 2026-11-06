"""Signed pre-sessions survive a restart. A bad login page is 400, not a bare 403."""
import io
import os
import secrets
import tempfile
import unittest
from contextlib import redirect_stderr
from email.message import EmailMessage
from pathlib import Path

from zai_devdashd import auth
from zai_devdashd import server
from zai_devdashd.server import Handler
from types import SimpleNamespace
from unittest.mock import patch

HOST = "192.0.2.10:8780"
ORIGIN = "https://192.0.2.10:8780"


def _handler(origin, cookie, peer):
    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/login"
    handler.requestline = "POST /login HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.client_address = (peer, 9)
    handler.close_connection = False
    handler.rfile = io.BytesIO()
    handler.wfile = io.BytesIO()
    headers = EmailMessage()
    headers["Host"] = HOST
    if origin is not None:
        headers["Origin"] = origin
    if cookie:
        headers["Cookie"] = "zai_devdashd_pre=" + cookie
    handler.headers = headers
    return handler


def _response(handler):
    raw = handler.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    code = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers.setdefault(key.lower(), []).append(value.strip())
    return code, headers, body


class PreSession(unittest.TestCase):
    def setUp(self):
        self.key = secrets.token_bytes(32)
        self.gate = auth.Gate(pre_key=self.key)
        # Patch the server authority before any field dereference: the public
        # lazy Engine must never be constructed by these synthetic HTTP tests.
        engine = SimpleNamespace(gate=self.gate, config={"auth_group": "synthetic-auth"})
        engine_patch = patch.object(server, "ENGINE", engine)
        engine_patch.start(); self.addCleanup(engine_patch.stop)
        env_patch = patch.dict(os.environ, {auth.FAKE_ENV: "1"})
        env_patch.start(); self.addCleanup(env_patch.stop)
        # These tests exercise login/session/origin behavior, not certificate
        # files. Do not read a process-global state path or invoke OpenSSL here.
        trust_patch = patch.object(auth, "trust_material", return_value=(b"", b"", "SYNTHETIC-FINGERPRINT"))
        trust_patch.start(); self.addCleanup(trust_patch.stop)

    def test_restart_keeps_the_same_token(self):
        now = 1_700_000_000.0
        first = auth.Gate(pre_key=self.key)
        token, csrf = first.create_pre(now)
        second = auth.Gate(pre_key=self.key)
        item = second.pre_session(token, now + 10)
        self.assertIsNotNone(item)
        self.assertEqual(item["csrf"], csrf)
        self.assertIsNone(second.pre_session(token, now + auth.PRE_TTL))
        stranger = auth.Gate(pre_key=secrets.token_bytes(32))
        self.assertIsNone(stranger.pre_session(token, now + 10))

    def test_expired_token_rerenders_login(self):
        token, csrf = self.gate.create_pre(now=1_000)
        peer = "203.0.113.61"
        handler = _handler(ORIGIN, token, peer)
        handler.post_login({"username": "smoke", "password": "not-the-fixture", "csrf_token": csrf})
        code, headers, body = _response(handler)
        self.assertEqual(code, 400)
        self.assertIn(auth.EXPIRED_NOTICE.encode(), body)
        issued = " ".join(headers.get("set-cookie", []))
        self.assertIn("zai_devdashd_pre=", issued)
        self.assertNotIn(token, issued)
        self.assertEqual(self.gate.locked_seconds(peer), 0)
        self.assertEqual(self.gate.failures.get(peer, []), [])

    def test_missing_cookie_is_400(self):
        peer = "203.0.113.62"
        handler = _handler(ORIGIN, "", peer)
        handler.post_login({"username": "smoke", "password": "not-the-fixture", "csrf_token": "missing"})
        code, headers, body = _response(handler)
        self.assertEqual(code, 400)
        self.assertIn(auth.EXPIRED_NOTICE.encode(), body)
        self.assertIn("zai_devdashd_pre=", " ".join(headers.get("set-cookie", [])))
        self.assertNotIn(b"forbidden", body)
        self.assertEqual(self.gate.failures.get(peer, []), [])

    def test_origin_without_port_is_400_and_logged(self):
        token, csrf = self.gate.create_pre()
        peer = "203.0.113.63"
        handler = _handler("https://192.0.2.10", token, peer)
        captured = io.StringIO()
        with redirect_stderr(captured):
            handler.post_login({
                "username": "smoke",
                "password": "not-the-fixture",
                "csrf_token": csrf,
            })
        code, _headers, body = _response(handler)
        self.assertEqual(code, 400)
        self.assertIn(auth.EXPIRED_NOTICE.encode(), body)
        log = captured.getvalue()
        self.assertIn("auth origin mismatch", log)
        self.assertIn("header=Origin", log)
        self.assertIn("host=%s" % HOST, log)
        self.assertIn("origin=https://192.0.2.10", log)
        self.assertNotIn(token, log)
        self.assertNotIn(csrf, log)
        self.assertEqual(self.gate.failures.get(peer, []), [])

    def test_bad_password_reaches_auth(self):
        token, csrf = self.gate.create_pre()
        peer = "203.0.113.64"
        handler = _handler(ORIGIN, token, peer)
        handler.post_login({
            "username": "smoke",
            "password": "not-the-fixture",
            "csrf_token": csrf,
        })
        code, _headers, body = _response(handler)
        self.assertEqual(code, 401)
        self.assertIn(b"The password was not accepted.", body)
        self.assertNotIn(auth.EXPIRED_NOTICE.encode(), body)
        self.assertEqual(len(self.gate.failures.get(peer, [])), 1)
        self.assertEqual(self.gate.locked_seconds(peer), 0)

    def test_key_file_is_mode_0600_and_stable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data" / "pre_session.key"
            first = auth.load_pre_key(path)
            second = auth.load_pre_key(path)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
