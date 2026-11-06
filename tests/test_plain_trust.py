from zai_devdashd import server, auth
import hashlib
import ssl
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest

_actual_trust_material = auth.trust_material


@pytest.fixture(autouse=True)
def synthetic_trust_material(monkeypatch):
    # HTTP body-contract fixture only, not a valid certificate or TLS pass.
    monkeypatch.setattr(auth, "trust_material", lambda root: (b"-----BEGIN CERTIFICATE-----\nSYNTHETIC BODY CONTRACT ONLY\n-----END CERTIFICATE-----\n", b"\x30\x00", "TEST-ONLY"))


def _parse(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    code = int(lines[0].split()[1])
    hdrs = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in lines[1:]}
    return code, hdrs, body

def test_http_cer_served_without_hsts():
    raw = server.plain_trust_response("/trust/root-ca.cer", "192.0.2.10:8780")
    code, h, body = _parse(raw)
    assert code == 200 and h["content-type"] == "application/x-x509-ca-cert" and body[:1] == b"\x30"
    assert "strict-transport-security" not in h and "location" not in h

def test_http_pem_and_page():
    code, h, body = _parse(server.plain_trust_response("/trust/root-ca.pem", "x"))
    assert code == 200 and b"BEGIN CERTIFICATE" in body and b"PRIVATE" not in body
    code, h, body = _parse(server.plain_trust_response("/trust/", "192.0.2.10:8780"))
    assert code == 200 and b"Certificate Trust Settings" in body and b"/trust/root-ca.cer" in body

def test_http_key_404_and_other_paths_not_bootstrap():
    code, _, _ = _parse(server.plain_trust_response("/trust/root-ca.key", "x"))
    assert code == 404
    for p in ("/", "/login", "/api/state", "/trust/../certs/root-ca.key", "/settings"):
        r = server.plain_trust_response(p, "x")
        assert r is None or _parse(r)[0] == 404

def test_hsts_disabled_on_https():
    import inspect
    src = inspect.getsource(server)
    assert '"Strict-Transport-Security", "max-age=0"' in src and "max-age=300" not in src

def test_config_inline_tables():
    from zai_devdashd import config
    data = config.parse_toml('[devices.s103]\nquiet_hours = [{start = "20:00", end = "24:00", tz = "America/New_York", note = "Tester\'s using it"}]\n')
    qh = data["devices"]["s103"]["quiet_hours"]
    assert qh == [{"start": "20:00", "end": "24:00", "tz": "America/New_York", "note": "Tester's using it"}]

def test_config_round_trip_live_shape():
    from zai_devdashd import config
    src = '[devices.s103]\nquiet_hours = [{start = "20:00", end = "24:00", tz = "America/New_York", note = "Tester\'s using it"}]\n'
    data = config.parse_toml(src)
    assert config.parse_toml(config.dump_toml(data)) == data

def test_config_float_round_trip():
    from zai_devdashd import config
    data = config.parse_toml("scale = 1.5\n")
    assert data["scale"] == 1.5
    assert config.parse_toml(config.dump_toml(data))["scale"] == 1.5


def test_fresh_certificate_public_file_plumbing_not_tls(tmp_path, monkeypatch):
    """Synthetic certificate/key bytes + fake OpenSSL; no crypto or TLS proof."""
    pem = b"-----BEGIN CERTIFICATE-----\nSYNTHETIC FRESH CERTIFICATE\n-----END CERTIFICATE-----\n"
    der = b"\x30SYNTHETIC-DER-NOT-VALID-CRYPTO"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(auth.site, "trust_file", lambda name: tmp_path / "public" / name)
    monkeypatch.setattr(auth.site, "mini_host", lambda: "example.local")
    monkeypatch.setattr(auth.site, "mini_ip", lambda: "192.0.2.44")
    monkeypatch.setattr(auth, "trust_material", _actual_trust_material)
    calls = []
    def fake_openssl(args, **kwargs):
        calls.append(args)
        assert args[0] == "/usr/bin/openssl"
        if args[1] == "req":
            assert "-x509" in args
            assert "basicConstraints = critical,CA:TRUE" in Path(args[args.index("-config") + 1]).read_text()
            Path(args[args.index("-out") + 1]).write_bytes(pem)
            Path(args[args.index("-keyout") + 1]).write_bytes(b"SYNTHETIC TEST KEY, NOT CRYPTOGRAPHIC")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        assert args[1:] == ["x509", "-outform", "DER"] and kwargs["input"] == pem
        assert kwargs["timeout"] == auth.TRUST_CONVERSION_TIMEOUT_S
        return SimpleNamespace(returncode=0, stdout=der)
    monkeypatch.setattr(auth.subprocess, "run", fake_openssl)
    crt, key = auth.ensure_certificate(state)
    assert crt.is_file() and key.is_file()
    assert crt.stat().st_mode & 0o777 == key.stat().st_mode & 0o777 == 0o600
    public_pem, public_der, fingerprint = auth.trust_material(state)
    assert (public_pem, public_der) == (ssl.DER_cert_to_PEM_cert(der).encode("ascii"), der)
    assert public_pem != pem  # Only canonical certificate output is exported.
    digest = hashlib.sha256(der).hexdigest().upper()
    assert fingerprint == ":".join(digest[n:n + 2] for n in range(0, len(digest), 2))
    assert len(calls) == 2


