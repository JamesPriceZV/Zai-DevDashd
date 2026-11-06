"""HTTPS dashboard. Plain HTTP redirects, except the public /trust certificate bootstrap."""
from __future__ import annotations

import hmac
import html
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from zai_devdashd import auth
from zai_devdashd import config as site
from zai_devdashd import labels as board
from zai_devdashd.aggregator import ENGINE, redact
from zai_devdashd.probes import log_exc, run_cmd

site.install(globals())
_SAFE_HOST = re.compile(r"\A[A-Za-z0-9._-]{1,253}(?::[0-9]{1,5})?\Z")
_SAFE_TARGET = re.compile(r"\A/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*\Z")

PUBLIC_STATIC = {
    "favicon.svg": "image/svg+xml",
    "logo.svg": "image/svg+xml",
}
LOGGED_IN_STATIC = {}
PAGE_BYTES = (STATIC_DIR / 'page.html').read_bytes()
def schedule_live_restart() -> bool:
    """Restart only an explicitly enabled service in this package's namespace."""
    service = site.CURRENT.get("service") or {}
    label = service.get("label")
    if service.get("allow_restart") is not True or not isinstance(label, str) or not re.fullmatch(
            r"org\.zai\.devdashd(?:\.[A-Za-z0-9_-]{1,64})?", label):
        return False
    uid = os.getuid()
    subprocess.Popen(
        ["/bin/launchctl", "kickstart", "-k", "gui/%d/%s" % (uid, label)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError, ssl.SSLError):
            return

    def log_message(self, fmt: str, *args) -> None:
        if args and len(args) > 1 and str(args[1]).startswith("2"):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def peer_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def cookies(self) -> dict:
        found: dict[str, str] = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            found[key.strip()] = value.strip()
        return found

    def client_ok(self) -> bool:
        return board.client_allowed(self.peer_ip())

    def localhost_open(self) -> bool:
        cfg = ENGINE.config
        return auth.localhost_unauth(
            self.peer_ip(),
            self.headers.get("X-Forwarded-For"),
            self.headers.get("Forwarded"),
            bool(cfg.get("allow_localhost_unauth", True)),
        )

    def session_info(self) -> tuple[str, dict | None]:
        token = self.cookies().get(auth.COOKIE) or ""
        return token, ENGINE.gate.session(token)

    def authorized(self) -> bool:
        _token, item = self.session_info()
        if item:
            return True
        return self.localhost_open()

    def request_csrf(self) -> str:
        _token, item = self.session_info()
        if item:
            return str(item.get("csrf") or "")
        if self.localhost_open():
            return ENGINE.csrf
        return ""

    def origin_ok(self) -> bool:
        return board.same_origin(
            self.headers.get("Host"),
            self.headers.get("Origin"),
            self.headers.get("Referer"),
        )

    def log_origin_mismatch(self) -> None:
        """Name the Host, Origin, and Referer values that failed. No cookies or tokens."""
        host = (self.headers.get("Host") or "").replace("\r", " ").replace("\n", " ").strip()[:200]
        origin = (self.headers.get("Origin") or "").replace("\r", " ").replace("\n", " ").strip()[:200]
        referer = (self.headers.get("Referer") or "").replace("\r", " ").replace("\n", " ").strip()[:200]
        host_l = host.lower()
        mismatched = []
        origin_netloc = (urlparse(origin).netloc or "").lower() if origin else ""
        referer_netloc = (urlparse(referer).netloc or "").lower() if referer else ""
        if not origin or origin.lower() == "null" or origin_netloc != host_l:
            mismatched.append("Origin")
        if not referer or referer.lower() == "null" or referer_netloc != host_l:
            mismatched.append("Referer")
        sys.stderr.write(
            "auth origin mismatch header=%s host=%s origin=%s referer=%s\n"
            % (",".join(mismatched) or "Origin", host or "-", origin or "-", referer or "-")
        )

    def read_form(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > 65536:
            return {}
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type") or ""
        if "application/json" in ctype:
            try:
                payload = json.loads(raw.decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}
        parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        return {key: (values[-1] if values else "") for key, values in parsed.items()}

    def _digest(self, sent: str, expected: str) -> bool:
        if not sent or not expected:
            return False
        try:
            return hmac.compare_digest(sent, expected)
        except (TypeError, ValueError):
            return False

    def csrf_ok(self, form: dict) -> bool:
        sent = str(form.get("csrf_token") or form.get("csrf") or self.headers.get("X-CSRF-Token") or "")
        return self._digest(sent, self.request_csrf())

    def pre_csrf_ok(self, form: dict) -> bool:
        sent = str(form.get("csrf_token") or form.get("csrf") or self.headers.get("X-CSRF-Token") or "")
        item = ENGINE.gate.pre_session(self.cookies().get(auth.PRE_COOKIE))
        if not item:
            return False
        return self._digest(sent, str(item.get("csrf") or ""))

    def guard_action(self, form: dict) -> bool:
        if not self.client_ok() or not self.origin_ok() or not self.csrf_ok(form):
            self.respond(403, "text/plain; charset=utf-8", b"forbidden\n")
            return False
        return True

    def finish_action(self, message: str, status: int = 200) -> None:
        accept = (self.headers.get("Accept") or "") + (self.headers.get("Content-Type") or "")
        if "application/json" in accept:
            raw = json.dumps({"ok": status < 400, "message": message}).encode("utf-8")
            self.respond(status, "application/json; charset=utf-8", raw)
            return
        body = (
            "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
            "<title>Monitor</title></head><body><main><h1>Monitor</h1><p>%s</p>"
            "<p><a href=\"/settings\">Back to settings</a></p></main></body></html>"
            % html.escape(message)
        ).encode("utf-8")
        self.respond(status, "text/html; charset=utf-8", body)

    def deny(self) -> None:
        path = urlparse(self.path).path
        if self.command == "GET" and path in ("/", "/index.html", "/settings"):
            self.redirect("/login")
            return
        self.respond(401, "application/json; charset=utf-8", b'{"error":"login"}\n')

    def auth_group(self) -> str:
        return str(ENGINE.config.get("auth_group") or "zai-devdashd")

    def group_missing(self) -> bool:
        group = self.auth_group()
        return not auth.valid_group(group) or not auth.group_exists(group)

    def send_login(self, status: int, notice: str) -> None:
        token, csrf = ENGINE.gate.create_pre()
        group = self.auth_group()
        _pem, _der, fingerprint = auth.trust_material(ROOT)
        body = auth.login_page(csrf, group, notice, self.group_missing(), fingerprint)
        self.respond(
            status,
            "text/html; charset=utf-8",
            body,
            extra=[("Set-Cookie", auth.pre_cookie(token))],
        )

    def post_login(self, form: dict) -> None:
        ip = self.peer_ip()
        group = self.auth_group()
        user = str(form.get("username") or "")
        if not self.origin_ok():
            self.log_origin_mismatch()
            self.send_login(400, auth.EXPIRED_NOTICE)
            return
        if not self.pre_csrf_ok(form):
            self.send_login(400, auth.EXPIRED_NOTICE)
            return
        wait = ENGINE.gate.locked_seconds(ip)
        if wait:
            auth.log_attempt(user, ip, "locked")
            self.send_login(429, auth.login_notice("locked", group, wait))
            return
        result = auth.authenticate(user, str(form.get("password") or ""), group)
        auth.log_attempt(user, ip, result)
        if result == "ok":
            ENGINE.gate.record_success(ip)
            ttl = int(ENGINE.config.get("session_ttl_s") or 12 * 3600)
            token, _csrf = ENGINE.gate.create_session(user, ttl)
            ENGINE.gate.drop_pre(self.cookies().get(auth.PRE_COOKIE))
            self.redirect("/", [auth.session_cookie(token, ttl), auth.pre_cookie("", 0)])
            return
        ENGINE.gate.record_failure(ip)
        if result == "no-group":
            self.send_login(403, auth.login_notice(result, group))
            return
        self.send_login(401, auth.login_notice(result, group))

    def reject_key_path(self, path: str) -> bool:
        if path.lower().endswith(".key"):
            self.respond(404, "text/plain; charset=utf-8", b"not found\n")
            return True
        return False

    def send_trust(self, kind: str) -> None:
        pem, der, _fingerprint = auth.trust_material(ROOT)
        if kind == "cer" and der:
            self.respond(200, "application/x-x509-ca-cert", der)
            return
        if kind == "pem" and pem:
            self.respond(200, "application/x-pem-file", pem)
            return
        self.respond(404, "text/plain; charset=utf-8", b"not found\n")

    def send_static(self, name: str, kinds: dict) -> None:
        content_type = kinds.get(name)
        path = STATIC_DIR / name
        if (
            not content_type
            or "/" in name
            or not path.is_file()
            or path.is_symlink()
        ):
            self.respond(404, "text/plain; charset=utf-8", b"not found\n")
            return
        self.respond(200, content_type, path.read_bytes())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if self.reject_key_path(path):
                return
            form = self.read_form()
            if path == "/login":
                self.post_login(form)
                return
            if not self.authorized():
                self.deny()
                return
            if path == "/logout":
                if not self.origin_ok() or not self.csrf_ok(form):
                    self.respond(403, "text/plain; charset=utf-8", b"forbidden\n")
                    return
                token, _item = self.session_info()
                ENGINE.gate.drop_session(token)
                self.redirect("/login", [auth.session_cookie("", 0)])
                return
            if not self.guard_action(form):
                return
            if path == "/action/settings":
                self.finish_action(ENGINE.action_save_settings(form))
                return
            if path == "/action/reconnect-worker":
                self.finish_action(ENGINE.action_reconnect())
                return
            if path == "/action/refresh-devices":
                self.finish_action(ENGINE.action_refresh_devices())
                return
            if path == "/action/open-device-console":
                token = str(form.get("udid") or form.get("identifier") or "")
                message, status = ENGINE.action_open_console(token)
                self.finish_action(message, status)
                return
            if path == "/action/restart":
                service = site.CURRENT.get("service") or {}
                if service.get("allow_restart") is not True:
                    self.finish_action("Service restart is disabled. Restart your foreground process manually.", 409)
                    return
                self.finish_action("Restart requested for the configured Zai-DevDashd service.")
                threading.Timer(0.4, schedule_live_restart).start()
                return
            self.respond(404, "text/plain; charset=utf-8", b"not found\n")
        except Exception as exc:
            log_exc("action", exc)
            try:
                self.finish_action("Action failed: %s" % exc.__class__.__name__, 500)
            except Exception:
                return

    def send_vncloc(self, path: str) -> None:
        name = unquote(path[len("/vncloc/"):]).strip()
        names = list(ENGINE.rfb_names)
        if not name or "/" in name or "\\" in name or name not in names:
            self.respond(404, "text/plain; charset=utf-8", b"not found\n")
            return
        href = board.vnc_href(name, "")
        if not href.startswith("vnc://"):
            self.respond(404, "text/plain; charset=utf-8", b"not found\n")
            return
        self.respond(
            200,
            "application/octet-stream",
            board.vncloc_plist(href),
            extra=[("Content-Disposition", 'attachment; filename="screen.vncloc"')],
        )

    def page_bytes(self) -> bytes:
        token = self.request_csrf().encode("ascii", "ignore")
        return PAGE_BYTES.replace(b"__CSRF_TOKEN__", token)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if self.reject_key_path(path):
                return
            if path == "/healthz":
                self.respond(200, "text/plain; charset=utf-8", b"ok\n")
                return
            if path == "/login":
                self.send_login(200, "")
                return
            if path == "/trust/root-ca.cer":
                self.send_trust("cer")
                return
            if path == "/trust/root-ca.pem":
                self.send_trust("pem")
                return
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name in PUBLIC_STATIC:
                    self.send_static(name, PUBLIC_STATIC)
                    return
            if not self.authorized():
                self.deny()
                return
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                self.send_static(name, LOGGED_IN_STATIC)
                return
            if path == "/api/events":
                self.stream_events()
                return
            if path == "/api/state":
                self.respond(200, "application/json; charset=utf-8", ENGINE.payload or b"{}")
                return
            if path == "/api/resources":
                try:
                    snap = json.loads(ENGINE.payload or b"{}")
                    block = ((snap.get("utilization") or {}).get("executive") or {}).get("v2") or {}
                except Exception:
                    block = {}
                self.respond(200, "application/json; charset=utf-8", json.dumps(redact(block), separators=(",", ":"), default=str).encode("utf-8"))
                return
            if path == "/api/test-pool":
                self.respond(200, "application/json; charset=utf-8", ENGINE.test_pool_body())
                return
            if path == "/api/insights":
                try:
                    snap = json.loads(ENGINE.payload or b"{}")
                    block = ENGINE.store.public_block(ENGINE._insight_keys(snap if isinstance(snap, dict) else {}))
                except Exception:
                    block = {"jobs": []}
                raw = json.dumps(redact(block), separators=(",", ":"), default=str).encode("utf-8")
                self.respond(200, "application/json; charset=utf-8", raw)
                return
            if path in ("/", "/index.html"):
                self.respond(200, "text/html; charset=utf-8", self.page_bytes())
                return
            if path.startswith("/vncloc/"):
                self.send_vncloc(path)
                return
            if path == "/settings":
                if not self.client_ok():
                    self.respond(403, "text/plain; charset=utf-8", b"forbidden\n")
                    return
                self.respond(200, "text/html; charset=utf-8", ENGINE.settings_body(csrf=self.request_csrf()))
                return
            self.respond(404, "text/plain; charset=utf-8", b"not found\n")
        except Exception:
            try:
                self.respond(500, "text/plain; charset=utf-8", b"error\n")
            except Exception:
                return

    def security_headers(self) -> None:
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.send_header("Strict-Transport-Security", "max-age=0")  # self-signed LAN CA: never pin HSTS (blocks first-time trust install)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")

    def respond(self, status: int, content_type: str, body: bytes, extra: list | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        for key, value in extra or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, cookies: list | None = None, status: int = 303) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.security_headers()
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.security_headers()
        self.end_headers()
        last = -1
        try:
            while True:
                payload, version = ENGINE.wait_snapshot(last, 15)
                if version == last:
                    self.write_chunk(b": keepalive\n\n")
                else:
                    self.write_chunk(b"event: state\ndata: " + payload + b"\n\n")
                    last = version
        except (BrokenPipeError, ConnectionResetError, TimeoutError, ssl.SSLError):
            return
        except Exception:
            return

    def write_chunk(self, data: bytes) -> None:
        self.wfile.write(("%X\r\n" % len(data)).encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()
def listener() -> tuple[int, str] | None:
    code, out, _err = run_cmd(["/usr/sbin/lsof", "-nP", "-iTCP:%d" % PORT, "-sTCP:LISTEN", "-Fp"], 2)
    if code != 0 and not out:
        return None
    pids = []
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pids.append(int(line[1:]))
    for pid in pids:
        if pid == os.getpid():
            continue
        _code, cmd, _err = run_cmd(["/bin/ps", "-p", str(pid), "-o", "command="], 3)
        return pid, cmd.decode("utf-8", "replace").strip()
    return None
class ReuseHTTPServer(ThreadingHTTPServer):
    # A kickstart leaves the previous listener's connections in TIME_WAIT.
    # Without this, bind fails and lsof -sTCP:LISTEN names no owner.
    allow_reuse_address = True

    def __init__(self, address, handler, certfile: str, keyfile: str) -> None:
        super().__init__(address, handler)
        self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ssl_context.load_cert_chain(certfile, keyfile)

    def process_request(self, request, client_address) -> None:
        try:
            request.settimeout(3)
            peeked = request.recv(1, socket.MSG_PEEK)
        except OSError:
            self.shutdown_request(request)
            return
        if not peeked:
            self.shutdown_request(request)
            return
        if peeked[0] != 0x16:
            self._plain_http(request)
            return
        try:
            wrapped = self.ssl_context.wrap_socket(request, server_side=True)
            wrapped.settimeout(None)
        except (ssl.SSLError, OSError):
            self.shutdown_request(request)
            return
        super().process_request(wrapped, client_address)

    def _plain_http(self, request) -> None:
        """A non-TLS first byte gets a 301 when the target is safe, otherwise a close."""
        try:
            request.settimeout(2)
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 8192:
                chunk = request.recv(1024)
                if not chunk:
                    break
                data += chunk
            target = "/"
            host = ""
            lines = data.split(b"\r\n")
            if lines and lines[0]:
                parts = lines[0].split(b" ")
                if len(parts) > 1:
                    target = parts[1].decode("latin-1", "replace")
            for line in lines:
                if line.lower().startswith(b"host:"):
                    host = line.split(b":", 1)[1].strip().decode("latin-1", "replace")
                    break
            bootstrap = plain_trust_response(target, host)
            if bootstrap is not None:
                request.sendall(bootstrap)
            elif host and _SAFE_HOST.match(host) and _SAFE_TARGET.match(target):
                location = "https://%s%s" % (host, target)
                body = ("https required. First visit? Install the dashboard certificate at http://%s/trust/\n" % host).encode("latin-1", "replace")
                raw = (
                    "HTTP/1.1 301 Moved Permanently\r\n"
                    "Location: %s\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "Content-Length: %d\r\n"
                    "Connection: close\r\n"
                    "Cache-Control: no-store\r\n"
                    "\r\n" % (location, len(body))
                ).encode("ascii")
                request.sendall(raw + body)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

TRUST_STEPS_HTML = (
    "<p><strong>iPhone or iPad:</strong> tap the certificate link in Safari and allow the download. "
    "Open Settings, tap Profile Downloaded, then Install. Then go to Settings &gt; General &gt; About &gt; "
    "Certificate Trust Settings and turn on full trust for Zai-DevDashd Dev Dashboard Root CA.</p>"
    "<p><strong>Mac:</strong> open the .cer file, add it to the System keychain in Keychain Access, "
    "double-click it, open Trust, and set When using this certificate to Always Trust.</p>"
    "<p><strong>Windows:</strong> open the .cer file, choose Install Certificate, Local Machine, "
    "and place it in Trusted Root Certification Authorities.</p>"
    "<p><strong>Firefox:</strong> Settings &gt; Privacy &amp; Security &gt; Certificates &gt; View Certificates &gt; "
    "Authorities &gt; Import, and trust it for websites.</p>"
)


def plain_trust_response(target: str, host: str):
    """Plain-HTTP bootstrap: only the PUBLIC root and an install page. No HSTS, no redirect. Never keys."""
    import html as _html
    path = (target or "/").split("?", 1)[0].split("#", 1)[0]
    if path.lower().endswith(".key"):
        return _plain_raw(404, "text/plain; charset=utf-8", b"not found\n")
    if path not in ("/trust", "/trust/", "/trust/root-ca.cer", "/trust/root-ca.pem"):
        return None
    pem, der, fingerprint = auth.trust_material(ROOT)
    if path == "/trust/root-ca.cer":
        return _plain_raw(200, "application/x-x509-ca-cert", der) if der else _plain_raw(404, "text/plain; charset=utf-8", b"not found\n")
    if path == "/trust/root-ca.pem":
        return _plain_raw(200, "application/x-pem-file", pem) if pem else _plain_raw(404, "text/plain; charset=utf-8", b"not found\n")
    safe_host = host if (host and _SAFE_HOST.match(host)) else ""
    https_link = ("https://%s/" % safe_host) if safe_host else "/"
    body = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>Trust the Zai-DevDashd Developer Dashboard</title>"
        "<style>body{font:16px -apple-system,system-ui,sans-serif;margin:0;background:#0b2a4a;color:#0b2a4a}"
        "main{max-width:640px;margin:0 auto;padding:24px}.card{background:#fff;border-radius:12px;padding:20px}"
        "h1{color:#fff;font-size:22px}code{word-break:break-all}a.btn{display:inline-block;background:#1f9a4a;color:#fff;"
        "padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:600}</style></head><body><main>"
        "<h1>Zai-DevDashd – Developer Dashboard</h1><div class=card>"
        "<h2>Step 1: trust the dashboard certificate</h2>"
        "<p><a class=btn href='/trust/root-ca.cer'>Download the certificate</a> · <a href='/trust/root-ca.pem'>PEM file</a></p>"
        "<p>Check the SHA-256 fingerprint: <code>%s</code></p>%s"
        "<h2>Step 2: open the dashboard</h2><p><a href='%s'>%s</a></p>"
        "</div></main></body></html>"
    ) % (_html.escape(fingerprint or ""), TRUST_STEPS_HTML, _html.escape(https_link), _html.escape(https_link))
    return _plain_raw(200, "text/html; charset=utf-8", body.encode("utf-8"))


def _plain_raw(code: int, ctype: str, body: bytes) -> bytes:
    reason = {200: "OK", 404: "Not Found"}.get(code, "OK")
    head = (
        "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
        "Connection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n"
        % (code, reason, ctype, len(body))
    ).encode("ascii")
    return head + body

def ensure_port_free() -> None:
    if not socket_bind_ok():
        sys.stderr.write("port %d is busy; release it or choose another port\n" % PORT)
        raise SystemExit(1)
def socket_bind_ok() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        sock.close()
def serve() -> None:
    site.ensure_loaded()
    site.install(globals())
    (ROOT / "tmp").mkdir(exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    cert, key = auth.ensure_certificate(ROOT)
    if site.adapter_enabled("remote") or site.adapter_enabled("devices"):
        try:
            ENGINE.refresh_rfb()
        except Exception as exc:
            log_exc("rfb", exc)
    ensure_port_free()
    server = ReuseHTTPServer((HOST, PORT), Handler, str(cert), str(key))
    server.daemon_threads = True
    threading.Thread(target=ENGINE.fast_loop, name="mon-fast", daemon=True).start()
    threading.Thread(target=ENGINE.slow_loop, name="mon-slow", daemon=True).start()
    if (site.CURRENT.get("labels") or {}).get("enabled") is True:
        threading.Thread(target=ENGINE.label_loop, name="mon-label", daemon=True).start()
    threading.Thread(target=ENGINE.aggregate_loop, name="mon-aggregate", daemon=True).start()
    try:
        ENGINE.publish()
    except Exception as exc:
        log_exc("startup", exc)
    sys.stderr.write("monitor listening on https://%s:%d\n" % (HOST, PORT))
    server.serve_forever()

