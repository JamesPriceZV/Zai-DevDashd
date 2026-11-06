"""Read-only Remote-Mac metadata probe; emits an allowlisted JSON snapshot.

No credentials, prompts, cookies, auth files or transcript bodies are exported.
"""
from __future__ import annotations

import heapq
import json
import math
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.parsers.expat import ExpatError

ACCOUNT_STALE_S = 86400
TAIL_BYTES = 131072
MAX_FILES = 32
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
SAFE_PLAN = re.compile(r"^[A-Za-z0-9 _.-]{1,64}$")
PROBE_CONFIG = {}  # Set only by the configured caller, before the entry point.


def numeric(value, low=0, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        valid = math.isfinite(value) and value >= low and (high is None or value <= high)
    except OverflowError:
        valid = False
    return value if valid else None


def window(value):
    if not isinstance(value, dict):
        return None
    out = {
        "used_percent": numeric(value.get("used_percent"), 0, 100),
        "window_minutes": numeric(value.get("window_minutes"), 1, 525600),
        "resets_at": numeric(value.get("resets_at"), 0, 4102444800),
    }
    return out if any(v is not None for v in out.values()) else None


def epoch(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def account_snapshot(home, now=None, deadline=None):
    now = time.time() if now is None else now
    out = {
        "label": "ChatGPT / Codex — Remote-Mac",
        "plan": None,
        "source": "Local Codex rollout token_count (last reported; Codex limits only)",
        "observed_at": None,
        "stale": True,
        "buckets": [],
    }
    root = Path(home) / ".codex" / "sessions"
    candidates = []
    # A bounded number of dated directories and file stats prevents historical
    # sessions from turning an interactive host probe into an unbounded scan.
    try:
        dirs = sorted(root.glob("*/*/*"), reverse=True)[:64]
        scanned = 0
        for directory in dirs:
            if deadline is not None and time.monotonic() >= deadline:
                break
            for path in directory.glob("rollout*.jsonl"):
                if path.is_symlink():
                    continue
                scanned += 1
                if scanned > 4096:
                    break
                try:
                    candidates.append((path.stat().st_mtime, path))
                except OSError:
                    pass
            if scanned > 4096:
                break
    except OSError:
        pass
    buckets = {}
    for _mtime, path in heapq.nlargest(MAX_FILES, candidates, key=lambda p: p[0]):
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            with path.open("rb") as handle:
                size = path.stat().st_size
                handle.seek(max(0, size - TAIL_BYTES))
                raw = handle.read(TAIL_BYTES)
            lines = raw.decode("utf-8", "replace").splitlines()
            if size > TAIL_BYTES and lines:
                lines = lines[1:]
        except OSError:
            continue
        for line in reversed(lines):
            if len(line) > 65536 or '"token_count"' not in line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(row, dict):
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            rate = payload.get("rate_limits")
            stamp = epoch(row.get("timestamp"))
            if not isinstance(rate, dict) or stamp is None or stamp > now + 300:
                continue
            limit_id = rate.get("limit_id") or "codex"
            if not isinstance(limit_id, str) or not SAFE_ID.fullmatch(limit_id):
                continue
            previous = buckets.get(limit_id)
            if previous and previous["observed_at"] >= stamp:
                continue
            plan = rate.get("plan_type")
            buckets[limit_id] = {
                "id": limit_id,
                "primary": window(rate.get("primary")),
                "secondary": window(rate.get("secondary")),
                "observed_at": stamp,
                "stale": now - stamp > ACCOUNT_STALE_S,
                "_plan": plan if isinstance(plan, str) and SAFE_PLAN.fullmatch(plan) else None,
            }
    if buckets:
        latest = max(buckets.values(), key=lambda b: b["observed_at"])
        out["observed_at"] = latest["observed_at"]
        out["plan"] = latest["_plan"]
        out["stale"] = all(b["stale"] for b in buckets.values())
        out["buckets"] = [{k: v for k, v in b.items() if k != "_plan"} for b in sorted(buckets.values(), key=lambda b: b["id"])[:16]]
    return out


# OpenSSH flags which consume a following argument (or a remainder attached to
# the same option token). Remote command text begins after the destination and
# is never inspected, classified or returned.
SSH_ARG_FLAGS = set("BbcDEeFIiJLlmOopQRSWw")
SSH_FORWARD_FLAGS = {"L": "local", "R": "remote", "D": "dynamic", "W": "stdio", "w": "tunnel"}


def parse_ssh_process(pid, command, established=False):
    # Read only the SSH option/destination prefix. In ps output, quotes in an
    # already-separated remote command need not balance; they must not affect
    # discovery or classification of the local transport.
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        first = next(lexer)
        if Path(first).name != "ssh":
            return None
        destination = None
        forwards = set()
        clear_forwarding = False
        for token in lexer:
            if token == "--":
                destination = next(lexer, None)
                break
            if not token.startswith("-") or token == "-":
                destination = token
                break
            flags = token[1:]
            pos = 0
            while pos < len(flags):
                flag = flags[pos]
                if flag in SSH_FORWARD_FLAGS:
                    forwards.add(SSH_FORWARD_FLAGS[flag])
                if flag in SSH_ARG_FLAGS or flag in SSH_FORWARD_FLAGS:
                    value = flags[pos + 1:] or next(lexer, "")
                    if flag == "o":
                        key = value.split("=", 1)[0].split(None, 1)[0].lower() if value else ""
                        if key == "clearallforwardings" and re.search(r"[=\s]+yes$", value, re.I):
                            clear_forwarding = True
                        if key in {"localforward", "remoteforward", "dynamicforward"}:
                            forwards.add({"localforward": "local", "remoteforward": "remote", "dynamicforward": "dynamic"}[key])
                            if "=" not in value and len(value.split(None, 1)) == 1:
                                next(lexer, "")
                    break
                pos += 1
    except (StopIteration, ValueError):
        return None
    if not destination or destination.startswith("-"):
        return None
    if clear_forwarding:
        forwards.clear()
    peer = destination.rsplit("@", 1)[-1].lower().strip("[]")
    return {
        "pid": int(pid),
        "peer": "SSH peer",
        "purpose": "SSH port forwarding" if forwards else "SSH session",
        "established": established if type(established) is bool else None,
        "forwarding": sorted(forwards),
    }


def established_ssh_pids(text):
    pids = set()
    current = None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
        elif line.startswith("n") and current is not None and "->" in line:
            pids.add(current)
    return pids


def process_metadata(text):
    out = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = {"ppid": int(parts[1]), "comm": parts[2]}
    return out


DESKTOP_APPS = {
    "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
    "/Applications/Codex.app/Contents/MacOS/Codex",
}


def ssh_snapshot(processes, sockets, metadata=""):
    complete = processes is not None and sockets is not None
    established = established_ssh_pids(sockets or "")
    parents = process_metadata(metadata or "")
    sessions = []
    for line in (processes or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        item = parse_ssh_process(int(parts[0]), parts[1], int(parts[0]) in established if sockets is not None else None)
        if item:
            owner = parents.get(parents.get(item["pid"], {}).get("ppid"), {})
            if not item["forwarding"] and owner.get("comm") in DESKTOP_APPS:
                item["purpose"] = "ChatGPT/Codex Remote"
            sessions.append(item)
    sessions = sorted(sessions, key=lambda r: r["pid"])[:32]
    return {
        "sessions": sessions,
        "established_sessions": sum(r["established"] for r in sessions) if complete else None,
        "explicit_forwards": sum(bool(r["forwarding"]) for r in sessions) if complete else None,
        "inspection_complete": complete,
        "note": ("Forwarding is reported only from explicit SSH flags; ordinary remote sessions are not port-forward tunnels." if complete else "SSH inspection unavailable: process or socket metadata could not be read."),
    }


def run(args, deadline, timeout=2):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=min(timeout, remaining), text=True)
        # lsof uses exit 1 without diagnostic output for a successful empty
        # match. Actual failures or partial permission-denied listings are
        # unknown, and stderr is never exported.
        is_lsof = args[0] == "/usr/sbin/lsof"
        if is_lsof and proc.stderr:
            return None
        if proc.returncode == 0 or (is_lsof and proc.returncode == 1 and not proc.stdout and not proc.stderr):
            return proc.stdout if len(proc.stdout) <= 524288 else None
        return None
    except (OSError, subprocess.SubprocessError):
        return None


PERFORMANCE_SOURCE = ("top second 1s sample; memory used estimate = 100 - memory_pressure free%; "
                      "GPU = max IOAccelerator Device Utilization %")
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
DEVICE_RE = re.compile(r"^disk[0-9]+s[0-9]+$")


def parse_performance(top_text, memory_text, gpu_text):
    """Accept only explicit OS counters; missing/invalid readings stay unknown."""
    samples = re.findall(r"^CPU usage:\s*([0-9.]+)% user,\s*([0-9.]+)% sys,\s*([0-9.]+)% idle",
                         top_text or "", re.M)
    cpu = None
    if len(samples) >= 2:
        try:
            user, system, idle = (float(value) for value in samples[-1])
            if all(numeric(v, 0, 100) is not None for v in (user, system, idle)) and abs(user + system + idle - 100) <= 1:
                cpu = round(100 - idle, 2)
        except ValueError:
            pass
    memory = None
    match = re.search(r"^System-wide memory free percentage:\s*([0-9.]+)%\s*$",
                      memory_text or "", re.M)
    if match:
        try:
            free = numeric(float(match.group(1)), 0, 100)
            memory = round(100 - free, 2) if free is not None else None
        except ValueError:
            pass
    gpu = []
    for value in re.findall(r'"Device Utilization %"\s*=\s*([0-9.]+)(?=[,}\s]|$)', gpu_text or ""):
        try:
            value = numeric(float(value), 0, 100)
            if value is not None:
                gpu.append(value)
        except ValueError:
            pass
    return {"cpu_pct": cpu, "mem_used_pct": memory, "gpu_pct": max(gpu) if gpu else None}


def performance_snapshot(deadline):
    values = parse_performance(
        run(["/usr/bin/top", "-l", "2", "-s", "1", "-n", "0"], deadline, timeout=3.5),
        run(["/usr/bin/memory_pressure", "-Q"], deadline, timeout=0.75),
        run(["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], deadline, timeout=0.75))
    measured = any(value is not None for value in values.values())
    return dict(values, performance_observed_at=time.time() if measured else None,
                performance_source=PERFORMANCE_SOURCE if measured else None)


def _plist(args, deadline, timeout=0.75):
    text = run(args, deadline, timeout)
    if text is None:
        return {}
    try:
        value = plistlib.loads(text.encode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, OverflowError, ExpatError):
        return {}


def disk_snapshot(deadline, status=None):
    """One mounted data-volume representative per local APFS capacity pool."""
    deadline = min(deadline, time.monotonic() + 2.5)
    out, inspected, incomplete = [], 0, False

    def finish():
        if isinstance(status, dict):
            state = "partial" if incomplete and out else ("unavailable" if incomplete else "available")
            status.update(state=state, partial=incomplete, attempted=inspected, verified=len(out),
                          as_of=time.time(), error={"available": None, "partial": "Disk inventory incomplete",
                                                     "unavailable": "Disk inventory unavailable"}[state])
        return out

    listing = _plist(["/usr/sbin/diskutil", "apfs", "list", "-plist"], deadline)
    containers = listing.get("Containers")
    if not isinstance(containers, list):
        incomplete = True
        return finish()
    if not containers:
        return finish()
    mount_text = run(["/sbin/mount"], deadline, timeout=0.5)
    if not mount_text or not any(re.fullmatch(r".+ on .+ \([^)]+\)", line)
                                 for line in mount_text.splitlines()):
        incomplete = True
        return finish()
    mounts = {}
    for line in mount_text.splitlines():
        match = re.fullmatch(r"(/dev/disk[0-9]+s[0-9]+) on (.+) \(([^)]*)\)", line)
        if match and {"apfs", "local"}.issubset(set(match.group(3).split(", "))):
            mounts[match.group(1)] = match.group(2)
    incomplete = len(containers) > 16
    for container in containers[:16]:
        if not isinstance(container, dict):
            incomplete = True
            continue
        container_id = container.get("APFSContainerUUID")
        total = numeric(container.get("CapacityCeiling"), 1)
        free = numeric(container.get("CapacityFree"), 0, total)
        if not isinstance(container_id, str) or not UUID_RE.fullmatch(container_id) or total is None or free is None:
            incomplete = True
            continue
        listed_volumes = container.get("Volumes")
        if not isinstance(listed_volumes, list) or any(not isinstance(v, dict) for v in listed_volumes):
            incomplete = True
        volumes = [v for v in (listed_volumes if isinstance(listed_volumes, list) else []) if isinstance(v, dict)
                   and (not v.get("Roles") or v.get("Roles") == ["Data"])]
        volumes.sort(key=lambda v: (v.get("Roles") != ["Data"], str(v.get("DeviceIdentifier") or "")))
        for volume in volumes:
            if inspected >= 6 or time.monotonic() >= deadline:
                incomplete = True
                return finish()
            device = volume.get("DeviceIdentifier")
            uuid = volume.get("APFSVolumeUUID")
            if not isinstance(device, str) or not DEVICE_RE.fullmatch(device) or not isinstance(uuid, str) or not UUID_RE.fullmatch(uuid):
                incomplete = True
                continue
            inspected += 1
            info = _plist(["/usr/sbin/diskutil", "info", "-plist", device], deadline)
            if not info:
                incomplete = True
                continue
            mount = info.get("MountPoint")
            # These are expected exclusions, not failed inspection.
            if str(info.get("BusProtocol") or "").replace(" ", "").lower() == "diskimage":
                continue
            if (info.get("FilesystemType") != "apfs" or info.get("VolumeUUID") != uuid or
                    info.get("APFSContainerReference") != container.get("ContainerReference") or
                    type(info.get("Internal")) is not bool):
                incomplete = True
                continue
            if not mount and "/dev/" + device not in mounts:
                continue
            if not isinstance(mount, str) or not mount.startswith("/") or len(mount) > 512:
                incomplete = True
                continue
            try:
                path = Path(mount)
                if str(path.resolve(strict=True)) != mount or mounts.get("/dev/" + device) != mount:
                    incomplete = True
                    continue
                stats = os.statvfs(mount)
                if stats.f_blocks <= 0 or stats.f_frsize <= 0:
                    incomplete = True
                    continue
            except (OSError, ValueError, RuntimeError):
                incomplete = True
                continue
            used = total - free
            out.append({"id": container_id.upper(), "container_id": container_id.upper(),
                        "volume_uuid": uuid.upper(), "name": str(info.get("VolumeName") or "APFS volume")[:80],
                        "mount": mount, "total_bytes": total, "used_bytes": used, "free_bytes": free,
                        "used_percent": round(used / total * 100, 2), "shared_capacity": True,
                        "capacity_scope": "apfs_container", "accounting": "APFS container",
                        "members": [{"name": str(info.get("VolumeName") or "APFS volume")[:80], "mount": mount}],
                        "internal": info["Internal"], "as_of": time.time()})
            break
    return finish()


def collect(config=None):
    config = PROBE_CONFIG if config is None else config
    started = time.time()
    deadline = time.monotonic() + 9
    expected = config.get("expected_computer") if isinstance(config, dict) else None
    if not isinstance(expected, str) or not expected:
        return {"observed_at": started, "error": "Expected host identity is not configured"}
    computer = (run(["/usr/sbin/scutil", "--get", "ComputerName"], deadline) or "").strip()
    # Refuse to scan sessions on an unexpected endpoint.
    if computer != expected:
        return {"computer_name": computer[:64], "observed_at": started}
    hw = (run(["/usr/sbin/sysctl", "-n", "hw.model", "machdep.cpu.brand_string", "hw.physicalcpu", "hw.memsize"], deadline) or "").splitlines()
    def integer(index):
        try:
            return int(hw[index])
        except (ValueError, IndexError):
            return None
    processes = run(["/bin/ps", "-axww", "-o", "pid=,command="], deadline)
    commands = run(["/bin/ps", "-axww", "-o", "pid=,ppid=,comm="], deadline)
    comms = [Path(row["comm"]).name.lower() for row in process_metadata(commands or "").values()]
    sockets = run(["/usr/sbin/lsof", "-nP", "-a", "-c", "ssh", "-iTCP", "-sTCP:ESTABLISHED", "-Fpn"], deadline)
    disk = shutil.disk_usage("/")
    performance = performance_snapshot(deadline)
    disks_status = {}
    disks = disk_snapshot(deadline, disks_status)
    return {
        "computer_name": computer,
        "observed_at": started,
        "hardware": {
            "model": hw[0] if hw else None,
            "cpu": hw[1] if len(hw) > 1 else None,
            "cores": integer(2),
            "memory_bytes": integer(3),
            "os": (run(["/usr/bin/sw_vers", "-productVersion"], deadline) or "").strip() or None,
            "arch": platform.machine(),
        },
        "load": {"load_1m": os.getloadavg()[0], "disk_free_bytes": disk.free, "disk_total_bytes": disk.total, **performance, "disks": disks, "disks_status": disks_status},
        "apps": {"codex": ("codex" in comms) if commands is not None else None, "chatgpt": ("chatgpt" in comms) if commands is not None else None},
        "ssh": ssh_snapshot(processes, sockets, commands),
        "account": account_snapshot(Path.home(), started, deadline) if config.get("include_account") is True else {},
    }


if __name__ == "__main__":
    print(json.dumps(collect(), allow_nan=False, separators=(",", ":")))