def test_explicit_ca_preference_and_existing_pair_preserved(tmp_path, monkeypatch):
    state = tmp_path / "state"; certs = state / "certs"; certs.mkdir(parents=True)
    public = tmp_path / "public"; public.mkdir()
    crt, key = certs / "server.crt", certs / "server.key"
    crt.write_bytes(b"SYNTHETIC EXISTING SERVER CERTIFICATE")
    key.write_bytes(b"SYNTHETIC EXISTING KEY DO NOT OVERWRITE")
    root_pem = certs / "root-ca.pem"; root_pem.write_bytes(b"SYNTHETIC STATE ROOT")
    installed_pem = public / "zai-devdashd-root-ca.pem"; installed_pem.write_bytes(b"SYNTHETIC INSTALLED ROOT")
    installed_der = public / "zai-devdashd-root-ca.cer"; installed_der.write_bytes(b"\x30SYNTHETIC INSTALLED DER")
    monkeypatch.setattr(auth.site, "trust_file", lambda name: public / name)
    monkeypatch.setattr(auth, "trust_material", _actual_trust_material)
    seen = []
    def fake_der(args, **kwargs):
        expected = ["/usr/bin/openssl", "x509"]
        if kwargs["input"] == installed_der.read_bytes():
            expected += ["-inform", "DER"]
        assert args == expected + ["-outform", "DER"]
        assert kwargs["timeout"] == auth.TRUST_CONVERSION_TIMEOUT_S
        seen.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=b"\x30SYNTHETIC DER:" + kwargs["input"])
    monkeypatch.setattr(auth.subprocess, "run", fake_der)
    assert auth.ensure_certificate(state) == (crt, key)
    assert seen == []  # Existing pair never invokes generation.
    first_der = b"\x30SYNTHETIC DER:SYNTHETIC STATE ROOT"
    assert auth.trust_material(state)[:2] == (ssl.DER_cert_to_PEM_cert(first_der).encode("ascii"), first_der)
    root_pem.unlink()  # Exact test-owned fixture only.
    second_der = b"\x30SYNTHETIC DER:SYNTHETIC INSTALLED ROOT"
    assert auth.trust_material(state)[:2] == (ssl.DER_cert_to_PEM_cert(second_der).encode("ascii"), second_der)
    installed_pem.unlink()
    third_der = b"\x30SYNTHETIC DER:" + installed_der.read_bytes()
    assert auth.trust_material(state)[:2] == (ssl.DER_cert_to_PEM_cert(third_der).encode("ascii"), third_der)
    assert seen == [b"SYNTHETIC STATE ROOT", b"SYNTHETIC INSTALLED ROOT", installed_der.read_bytes()]
    assert crt.read_bytes() == b"SYNTHETIC EXISTING SERVER CERTIFICATE"
    assert key.read_bytes() == b"SYNTHETIC EXISTING KEY DO NOT OVERWRITE"


def _actual_trust_fixture(tmp_path, monkeypatch, encoding="pem"):
    """Enable real selection/plumbing with synthetic files, never real crypto."""
    state = tmp_path / "state"
    certs = state / "certs"
    certs.mkdir(parents=True)
    public = tmp_path / "public"
    public.mkdir()
    monkeypatch.setattr(auth.site, "trust_file", lambda name: public / name)
    monkeypatch.setattr(auth, "trust_material", _actual_trust_material)
    source = certs / "root-ca.pem" if encoding == "pem" else public / "zai-devdashd-root-ca.cer"
    return state, source


