"""Deterministic privacy, freshness, SSH classification and collector failure tests."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone

import pytest

from zai_devdashd import linked_hosts
from zai_devdashd.resources import remote_probe as probe

NOW = 1793755200.0


@pytest.fixture(autouse=True)
def configured_synthetic_remote(monkeypatch):
    from zai_devdashd import config
    monkeypatch.setitem(config.CURRENT, "remote", {"computer": "Remote-Mac", "host": "remote.example.test"})



def event(stamp=NOW, limit_id="codex", primary=None, plan="pro", **extra):
    return {
        "timestamp": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": {
            "limit_id": limit_id, "plan_type": plan,
            "primary": primary or {"used_percent": 7, "window_minutes": 10080, "resets_at": NOW + 10000},
            **extra,
        }},
    }


def rollout(home, name, events):
    root = home / ".codex/sessions/2026/11/03"
    root.mkdir(parents=True, exist_ok=True)
    path = root / ("rollout-" + name + ".jsonl")
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def wire(stamp=NOW):
    return {
        "computer_name": "Remote-Mac", "observed_at": stamp,
        "hardware": {"model": "Macmini8,1", "cpu": "Intel Core i5", "cores": 6, "memory_bytes": 8589934592,
                     "os": "15.8.2", "arch": "x86_64"},
        "load": {"load_1m": 1.5, "disk_free_bytes": 1234, "disk_total_bytes": 5678},
        "apps": {"codex": True, "chatgpt": False},
        "ssh": {"sessions": [{"pid": 42, "peer": "Primary-Mac", "purpose": "raw secret", "established": True, "forwarding": []}]},
        "account": {"label": "untrusted", "plan": "pro", "source": "secret source",
                    "buckets": [{"id": "codex", "primary": {"used_percent": 7, "window_minutes": 10080, "resets_at": NOW + 10000},
                                 "secondary": None, "observed_at": stamp}]},
    }


def test_account_grouped_buckets_latest_event_and_privacy(tmp_path):
    rollout(tmp_path, "a", [
        {"type": "response_item", "payload": {"type": "message", "content": 'secret prompt "token_count"'}},
        event(NOW - 20, "codex", secret="never export", account_id="private-account"),
        event(NOW - 10, "codex", primary={"used_percent": 8, "window_minutes": 10080, "resets_at": NOW + 1000}),
        event(NOW - 5, "fast", primary={"used_percent": 2, "window_minutes": 300, "resets_at": NOW + 100}),
    ])
    (tmp_path / ".codex/auth.json").write_text('{"access_token":"credential-never-read"}')
    out = probe.account_snapshot(tmp_path, NOW)
    buckets = {b["id"]: b for b in out["buckets"]}
    assert buckets["codex"]["primary"]["used_percent"] == 8
    assert buckets["fast"]["primary"]["window_minutes"] == 300
    assert out["observed_at"] == NOW - 5 and out["plan"] == "pro" and not out["stale"]
    rendered = json.dumps(out)
    for secret in ("secret prompt", "never export", "private-account", "credential-never-read", "access_token"):
        assert secret not in rendered
    assert out["source"].endswith("Codex limits only)")


def test_account_missing_malformed_future_and_unknown_windows(tmp_path):
    path = rollout(tmp_path, "a", [
        event(NOW + 301),
        event(NOW, "unknown", primary={"used_percent": -3, "window_minutes": "week", "resets_at": float("nan")}),
        event(NOW, "bad@example.com"),
    ])
    with path.open("a") as handle:
        handle.write('broken "token_count"\n[]\n')
    out = probe.account_snapshot(tmp_path, NOW)
    assert len(out["buckets"]) == 1
    assert out["buckets"][0]["id"] == "unknown" and out["buckets"][0]["primary"] is None
    missing = probe.account_snapshot(tmp_path / "missing", NOW)
    assert missing["observed_at"] is None and missing["plan"] is None
    assert missing["buckets"] == [] and missing["stale"] is True


def test_account_stale_uses_source_timestamp_not_machine_poll(tmp_path):
    rollout(tmp_path, "old", [event(NOW - 86401)])
    out = probe.account_snapshot(tmp_path, NOW)
    assert out["stale"] and out["buckets"][0]["stale"]
    assert out["observed_at"] == NOW - 86401


def test_tail_reader_skips_oversized_rows_and_symlinks(tmp_path):
    real = rollout(tmp_path, "real", [event()])
    shadow = real.with_name("rollout-symlink.jsonl")
    shadow.symlink_to(real)
    real.write_text('{"type":"message","payload":"' + "secret" * 30000 + '"}\n' + json.dumps(event()) + "\n")
    out = probe.account_snapshot(tmp_path, NOW)
    assert len(out["buckets"]) == 1 and "secret" not in json.dumps(out)


@pytest.mark.parametrize("command,expected", [
    ("ssh -fNTL8765:127.0.0.1:8765 devdash@primary.example.test", ["local"]),
    ("ssh -NT -R 9000:localhost:9000 -D1080 primary.example.test", ["dynamic", "remote"]),
    ("ssh -o LocalForward=8765:localhost:8765 -N primary.example.test", ["local"]),
    ("ssh -o 'LocalForward 8765 localhost:8765' primary.example.test", ["local"]),
    ("ssh -o LocalForward 8765:localhost:8765 primary.example.test", ["local"]),
    ("ssh -o BatchMode=yes -p22 primary.example.test python -c '-Lnot-a-forward secret'", []),
    ("ssh primary.example.test 'codex app-server -L secret-token'", []),
    ("ssh -J jump.example primary.example.test", []),
])
def test_ssh_classification_ignores_remote_command(command, expected):
    out = probe.parse_ssh_process(123, command, established=True)
    assert out["peer"] == "SSH peer" and out["forwarding"] == expected
    assert out["purpose"] == ("SSH port forwarding" if expected else "SSH session")
    assert "secret" not in json.dumps(out) and "devdash" not in json.dumps(out)


def test_ssh_connections_and_unknown_peer_are_metadata_only():
    out = probe.ssh_snapshot(
        "10 ssh primary.example.test run-secret\n11 ssh -NL8765:localhost:8765 example.invalid\n12 /bin/zsh\n",
        "p10\nn192.0.2.20:123->192.0.2.10:22\np99\nnother:1->other:2\n",
    )
    assert out["established_sessions"] == 1 and out["explicit_forwards"] == 1
    assert out["sessions"][1]["peer"] == "SSH peer" and not out["sessions"][1]["established"]
    assert "example.invalid" not in json.dumps(out) and "run-secret" not in json.dumps(out)


def test_unknown_or_malformed_ssh_command_not_a_tunnel():
    assert probe.parse_ssh_process(1, "ssh") is None
    assert probe.parse_ssh_process(1, "not-ssh -NLx primary.example.test") is None
    assert probe.parse_ssh_process(1, 'ssh "unterminated') is None


def test_backend_sanitizes_peer_purpose_account_and_unexpected_fields():
    raw = wire()
    raw["credential"] = "private"
    raw["account"]["account_id"] = "private"
    raw["account"]["buckets"][0]["primary"]["token"] = "private"
    out = linked_hosts._sanitize(raw, NOW)
    assert out["ssh"]["sessions"][0]["purpose"] == "SSH session"
    assert out["account"]["label"] == "ChatGPT / Codex — Remote-Mac"
    assert out["account"]["source"] == linked_hosts.SOURCE
    assert "private" not in json.dumps(out) and "raw secret" not in json.dumps(out)
    assert set(out["account"]["buckets"][0]["primary"]) == {"used_percent", "window_minutes", "resets_at"}


def test_backend_wrong_host_and_invalid_machine_timestamp_rejected():
    raw = wire()
    raw["computer_name"] = "Primary-Mac"
    with pytest.raises(linked_hosts.ProbeError, match="Unexpected host"):
        linked_hosts._sanitize(raw, NOW)
    raw = wire(NOW + 301)
    with pytest.raises(linked_hosts.ProbeError, match="Invalid probe"):
        linked_hosts._sanitize(raw, NOW)


def test_backend_unavailable_is_null_not_zero_and_no_bool_numbers():
    raw = wire()
    raw["hardware"]["cores"] = True
    raw["load"]["load_1m"] = float("inf")
    raw["account"]["buckets"][0]["primary"] = {"used_percent": True}
    out = linked_hosts._sanitize(raw, NOW)
    assert out["hardware"]["cores"] is None and out["load"]["load_1m"] is None
    assert out["account"]["buckets"][0]["primary"] is None
    assert out["account"]["buckets"][0]["secondary"] is None


def test_collector_disabled_and_waiting_does_not_connect(monkeypatch):
    monkeypatch.setattr(linked_hosts, "_run_probe", lambda *_: pytest.fail("should not connect"))
    collector = linked_hosts.RemoteCollector()
    assert collector.snapshot(NOW)["state"] == "disabled"
    assert collector.poll({}) is False
    assert collector.snapshot(NOW)["account"]["stale"] is True


def test_collector_caches_rate_limits_and_network_failure_keeps_old_data(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(linked_hosts.time, "time", lambda: clock[0])
    monkeypatch.setattr(linked_hosts, "_run_probe", lambda *_: json.dumps(wire()).encode())
    collector = linked_hosts.RemoteCollector()
    cfg = {"enabled": True, "host": "remote.example.test", "computer": "Remote-Mac", "include_account": True}
    assert collector.poll(cfg)
    snap = collector.snapshot(NOW)
    assert snap["state"] == "online" and snap["stale"] is False
    assert snap["account"]["buckets"][0]["primary"]["used_percent"] == 7
    assert collector.poll(cfg) is False
    clock[0] += 61
    def fail(*_):
        raise linked_hosts.ProbeError("SSH probe unavailable")
    monkeypatch.setattr(linked_hosts, "_run_probe", fail)
    assert collector.poll(cfg)
    cached = collector.snapshot(clock[0])
    assert cached["state"] == "unreachable" and cached["stale"]
    assert cached["observed_at"] == NOW and cached["hardware"]["model"] == "Macmini8,1"
    assert cached["account"]["stale"] and cached["account"]["buckets"][0]["stale"]
    assert cached["error"] == "SSH probe unavailable"
    cached["hardware"]["model"] = "mutated by caller"
    assert collector.snapshot(clock[0])["hardware"]["model"] == "Macmini8,1"


@pytest.mark.parametrize("payload", [
    b"not json private stderr", b"\xff", b"[]" , b"x" * (linked_hosts.MAX_OUTPUT_BYTES + 1),
])
def test_collector_malformed_output_is_generic_and_does_not_publish_content(monkeypatch, payload):
    monkeypatch.setattr(linked_hosts, "_run_probe", lambda *_: payload)
    collector = linked_hosts.RemoteCollector()
    assert collector.poll({"enabled": True, "host": "remote.example.test", "computer": "Remote-Mac"})
    snap = collector.snapshot(NOW)
    assert snap["state"] == "unreachable" and snap["error"] == "Invalid probe response"
    assert "private stderr" not in json.dumps(snap)


def test_collector_invalid_host_never_reaches_ssh(monkeypatch):
    monkeypatch.setattr(linked_hosts, "_run_probe", lambda *_: pytest.fail("should not connect"))
    collector = linked_hosts.RemoteCollector()
    collector.poll({"enabled": True, "host": "-oProxyCommand=secret"})
    assert collector.snapshot(NOW)["error"] == "Invalid SSH host configuration"


def test_collector_time_based_stale_and_disable_clears_published_data(monkeypatch):
    monkeypatch.setattr(linked_hosts.time, "time", lambda: NOW)
    monkeypatch.setattr(linked_hosts, "_run_probe", lambda *_: json.dumps(wire()).encode())
    collector = linked_hosts.RemoteCollector()
    collector.poll({"enabled": True, "host": "remote.example.test", "computer": "Remote-Mac"})
    snap = collector.snapshot(NOW + 181)
    assert snap["state"] == "online" and snap["stale"] and snap["account"]["stale"]
    collector.poll({"enabled": False})
    assert collector.snapshot(NOW)["state"] == "disabled"
    assert collector.snapshot(NOW)["hardware"] == {}


def test_transport_is_bounded_reaps_child_and_uses_strict_ssh_options(monkeypatch):
    real_popen = subprocess.Popen
    created = []
    def fake_popen(args, **kwargs):
        assert args[0] == "/usr/bin/ssh" and args[-3:] == ["remote", "/usr/bin/python3", "-"]
        for option in ("BatchMode=yes", "ConnectTimeout=5", "StrictHostKeyChecking=yes", "ForwardAgent=no"):
            assert option in args
        proc = real_popen(["/usr/bin/python3", "-c", "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(b'x'*70000)"], **kwargs)
        created.append(proc)
        return proc
    monkeypatch.setattr(linked_hosts.subprocess, "Popen", fake_popen)
    with pytest.raises(linked_hosts.ProbeError, match="Invalid probe response"):
        linked_hosts._run_probe("remote", b"script", 2)
    assert created[0].poll() is not None


def test_transport_deadline_terminates_only_its_child(monkeypatch):
    real_popen = subprocess.Popen
    created = []
    def fake_popen(args, **kwargs):
        proc = real_popen(["/usr/bin/python3", "-c", "import sys,time;sys.stdin.buffer.read();time.sleep(5)"], **kwargs)
        created.append(proc)
        return proc
    monkeypatch.setattr(linked_hosts.subprocess, "Popen", fake_popen)
    before = time.monotonic()
    with pytest.raises(linked_hosts.ProbeError, match="timed out"):
        linked_hosts._run_probe("remote", b"script", 0.05)
    assert time.monotonic() - before < 2 and created[0].poll() is not None



def test_explicit_forwarding_clear_option_and_stdio():
    out = probe.parse_ssh_process(1, "ssh -NL8765:localhost:8765 -o ClearAllForwardings=yes primary.example.test", True)
    assert out["forwarding"] == []
    assert probe.parse_ssh_process(1, "ssh -W localhost:8765 primary.example.test", True)["forwarding"] == ["stdio"]


def test_oversized_numeric_and_deep_json_fail_closed(monkeypatch):
    assert probe.numeric(10 ** 10000) is None
    assert linked_hosts._number(10 ** 10000) is None
    monkeypatch.setattr(linked_hosts, "_run_probe", lambda *_: b"[" * 2000 + b"]" * 2000)
    collector = linked_hosts.RemoteCollector()
    collector.poll({"enabled": True, "host": "remote.example.test", "computer": "Remote-Mac"})
    assert collector.snapshot(NOW)["error"] == "Invalid probe response"


def test_polling_is_single_authority_and_snapshot_remains_nonblocking(monkeypatch):
    import threading
    started, release = threading.Event(), threading.Event()
    calls = []
    def delayed(*_):
        calls.append(True)
        started.set()
        assert release.wait(2)
        return json.dumps(wire()).encode()
    monkeypatch.setattr(linked_hosts, "_run_probe", delayed)
    collector = linked_hosts.RemoteCollector()
    worker = threading.Thread(target=collector.poll, args=({"enabled": True, "host": "remote.example.test", "computer": "Remote-Mac"},))
    worker.start()
    try:
        assert started.wait(1)
        before = time.monotonic()
        assert collector.snapshot(NOW)["state"] == "waiting"
        assert time.monotonic() - before < 0.5
        assert collector.poll({"enabled": True, "host": "remote.example.test", "computer": "Remote-Mac"}) is False and len(calls) == 1
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert collector.snapshot(NOW)["state"] == "online"



def test_remote_command_unbalanced_quote_does_not_hide_ssh_prefix():
    out = probe.parse_ssh_process(44, "ssh primary.example.test echo unbalanced\"", True)
    assert out["peer"] == "SSH peer" and out["forwarding"] == []


def test_public_forwarding_is_bool_and_remote_parent_classification_is_metadata_only():
    processes = "44 ssh primary.example.test secret-remote-command -Lignored\n45 ssh -NL8765:localhost:8765 primary.example.test\n46 ssh primary.example.test\n"
    metadata = ("44 7292 /usr/bin/ssh\n45 7292 /usr/bin/ssh\n46 2 /usr/bin/ssh\n"
                "7292 1 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n2 1 /bin/zsh\n")
    socks = "p44\nn127.0.0.1:1->192.0.2.10:22\n"
    ssh = probe.ssh_snapshot(processes, socks, metadata)
    assert [s["purpose"] for s in ssh["sessions"]] == ["ChatGPT/Codex Remote", "SSH port forwarding", "SSH session"]
    raw = wire()
    raw["ssh"] = ssh
    result = linked_hosts._sanitize(raw, NOW)["ssh"]
    assert [s["forwarding"] for s in result["sessions"]] == [False, True, False]
    assert all(type(s["forwarding"]) is bool for s in result["sessions"])
    assert result["sessions"][0]["purpose"] == "ChatGPT/Codex Remote"
    assert result["explicit_forwards"] == 1 and result["established_sessions"] == 1
    assert "secret-remote-command" not in json.dumps(result)
    assert "ChatGPT.app" not in json.dumps(result)


def test_unrecognized_parent_path_does_not_claim_codex_remote():
    out = probe.ssh_snapshot("44 ssh primary.example.test\n", "", "44 8 /usr/bin/ssh\n8 1 /other/Codex.app/Contents/MacOS/Codex\n")
    assert out["sessions"][0]["purpose"] == "SSH session"


def test_failed_probe_inspections_are_unknown_and_successful_empty_is_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.Path, "home", classmethod(lambda cls: tmp_path))
    def unavailable(args, deadline, timeout=2):
        return "Remote-Mac" if args[0] == "/usr/sbin/scutil" else None
    monkeypatch.setattr(probe, "run", unavailable)
    unknown = probe.collect({"expected_computer": "Remote-Mac"})
    assert unknown["apps"] == {"codex": None, "chatgpt": None}
    assert unknown["ssh"]["established_sessions"] is None
    assert unknown["ssh"]["explicit_forwards"] is None
    assert unknown["ssh"]["inspection_complete"] is False
    public = linked_hosts._sanitize(unknown, time.time())
    assert public["apps"] == {"codex": None, "chatgpt": None}
    assert public["ssh"]["established_sessions"] is None and public["ssh"]["explicit_forwards"] is None
    def empty(args, deadline, timeout=2):
        return "Remote-Mac" if args[0] == "/usr/sbin/scutil" else ""
    monkeypatch.setattr(probe, "run", empty)
    known = probe.collect({"expected_computer": "Remote-Mac"})
    assert known["apps"] == {"codex": False, "chatgpt": False}
    assert known["ssh"]["inspection_complete"] is True
    assert known["ssh"]["established_sessions"] == 0 and known["ssh"]["explicit_forwards"] == 0


def test_failed_socket_inspection_preserves_sessions_but_not_invented_status():
    raw = wire()
    raw["ssh"] = probe.ssh_snapshot("44 ssh -NL8765:localhost:8765 primary.example.test\n", None)
    assert raw["ssh"]["sessions"][0]["established"] is None
    public = linked_hosts._sanitize(raw, NOW)["ssh"]
    assert public["sessions"][0]["forwarding"] is True
    assert public["sessions"][0]["established"] is None
    assert public["established_sessions"] is None and public["explicit_forwards"] is None
    assert "unavailable" in public["note"]


def test_probe_run_deadline_error_and_lsof_no_matches_are_distinct(monkeypatch):
    assert probe.run(["/bin/ps"], time.monotonic() - 1) is None
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private command", 1, stderr="private stderr")
    monkeypatch.setattr(probe.subprocess, "run", timeout)
    assert probe.run(["/bin/ps"], time.monotonic() + 5) is None
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", ""))
    assert probe.run(["/usr/sbin/lsof"], time.monotonic() + 5) == ""
    assert probe.run(["/bin/ps"], time.monotonic() + 5) is None
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "permission denied secret"))
    assert probe.run(["/usr/sbin/lsof"], time.monotonic() + 5) is None


TOP_SAMPLE = ("CPU usage: 10% user, 20% sys, 70% idle\n"
              "CPU usage: 12.5% user, 20% sys, 67.5% idle\n")
# Deliberately synthetic UUIDv4-shaped vectors; not hardware or volume identifiers.
CONTAINER_UUID = "00000000-0000-4000-8000-000000000001"
VOLUME_UUID = "00000000-0000-4000-8000-000000000002"


def test_performance_uses_second_cpu_sample_and_exact_gpu_counter():
    out = probe.parse_performance(
        TOP_SAMPLE, "System-wide memory free percentage: 55%\n",
        '"Device Unit 0 Utilization %"=99,"Device Utilization % at cur p-state"=98,'
        '"Device Utilization %"=24,"Device Utilization %"=32')
    assert out == {"cpu_pct": 32.5, "mem_used_pct": 45, "gpu_pct": 32}


@pytest.mark.parametrize("top,memory,gpu", [
    ("CPU usage: 1% user, 1% sys, 98% idle\n", "", ""),
    ("CPU usage: 1% user, 1% sys, 98% idle\nCPU usage: 200% user, 1% sys, 0% idle\n",
     "System-wide memory free percentage: 101%", '"Device Utilization %"=200'),
    ("", "free 20%", '"Device Unit 1 Utilization %"=20'),
])
def test_unavailable_or_invalid_performance_is_unknown_not_zero(top, memory, gpu):
    assert probe.parse_performance(top, memory, gpu) == {
        "cpu_pct": None, "mem_used_pct": None, "gpu_pct": None}


def test_performance_commands_remain_bounded_and_only_counters_are_exported(monkeypatch):
    calls = []
    def run(args, deadline, timeout=2):
        calls.append((args, deadline, timeout))
        return {"/usr/bin/top": TOP_SAMPLE,
                "/usr/bin/memory_pressure": "System-wide memory free percentage: 55%",
                "/usr/sbin/ioreg": '"Device Utilization %"=24'}.get(args[0])
    monkeypatch.setattr(probe, "run", run)
    deadline = time.monotonic() + 9
    out = probe.performance_snapshot(deadline)
    assert len(calls) == 3 and all(c[1] == deadline and c[2] <= 3.5 for c in calls)
    assert calls[0][0] == ["/usr/bin/top", "-l", "2", "-s", "1", "-n", "0"]
    assert out["performance_observed_at"] is not None
    assert "used estimate" in out["performance_source"]
    monkeypatch.setattr(probe, "run", lambda *a, **k: None)
    unknown = probe.performance_snapshot(deadline)
    assert unknown["performance_observed_at"] is None and unknown["performance_source"] is None


def test_performance_sanitizer_rejects_bad_ranges_and_untrusted_source():
    raw = wire()
    raw["load"].update(cpu_pct=32.5, mem_used_pct=45, gpu_pct=24,
                       performance_observed_at=NOW + 2, performance_source="private command")
    out = linked_hosts._sanitize(raw, NOW + 5)["load"]
    assert (out["cpu_pct"], out["mem_used_pct"], out["gpu_pct"]) == (32.5, 45, 24)
    assert out["performance_observed_at"] == NOW + 2
    assert out["performance_source"] == linked_hosts.PERFORMANCE_SOURCE
    assert "private command" not in json.dumps(out)
    raw["load"].update(cpu_pct=True, mem_used_pct=-1, gpu_pct=float("nan"))
    out = linked_hosts._sanitize(raw, NOW + 5)["load"]
    assert all(out[k] is None for k in ("cpu_pct", "mem_used_pct", "gpu_pct"))
    raw["load"].update(cpu_pct=20, performance_observed_at=NOW - 500)
    assert linked_hosts._sanitize(raw, NOW)["load"]["cpu_pct"] is None


def disk_fixture(tmp_path, monkeypatch, volume_count=1):
    import plistlib
    from types import SimpleNamespace
    mount = tmp_path / "MountedData"
    mount.mkdir()
    volumes = [{"DeviceIdentifier": "disk1s%d" % n, "APFSVolumeUUID": VOLUME_UUID,
                "Roles": ["Data"]} for n in range(1, volume_count + 1)]
    listing = {"Containers": [{"APFSContainerUUID": CONTAINER_UUID, "ContainerReference": "disk1",
                               "CapacityCeiling": 10000, "CapacityFree": 4000, "Volumes": volumes}]}
    info = {"FilesystemType": "apfs", "VolumeUUID": VOLUME_UUID, "APFSContainerReference": "disk1",
            "MountPoint": str(mount), "VolumeName": "Data", "Internal": True, "BusProtocol": "PCI-Express"}
    calls = []
    def run(args, deadline, timeout=2):
        calls.append(args)
        if args[0] == "/sbin/mount":
            return "\n".join("/dev/disk1s%d on %s (apfs, local)" % (n, mount) for n in range(1, volume_count + 1))
        return plistlib.dumps(listing if args[1] == "apfs" else {k: v for k, v in info.items() if v is not None}).decode()
    monkeypatch.setattr(probe, "run", run)
    monkeypatch.setattr(probe.os.path, "ismount", lambda p: False)
    monkeypatch.setattr(probe.os, "statvfs", lambda p: SimpleNamespace(f_blocks=100, f_frsize=4096))
    return listing, info, calls, mount


def test_disks_verify_mount_and_deduplicate_shared_apfs_capacity(tmp_path, monkeypatch):
    listing, info, calls, mount = disk_fixture(tmp_path, monkeypatch, volume_count=3)
    disks = probe.disk_snapshot(time.monotonic() + 9)
    assert len(disks) == 1
    disk = disks[0]
    assert disk["total_bytes"] == 10000 and disk["used_bytes"] == 6000 and disk["free_bytes"] == 4000
    assert disk["used_percent"] == 60
    assert disk["container_id"] == CONTAINER_UUID and disk["volume_uuid"] == VOLUME_UUID
    assert disk["mount"] == str(mount) and disk["shared_capacity"] is True
    assert disk["accounting"] == "APFS container"
    assert len(calls) == 3
    assert linked_hosts._disks(disks, time.time())[0]["used_percent"] == 60


@pytest.mark.parametrize("change", [
    {"FilesystemType": "nfs"}, {"VolumeUUID": "wrong"}, {"APFSContainerReference": "disk2"},
    {"Internal": None}, {"BusProtocol": "Disk Image"}, {"MountPoint": "/missing/not-a-real-mount"},
])
def test_disk_probe_rejects_unverified_or_nonlocal_mounts(tmp_path, monkeypatch, change):
    listing, info, calls, mount = disk_fixture(tmp_path, monkeypatch)
    info.update(change)
    assert probe.disk_snapshot(time.monotonic() + 9) == []


def test_disk_probe_rejects_symlink_and_bounds_number_of_info_reads(tmp_path, monkeypatch):
    listing, info, calls, mount = disk_fixture(tmp_path, monkeypatch, volume_count=20)
    alias = tmp_path / "alias"
    alias.symlink_to(mount, target_is_directory=True)
    info["MountPoint"] = str(alias)
    assert probe.disk_snapshot(time.monotonic() + 9) == []
    assert sum(len(args) > 1 and args[1] == "info" for args in calls) == 6


def test_disk_plist_errors_and_expired_deadline_are_unavailable(monkeypatch):
    monkeypatch.setattr(probe, "run", lambda *a, **k: "<plist><bad")
    assert probe.disk_snapshot(time.monotonic() + 9) == []
    monkeypatch.setattr(probe, "run", lambda *a, **k: None)
    assert probe.disk_snapshot(time.monotonic() - 1) == []


def test_disk_sanitizer_recomputes_percent_and_strips_unexpected_fields():
    disk = {"container_id": CONTAINER_UUID, "volume_uuid": VOLUME_UUID, "name": "Startup",
            "mount": "/System/Volumes/Data", "total_bytes": 10000, "free_bytes": 2500,
            "used_bytes": 1, "used_percent": 0, "as_of": NOW, "internal": True, "secret": "private"}
    out = linked_hosts._disks([disk, disk], NOW)
    assert len(out) == 1 and out[0]["used_bytes"] == 7500 and out[0]["used_percent"] == 75
    assert "secret" not in json.dumps(out) and "private" not in json.dumps(out)
    for change in ({"free_bytes": 10001}, {"container_id": "not-a-uuid"}, {"internal": 1},
                   {"mount": "/Volumes/../private"}, {"total_bytes": float("inf")}):
        assert linked_hosts._disks([dict(disk, **change)], NOW) == []


def test_collect_adds_telemetry_without_weakening_unexpected_host_guard(monkeypatch):
    calls = []
    monkeypatch.setattr(probe, "run", lambda args, *a, **k: calls.append(args) or "Unexpected")
    out = probe.collect({"expected_computer": "Remote-Mac"})
    assert out["computer_name"] == "Unexpected"
    assert len(calls) == 1 and "load" not in out


def test_disk_status_reports_verified_pool_with_own_clock(tmp_path, monkeypatch):
    disk_fixture(tmp_path, monkeypatch)
    status = {}
    disks = probe.disk_snapshot(time.monotonic() + 9, status)
    assert len(disks) == status["verified"] == status["attempted"] == 1
    assert status["state"] == "available" and status["partial"] is False and status["error"] is None
    assert status["as_of"] >= disks[0]["as_of"]


@pytest.mark.parametrize("answer", [None, "", "<plist><bad", "not a plist"])
def test_bad_or_missing_disk_list_is_unavailable_not_healthy_empty(monkeypatch, answer):
    monkeypatch.setattr(probe, "run", lambda *a, **k: answer)
    status = {}
    assert probe.disk_snapshot(time.monotonic() + 9, status) == []
    assert status["state"] == "unavailable" and status["partial"] is True
    assert status["attempted"] == status["verified"] == 0
    assert status["error"] == "Disk inventory unavailable"


def test_expected_helper_unmounted_and_disk_image_exclusions_are_not_failures(tmp_path, monkeypatch):
    import plistlib
    listing, info, calls, mount = disk_fixture(tmp_path, monkeypatch)
    listing["Containers"][0]["Volumes"][0]["Roles"] = ["System"]
    status = {}
    assert probe.disk_snapshot(time.monotonic() + 9, status) == []
    assert status["state"] == "available" and status["attempted"] == 0
    listing["Containers"][0]["Volumes"][0]["Roles"] = ["Data"]
    info["BusProtocol"] = "Disk Image"
    assert probe.disk_snapshot(time.monotonic() + 9, status) == []
    assert status["state"] == "available" and status["attempted"] == 1
    info.pop("MountPoint")
    info["BusProtocol"] = "PCI-Express"
    def run(args, *a, **k):
        if args[0] == "/sbin/mount":
            return "/dev/disk9s1 on /Other (apfs, local)"
        return plistlib.dumps(listing if args[1] == "apfs" else info).decode()
    monkeypatch.setattr(probe, "run", run)
    assert probe.disk_snapshot(time.monotonic() + 9, status) == []
    assert status["state"] == "available"


def test_mount_metadata_failure_and_info_budget_are_explicit(tmp_path, monkeypatch):
    listing, info, calls, mount = disk_fixture(tmp_path, monkeypatch, volume_count=20)
    info["MountPoint"] = "/missing/not-a-real-mount"
    status = {}
    assert probe.disk_snapshot(time.monotonic() + 9, status) == []
    assert status["state"] == "unavailable" and status["attempted"] == 6
    assert status["partial"] is True
    assert probe.disk_snapshot(time.monotonic() - 1, status) == []
    assert status["state"] == "unavailable"
    assert status["attempted"] == 0


def test_partial_disk_inventory_retains_verified_pool_and_marks_failure(tmp_path, monkeypatch):
    listing, info, calls, mount = disk_fixture(tmp_path, monkeypatch)
    listing["Containers"].append({"APFSContainerUUID": "invalid"})
    status = {}
    assert len(probe.disk_snapshot(time.monotonic() + 9, status)) == 1
    assert status["state"] == "partial" and status["verified"] == 1
    assert status["error"] == "Disk inventory incomplete"


def test_disk_status_sanitizer_allowlist_unknowns_and_rejected_rows():
    raw = wire()
    raw["load"]["disks_status"] = {
        "state": "available", "partial": False, "attempted": 1, "verified": 1,
        "as_of": NOW, "error": "private raw error", "command": "private raw command"}
    out = linked_hosts._sanitize(raw, NOW)["load"]["disks_status"]
    assert out["state"] == "unavailable" and out["partial"] is True  # reported disk was rejected/missing
    assert out["verified"] == 0 and out["error"] == "Disk inventory unavailable"
    assert "private" not in json.dumps(out)
    raw["load"]["disks_status"].update(verified=0)
    out = linked_hosts._sanitize(raw, NOW)["load"]["disks_status"]
    assert out["state"] == "available" and out["error"] is None
    raw["load"]["disks_status"].update(attempted=True)
    assert linked_hosts._sanitize(raw, NOW)["load"]["disks_status"]["state"] == "unavailable"
    del raw["load"]["disks_status"]
    missing = linked_hosts._sanitize(raw, NOW)["load"]["disks_status"]
    assert missing["state"] == "unavailable" and missing["attempted"] is None


def test_disk_status_cached_clock_and_failure_staleness_are_preserved(monkeypatch):
    raw = wire()
    raw["load"]["disks_status"] = {"state": "available", "partial": False,
                                    "attempted": 0, "verified": 0, "as_of": NOW}
    monkeypatch.setattr(linked_hosts.time, "time", lambda: NOW)
    monkeypatch.setattr(linked_hosts, "_run_probe", lambda *_: json.dumps(raw).encode())
    collector = linked_hosts.RemoteCollector()
    collector.poll({"enabled": True, "host": "remote.example.test", "computer": "Remote-Mac"})
    assert collector.snapshot(NOW)["load"]["disks_status"]["stale"] is False
    old = collector.snapshot(NOW + 181)["load"]["disks_status"]
    assert old["as_of"] == NOW and old["stale"] is True


def test_info_error_dictionary_is_not_an_expected_unmounted_exclusion(tmp_path, monkeypatch):
    import plistlib
    listing, info, calls, mount = disk_fixture(tmp_path, monkeypatch)
    def run(args, *a, **k):
        if args[0] == "/sbin/mount":
            return "/dev/disk9s1 on /Other (apfs, local)"
        return plistlib.dumps(listing if args[1] == "apfs" else {"ErrorMessage": "private diagnostic"}).decode()
    monkeypatch.setattr(probe, "run", run)
    status = {}
    assert probe.disk_snapshot(time.monotonic() + 9, status) == []
    assert status["state"] == "unavailable" and status["attempted"] == 1
    assert "private diagnostic" not in json.dumps(status)


def test_unconfigured_probe_does_not_observe_host(monkeypatch):
    monkeypatch.setattr(probe, "run", lambda *_: pytest.fail("unconfigured probe must remain inert"))
    assert "not configured" in probe.collect({})["error"]


def test_collector_requires_expected_computer_before_connect(monkeypatch):
    monkeypatch.setattr(linked_hosts, "_run_probe", lambda *_: pytest.fail("unbound identity must not connect"))
    monkeypatch.setitem(linked_hosts.site.CURRENT, "remote", {})
    collector = linked_hosts.RemoteCollector()
    collector.poll({"enabled": True, "host": "remote.example.test"})
    assert "identity is not configured" in collector.snapshot(NOW)["error"]


def test_account_opt_out_strips_cached_and_unsolicited_producer_account(monkeypatch):
    monkeypatch.setattr(linked_hosts.time, "time", lambda: NOW)
    calls = []
    def payload(*args):
        calls.append(args)
        return json.dumps(wire()).encode()
    monkeypatch.setattr(linked_hosts, "_run_probe", payload)
    collector = linked_hosts.RemoteCollector()
    cfg = {"enabled": True, "host": "remote.example.test", "computer": "Remote-Mac", "include_account": True}
    assert collector.poll(cfg)
    assert collector.snapshot(NOW)["account"]["buckets"]
    # Even a busy old poll cannot defer the desired privacy policy.
    collector._poll_lock.acquire()
    try:
        assert collector.poll(dict(cfg, include_account=False)) is False
        assert collector.snapshot(NOW)["account"]["buckets"] == []
        assert collector.snapshot(NOW)["account"]["plan"] is None
    finally:
        collector._poll_lock.release()
    assert collector.poll(dict(cfg, include_account=False))
    assert len(calls) == 2
    assert collector.snapshot(NOW)["account"]["buckets"] == []


def test_failed_endpoint_switch_never_labels_old_host_as_new(monkeypatch):
    monkeypatch.setattr(linked_hosts.time, "time", lambda: NOW)
    monkeypatch.setattr(linked_hosts, "_run_probe", lambda *_: json.dumps(wire()).encode())
    collector = linked_hosts.RemoteCollector()
    cfg = {"enabled": True, "name": "Synthetic A", "host": "a.example.test", "computer": "Remote-Mac"}
    assert collector.poll(cfg)
    assert collector.snapshot(NOW)["hardware"]["model"] == "Macmini8,1"
    def fail(*_): raise linked_hosts.ProbeError("SSH probe unavailable")
    monkeypatch.setattr(linked_hosts, "_run_probe", fail)
    assert collector.poll(dict(cfg, name="Synthetic B", host="b.example.test", computer="Synthetic-B"))
    changed = collector.snapshot(NOW)
    assert changed["name"] == "Synthetic B" and changed["hardware"] == {}
    assert changed["observed_at"] is None and changed["stale"]


@pytest.mark.parametrize("change", ["endpoint", "account_policy"])
def test_late_old_reply_cannot_publish_after_desired_contract_changes(monkeypatch, change):
    import threading
    started, release = threading.Event(), threading.Event()
    def delayed(*_):
        started.set(); assert release.wait(2)
        return json.dumps(wire()).encode()
    monkeypatch.setattr(linked_hosts.time, "time", lambda: NOW)
    monkeypatch.setattr(linked_hosts, "_run_probe", delayed)
    collector = linked_hosts.RemoteCollector()
    cfg = {"enabled": True, "name": "Synthetic A", "host": "a.example.test", "computer": "Remote-Mac", "include_account": True}
    worker = threading.Thread(target=collector.poll, args=(cfg,))
    worker.start()
    try:
        assert started.wait(1)
        desired = dict(cfg, host="b.example.test", computer="Synthetic-B", name="Synthetic B") if change == "endpoint" else dict(cfg, include_account=False)
        assert collector.poll(desired) is False
        assert collector.snapshot(NOW)["account"]["buckets"] == []
    finally:
        release.set(); worker.join(2)
    assert not worker.is_alive()
    snap = collector.snapshot(NOW)
    assert snap["hardware"] == {} and snap["observed_at"] is None
    assert snap["account"]["buckets"] == []
    assert snap["state"] == "waiting"
