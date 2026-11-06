"""Local-account login, sessions, and the TLS certificate.

Passwords are passed only as a dscl argv. They are never logged, stored,
or placed in a URL. ZAI_DEVDASHD_FAKE_AUTH=1 is the smoke-test authenticator
and must not be set on the live LaunchAgent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import ssl
import stat
import tempfile
import subprocess
import sys
import threading
import time
from pathlib import Path

from zai_devdashd import config as site

USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
GROUP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
FAKE_ENV = "ZAI_DEVDASHD_FAKE_AUTH"
FAKE_USER = "smoke"
FAKE_PASSWORD = "smoke-secret"
FAKE_OUTSIDER = "outsider"
LOCK_WINDOW = 15 * 60
LOCK_BASE = 15 * 60
LOCK_CAP = 24 * 3600
COOKIE = "zai_devdashd_session"
PRE_COOKIE = "zai_devdashd_pre"
PRE_TTL = 600
EXPIRED_NOTICE = "Your sign-in page expired. Please try again."


def valid_user(name: str | None) -> bool:
    return bool(USER_RE.match(name or ""))


def valid_group(name: str | None) -> bool:
    return bool(GROUP_RE.match(name or ""))


def membership_ok(text: str | None) -> bool:
    flat = " ".join((text or "").split()).lower()
    if "not a member" in flat or "cannot be found" in flat or "group not found" in flat:
        return False
    return "is a member" in flat


def localhost_unauth(ip: str | None, forwarded_for: str | None, forwarded: str | None, allowed: bool) -> bool:
    """True only for a direct loopback peer with no forwarding headers."""
    if not allowed:
        return False
    raw = (ip or "").split("%", 1)[0].strip()
    if raw.startswith("::ffff:"):
        raw = raw[7:]
    if raw not in ("127.0.0.1", "::1"):
        return False
    if (forwarded_for or "").strip() or (forwarded or "").strip():
        return False
    return True


def session_cookie(token: str, max_age: int) -> str:
    if max_age <= 0 or not token:
        return "%s=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" % COOKIE
    return "%s=%s; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (COOKIE, token, max_age)


def pre_cookie(token: str, max_age: int = PRE_TTL) -> str:
    if max_age <= 0 or not token:
        return "%s=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" % PRE_COOKIE
    return "%s=%s; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (PRE_COOKIE, token, max_age)


def pre_key_path(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else Path(site.ROOT)
    return base / "data" / "pre_session.key"


def load_pre_key(path: Path) -> bytes:
    """Return the HMAC key, creating a 0600 file when it is missing.

    The bytes are never logged. A symlink is refused.
    """
    path = Path(path)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise OSError("pre-session key must be a regular file")
    if path.is_file():
        data = path.read_bytes()
        if len(data) >= 32:
            os.chmod(path, 0o600)
            return data
    data = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise OSError("pre-session key must be a regular file")
        data = path.read_bytes()
        os.chmod(path, 0o600)
        return data
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return data


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def log_attempt(user: str, ip: str, result: str) -> None:
    safe_user = user if valid_user(user) else "-"
    safe_ip = (ip or "-").split("%", 1)[0][:64]
    sys.stderr.write("auth %s %s %s\n" % (safe_user, safe_ip, result))


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    """Run a command. The caller must not log args: dscl's argv holds the password."""
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""
    text = (completed.stdout or b"").decode("utf-8", "replace")
    text += (completed.stderr or b"").decode("utf-8", "replace")
    return completed.returncode, text


def group_exists(group: str) -> bool:
    if os.environ.get(FAKE_ENV) == "1":
        return True
    if not valid_group(group):
        return False
    code, _text = _run(["/usr/bin/dscl", ".", "-read", "/Groups/" + group], 5)
    return code == 0