@pytest.mark.parametrize("encoding", ["pem", "der"])
@pytest.mark.parametrize("block", [b"PRIVATE KEY", b"RSA PRIVATE KEY", b"ENCRYPTED PRIVATE KEY"])
def test_private_key_bearing_trust_input_is_never_exported(tmp_path, monkeypatch, encoding, block):
    state, source = _actual_trust_fixture(tmp_path, monkeypatch, encoding)
    source.write_bytes(b"SYNTHETIC CERTIFICATE BODY\n-----BEGIN " + block + b"-----\nSYNTHETIC SECRET\n-----END " + block + b"-----\n")
    def forbidden(*args, **kwargs):
        raise AssertionError("key-bearing input must not reach OpenSSL")
    monkeypatch.setattr(auth.subprocess, "run", forbidden)
    assert auth.trust_material(state) == (b"", b"", "")
    monkeypatch.setattr(server, "ROOT", state)
    for path in ("/trust/root-ca.pem", "/trust/root-ca.cer"):
        code, _, body = _parse(server.plain_trust_response(path, "example.invalid:8780"))
        assert code == 404 and b"SYNTHETIC SECRET" not in body and b"PRIVATE KEY" not in body


@pytest.mark.parametrize("encoding", ["pem", "der"])
@pytest.mark.parametrize("result", ["invalid", "empty-output", "timeout", "oversized-output"])
def test_failed_or_unbounded_conversion_exports_no_trust_material(tmp_path, monkeypatch, encoding, result):
    state, source = _actual_trust_fixture(tmp_path, monkeypatch, encoding)
    raw = b"SYNTHETIC MALFORMED PEM" if encoding == "pem" else b"\x30SYNTHETIC MALFORMED DER"
    source.write_bytes(raw)
    # A lower-priority server certificate must not revive failed selected CA data.
    (state / "certs" / "server.crt").write_bytes(b"SYNTHETIC FALLBACK DO NOT SELECT")
    calls = []
    def fake_conversion(args, **kwargs):
        calls.append(args)
        expected = ["/usr/bin/openssl", "x509"]
        if encoding == "der":
            expected += ["-inform", "DER"]
        assert args == expected + ["-outform", "DER"]
        assert kwargs["input"] == raw and kwargs["timeout"] == auth.TRUST_CONVERSION_TIMEOUT_S
        if result == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        output = b"\x30SYNTHETIC OUTPUT"
        if result == "empty-output":
            output = b""
        elif result == "oversized-output":
            output = b"\x30" * (auth.TRUST_DER_LIMIT + 1)
        return SimpleNamespace(returncode=1 if result == "invalid" else 0, stdout=output)
    monkeypatch.setattr(auth.subprocess, "run", fake_conversion)
    assert auth.trust_material(state) == (b"", b"", "")
    assert len(calls) == 1


@pytest.mark.parametrize("encoding", ["pem", "der"])
def test_oversized_or_empty_preferred_trust_input_never_converts(tmp_path, monkeypatch, encoding):
    state, source = _actual_trust_fixture(tmp_path, monkeypatch, encoding)
    def forbidden(*args, **kwargs):
        raise AssertionError("empty or oversized input must not invoke OpenSSL")
    monkeypatch.setattr(auth.subprocess, "run", forbidden)
    for body in (b"", b"X" * (auth.TRUST_INPUT_LIMIT + 1)):
        source.write_bytes(body)
        assert auth.trust_material(state) == (b"", b"", "")


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_nonregular_preferred_trust_input_is_rejected_without_crypto(tmp_path, monkeypatch, kind):
    state, source = _actual_trust_fixture(tmp_path, monkeypatch)
    if kind == "symlink":
        target = tmp_path / "unknown-pem"
        target.write_bytes(b"SYNTHETIC UNKNOWN TARGET")
        source.symlink_to(target)
    else:
        auth.os.mkfifo(source, 0o600)
    def forbidden(*args, **kwargs):
        raise AssertionError("nonregular selected input must not invoke OpenSSL")
    monkeypatch.setattr(auth.subprocess, "run", forbidden)
    assert auth.trust_material(state) == (b"", b"", "")


def test_missing_trust_material_has_no_fingerprint(tmp_path, monkeypatch):
    state, _ = _actual_trust_fixture(tmp_path, monkeypatch)
    def forbidden(*args, **kwargs):
        raise AssertionError("missing input must not invoke OpenSSL")
    monkeypatch.setattr(auth.subprocess, "run", forbidden)
    assert auth.trust_material(state) == (b"", b"", "")


