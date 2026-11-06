"""Mini, Worker, device, and GitHub probes."""
from __future__ import annotations

import ctypes
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from zai_devdashd import config as site
from zai_devdashd import labels as board

site.install(globals())


def install_config() -> None:
    """Refresh configured paths only at explicit application startup."""
    site.ensure_loaded()
    site.install(globals())
ET = ZoneInfo('America/New_York')
_JOB_KEY = re.compile(r"[^A-Za-z0-9_.:-]+")
CANON_STEPS = ["MAC_BFT", "PHONE_BFT", "MAC_UNIT", "DIESEL", "REL"]
STEP_WORDS = {
    "MAC_BFT": "Mac build",
    "PHONE_BFT": "phone build",
    "MAC_UNIT": "Mac unit tests",
    "DIESEL": "Diesel",
    "REL": "release build",
}
STEP_LOG_SUFFIX = {
    "MAC_BFT": "-mac-bft.log",
    "PHONE_BFT": "-phone-bft.log",
    "MAC_UNIT": "-mac-unit.log",
    "DIESEL": "-phone-diesel.log",
    "REL": "-phone-release.log",
}
# One subprocess for GPU, memory, and swap. CPU is in-process host ticks.
LOAD_SH = """
echo LIVE_BEGIN
/usr/sbin/ioreg -r -d 1 -c IOAccelerator 2>/dev/null | /usr/bin/grep 'Device Utilization %' | /usr/bin/head -n 8
/usr/bin/memory_pressure -Q 2>/dev/null | /usr/bin/grep 'free percentage'
/usr/bin/vm_stat
/usr/sbin/sysctl -n vm.swapusage
echo LIVE_END
"""
HW_SH = """
echo HW_SYS
/usr/sbin/sysctl -n hw.model machdep.cpu.brand_string hw.ncpu hw.memsize
echo HW_PRO
/usr/sbin/system_profiler SPHardwareDataType 2>/dev/null | /usr/bin/grep -E 'Model Name:|Chip:|Total Number of Cores:|Memory:'
echo HW_END
"""
PROC_LOCK = threading.Lock()
STRONG_ERROR_RE = re.compile(
    r"XCTAssert|\*\* TEST|Test Case '.+' failed \(|^Failing tests:",
    re.I,
)
WEAK_ERROR_RE = re.compile(r"(?:^|\s)error:|\bfailed\b", re.I)
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.I)
ETA_N = 5
# File polls stay on their short cadence. A panel is stale only after several
# missed publishes, so a 15s as-of refresh does not flicker the stale badge.
STALE_S = {
    "local": 45,
    "git": 45,
    "mini": 45,
    "worker": 60,
    "devices": 120,
    "github": 180,
}
_CLOCK_FIELDS = re.compile(
    r'"(?:as_of|as_of_et|server_time|server_time_et)"\s*:\s*"(?:[^"\\]|\\.)*"'
)
JSONL_NAME = re.compile(r"^(.+)-\d{8}-\d{6}\.jsonl$")
# "grok" is the headless CLI. "Grok Bot" is the desktop app and would otherwise
# mark the Mini active forever via its helper processes.
INTEREST_RE = re.compile(
    r"xcodebuild|xcrun|devicectl|(?:^|[\s/])grok(?:\s|$)(?!Bot\b)|eng-[A-Za-z0-9._-]*-pipeline|build-slot|(?:^|[\s/_-])zai(?:[\s/._-]|$)|codex\s+exec\b",
    re.I,
)
DEVICE_CMD_RE = re.compile(r"xcodebuild|devicectl", re.I)


def _device_tool_args(command):
    try:
        args = shlex.split(command or "")
    except ValueError:
        return []
    if args and Path(args[0]).name == "xcrun":
        args = args[1:]
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option in ("--sdk", "-sdk", "--toolchain", "-toolchain"):
                if not args:
                    return []
                args.pop(0)
            elif option not in ("--run", "-r", "--no-cache", "--verbose", "--log"):
                return []
    return args


def device_runtime_command(command):
    """Only executing test/launch actions imply runtime activity; option values do not."""
    args = _device_tool_args(command)
    if not args:
        return False
    tool, rest = Path(args[0]).name, args[1:]
    if tool == "xcodebuild":
        flags = {"-quiet", "-verbose", "-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration", "-disableAutomaticPackageResolution", "-skipPackageUpdates"}
        i = 0
        while i < len(rest):
            arg = rest[i]
            if arg.startswith("-"):
                i += 1 if arg in flags or ":" in arg else 2
                continue
            if "=" not in arg and arg in ("build", "build-for-testing", "archive", "test", "test-without-building"):
                return arg in ("test", "test-without-building")
            i += 1
        return False
    if tool == "devicectl":
        while rest and rest[0].startswith("-"):
            option = rest.pop(0)
            if option in ("--timeout", "--json-output", "--log-output") and rest:
                rest.pop(0)
            elif "=" not in option:
                return False
        return rest[:3] == ["device", "process", "launch"]
    return False


def device_runtime_targets(command):
    """Bind runtime activity to explicit destinations, never incidental paths or text."""
    if not device_runtime_command(command):
        return []
    args = _device_tool_args(command)
    tool = Path(args[0]).name
    option = "-destination" if tool == "xcodebuild" else "--device"
    targets = []
    for i, arg in enumerate(args[1:], 1):
        value = args[i + 1] if arg == option and i + 1 < len(args) else (arg[len(option) + 1:] if arg.startswith(option + "=") else None)
        if value is None:
            continue
        if tool == "devicectl":
            targets.append(value)
        else:
            for part in value.split(","):
                key, _, val = part.partition("=")
                if key.strip() in ("id", "name") and val.strip():
                    targets.append(val.strip())
    return targets
Worker_PS_RE = re.compile(r"xcodebuild|xcrun|devicectl|python|mlx|llama|zai", re.I)