def authenticate(user: str, password: str, group: str) -> str:
    """Return ok, bad-user, bad-password, not-member, or no-group. Never echo the password."""
    if not valid_user(user):
        return "bad-user"
    if not password or "\x00" in password or len(password) > 1024:
        return "bad-password"
    if os.environ.get(FAKE_ENV) == "1":
        if password != FAKE_PASSWORD:
            return "bad-password"
        if user == FAKE_USER:
            return "ok"
        return "not-member"
    if not valid_group(group) or not group_exists(group):
        return "no-group"
    code, _text = _run(["/usr/bin/dscl", ".", "-authonly", user, password], 8)
    if code != 0:
        return "bad-password"
    code, text = _run(
        ["/usr/bin/dsmemberutil", "checkmembership", "-U", user, "-G", group],
        5,
    )
    if code == 0 and membership_ok(text):
        return "ok"
    return "not-member"


class Gate:
    def __init__(self, pre_key: bytes | None = None) -> None:
        self.lock = threading.Lock()
        self.failures: dict[str, list[float]] = {}
        self.locked_until: dict[str, float] = {}
        self.level: dict[str, int] = {}
        self.sessions: dict[str, dict] = {}
        self._pre_key = pre_key

    def locked_seconds(self, ip: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self.lock:
            until = self.locked_until.get(ip, 0.0)
            if until > now:
                return int(until - now)
            return 0

    def record_failure(self, ip: str, now: float | None = None) -> bool:
        """Count a failure. Return True when this failure starts a lockout."""
        now = time.time() if now is None else now
        with self.lock:
            stamps = [stamp for stamp in self.failures.get(ip, []) if now - stamp < LOCK_WINDOW]
            stamps.append(now)
            if len(stamps) < 5:
                self.failures[ip] = stamps
                return False
            self.failures[ip] = []
            level = self.level.get(ip, 0) + 1
            self.level[ip] = level
            duration = min(LOCK_CAP, LOCK_BASE * (2 ** (level - 1)))
            self.locked_until[ip] = now + duration
            return True

    def record_success(self, ip: str) -> None:
        with self.lock:
            self.failures.pop(ip, None)
            self.level.pop(ip, None)
            self.locked_until.pop(ip, None)

    def create_session(self, user: str, ttl: int, now: float | None = None) -> tuple[str, str]:
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self.lock:
            dead = [key for key, item in self.sessions.items() if item["expires"] <= now]
            for key in dead:
                self.sessions.pop(key, None)
            self.sessions[token] = {"user": user, "csrf": csrf, "expires": now + max(60, ttl)}
        return token, csrf

    def session(self, token: str | None, now: float | None = None) -> dict | None:
        if not token:
            return None
        now = time.time() if now is None else now
        with self.lock:
            item = self.sessions.get(token)
            if not item:
                return None
            if item["expires"] <= now:
                self.sessions.pop(token, None)
                return None
            return dict(item)

    def drop_session(self, token: str | None) -> None:
        if not token:
            return
        with self.lock:
            self.sessions.pop(token, None)

    def drop_pre(self, token: str | None) -> None:
        """The browser cookie is cleared by the caller. The signature stays valid until expiry."""
        return None

    def _signing_key(self) -> bytes:
        if self._pre_key is None:
            self._pre_key = load_pre_key(pre_key_path())
        return self._pre_key

    def _mac(self, message: bytes) -> str:
        digest = hmac.new(self._signing_key(), message, hashlib.sha256).digest()
        return _b64(digest)

    def create_pre(self, now: float | None = None) -> tuple[str, str]:
        """HMAC token: expiry, nonce, signature. The CSRF value is an HMAC of that token."""
        now = time.time() if now is None else float(now)
        expires = int(now) + PRE_TTL
        nonce = secrets.token_urlsafe(18)
        payload = "%d.%s" % (expires, nonce)
        token = "%s.%s" % (payload, self._mac(payload.encode("ascii")))
        csrf = self._mac(b"csrf." + token.encode("ascii"))
        return token, csrf

    def pre_session(self, token: str | None, now: float | None = None) -> dict | None:
        if not token or len(token) > 512 or token.count(".") != 2:
            return None
        exp_text, nonce, sent = token.split(".", 2)
        if not exp_text.isdigit() or not nonce or not sent:
            return None
        expires = int(exp_text)
        now = time.time() if now is None else float(now)
        if now >= expires:
            return None
        payload = "%s.%s" % (exp_text, nonce)
        expected = self._mac(payload.encode("ascii"))
        if len(sent) != len(expected) or not hmac.compare_digest(sent, expected):
            return None
        csrf = self._mac(b"csrf." + token.encode("ascii"))
        return {"csrf": csrf, "expires": expires}


def login_notice(result: str, group: str, retry: int = 0) -> str:
    if result == "no-group":
        return "The local group %s does not exist, so remote login is closed." % group
    if result == "not-member":
        return "That account is not in the group %s." % group
    if result == "bad-user":
        return "That username is not a local account name."
    if result == "locked":
        return "Too many sign-in attempts. Try again in %d seconds." % max(1, retry)
    if result == "bad-password":
        return "The password was not accepted."
    return "Sign-in failed."


TRUST_INPUT_LIMIT = 64 * 1024
TRUST_DER_LIMIT = 64 * 1024
TRUST_CONVERSION_TIMEOUT_S = 5.0


def _read_public_certificate(path: Path) -> bytes | None:
    """Read one bounded regular file without following a linked final name."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= TRUST_INPUT_LIMIT:
            return None
        chunks = []
        remaining = TRUST_INPUT_LIMIT + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity(before) != identity(after) or not 0 < len(raw) <= TRUST_INPUT_LIMIT:
            return None
        return raw
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def trust_material(root: Path) -> tuple[bytes, bytes, str]:
    """Export only a validated, canonical public certificate, never raw input.

    The first existing CA source remains authoritative, including when invalid.
    A fresh installation's self-signed server certificate is the fallback only
    when no separate CA material exists. Conversion has bounded input/output and
    a timeout; no original file bytes are returned on failure.
    """
    candidates = (
        (root / "certs" / "root-ca.pem", False),
        (site.trust_file("zai-devdashd-root-ca.pem"), False),
        (site.trust_file("zai-devdashd-root-ca.cer"), True),
        (root / "certs" / "server.crt", False),
    )
    selected = None
    for path, is_der in candidates:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return b"", b"", ""
        selected = path, is_der
        break
    if selected is None:
        return b"", b"", ""
    path, is_der = selected
    raw = _read_public_certificate(path)
    if not raw or b"PRIVATE KEY" in raw.upper():
        return b"", b"", ""
    command = ["/usr/bin/openssl", "x509"]
    if is_der:
        command += ["-inform", "DER"]
    command += ["-outform", "DER"]
    try:
        # x509 emits one certificate from the bounded input. Reject an invalid,
        # empty or oversized result before constructing any public response.
        completed = subprocess.run(
            command, input=raw, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=False,
            timeout=TRUST_CONVERSION_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return b"", b"", ""
    der = completed.stdout
    if (completed.returncode != 0 or not isinstance(der, bytes)
            or not 0 < len(der) <= TRUST_DER_LIMIT or b"PRIVATE KEY" in der.upper()):
        return b"", b"", ""
    # Successful x509 conversion validates the certificate. Canonicalizing only
    # its DER avoids exporting input comments, extra PEM blocks or file text.
    pem = ssl.DER_cert_to_PEM_cert(der).encode("ascii")
    digest = hashlib.sha256(der).hexdigest().upper()
    fingerprint = ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))
    return pem, der, fingerprint


def login_page(token: str, group: str, notice: str, group_missing: bool, fingerprint: str = "") -> bytes:
    import html as html_mod

    missing = ""
    if group_missing:
        missing = (
            "<p>The local group %s does not exist yet, so nobody can sign in remotely. "
            "This Mac can still open the monitor from itself.</p>"
            % html_mod.escape(group)
        )
    notice_html = "<p role=\"alert\">%s</p>" % html_mod.escape(notice) if notice else ""
    finger = html_mod.escape(fingerprint) if fingerprint else "not available on this Mac yet"
    body = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>Zai-DevDashd – Developer Dashboard</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">

<style>
  :root { color-scheme: light dark; }
  *, *::before, *::after { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    font: 16px/1.45 ui-sans-serif, system-ui, sans-serif;
    color: #f4f7f2;
    background: linear-gradient(180deg, #0b2a4a 0%%, #12385a 55%%, #145c3a 100%%);
  }
  main {
    max-width: 36rem;
    margin: 0 auto;
    padding: 24px 16px 48px;
  }
  .card {
    background: #ffffff;
    color: #102033;
    border-radius: 16px;
    padding: 20px 16px;
    margin-top: 16px;
  }
  @media (prefers-color-scheme: dark) {
    .card { background: #102033; color: #f4f7f2; }
    .card a { color: #9ecbff; }
  }
  img.logo { width: 96px; height: auto; }
  h1 { font-size: clamp(1.35rem, 1rem + 1.4vw, 1.8rem); margin: 8px 0; }
  label { display: block; font-weight: 700; margin-top: 12px; }
  input { display: block; width: 100%%; max-width: 100%%; margin-top: 4px; font: inherit; font-size: 1rem; padding: 8px; min-height: 48px; }
  button { font: inherit; font-weight: 700; margin-top: 16px; padding: 8px 14px; min-height: 48px; color: #ffffff; background: #0b2a4a; border: 0; border-radius: 8px; }
  .trust h2 { font-size: 1.05rem; }
  code { word-break: break-all; }
</style>
</head>
<body>
<main>
  <img class="logo" src="/static/logo.svg" alt="Zai-DevDashd" width="96" height="96">
  <h1>Zai-DevDashd – Developer Dashboard</h1>
  <div class="card">
  %(missing)s
  %(notice)s
  <form method="POST" action="/login">
    <input type="hidden" name="csrf_token" value="%(token)s">
    <label for="username">Account</label>
    <input id="username" name="username" type="text" autocomplete="username" autocapitalize="off" autocorrect="off" spellcheck="false" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
  <p>Use a local macOS account in the group %(group)s. The password stays on this Mac.</p>
  </div>
  <div class="card trust">
    <h2>Trust this dashboard</h2>
    <p>Download the dashboard certificate and trust it once. SHA-256 fingerprint: <code>%(fingerprint)s</code></p>
    <p><a href="/trust/root-ca.cer">Download the certificate</a> · <a href="/trust/root-ca.pem">PEM file</a></p>
    <p><strong>Mac:</strong> open the .cer file, choose the System keychain in Keychain Access, double-click the certificate, open Trust, and set When using this certificate to Always Trust.</p>
    <p><strong>iPhone or iPad:</strong> tap Download in Safari and allow it, then Settings → Profile Downloaded → Install. Then Settings → General → About → Certificate Trust Settings, and turn on full trust for Zai-DevDashd Dev Dashboard Root CA.</p>
    <p>For access from another device, ask your administrator for this dashboard's certificate and verify its fingerprint before trusting it.</p>
    <p><strong>Windows:</strong> open the .cer file, choose Install Certificate, Local Machine, and place it in Trusted Root Certification Authorities.</p>
    <p><strong>Firefox:</strong> Settings → Privacy &amp; Security → Certificates → View Certificates → Authorities → Import, and trust it for websites.</p>
  </div>
</main>
<script>
(function () {
  var form = document.querySelector("form");
  if (!form) return;
  form.addEventListener("submit", function () {
    var button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
  });
})();
</script>
</body>
</html>
""" % {
        "missing": missing,
        "notice": notice_html,
        "token": html_mod.escape(token, quote=True),
        "group": html_mod.escape(group),
        "fingerprint": finger,
    }
    return body.encode("utf-8")


def ensure_certificate(root: Path) -> tuple[Path, Path]:
    """Keep existing pairs; publish fresh owned staging outputs exclusively.

    Any final names already published are retained after failure. Without an
    exclusive writer lease, even stat-then-unlink could remove a replacement.
    A partial final pair is intentionally held on the next startup.
    """
    directory = root / "certs"
    if directory.is_symlink():
        raise RuntimeError("certificate directory is linked; preserving its state")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stage_fd = None
    stage_name = None
    pair_identity = lambda info: (info.st_dev, info.st_ino)
    def entry(name, descriptor):
        try:
            return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
    def same_directory():
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or pair_identity(info) != pair_identity(os.fstat(directory_fd)):
            raise RuntimeError("certificate directory changed; preserving its state")
    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode):
            raise RuntimeError("certificate directory is not a directory")
        os.fchmod(directory_fd, 0o700)
        crt, key = directory / "server.crt", directory / "server.key"
        old = {name: entry(name, directory_fd) for name in ("server.crt", "server.key")}
        if any(info is not None and stat.S_ISLNK(info.st_mode) for info in old.values()):
            raise RuntimeError("existing certificate pair is linked; preserving its files")
        if all(info is not None and stat.S_ISREG(info.st_mode) for info in old.values()):
            for name, info in old.items():
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                try:
                    if pair_identity(os.fstat(fd)) != pair_identity(info):
                        raise RuntimeError("existing certificate pair changed; preserving its files")
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
            same_directory()
            return crt, key
        if any(info is not None for info in old.values()):
            raise RuntimeError("existing certificate pair is incomplete; preserving its files")
        if any(entry(name, directory_fd) is not None for name in ("root-ca.key", "root-ca.pem")):
            raise RuntimeError("certificate is missing from the state certs directory")
        same_directory()
        stage = Path(tempfile.mkdtemp(prefix=".certificate-generation-", dir=directory))
        stage_name = stage.name
        stage_fd = os.open(stage, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        config = stage / "san.cnf"
        host = site.mini_host() or "localhost"
        ip = site.mini_ip()
        san = "DNS:%s,DNS:localhost,IP:127.0.0.1" % host
        if ip:
            san += ",IP:%s" % ip
        config.write_text(
            ("[req]\ndistinguished_name = dn\nx509_extensions = v3\nprompt = no\n"
             "[dn]\nCN = %s\n[v3]\nsubjectAltName = %s\n"
             "basicConstraints = critical,CA:TRUE\n"
             "keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign\n"
             "extendedKeyUsage = serverAuth\n") % (host, san), encoding="utf-8")
        os.chmod(config, 0o600)
        same_directory()
        completed = subprocess.run(
            ["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
             "-days", "825", "-nodes", "-keyout", str(stage / "server.key"),
             "-out", str(stage / "server.crt"), "-config", str(config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        fresh = {name: entry(name, stage_fd) for name in ("server.crt", "server.key")}
        if completed.returncode != 0 or any(info is None or not stat.S_ISREG(info.st_mode) for info in fresh.values()):
            raise RuntimeError("could not generate the monitor certificate")
        same_directory()
        for name, info in fresh.items():
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=stage_fd)
            try:
                if pair_identity(os.fstat(fd)) != pair_identity(info):
                    raise RuntimeError("staged certificate pair changed")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            # Hard-link publication is exclusive: concurrent destination state is
            # never replaced, even after the initial absence check.
            os.link(name, name, src_dir_fd=stage_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        same_directory()
        return crt, key
    finally:
        if stage_fd is not None:
            # Only the three exact output names within this newly owned private
            # staging inode. Unknown directories/entries are retained, not walked.
            for name in ("san.cnf", "server.crt", "server.key"):
                try:
                    os.unlink(name, dir_fd=stage_fd)
                except (FileNotFoundError, IsADirectoryError):
                    pass
            current = entry(stage_name, directory_fd)
            if current is not None and pair_identity(current) == pair_identity(os.fstat(stage_fd)):
                try:
                    os.rmdir(stage_name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(stage_fd)
        os.close(directory_fd)