@pytest.mark.parametrize("existing", ["server.key", "server.crt"])
def test_partial_existing_certificate_pair_is_not_overwritten(tmp_path, monkeypatch, existing):
    certs = tmp_path / "certs"; certs.mkdir()
    path = certs / existing; path.write_bytes(b"SYNTHETIC UNKNOWN STATE PRESERVE")
    def forbidden(*args, **kwargs):
        raise AssertionError("partial state must not invoke OpenSSL")
    monkeypatch.setattr(auth.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="incomplete"):
        auth.ensure_certificate(tmp_path)
    assert path.read_bytes() == b"SYNTHETIC UNKNOWN STATE PRESERVE"
    assert not (certs / ("server.crt" if existing == "server.key" else "server.key")).exists()


def test_dangling_existing_key_link_is_preserved(tmp_path, monkeypatch):
    certs = tmp_path / "certs"; certs.mkdir()
    key = certs / "server.key"; target = tmp_path / "unknown-target"
    key.symlink_to(target)
    def forbidden(*args, **kwargs):
        raise AssertionError("unknown linked state must not invoke OpenSSL")
    monkeypatch.setattr(auth.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="linked"):
        auth.ensure_certificate(tmp_path)
    assert key.is_symlink() and not target.exists() and not (certs / "server.crt").exists()


@pytest.mark.parametrize("collision", ["server.crt", "server.key"])
def test_generation_never_overwrites_concurrent_destination(tmp_path, monkeypatch, collision):
    certs = tmp_path / "certs"; certs.mkdir()
    unknown_config = certs / "san.cnf"; unknown_config.write_bytes(b"UNKNOWN CONFIG PRESERVE")
    def fake_generate(args, **kwargs):
        for flag in ("-out", "-keyout"):
            target = Path(args[args.index(flag) + 1])
            assert target.parent != certs and target.parent.parent == certs
            assert target.parent.stat().st_mode & 0o777 == 0o700
            target.write_bytes(b"SYNTHETIC OWNED GENERATION")
        (certs / collision).write_bytes(b"CONCURRENT UNKNOWN DESTINATION")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(auth.subprocess, "run", fake_generate)
    with pytest.raises(FileExistsError):
        auth.ensure_certificate(tmp_path)
    assert (certs / collision).read_bytes() == b"CONCURRENT UNKNOWN DESTINATION"
    other = "server.key" if collision == "server.crt" else "server.crt"
    if collision == "server.key":
        # Conservatively retain the final published certificate: it may have
        # acquired another writer, so no stat/unlink cleanup is safe here.
        assert (certs / other).read_bytes() == b"SYNTHETIC OWNED GENERATION"
    else:
        assert not (certs / other).exists()
    assert unknown_config.read_bytes() == b"UNKNOWN CONFIG PRESERVE"
    assert not list(certs.glob(".certificate-generation-*"))


def test_failed_generation_preserves_concurrent_unknown_final(tmp_path, monkeypatch):
    certs = tmp_path / "certs"; certs.mkdir()
    def failed_generate(args, **kwargs):
        Path(args[args.index("-out") + 1]).write_bytes(b"SYNTHETIC PARTIAL OWNED OUTPUT")
        (certs / "server.key").write_bytes(b"CONCURRENT UNKNOWN KEY")
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(auth.subprocess, "run", failed_generate)
    with pytest.raises(RuntimeError, match="could not generate"):
        auth.ensure_certificate(tmp_path)
    assert (certs / "server.key").read_bytes() == b"CONCURRENT UNKNOWN KEY"
    assert not (certs / "server.crt").exists()
    assert not list(certs.glob(".certificate-generation-*"))


def test_publication_collision_cleanup_preserves_replacement_of_owned_partial(tmp_path, monkeypatch):
    certs = tmp_path / "certs"; certs.mkdir()
    def fake_generate(args, **kwargs):
        for flag in ("-out", "-keyout"):
            Path(args[args.index(flag) + 1]).write_bytes(b"SYNTHETIC OWNED OUTPUT")
        return SimpleNamespace(returncode=0)
    actual_link = auth.os.link
    def racing_link(src, dst, **kwargs):
        if dst == "server.key":
            (certs / "server.crt").unlink()
            (certs / "server.crt").write_bytes(b"CONCURRENT REPLACEMENT CERT")
            (certs / "server.key").write_bytes(b"CONCURRENT UNKNOWN KEY")
            raise FileExistsError("synthetic publication collision")
        return actual_link(src, dst, **kwargs)
    monkeypatch.setattr(auth.subprocess, "run", fake_generate)
    monkeypatch.setattr(auth.os, "link", racing_link)
    with pytest.raises(FileExistsError):
        auth.ensure_certificate(tmp_path)
    assert (certs / "server.crt").read_bytes() == b"CONCURRENT REPLACEMENT CERT"
    assert (certs / "server.key").read_bytes() == b"CONCURRENT UNKNOWN KEY"