ENV = os.environ.copy()
ENV["DEVELOPER_DIR"] = site.developer_dir() or "/Applications/Xcode.app/Contents/Developer"
ENV["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + ENV.get("PATH", "")
ENV["GH_PROMPT_DISABLED"] = "1"
ENV["GIT_TERMINAL_PROMPT"] = "0"
ENV["PYTHONUNBUFFERED"] = "1"

_REDACTIONS = [
    re.compile(r"xai-[A-Za-z0-9_\-]{8,}", re.I),
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"gho_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"\bBSA[A-Za-z0-9]{16,}\b"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)(\b[A-Za-z_][A-Za-z0-9_.-]{0,40}=)([A-Fa-f0-9]{33,})"),
    re.compile(r"(?i)(\b[A-Za-z_][A-Za-z0-9_.-]{0,40}=)([A-Za-z0-9+/]{33,}={0,2})"),
]
OPEN_RE = re.compile(r"(NEEDS OWNER:|DECISION:)\s*(.*)$")
TAG_RE = re.compile(r"\[(D-[A-Za-z0-9_-]+)\]")
RES_RE = re.compile(r"RESOLVED:\s*(.*)$")
DECIDED_RE = re.compile(r"DECIDED:")
STATUS_RE = re.compile(
    r"^(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{1,2}:\d{2})\s+ET\s+([^:]+):\s*(.*)$"
)
START_RE = re.compile(r"^START\s+(.+?)\s+HEAD\s+([0-9a-fA-F]+)\s*$")
END_RE = re.compile(r"^END\s+(.+?)\s*$")
EXIT_RE = re.compile(r"^([A-Z][A-Z0-9_]*)_EXIT:(-?\d+)\s*$")
WHEN_RE = re.compile(
    r"^[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\s+(?:[A-Z]{2,4}\s+)?(\d{4})$"
)
MEANING_RE = re.compile(
    r"test case|testing started|\*\* test|test failed|test succeeded|testing failed|_EXIT:|error:|fatal error",
    re.I,
)
TOP_CPU_RE = re.compile(r"CPU usage:\s*([0-9.]+)%\s*user,\s*([0-9.]+)%\s*sys", re.I)
GPU_RE = re.compile(r'Device Utilization %"\s*=\s*([0-9.]+)')
MEM_FREE_RE = re.compile(r"System-wide memory free percentage:\s*([0-9.]+)\s*%", re.I)
SWAP_RE = re.compile(r"used\s*=\s*([0-9.]+)\s*([KMGT])", re.I)
VM_PAGE_RE = re.compile(r"Pages ([A-Za-z ]+):\s*(\d+)")



def worker_socket_path(root=None) -> Path:
    """ssh ControlPath: short, no whitespace (ssh splits -o values on spaces), private dir.
    The state root may contain spaces (~/Library/Application Support), so the socket lives in
    a short private directory in the user's dedicated package namespace."""
    import hashlib
    base = str(root if root is not None else ROOT)
    tag = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    directory = Path.home() / ".zai-devdashd" / "ssh" / tag
    path = directory / "worker.sock"
    if any(ch.isspace() for ch in str(path)) or len(str(path)) >= 100:
        raise ValueError("Configure a short, whitespace-free home path for SSH sockets")
    for folder in (directory.parent.parent, directory.parent, directory):
        try:
            info = folder.lstat()
        except FileNotFoundError:
            folder.mkdir(mode=0o700)
            info = folder.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or folder.resolve() != folder):
            raise ValueError("SSH socket directory must be private and user-owned")
    return path

