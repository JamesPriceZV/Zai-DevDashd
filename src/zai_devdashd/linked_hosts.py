"""Cached, bounded read-only telemetry for a separately configured Remote-Mac host."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import selectors
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from zai_devdashd import config as site

MAX_OUTPUT_BYTES = 65536
ACCOUNT_STALE_S = 86400
PROBE = Path(__file__).resolve().parent / "resources" / "remote_probe.py"
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
PLAN_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,64}$")
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
PERFORMANCE_SOURCE = ("top second 1s sample; memory used estimate = 100 - memory_pressure free%; "
                      "GPU = max IOAccelerator Device Utilization %")
SOURCE = "Local Codex rollout token_count (last reported; Codex limits only)"


class ProbeError(Exception):
    pass


def _number(value, low=0, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        valid = math.isfinite(value) and value >= low and (high is None or value <= high)
    except OverflowError:
        valid = False
    return value if valid else None


def _text(value, size=160):
    return value[:size] if isinstance(value, str) and not any(ord(c) < 32 for c in value) else None


def _window(raw):
    if not isinstance(raw, dict):
        return None
    window = {
        "used_percent": _number(raw.get("used_percent"), 0, 100),
        "window_minutes": _number(raw.get("window_minutes"), 1, 525600),
        "resets_at": _number(raw.get("resets_at"), 0, 4102444800),
    }
    return window if any(value is not None for value in window.values()) else None


def _account(raw, now):
    raw = raw if isinstance(raw, dict) else {}
    plan = raw.get("plan")
    buckets = []
    seen = set()
    for item in (raw.get("buckets") or [])[:16]:
        if not isinstance(item, dict):
            continue
        identity = item.get("id")
        stamp = _number(item.get("observed_at"), 0, now + 300)
        if not isinstance(identity, str) or not ID_RE.fullmatch(identity) or identity in seen or stamp is None:
            continue
        seen.add(identity)
        buckets.append({
            "id": identity,
            "primary": _window(item.get("primary")),
            "secondary": _window(item.get("secondary")),
            "observed_at": stamp,
            "stale": now - stamp > ACCOUNT_STALE_S,
        })
    observed = max((b["observed_at"] for b in buckets), default=None)
    return {
        "label": "ChatGPT / Codex — Remote-Mac",
        "plan": plan if isinstance(plan, str) and PLAN_RE.fullmatch(plan) else None,
        "source": SOURCE,
        "observed_at": observed,
        "stale": not buckets or all(b["stale"] for b in buckets),
        "buckets": buckets,
    }



def _disks(raw, now, machine_stamp=None):
    out, seen = [], set()
    for item in (raw if isinstance(raw, list) else [])[:6]:
        if not isinstance(item, dict):
            continue
        container = item.get("container_id")
        uuid = item.get("volume_uuid")
        total = _number(item.get("total_bytes"), 1)
        free = _number(item.get("free_bytes"), 0, total)
        mount = _text(item.get("mount"), 512)
        stamp = _number(item.get("as_of"), 0 if machine_stamp is None else machine_stamp - 1,
                        now + 300 if machine_stamp is None else min(now + 300, machine_stamp + 15))
        if (not isinstance(container, str) or not UUID_RE.fullmatch(container) or
                not isinstance(uuid, str) or not UUID_RE.fullmatch(uuid) or
                container.upper() in seen or total is None or free is None or stamp is None or
                not mount or not mount.startswith("/") or ".." in mount.split("/") or
                type(item.get("internal")) is not bool):
            continue
        seen.add(container.upper())
        used = total - free
        out.append({"id": container.upper(), "container_id": container.upper(),
                    "volume_uuid": uuid.upper(), "name": _text(item.get("name"), 80) or "APFS volume",
                    "mount": mount, "total_bytes": total, "used_bytes": used, "free_bytes": free,
                    "used_percent": round(used / total * 100, 2), "shared_capacity": True,
                    "capacity_scope": "apfs_container", "accounting": "APFS container",
                    "members": [{"name": _text(item.get("name"), 80) or "APFS volume", "mount": mount}],
                    "internal": item["internal"], "as_of": stamp})
    return out


def _disks_status(raw, now, machine_stamp, verified):
    raw = raw if isinstance(raw, dict) else {}
    attempted = _number(raw.get("attempted"), 0, 6)
    if attempted is not None and int(attempted) != attempted:
        attempted = None
    stamp = _number(raw.get("as_of"), machine_stamp - 1, min(now + 300, machine_stamp + 15))
    valid = (attempted is not None and int(attempted) == attempted and attempted >= verified and
             raw.get("verified") == verified and type(raw.get("verified")) is int and
             type(raw.get("partial")) is bool and stamp is not None)
    state = raw.get("state") if valid else None
    if state not in ("available", "partial", "unavailable") or (state == "unavailable" and verified):
        state = "partial" if verified else "unavailable"
    if raw.get("partial") is True and state == "available":
        state = "partial" if verified else "unavailable"
    return {"state": state, "partial": state != "available", "as_of": stamp,
            "attempted": int(attempted) if attempted is not None else None, "verified": verified,
            "error": {"available": None, "partial": "Disk inventory incomplete",
                      "unavailable": "Disk inventory unavailable"}[state]}


def _sanitize(raw, now, expected_computer=None):
    if not isinstance(raw, dict):
        raise ProbeError("Invalid probe response")
    expected_computer = expected_computer or (site.CURRENT.get("remote") or {}).get("computer")
    if not _text(expected_computer, 64) or raw.get("computer_name") != expected_computer:
        raise ProbeError("Unexpected host identity")
    stamp = _number(raw.get("observed_at"), 0, now + 300)
    if stamp is None:
        raise ProbeError("Invalid probe response")
    hardware = raw.get("hardware") if isinstance(raw.get("hardware"), dict) else {}
    load = raw.get("load") if isinstance(raw.get("load"), dict) else {}
    performance_stamp = _number(load.get("performance_observed_at"), stamp - 1, min(now + 300, stamp + 15))
    disks = _disks(load.get("disks"), now, stamp)
    disks_status = _disks_status(load.get("disks_status"), now, stamp, len(disks))
    apps = raw.get("apps") if isinstance(raw.get("apps"), dict) else {}
    ssh = raw.get("ssh") if isinstance(raw.get("ssh"), dict) else {}
    sessions = []
    for item in (ssh.get("sessions") or [])[:32]:
        if not isinstance(item, dict):
            continue
        pid = _number(item.get("pid"), 1, 2147483647)
        if pid is None or int(pid) != pid:
            continue
        forwarding = [x for x in (item.get("forwarding") or []) if x in ("local", "remote", "dynamic", "stdio", "tunnel")]
        sessions.append({
            "pid": int(pid),
            "peer": "SSH peer",
            "purpose": "SSH port forwarding" if forwarding else ("ChatGPT/Codex Remote" if item.get("purpose") == "ChatGPT/Codex Remote" else "SSH session"),
            "established": item.get("established") if type(item.get("established")) is bool else None,
            "forwarding": bool(forwarding),
        })
    inspected = ssh.get("inspection_complete")
    complete = inspected is True if type(inspected) is bool else (
        isinstance(ssh.get("sessions"), list) and all(item["established"] is not None for item in sessions))
    return {
        "observed_at": stamp,
        "hardware": {
            "model": _text(hardware.get("model")),
            "cpu": _text(hardware.get("cpu")),
            "cores": _number(hardware.get("cores"), 1, 1024),
            "memory_bytes": _number(hardware.get("memory_bytes"), 1),
            "os": _text(hardware.get("os"), 64),
            "arch": _text(hardware.get("arch"), 32),
        },
        "load": {
            "load_1m": _number(load.get("load_1m"), 0),
            "cpu_pct": _number(load.get("cpu_pct"), 0, 100) if performance_stamp is not None else None,
            "mem_used_pct": _number(load.get("mem_used_pct"), 0, 100) if performance_stamp is not None else None,
            "gpu_pct": _number(load.get("gpu_pct"), 0, 100) if performance_stamp is not None else None,
            "performance_observed_at": performance_stamp,
            "performance_source": PERFORMANCE_SOURCE if performance_stamp is not None else None,
            "disks": disks,
            "disks_status": disks_status,
            "disk_free_bytes": _number(load.get("disk_free_bytes"), 0),
            "disk_total_bytes": _number(load.get("disk_total_bytes"), 1),
        },
        "apps": {"codex": apps.get("codex") if type(apps.get("codex")) is bool else None, "chatgpt": apps.get("chatgpt") if type(apps.get("chatgpt")) is bool else None},
        "ssh": {
            "sessions": sessions,
            "established_sessions": sum(item["established"] is True for item in sessions) if complete else None,
            "explicit_forwards": sum(item["forwarding"] for item in sessions) if complete else None,
            "inspection_complete": complete,
            "note": ("Forwarding is reported only from explicit SSH flags; ordinary remote sessions are not port-forward tunnels." if complete else "SSH inspection unavailable: process or socket metadata could not be read."),
        },
        "account": _account(raw.get("account"), now),
    }


def _run_probe(host, source, timeout=15):
    args = [
        "/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=yes", "-o", "ForwardAgent=no",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
        host, "/usr/bin/python3", "-",
    ]
    deadline = time.monotonic() + timeout
    proc = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
        # The packaged script is small enough for the local pipe, while SSH
        # reads and buffers its stdin even before remote Python is ready.
        proc.stdin.write(source)
        proc.stdin.close()
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                raise ProbeError("SSH probe timed out")
            for key, _events in selector.select(left):
                chunk = os.read(key.fileobj.fileno(), min(8192, MAX_OUTPUT_BYTES + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > MAX_OUTPUT_BYTES:
                    raise ProbeError("Invalid probe response")
        left = deadline - time.monotonic()
        if left <= 0:
            raise ProbeError("SSH probe timed out")
        if proc.wait(timeout=left) != 0:
            raise ProbeError("SSH probe unavailable")
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise ProbeError("SSH probe timed out")
    except OSError:
        raise ProbeError("SSH probe unavailable")
    finally:
        selector.close()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            for stream in (proc.stdout, proc.stdin):
                if stream is not None:
                    stream.close()


class RemoteCollector:
    """One cached authority; polling never blocks snapshot readers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._data = None
        self._data_identity = None
        self._error = None
        self._last_attempt = None
        self._last_attempt_generation = None
        self._generation = 0
        self._cfg = {"enabled": False, "name": "Remote host", "host": "", "computer": "",
                     "include_account": False, "probe_interval_s": 60, "stale_s": 180}

    def poll(self, cfg):
        cfg = cfg if isinstance(cfg, dict) else {}
        enabled = cfg.get("enabled") is True
        host = cfg.get("host", "")
        safe_host = isinstance(host, str) and HOST_RE.fullmatch(host)
        name = _text(cfg.get("name"), 64) or "Remote host"
        computer = _text(cfg.get("computer"), 64) or (site.CURRENT.get("remote") or {}).get("computer") or ""
        include_account = cfg.get("include_account") is True
        interval = _number(cfg.get("probe_interval_s", 60), 10, 3600) or 60
        stale_s = _number(cfg.get("stale_s", 180), 10, 86400) or 180
        config = {"enabled": enabled, "name": name, "host": host if safe_host else None, "computer": computer,
                  "include_account": include_account, "probe_interval_s": interval, "stale_s": stale_s}
        identity = computer, config["host"], include_account
        now = time.time()
        # Publish desired identity/policy even while a previous probe is in flight.
        # Old replies cannot re-enable account data or relabel another host's data.
        with self._lock:
            if config != self._cfg:
                self._generation += 1
            old_identity = self._cfg["computer"], self._cfg["host"], self._cfg["include_account"]
            self._cfg = config
            generation = self._generation
            if identity != old_identity or not enabled:
                self._data = None
                self._data_identity = None
                self._error = None
            elif not include_account and self._data is not None:
                self._data["account"] = _account({}, now)
            if not enabled:
                self._error = None
                return False
            if not self._poll_lock.acquire(False):
                return False
        try:
            with self._lock:
                if generation != self._generation:
                    return False
                if (self._last_attempt_generation == generation and self._last_attempt is not None
                        and now - self._last_attempt < interval):
                    return False
                self._last_attempt = now
                self._last_attempt_generation = generation
            try:
                if not safe_host:
                    raise ProbeError("Invalid SSH host configuration")
                if not computer:
                    raise ProbeError("Expected remote computer identity is not configured")
                source = PROBE.read_bytes()
                marker = b'\nif __name__ == "__main__":'
                if source.count(marker) != 1:
                    raise ProbeError("Probe resource unavailable")
                options = {"expected_computer": computer, "include_account": include_account}
                source = source.replace(marker, b'\nPROBE_CONFIG = ' + repr(options).encode('utf-8') + b'\n' + marker)
                if len(source) > 65536:
                    raise ProbeError("Probe resource unavailable")
                payload = _run_probe(host, source, 15)
                if len(payload) > MAX_OUTPUT_BYTES:
                    raise ProbeError("Invalid probe response")
                try:
                    raw = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    raise ProbeError("Invalid probe response")
                data = _sanitize(raw, time.time(), computer)
                # Consumer opt-out is mandatory even if a producer sends account data.
                if not include_account:
                    data["account"] = _account({}, now)
            except ProbeError as exc:
                # Every ProbeError is one of our fixed strings, never stderr.
                with self._lock:
                    if generation == self._generation:
                        self._error = str(exc)
            except (OSError, TypeError, ValueError, AttributeError, OverflowError):
                with self._lock:
                    if generation == self._generation:
                        self._error = "Invalid probe response"
            else:
                with self._lock:
                    if generation == self._generation:
                        self._data = data
                        self._data_identity = identity
                        self._error = None
            return True
        finally:
            self._poll_lock.release()

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            cfg = dict(self._cfg)
            desired_identity = cfg["computer"], cfg["host"], cfg["include_account"]
            data = copy.deepcopy(self._data) if self._data_identity == desired_identity else None
            error = self._error
        enabled = cfg["enabled"]
        if not enabled:
            data = None
        observed = data.get("observed_at") if data else None
        stale = observed is None or now - observed > cfg["stale_s"] or bool(error)
        state = "disabled" if not enabled else ("unreachable" if error else ("online" if data else "waiting"))
        out = data or {
            "observed_at": None, "hardware": {}, "load": {}, "apps": {"codex": None, "chatgpt": None},
            "ssh": {"sessions": [], "established_sessions": None, "explicit_forwards": None,
                    "inspection_complete": False, "note": "No live SSH metadata collected yet."},
            "account": _account({}, now),
        }
        out.update(name=cfg["name"], enabled=enabled, state=state, stale=stale, error=error,
                   as_of_et=datetime.fromtimestamp(observed, ZoneInfo("America/Toronto")).strftime("%Y-%m-%d %H:%M:%S ET") if observed is not None else None)
        disk_status = out.get("load", {}).get("disks_status")
        if isinstance(disk_status, dict):
            disk_status["stale"] = stale or disk_status.get("as_of") is None or now - disk_status["as_of"] > cfg["stale_s"]
        if cfg["include_account"] is not True:
            out["account"] = _account({}, now)
        account = out["account"]
        for bucket in account["buckets"]:
            bucket["stale"] = stale or now - bucket["observed_at"] > ACCOUNT_STALE_S
        account["stale"] = stale or not account["buckets"] or all(b["stale"] for b in account["buckets"])
        return out