def test_key_publication_failure_retains_partial_and_refuses_next_start(tmp_path, monkeypatch):
    certs = tmp_path / "certs"
    certs.mkdir()
    calls = []
    def fake_generate(args, **kwargs):
        calls.append(args)
        for flag in ("-out", "-keyout"):
            Path(args[args.index(flag) + 1]).write_bytes(b"SYNTHETIC OWNED OUTPUT")
        return SimpleNamespace(returncode=0)
    actual_link = auth.os.link
    def failed_key_link(src, dst, **kwargs):
        if dst == "server.key":
            raise OSError("synthetic final-key publication failure")
        return actual_link(src, dst, **kwargs)
    monkeypatch.setattr(auth.subprocess, "run", fake_generate)
    monkeypatch.setattr(auth.os, "link", failed_key_link)
    with pytest.raises(OSError, match="synthetic final-key"):
        auth.ensure_certificate(tmp_path)
    assert (certs / "server.crt").read_bytes() == b"SYNTHETIC OWNED OUTPUT"
    assert not (certs / "server.key").exists()
    assert not list(certs.glob(".certificate-generation-*"))
    with pytest.raises(RuntimeError, match="incomplete"):
        auth.ensure_certificate(tmp_path)
    assert len(calls) == 1


def test_late_after_check_replacement_cannot_be_deleted_by_final_name_cleanup(tmp_path, monkeypatch):
    """Arm the old check/unlink race; fixed code must never unlink final names."""
    certs = tmp_path / "certs"
    certs.mkdir()
    state = {"failed_key": False, "final_fd": None, "late_replacement": False, "final_unlinks": []}
    actual_link, actual_stat, actual_unlink = auth.os.link, auth.os.stat, auth.os.unlink
    def fake_generate(args, **kwargs):
        for flag in ("-out", "-keyout"):
            Path(args[args.index(flag) + 1]).write_bytes(b"SYNTHETIC OWNED OUTPUT")
        return SimpleNamespace(returncode=0)
    def failed_key_link(src, dst, **kwargs):
        state["final_fd"] = kwargs["dst_dir_fd"]
        if dst == "server.key":
            state["failed_key"] = True
            raise FileExistsError("synthetic final-key collision")
        return actual_link(src, dst, **kwargs)
    def late_stat(path, *args, **kwargs):
        info = actual_stat(path, *args, **kwargs)
        if state["failed_key"] and path == "server.crt" and kwargs.get("dir_fd") == state["final_fd"]:
            # Return the original inode, but replace its name before an unsafe
            # old cleanup can unlink it. No inode check can make that unlink safe.
            actual_unlink(path, dir_fd=state["final_fd"])
            (certs / "server.crt").write_bytes(b"SYNTHETIC LATE REPLACEMENT")
            state["late_replacement"] = True
        return info
    def recorded_unlink(path, *args, **kwargs):
        if path in ("server.crt", "server.key") and kwargs.get("dir_fd") == state["final_fd"]:
            state["final_unlinks"].append(path)
        return actual_unlink(path, *args, **kwargs)
    monkeypatch.setattr(auth.subprocess, "run", fake_generate)
    monkeypatch.setattr(auth.os, "link", failed_key_link)
    monkeypatch.setattr(auth.os, "stat", late_stat)
    monkeypatch.setattr(auth.os, "unlink", recorded_unlink)
    with pytest.raises(FileExistsError):
        auth.ensure_certificate(tmp_path)
    assert state["final_unlinks"] == []
    expected = b"SYNTHETIC LATE REPLACEMENT" if state["late_replacement"] else b"SYNTHETIC OWNED OUTPUT"
    assert (certs / "server.crt").read_bytes() == expected
    assert not list(certs.glob(".certificate-generation-*"))


def test_linked_certificate_directory_is_refused_before_chmod(tmp_path, monkeypatch):
    target = tmp_path / "unknown-certificate-directory"; target.mkdir(mode=0o755)
    (target / "unknown").write_bytes(b"PRESERVE UNKNOWN DIRECTORY")
    (tmp_path / "certs").symlink_to(target, target_is_directory=True)
    def forbidden(*args, **kwargs):
        raise AssertionError("linked certificate directory must not invoke OpenSSL")
    monkeypatch.setattr(auth.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="directory is linked"):
        auth.ensure_certificate(tmp_path)
    assert target.stat().st_mode & 0o777 == 0o755
    assert (target / "unknown").read_bytes() == b"PRESERVE UNKNOWN DIRECTORY"