class ProbeMixin:
    def refresh_rfb(self) -> None:
        now = time.time()
        if self.rfb_names and now - self.rfb_at < 60:
            return
        _code, out, _err = run_cmd(["/usr/bin/dns-sd", "-B", "_rfb._tcp", "local."], 2)
        names: list[str] = []
        for line in out.decode("utf-8", "replace").splitlines():
            if "_rfb._tcp." not in line:
                continue
            name = line.split("_rfb._tcp.", 1)[1].strip()
            if name and name not in names:
                names.append(name)
        with self.state_lock:
            if names:
                self.rfb_names = names
            self.rfb_at = time.time()
    def job_mini(self) -> float:
        try:
            self.refresh_rfb()
        except Exception as exc:
            log_exc("rfb", exc)
        code, out, err = run_cmd(["/bin/ps", "-axo", "pid=,ppid=,pcpu=,etime=,command="], 8)
        if code != 0:
            self.set_source_error("mini", "mini: ps failed " + err.decode("utf-8", "replace")[:200])
            return 5
        rows = []
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            rows.append({
                "pid": pid,
                "ppid": ppid,
                "cpu": parts[2],
                "elapsed": parts[3],
                "command": parts[4],
            })
        tree = {self.pid}
        changed = True
        while changed:
            changed = False
            for row in rows:
                if row["ppid"] in tree and row["pid"] not in tree:
                    tree.add(row["pid"])
                    changed = True
        processes = []
        commands = []
        for row in rows:
            if row["pid"] in tree:
                continue
            if not INTEREST_RE.search(row["command"]):
                continue
            short = clip(re.sub(r"\s+", " ", row["command"]), 180)
            processes.append({
                "pid": row["pid"],
                "cpu": row["cpu"],
                "elapsed": row["elapsed"],
                "command": short,
            })
            commands.append(row["command"])
        slots = []
        if SLOT_DIR.is_dir():
            for path in sorted(SLOT_DIR.glob("build-slot-*")):
                try:
                    kind = "dir" if path.is_dir() else "file"
                    item = {"name": path.name, "kind": kind, "mtime_et": fmt_et(path.stat().st_mtime)}
                    if path.is_file():
                        item["text"] = clip(path.read_text(errors="replace"), 400)
                    elif path.is_dir():
                        item["entries"] = sorted(child.name for child in path.iterdir())[:20]
                    slots.append(item)
                except OSError as exc:
                    slots.append({"name": path.name, "kind": "error", "text": str(exc)})
        with self.state_lock:
            self.mini_processes = processes
            self.mini_commands = commands
            self.mini_slots = slots
            self.mini_as_of = time.time()
            self.mini_error = None
        return 5
    def sample_host_cpu(self) -> None:
        percent = self.host_cpu.percent()
        if percent is None:
            return
        with self.state_lock:
            self.mini_load["cpu"] = percent
    def job_hw(self) -> float:
        if self.mini_hw:
            return 10 ** 9
        code, out, _err = run_cmd(["/bin/sh", "-c", HW_SH], 30)
        hw = hardware_from_probe(out.decode("utf-8", "replace")) if out else None
        if code != 0 and not hw:
            sys.stderr.write("mini hardware probe failed\n")
            return 60
        if not hw:
            sys.stderr.write("mini hardware probe returned no config\n")
            return 60
        with self.state_lock:
            self.mini_hw = hw
        self._save_hardware()
        return 10 ** 9
    def job_load(self) -> float:
        code, out, _err = run_cmd(["/bin/sh", "-c", LOAD_SH], 8)
        if not out:
            if code != 0:
                sys.stderr.write("mini load probe failed\n")
            return 4
        parsed = parse_load_text(out.decode("utf-8", "replace"))
        with self.state_lock:
            for key in ("gpu", "mem", "swap"):
                if parsed.get(key) is not None:
                    self.mini_load[key] = parsed[key]
        return 4
    def _load_hardware(self) -> None:
        try:
            raw = json.loads(HW_CACHE.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        mini = clean_hw(raw.get("mini"))
        remote = clean_hw(raw.get("worker"))
        if mini:
            self.mini_hw = mini
        if remote:
            self.worker_hw = remote
    def _save_hardware(self) -> None:
        with self.state_lock:
            payload = {"mini": self.mini_hw, "worker": self.worker_hw}
        if not payload["mini"] and not payload["worker"]:
            return
        HW_CACHE.parent.mkdir(exist_ok=True)
        tmp = HW_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(HW_CACHE)
    def worker_target(self, force: bool = False) -> str:
        """user@host. mDNS when enabled, otherwise the fallback IP."""
        cfg = self.config.get("worker") or board.DEFAULT_CONFIG["worker"]
        user = cfg.get("user") or ""
        fallback = cfg.get("ip") or ""
        host = fallback
        if cfg.get("resolve_mdns", True):
            now = time.time()
            cached = self.resolved_ip
            if not force and cached and now - self.resolved_at < 300:
                host = cached
            else:
                found = self.resolve_mdns(cfg.get("host") or "")
                host = found or fallback
                self.resolved_ip = host
                self.resolved_at = now
        else:
            self.resolved_ip = fallback
            self.resolved_at = time.time()
        return "%s@%s" % (user, host)
    def resolve_mdns(self, name: str) -> str:
        if not name or not name.endswith(".local"):
            return ""
        code, out, _err = run_cmd(["/usr/bin/dscacheutil", "-q", "host", "-a", "name", name], 3)
        text = out.decode("utf-8", "replace") if out else ""
        match = re.search(r"ip_address:\s*([0-9]+(?:\.[0-9]+){3})", text)
        if code == 0 and match:
            return match.group(1)
        code, out, err = run_cmd(["/usr/bin/dns-sd", "-G", "v4", name], 3)
        blob = ((out or b"") + b"\n" + (err or b"")).decode("utf-8", "replace")
        match = re.search(r"\b([0-9]{1,3}(?:\.[0-9]{1,3}){3})\b", blob)
        if match:
            return match.group(1)
        return ""
    def drop_worker_socket(self) -> None:
        cfg = self.config.get("worker") or board.DEFAULT_CONFIG["worker"]
        user = cfg.get("user") or ""
        host = self.resolved_ip or cfg.get("ip") or ""
        sock = worker_socket_path()
        run_cmd(
            ["ssh", "-o", "ControlPath=" + str(sock), "-O", "exit", "%s@%s" % (user, host)],
            8,
        )
        try:
            sock.unlink()
        except OSError:
            pass
    def note_worker_failure(self, message: str) -> float:
        heal = False
        with self.state_lock:
            self.worker_fails += 1
            fails = self.worker_fails
            if self.worker_down_since is None:
                self.worker_down_since = time.time()
            self.worker_error = "Worker unreachable since " + (fmt_et(self.worker_down_since) or "")
            if message:
                self.worker_error += " (" + clip(message, 180) + ")"
            delay = float(board.backoff_seconds(fails))
            self.worker_next_try = time.time() + delay
            self.worker_recovering = True
            if fails >= 2:
                heal = True
        if heal:
            self.drop_worker_socket()
            try:
                self.worker_target(force=True)
            except Exception as exc:
                log_exc("worker resolve", exc)
        return delay
    def job_worker(self) -> float:
        self.reload_config()
        interval = float((self.config.get("worker") or {}).get("probe_interval_s") or 10)
        probe_path = site.probe_script()
        try:
            probe = probe_path.read_bytes()
        except OSError as exc:
            return self.note_worker_failure("probe missing: %s" % exc)
        need_hw = self.worker_hw is None
        if need_hw:
            probe = b"HW_ONCE=1\n" + probe
        try:
            target = self.worker_target(False)
        except Exception as exc:
            return self.note_worker_failure(str(exc))
        sock = worker_socket_path()
        args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=4",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=60",
            "-o", "ControlPath=" + str(sock),
            target,
            "/bin/sh",
        ]
        timeout = 40 if need_hw else 22
        code, out, err = run_cmd(args, timeout, probe)
        if code != 0 and (b"already exists" in err or b"ControlSocket" in err):
            try:
                sock.unlink()
            except OSError:
                pass
            code, out, err = run_cmd(args, timeout, probe)
        if code != 0 or b"PROBE_END" not in out:
            message = err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace")[-200:]
            return self.note_worker_failure(message)
        parsed = parse_worker(out.decode("utf-8", "replace"))
        with self.state_lock:
            if parsed.get("hardware"):
                self.worker_hw = parsed["hardware"]
            else:
                parsed["hardware"] = self.worker_hw
            parsed["load"] = merge_load(self.worker.get("load"), parsed.get("load"))
            self.worker = parsed
            self.worker_as_of = time.time()
            self.worker_error = None
            self.worker_down_since = None
            self.worker_fails = 0
            self.worker_recovering = False
            self.worker_next_try = 0.0
            have_hw = bool(parsed.get("hardware"))
        if have_hw:
            self._save_hardware()
        return interval
    def note_devices_failure(self, message: str) -> float:
        with self.state_lock:
            self.devices_fails += 1
            fails = self.devices_fails
            delay = float(board.backoff_seconds(fails))
            self.devices_next_try = time.time() + delay
            self.devices_recovering = True
        if fails >= 2:
            stale = ROOT / "tmp" / "devices.json"
            try:
                stale.unlink()
            except OSError:
                pass
        self.set_source_error("devices", "device list: " + clip(message, 240))
        return delay
    def job_devices(self) -> float:
        self.reload_config()
        interval = float((self.config.get("devices") or {}).get("probe_interval_s") or 30)
        out_path = ROOT / "tmp" / "devices.json"
        code, _out, err = run_cmd(
            ["xcrun", "devicectl", "list", "devices", "--json-output", str(out_path)],
            25,
        )
        if code != 0 or not out_path.exists():
            return self.note_devices_failure(err.decode("utf-8", "replace") or "devicectl failed")
        try:
            payload = json.loads(out_path.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            return self.note_devices_failure(str(exc))
        devices = ((payload.get("result") or {}).get("devices")) or []
        logs = self.scan_recent_logs()
        with self.state_lock:
            commands = [cmd for cmd in self.mini_commands if device_runtime_command(cmd)]
            commands.extend(
                proc["command"] for proc in self.worker.get("processes", []) if device_runtime_command(proc.get("command") or "")
            )
        items = []
        for device in devices:
            if not isinstance(device, dict):
                continue
            props = device.get("deviceProperties") or {}
            hw = device.get("hardwareProperties") or {}
            conn = device.get("connectionProperties") or {}
            name = str(props.get("name") or "")
            dtype = str(hw.get("deviceType") or "")
            if not want_device(name, dtype):
                continue
            reality = str(hw.get("reality") or "")
            if reality.lower() == "simulated":
                continue
            udid = str(hw.get("udid") or "")
            identifier = str(device.get("identifier") or "")
            aliases = device_aliases(name, udid, identifier)
            hits = referenced(aliases, [target for cmd in commands for target in device_runtime_targets(cmd)])
            step = match_log(hits, logs) if hits else None
            os_num = props.get("osVersionNumber")
            os_text = str(os_num).strip() if os_num else ""
            if dtype == "iPad":
                os_name = "iPadOS"
            elif dtype == "iPhone":
                os_name = "iOS"
            else:
                os_name = dtype or ""
            os_label = (os_name + (" " + os_text if os_text else "")).strip()
            items.append({
                "name": name,
                "model": hw.get("marketingName") or hw.get("productType") or "",
                "product_type": str(hw.get("productType") or ""),
                "storage_bytes": hw.get("internalStorageCapacity"),
                "device_type": dtype,
                "os": os_text,
                "os_label": os_label,
                "state": board.device_state(conn),
                "pairing_state": str(conn.get("pairingState") or ""),
                "trust_state": str(conn.get("trustState") or ""),
                "udid": udid,
                "identifier": identifier,
                "reality": hw.get("reality") or "",
                "active": bool(hits),
                "step": step,
            })
        items.sort(key=lambda item: (0 if item["active"] else 1, item["name"]))
        pool = getattr(self, "pool", None)
        if pool is not None:
            try:
                pool.observe(items, self.config, time.time(), work_dir=ROOT / "tmp")
            except Exception as exc:
                log_exc("pool", exc)
        with self.state_lock:
            self.devices = items
            self.recent_logs = logs
            self.devices_as_of = time.time()
            self.devices_error = None
            self.devices_fails = 0
            self.devices_recovering = False
            self.devices_next_try = 0.0
        return interval
    def scan_recent_logs(self) -> list[dict]:
        cutoff = time.time() - 10 * 60
        found = []
        for directory in (PIPELINE_DIR, HEADLESS_LOGS):
            if not directory.is_dir():
                continue
            try:
                children = list(directory.iterdir())
            except OSError:
                continue
            for path in children:
                if not path.is_file():
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    found.append({"path": str(path), "name": path.name, "mtime": mtime})
        found.sort(key=lambda item: item["mtime"], reverse=True)
        return found[:40]
    def job_github(self) -> float:
        now = time.time()
        if self.gh_phase == "rate":
            if now < self.gh_backoff_until:
                return max(5.0, min(30.0, self.gh_backoff_until - now))
            self.gh_cycle = now
            return self.github_rate()
        if now - self.gh_cycle > 60 and self.gh_phase != "rate":
            # An expired phase cannot reach list/comment completion again.
            # Recover admission without claiming a new successful observation.
            self.gh_phase = "rate"
            self.gh_i = 0
            self.set_source_error("github", "github: refresh cycle timed out; retrying")
            return 5
        if self.gh_phase == "list":
            return self.github_list()
        if self.gh_phase == "comments":
            return self.github_comment()
        self.gh_phase = "rate"
        return site.poll_github()
    def github_rate(self) -> float:
        code, out, err = run_cmd([GH, "api", "rate_limit"], 20)
        if code != 0:
            self.set_source_error("github", "github: rate_limit " + clip(err.decode("utf-8", "replace"), 200))
            self.gh_phase = "rate"
            return site.poll_github()
        try:
            payload = json.loads(out.decode("utf-8", "replace"))
            core = (payload.get("resources") or {}).get("core") or {}
            limit = int(core.get("limit") or 0)
            remaining = int(core.get("remaining") or 0)
            reset = int(core.get("reset") or 0)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self.set_source_error("github", f"github: rate_limit parse {exc}")
            return site.poll_github()
        backed = bool(limit) and remaining < 0.2 * limit
        note = None
        if backed:
            note = (
                f"core remaining {remaining}/{limit} is under 20%; "
                f"backing off until {fmt_et(reset)}"
            )
            self.gh_backoff_until = float(reset)
        else:
            self.gh_backoff_until = 0
        with self.state_lock:
            self.gh_rate = {
                "limit": limit,
                "remaining": remaining,
                "reset": reset,
                "reset_et": fmt_et(reset),
                "backed_off": backed,
            }
            self.gh_backoff_note = note
        if backed:
            self.gh_phase = "rate"
            return max(15.0, min(90.0, self.gh_backoff_until - time.time()))
        self.gh_phase = "list"
        return 0
    def github_list(self) -> float:
        code, out, err = run_cmd(
            [
                GH, "pr", "list",
                "--repo", REPO_SLUG,
                "--base", "main",
                "--state", "open",
                "--limit", "100",
                "--json",
                "number,title,url,headRefName,headRefOid,reviewDecision,isDraft,updatedAt,statusCheckRollup,latestReviews",
            ],
            40,
        )
        if code != 0:
            self.set_source_error("github", "github: pr list " + clip(err.decode("utf-8", "replace"), 240))
            self.gh_phase = "rate"
            return site.poll_github()
        try:
            prs = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            self.set_source_error("github", f"github: pr list parse {exc}")
            self.gh_phase = "rate"
            return site.poll_github()
        if not isinstance(prs, list):
            self.set_source_error("github", "github: pr list was not a list")
            self.gh_phase = "rate"
            return site.poll_github()
        prs = sorted(prs, key=queue_key)
        with self.state_lock:
            self.gh_raw = prs
            self.gh_as_of = time.time()
            self.gh_error = None
            alive = {pr.get("number") for pr in prs}
            self.gh_comments = {num: comments for num, comments in self.gh_comments.items() if num in alive}
        self.gh_i = 0
        self.gh_phase = "comments"
        return 0
    def github_comment(self) -> float:
        with self.state_lock:
            prs = list(self.gh_raw)
        queued = prs[:12]
        if self.gh_i >= len(queued):
            self.gh_phase = "rate"
            return site.poll_github()
        number = queued[self.gh_i].get("number")
        self.gh_i += 1
        if not isinstance(number, int):
            return 0
        code, out, err = run_cmd(
            [GH, "api", f"repos/{REPO_SLUG}/pulls/{number}/comments?per_page=5&sort=created&direction=desc"],
            20,
        )
        if code != 0:
            self.set_source_error("github", f"github: comments #{number} " + clip(err.decode("utf-8", "replace"), 180))
            return 0
        try:
            comments = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return 0
        if isinstance(comments, list):
            with self.state_lock:
                self.gh_comments[number] = comments
                self.gh_as_of = time.time()
        elif isinstance(comments, dict) and comments.get("message"):
            self.set_source_error("github", f"github: comments #{number} " + clip(str(comments.get("message")), 160))
        return 0

def redact_text(value: str) -> str:
    if not value:
        return value
    for index, pattern in enumerate(_REDACTIONS):
        if index >= 7:
            value = pattern.sub(r"\1<redacted>", value)
        else:
            value = pattern.sub("<redacted>", value)
    return value
def redact(obj):
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    if isinstance(obj, dict):
        return {key: redact(val) for key, val in obj.items()}
    return obj
def iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, ET).isoformat()
def fmt_et(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, ET).strftime("%Y-%m-%d %H:%M:%S ET")
def human_dur(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
def clip(text: str, limit: int) -> str:
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
def parse_top_cpu(text: str) -> int | None:
    match = TOP_CPU_RE.search(text or "")
    if not match:
        return None
    busy = float(match.group(1)) + float(match.group(2))
    return int(round(max(0.0, min(100.0, busy))))
def parse_gpu(text: str) -> int | None:
    values = [float(item) for item in GPU_RE.findall(text or "")]
    if not values:
        return None
    return int(round(max(0.0, min(100.0, max(values)))))
def parse_mem_used(text: str) -> int | None:
    match = MEM_FREE_RE.search(text or "")
    if match:
        used = 100.0 - float(match.group(1))
        return int(round(max(0.0, min(100.0, used))))
    counts = {}
    for name, value in VM_PAGE_RE.findall(text or ""):
        counts[name.strip().lower()] = int(value)
    if "free" not in counts:
        return None
    total = sum(counts.values())
    if total <= 0:
        return None
    free = counts.get("free", 0) + counts.get("speculative", 0)
    return int(round(max(0.0, min(100.0, 100.0 * (total - free) / total))))
def parse_swap_label(text: str) -> str | None:
    match = SWAP_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    mb = value
    if unit == "K":
        mb = value / 1024.0
    elif unit == "G":
        mb = value * 1024.0
    elif unit == "T":
        mb = value * 1024.0 * 1024.0
    if mb >= 1024:
        return "%.1f GB" % (mb / 1024.0)
    if mb >= 10 or abs(mb - round(mb)) < 0.05:
        return "%.0f MB" % mb
    return "%.1f MB" % mb
def parse_load_text(text: str) -> dict:
    return {
        "cpu": parse_top_cpu(text),
        "gpu": parse_gpu(text),
        "mem": parse_mem_used(text),
        "swap": parse_swap_label(text),
    }
def merge_load(old: dict | None, new: dict | None) -> dict | None:
    merged = dict(old or {})
    for key in ("cpu", "gpu", "mem", "swap"):
        if new and new.get(key) is not None:
            merged[key] = new[key]
    return merged or None
def _profiler_field(text: str, label: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(label)}:\s*(.+)$", text or "", re.M)
    return match.group(1).strip() if match else None
def hardware_from_parts(sysctl_lines: list[str], profiler_text: str) -> dict | None:
    model_id = chip = None
    ncpu = None
    mem_bytes = None
    if len(sysctl_lines) >= 4:
        model_id = sysctl_lines[0] or None
        chip = sysctl_lines[1] or None
        try:
            ncpu = int(sysctl_lines[2])
        except (TypeError, ValueError):
            ncpu = None
        try:
            mem_bytes = int(sysctl_lines[3])
        except (TypeError, ValueError):
            mem_bytes = None
    model_name = _profiler_field(profiler_text, "Model Name")
    chip = _profiler_field(profiler_text, "Chip") or chip
    cores_text = _profiler_field(profiler_text, "Total Number of Cores") or ""
    mem_text = _profiler_field(profiler_text, "Memory") or ""
    cores = None
    cores_match = re.search(r"\d+", cores_text)
    if cores_match:
        cores = int(cores_match.group(0))
    elif ncpu:
        cores = ncpu
    memory_gb = None
    mem_match = re.search(r"(\d+(?:\.\d+)?)\s*GB", mem_text, re.I)
    if mem_match:
        memory_gb = int(round(float(mem_match.group(1))))
    elif mem_bytes:
        memory_gb = int(round(mem_bytes / float(1024 ** 3)))
    chip_short = (chip or "").removeprefix("Apple ").strip()
    title = model_name or model_id or ""
    if chip_short and chip_short.lower() not in title.lower():
        title = (title + " " + chip_short).strip()
    if not title and not cores and not memory_gb:
        return None
    bits = []
    if title:
        bits.append(title)
    if cores:
        bits.append("%d-core CPU" % cores)
    if memory_gb:
        bits.append("%d GB" % memory_gb)
    config = " · ".join(bits)
    if not config or re.search(r"serial|uuid|udid", config, re.I):
        return None
    return {"title": title, "cores": cores, "memory_gb": memory_gb, "config": config}
def hardware_from_probe(text: str) -> dict | None:
    sysctl_lines: list[str] = []
    profiler: list[str] = []
    mode = None
    for line in (text or "").splitlines():
        marker = line.strip()
        if marker == "HW_SYS":
            mode = "sys"
            continue
        if marker == "HW_PRO":
            mode = "pro"
            continue
        if marker == "HW_END":
            mode = None
            continue
        if mode == "sys":
            sysctl_lines.append(line.strip())
        elif mode == "pro":
            profiler.append(line)
    if not sysctl_lines and not profiler:
        return None
    return hardware_from_parts(sysctl_lines, "\n".join(profiler))
def load_phrase(load: dict | None) -> str:
    if not load:
        return ""
    parts = []
    if isinstance(load.get("cpu"), (int, float)):
        parts.append("CPU %d%%" % int(round(load["cpu"])))
    if isinstance(load.get("gpu"), (int, float)):
        parts.append("GPU %d%%" % int(round(load["gpu"])))
    if isinstance(load.get("mem"), (int, float)):
        parts.append("Mem %d%%" % int(round(load["mem"])))
    if load.get("swap"):
        parts.append("Swap " + str(load["swap"]))
    return " · ".join(parts)
def spec_line(config: str | None, load: dict | None) -> str:
    return " · ".join(part for part in (config or "", load_phrase(load)) if part)
def cpu_percent_from_ticks(prev: tuple | None, curr: tuple | None) -> int | None:
    if not prev or not curr or len(prev) != len(curr):
        return None
    delta = [int(b) - int(a) for a, b in zip(prev, curr)]
    total = sum(delta)
    if total <= 0 or len(delta) < 3:
        return None
    busy = total - delta[2]
    return int(round(max(0.0, min(100.0, 100.0 * busy / total))))
def host_cpu_ticks() -> tuple[int, int, int, int] | None:
    try:
        libc = host_cpu_ticks.lib  # type: ignore[attr-defined]
    except AttributeError:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        libc.host_statistics.restype = ctypes.c_int
        libc.host_statistics.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        libc.mach_host_self.restype = ctypes.c_uint
        host_cpu_ticks.lib = libc  # type: ignore[attr-defined]
    info = (ctypes.c_uint * 4)()
    count = ctypes.c_uint(4)
    status = libc.host_statistics(libc.mach_host_self(), 3, ctypes.byref(info), ctypes.byref(count))
    if status != 0:
        return None
    return (int(info[0]), int(info[1]), int(info[2]), int(info[3]))
class HostCPU:
    def __init__(self) -> None:
        self.prev: tuple[int, int, int, int] | None = None

    def percent(self) -> int | None:
        ticks = host_cpu_ticks()
        if ticks is None:
            return None
        prev = self.prev
        self.prev = ticks
        return cpu_percent_from_ticks(prev, ticks)
def clean_hw(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    config = raw.get("config")
    if not isinstance(config, str) or not config.strip() or len(config) > 180:
        return None
    if re.search(r"serial|uuid|udid", config, re.I):
        return None
    hw = {"config": config.strip()}
    title = raw.get("title")
    if isinstance(title, str) and title.strip():
        hw["title"] = title.strip()[:80]
    cores = raw.get("cores")
    if isinstance(cores, int):
        hw["cores"] = cores
    memory = raw.get("memory_gb")
    if isinstance(memory, int):
        hw["memory_gb"] = memory
    return hw
def device_key(name: str) -> str | None:
    match = re.search(r"(\d{3})", name or "")
    if not match:
        return None
    return "s" + match.group(1)
def slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (cleaned[:60] or "item")
def device_spec(dev: dict) -> str:
    if dev.get("placeholder"):
        return "Not in the last device list"
    parts = [dev.get("model") or "", dev.get("os_label") or "", dev.get("state") or ""]
    return " · ".join(str(part) for part in parts if part)
def present_devices(items: list) -> list[dict]:
    numbered: dict[str, dict] = {}
    extras = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        copy = dict(item)
        if str(copy.get("reality") or "").lower() == "simulated":
            continue
        key = device_key(copy.get("name") or "")
        if key and key not in numbered:
            numbered[key] = copy
        elif str(copy.get("device_type") or "") in ("iPhone", "iPad", "iPod") or "ipad" in (copy.get("name") or "").lower():
            extras.append(copy)
    extras.sort(key=lambda dev: dev.get("name") or "")
    presented = []
    configured = tuple((site.CURRENT.get("devices") or {}).get("numbers") or [])
    for number in configured:
        key, label, anchor = "s" + str(number), "D" + str(number), "dev-s" + str(number)
        dev = numbered.get(key)
        if not dev:
            dev = {
                "name": label,
                "model": "",
                "state": "unavailable",
                "os_label": "",
                "active": False,
                "placeholder": True,
            }
        dev["anchor"] = anchor
        dev["tile_name"] = label
        dev["spec"] = device_spec(dev)
        presented.append(dev)
    preferred = {"s" + str(number) for number in configured}
    for key in sorted(k for k in numbered if k not in preferred):
        dev = numbered[key]
        label = dev.get("name") or "Device " + key[1:]
        dev["anchor"] = "dev-" + key
        dev["tile_name"] = label
        dev["spec"] = device_spec(dev)
        presented.append(dev)
    used = {dev["anchor"] for dev in presented}
    for dev in extras:
        anchor = "dev-" + slug(dev.get("name") or "ipad")
        if anchor in used:
            anchor = slug(anchor + "-b")
        used.add(anchor)
        dev["anchor"] = anchor
        dev["tile_name"] = dev.get("name") or "iPad"
        dev["spec"] = device_spec(dev)
        presented.append(dev)
    return presented
def load_level(load: dict | None) -> int | None:
    if not isinstance(load, dict):
        return None
    values = []
    for key in ("cpu", "gpu"):
        if isinstance(load.get(key), (int, float)):
            values.append(int(round(load[key])))
    return max(values) if values else None
def tile_tone(status: str, level: int | None) -> str:
    if status == "offline":
        return "off"
    if status == "failing":
        return "fail"
    if status == "active":
        if level is None or level >= 40:
            return "busy"
        return "low"
    return "idle"
def short_command(command: str) -> str:
    parts = (command or "").split()
    if not parts:
        return ""
    exe = Path(parts[0]).name
    return clip(" ".join([exe] + parts[1:3]), 80)
def _job_name(item: dict, fallback: str) -> str:
    return item.get("fm_label") or item.get("label") or item.get("name") or fallback
def build_tiles(mini: dict, worker: dict, devices: list[dict]) -> list[dict]:
    tiles = []
    mini = mini or {}
    worker = worker or {}
    load = mini.get("load") if isinstance(mini.get("load"), dict) else {}
    mini_hw = mini.get("hardware") if isinstance(mini.get("hardware"), dict) else {}
    job = mini.get("job") or "Idle"
    failing = bool(mini.get("failing"))
    active = bool(mini.get("active")) and not failing
    if failing:
        status = "failing"
    elif active:
        status = "active"
    else:
        status = "idle"
    cpu = load.get("cpu") if isinstance(load.get("cpu"), (int, float)) else None
    tiles.append({
        "id": "mini",
        "name": site.mini_name(),
        "anchor": "sys-mini",
        "status": status,
        "tone": tile_tone(status, load_level(load) if status == "active" else None),
        "job": job,
        "goal": mini.get("goal") or "",
        "step": mini.get("step") or "",
        "connect": mini.get("connect"),
        "spec": mini_hw.get("config") or "",
        "cpu": None if cpu is None else int(round(cpu)),
        "gpu": None if not isinstance(load.get("gpu"), (int, float)) else int(round(load["gpu"])),
        "mem": None if not isinstance(load.get("mem"), (int, float)) else int(round(load["mem"])),
    })
    s_load = worker.get("load") if isinstance(worker.get("load"), dict) else {}
    s_hw = worker.get("hardware") if isinstance(worker.get("hardware"), dict) else {}
    s_off = bool(worker.get("unreachable_since") or worker.get("error"))
    s_fail = bool(worker.get("failing"))
    s_active = bool(worker.get("active")) and not s_off
    if s_off:
        s_status = "offline"
    elif s_fail:
        s_status = "failing"
    elif s_active:
        s_status = "active"
    else:
        s_status = "idle"
    s_cpu = s_load.get("cpu") if isinstance(s_load.get("cpu"), (int, float)) else None
    tiles.append({
        "id": "worker",
        "name": site.worker_name(),
        "anchor": "sys-worker",
        "status": s_status,
        "tone": tile_tone(s_status, load_level(s_load) if s_status == "active" else None),
        "job": "Offline" if s_off else (worker.get("job") or "Idle"),
        "goal": "" if s_off else (worker.get("goal") or ""),
        "step": "" if s_off else (worker.get("step") or ""),
        "connect": None if s_off else worker.get("connect"),
        "spec": s_hw.get("config") or "",
        "cpu": None if s_off or s_cpu is None else int(round(s_cpu)),
        "gpu": None if s_off or not isinstance(s_load.get("gpu"), (int, float)) else int(round(s_load["gpu"])),
        "mem": None if s_off or not isinstance(s_load.get("mem"), (int, float)) else int(round(s_load["mem"])),
    })
    for dev in devices:
        state = (dev.get("state") or "").lower()
        off = not board.device_reachable(state, bool(dev.get("placeholder")))
        outcome = (dev.get("outcome") or "") + " " + (dev.get("label") or "")
        dev_active = bool(dev.get("active")) and not off
        dev_fail = dev_active and "failed" in outcome.lower()
        named = dev.get("job") or ""
        if off:
            d_status = "offline"
            d_job = "Offline"
        elif dev_fail:
            d_status = "failing"
            d_job = named or dev.get("tile_name") or dev.get("name") or "Device"
        elif dev_active:
            d_status = "active"
            d_job = named or dev.get("tile_name") or dev.get("name") or "Device"
        else:
            d_status = "idle"
            d_job = named or "Idle"
        tiles.append({
            "id": dev.get("anchor"),
            "name": dev.get("tile_name") or dev.get("name") or "Device",
            "anchor": dev.get("anchor"),
            "status": d_status,
            "tone": tile_tone(d_status, None),
            "job": d_job,
            "goal": "" if off else (dev.get("goal") or ""),
            "step": "" if off else (dev.get("step_line") or ""),
            "connect": None if off else dev.get("connect"),
            "spec": dev.get("spec") or "",
            "cpu": None,
            "gpu": None,
            "mem": None,
            "pool_status": dev.get("pool_status") or "",
            "product_type": dev.get("product_type") or "",
            "storage_bytes": dev.get("storage_bytes"),
            "device_state": dev.get("state") or "",
            "os_label": dev.get("os_label") or "",
        })
    return tiles
def log_exc(where: str, exc: BaseException) -> None:
    sys.stderr.write("%s: %s: %s\n%s\n" % (
        where, exc.__class__.__name__, exc, traceback.format_exc().rstrip(),
    ))
def run_cmd(args: list[str], timeout: float, data: bytes | None = None) -> tuple[int, bytes, bytes]:
    with PROC_LOCK:
        return _run_cmd(args, timeout, data)
def _run_cmd(args: list[str], timeout: float, data: bytes | None = None) -> tuple[int, bytes, bytes]:
    try:
        completed = subprocess.run(
            args,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=ENV,
            check=False,
        )
        return completed.returncode, completed.stdout or b"", completed.stderr or b""
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        err = exc.stderr or b"timeout"
        if isinstance(out, str):
            out = out.encode()
        if isinstance(err, str):
            err = err.encode()
        return 124, out, err
    except OSError as exc:
        return 127, b"", str(exc).encode()
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
def queue_key(pr: dict) -> tuple:
    number = pr.get("number") or 0
    if number in QUEUE_INDEX:
        return (0, QUEUE_INDEX[number])
    return (1, number)
def meaningful_line(lines: list[str]) -> str | None:
    chosen = None
    for line in lines:
        stripped = line.strip()
        if stripped and MEANING_RE.search(stripped):
            chosen = stripped
    if chosen:
        return clip(chosen, 240)
    for line in reversed(lines):
        stripped = line.strip()
        if stripped:
            return clip(stripped, 240)
    return None
def tail_lines(path: Path, max_bytes: int = 8192) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()
def match_log(commands: list[str], logs: list[dict]) -> str | None:
    if not logs:
        return None
    for command in commands:
        lowered = command.lower()
        for log in logs:
            name = log["name"].lower()
            if name and name in lowered:
                return meaningful_line(tail_lines(Path(log["path"])))
        tokens = [tok for tok in re.findall(r"[A-Za-z0-9_.:-]{6,}", command) if not tok.isdigit()]
        for log in logs:
            lname = log["name"].lower()
            if any(tok.lower() in lname for tok in tokens):
                return meaningful_line(tail_lines(Path(log["path"])))
    return None
IOS_FAMILY = ("iPhone", "iPad", "iPod")
def want_device(name: str, device_type: str) -> bool:
    """Every discovered iPhone, iPad, or iPod. Accessories, Macs, Watches, TVs, and HomePods stay out."""
    return device_type in IOS_FAMILY
def device_aliases(name: str, udid: str, identifier: str) -> list[str]:
    aliases = [name, udid, identifier]
    return [alias for alias in aliases if alias and len(alias) >= 4]
def referenced(aliases: list[str], commands: list[str]) -> list[str]:
    hits = []
    for command in commands:
        lowered = command.lower()
        if any(alias.lower() in lowered for alias in aliases):
            hits.append(command)
    return hits
def parse_worker(text: str) -> dict:
    processes = []
    files = []
    runs = []
    runlogs = []
    tails: dict[str, list[str]] = {}
    build_product = None
    mode = None
    live_lines: list[str] = []
    sysctl_lines: list[str] = []
    profiler_lines: list[str] = []
    for line in text.splitlines():
        marker = line.strip()
        if marker == "LIVE_BEGIN":
            mode = "live"
            continue
        if marker == "LIVE_END":
            mode = None
            continue
        if marker == "HW_SYS":
            mode = "sys"
            continue
        if marker == "HW_PRO":
            mode = "pro"
            continue
        if marker == "HW_END":
            mode = None
            continue
        if mode == "live":
            live_lines.append(line)
            continue
        if mode == "sys":
            sysctl_lines.append(line.strip())
            continue
        if mode == "pro":
            profiler_lines.append(line)
            continue
        parts = line.split("\t")
        kind = parts[0] if parts else ""
        if kind == "PS" and len(parts) >= 5:
            processes.append({
                "pid": parts[1],
                "cpu": parts[2],
                "elapsed": parts[3],
                "command": clip(parts[4], 180),
            })
        elif kind == "FILE" and len(parts) >= 4:
            files.append({"label": parts[1], "mtime": parts[2], "path": parts[3], "tail": []})
        elif kind == "RUN" and len(parts) >= 3:
            runs.append({"mtime": parts[1], "path": parts[2]})
        elif kind == "RUNLOG" and len(parts) >= 3:
            runlogs.append({"label": "run log", "mtime": parts[1], "path": parts[2], "tail": []})
        elif kind == "TAIL" and len(parts) >= 3:
            tails.setdefault(parts[1], []).append(parts[2])
        elif kind == "BP" and len(parts) >= 3:
            build_product = {"mtime": parts[1], "path": parts[2]}
    for item in files + runlogs:
        item["tail"] = tails.get(item["path"], [])[:3]
        epoch = None
        try:
            epoch = float(item["mtime"])
        except (TypeError, ValueError):
            epoch = None
        item["mtime_et"] = fmt_et(epoch)
        item["name"] = Path(item["path"]).name
    if build_product:
        try:
            epoch = float(build_product["mtime"])
        except (TypeError, ValueError):
            epoch = None
        build_product["mtime_et"] = fmt_et(epoch)
        build_product["name"] = Path(build_product["path"]).name
    active = any(Worker_PS_RE.search(proc["command"]) for proc in processes)
    hardware = None
    if sysctl_lines or profiler_lines:
        hardware = hardware_from_parts(sysctl_lines, "\n".join(profiler_lines))
    load = parse_load_text("\n".join(live_lines)) if live_lines else None
    return {
        "processes": processes,
        "files": files,
        "runs": runs,
        "runlogs": runlogs,
        "build_product": build_product,
        "active": active,
        "hardware": hardware,
        "load": load,
    }
def fresh(as_of: float | None, threshold: float, error: str | None, **fields) -> dict:
    now = time.time()
    stale = as_of is None or (now - as_of) > threshold or bool(error)
    fields["as_of"] = iso(as_of)
    fields["as_of_et"] = fmt_et(as_of)
    fields["stale"] = stale
    fields["error"] = error
    return fields
