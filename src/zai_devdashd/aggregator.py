"""Collect the snapshot the page renders. Probes live in probes.py."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo

from zai_devdashd import auth, insights
from zai_devdashd import usage as usage_mod
from zai_devdashd import hwinfo
from zai_devdashd import accomplishments
from zai_devdashd import executive
from zai_devdashd import executive_live
from zai_devdashd import executive_brief
from zai_devdashd import autotune_status
from zai_devdashd import release_scope
from zai_devdashd import programs
from zai_devdashd import live_work
from zai_devdashd.linked_hosts import RemoteCollector
from zai_devdashd import config as site
from zai_devdashd import pool as device_pool
from zai_devdashd import labels as board
from zai_devdashd.probes import (
    DEVICE_NUMBERS, GH, DECISION_LINK, HOST, HW_CACHE, PIPELINE_DIR, PORT, QUEUE,
    device_runtime_command, device_runtime_targets, device_aliases, referenced,
    QUEUE_INDEX, REPO_SLUG, ROOT, SLOT_DIR, STATUS_PATH,
    install_config, ProbeMixin, HostCPU, build_tiles, clean_hw, clip, cpu_percent_from_ticks,
    fmt_et, fresh, hardware_from_parts, human_dur, iso, load_phrase, log_exc,
    parse_gpu, parse_load_text, parse_mem_used, parse_worker, parse_swap_label,
    parse_top_cpu, pid_alive, present_devices, queue_key, redact, redact_text,
    run_cmd, short_command, slug, spec_line, tail_lines, tile_tone,
)

ET = ZoneInfo('America/New_York')
_JOB_KEY = re.compile(r"[^A-Za-z0-9_.:-]+")
_SAFE_HOST = re.compile(r"\A[A-Za-z0-9._-]{1,253}(?::[0-9]{1,5})?\Z")
_SAFE_TARGET = re.compile(r"\A/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*\Z")

CANON_STEPS = ["MAC_BFT", "PHONE_BFT", "MAC_UNIT", "DIESEL", "REL"]
STEP_WORDS = {
    "MAC_BFT": "Mac build",
    "PHONE_BFT": "phone build",
    "MAC_UNIT": "Mac unit tests",
    "DIESEL": "Integration tests",
    "REL": "release build",
}
STEP_LOG_SUFFIX = {
    "MAC_BFT": "-mac-bft.log",
    "PHONE_BFT": "-phone-bft.log",
    "MAC_UNIT": "-mac-unit.log",
    "DIESEL": "-phone-runtime.log",
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
    r'|"(?:generated_at|age_s)"\s*:\s*-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?'
)
JSONL_NAME = re.compile(r"^(.+)-\d{8}-\d{6}\.jsonl$")
# "grok" is the headless CLI. "Grok Bot" is the desktop app and would otherwise
# mark the Mini active forever via its helper processes.
INTEREST_RE = re.compile(
    r"xcodebuild|xcrun|devicectl|(?:^|[\s/])grok(?:\s|$)(?!Bot\b)|eng-[A-Za-z0-9._-]*-pipeline|build-slot",
    re.I,
)
DEVICE_CMD_RE = re.compile(r"xcodebuild|devicectl", re.I)
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


def oldest_due_job(order, deadlines, now):
    """Serve overdue sources fairly when a short-cadence probe runs slowly."""
    due = [name for name in order if now >= deadlines[name]]
    return min(due, key=lambda name: deadlines[name]) if due else None


class Engine(ProbeMixin):
    def __init__(self) -> None:
        site.ensure_loaded()
        site.install(globals())
        install_config()
        self.state_lock = threading.Lock()
        self.cv = threading.Condition()
        self.version = 0
        self.payload = b"{}"
        self._content_key = ""
        self._content_at = 0.0
        self.stop = False
        self.pid = os.getpid()
        self.label_lock = threading.Lock()
        self.subjects: dict[str, str] = {}
        self.wanted_shas: set[str] = set()
        self.fm_cache: dict[str, dict] = {}
        self.fm_queue: list[dict] = []
        self.fm_queued: set[str] = set()
        self.fm_down_until = 0.0
        self.brief_pr_cache: dict[str, tuple] = {}
        self.brief_text_cache: dict = {}
        self._load_labels()
        self.store = insights.Store(ROOT / "data" / "telemetry.db")
        self.pool = device_pool.PoolStore(ROOT / "data" / "pool.db")
        self.lane_clocks: dict[str, dict] = {}
        self.worktrees: dict[str, str] = {}
        self.worktrees_at = 0.0
        self.ledger_lines_full: list[str] = []
        self.analyze_attempt: dict[str, float] = {}
        self.csrf = secrets.token_urlsafe(32)
        self.gate = auth.Gate()
        self.config = board.load_config(CONFIG_PATH)
        self.programs = programs.Cache(registry_path=site.programs_registry())
        self.executive_owner = executive_live.Cache(site.executive_owner(), producer=(site.CURRENT.get("executive_feed") or {}).get("producer_id", ""))
        monitor = site.CURRENT.get("autotune_monitor") or {}
        self.autotune_status = autotune_status.Cache(monitor.get("status_path"), monitor.get("policy_sha256"), monitor.get("producer_chat_id"), historical_valid=monitor.get("historical_valid"))
        ai = site.CURRENT.get("executive_ai") or {}
        ai_model = ai.get("model") if ai.get("model") in ("system", "pcc") else "system"
        self.executive_brief = executive_brief.Cache(
            ROOT / "cache" / "executive" / "brief.json",
            enabled=ai.get("enabled") is True and ai.get("model") in ("system", "pcc"),
            model=ai_model, market=executive_brief.market_context())
        self.force_jobs: set[str] = set()
        self.worker_fails = 0
        self.worker_next_try = 0.0
        self.worker_recovering = False
        self.devices_fails = 0
        self.devices_next_try = 0.0
        self.devices_recovering = False
        self.resolved_ip = ""
        self.resolved_at = 0.0
        self.rfb_names: list[str] = []
        self.rfb_at = 0.0
        self.lane_scans: dict[str, tuple] = {}

        self.status_parsed: list[dict] = []
        self.status_raws: list[str] = []
        self.status_events: list[dict] = []
        self.status_key = None
        self.status_as_of = None
        self.status_error = None

        self.decision_file_events: list[dict] = []
        self.decision_key = None

        self.pipe_cache: dict[str, tuple] = {}
        self.pipe_files: list[dict] = []
        self.pipe_as_of = None
        self.pipe_error = None

        self.jsonl: dict[str, JsonlCursor] = {}
        self.lane_meta: dict[str, dict] = {}
        self.lanes_as_of = None
        self.lanes_error = None

        self.ledger_entries: list[dict] = []
        self.needs: list[str] = []
        self.ledger_key = None
        self.ledger_as_of = None
        self.ledger_error = None

        self.refs: dict[str, str] = {}
        self.refs_as_of = None
        self.refs_error = None

        self.gh_raw: list[dict] = []
        self.gh_comments: dict[int, list] = {}
        self.gh_as_of = None
        self.gh_error = None
        self.gh_rate = None
        self.gh_backoff_until = 0.0
        self.gh_phase = "rate"
        self.gh_i = 0
        self.gh_cycle = 0.0
        self.gh_backoff_note = None

        self.mini_processes: list[dict] = []
        self.mini_slots: list[dict] = []
        self.mini_commands: list[str] = []
        self.remote_collector = RemoteCollector()
        self.usage_collector = usage_mod.UsageCollector()
        self.usage_block: dict | None = None
        self.usage_refreshing = False
        self.usage_thread = None
        self.hw_probe: dict = {}
        self.hw_topo: dict = {}
        self.hw_topo_at: dict = {}
        self.hw_history: dict = {}
        self.mini_as_of = None
        self.mini_error = None
        self.mini_hw = None
        self.mini_load = {"cpu": None, "gpu": None, "mem": None, "swap": None}
        self.host_cpu = HostCPU()
        self.worker_hw = None
        self._load_hardware()

        self.worker = {
            "processes": [],
            "files": [],
            "runs": [],
            "runlogs": [],
            "build_product": None,
            "active": False,
            "hardware": self.worker_hw,
            "load": None,
        }
        self.worker_as_of = None
        self.worker_error = None
        self.worker_down_since = None

        self.devices: list[dict] = []
        self.devices_as_of = None
        self.devices_error = None
        self.recent_logs: list[dict] = []
    def set_source_error(self, source: str, message: str) -> None:
        with self.state_lock:
            if source == "status":
                self.status_error = message
            elif source == "pipelines":
                self.pipe_error = message
            elif source == "lanes":
                self.lanes_error = message
            elif source == "ledger":
                self.ledger_error = message
            elif source == "git":
                self.refs_error = message
            elif source == "github":
                self.gh_error = message
            elif source == "mini":
                self.mini_error = message
            elif source == "worker":
                self.worker_error = message
            elif source == "devices":
                self.devices_error = message
        sys.stderr.write(message + "\n")
    def poll_local(self) -> None:
        if not site.adapter_enabled("git"):
            return
        for name, fn in (
            ("status", self.read_status),
            ("pipelines", self.read_pipelines),
            ("lanes", self.read_lanes),
            ("ledger", self.read_ledger),
        ):
            try:
                fn()
            except Exception as exc:
                self.set_source_error(name, f"{name}: {exc.__class__.__name__}: {exc}")
    def read_status(self) -> None:
        if not STATUS_PATH.exists():
            self.set_source_error("status", "status: mission/status.md is missing")
            return
        st = STATUS_PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
        events = self.status_events
        parsed = self.status_parsed
        raws = self.status_raws
        if key != self.status_key:
            text = STATUS_PATH.read_text(errors="replace")
            parsed, raws = parse_status(text)
            events = decision_events_from_lines(parsed, "mission/status.md", st.st_mtime)
            self.status_key = key
        extra = self.decision_file_events
        if DECISIONS_PATH.exists():
            dst = DECISIONS_PATH.stat()
            dkey = (dst.st_mtime_ns, dst.st_size)
            if dkey != self.decision_key:
                dparsed, _ = parse_status(DECISIONS_PATH.read_text(errors="replace"))
                extra = decision_events_from_lines(dparsed, "mission/decisions.md", dst.st_mtime)
                self.decision_key = dkey
                self.decision_file_events = extra
        else:
            self.decision_key = None
            extra = []
            self.decision_file_events = []
        with self.state_lock:
            self.status_parsed = parsed
            self.status_raws = raws
            self.status_events = events
            self.status_as_of = time.time()
            self.status_error = None
    def read_pipelines(self) -> None:
        files = []
        if PIPELINE_DIR.is_dir():
            for path in PIPELINE_DIR.glob("eng-*-pipeline.log"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = (st.st_mtime_ns, st.st_size)
                cached = self.pipe_cache.get(str(path))
                if cached and cached[0] == key:
                    parsed = cached[1]
                else:
                    text = path.read_text(errors="replace")
                    parsed = parse_pipeline_text(path.name[: -len(".log")], text, st.st_mtime)
                    self.pipe_cache[str(path)] = (key, parsed)
                files.append(parsed)
        with self.state_lock:
            self.pipe_files = files
            self.pipe_as_of = time.time()
            self.pipe_error = None
    def read_lanes(self) -> None:
        meta: dict[str, dict] = {}
        if HEADLESS_STATE.is_dir():
            for path in HEADLESS_STATE.iterdir():
                name = path.name
                if name.endswith(".session"):
                    lane = name[: -len(".session")]
                    meta.setdefault(lane, {})["session"] = path.read_text(errors="replace").strip()
                elif name.endswith(".branch"):
                    lane = name[: -len(".branch")]
                    meta.setdefault(lane, {})["branch"] = path.read_text(errors="replace").strip()
                elif name.endswith(".history"):
                    lane = name[: -len(".history")]
                    history = path.read_text(errors="replace").strip().splitlines()
                    meta.setdefault(lane, {})["history"] = history[-1] if history else ""
                    meta[lane]["history_blob"] = "\n".join(history[-8:])
        if HEADLESS_LOCKS.is_dir():
            for path in HEADLESS_LOCKS.iterdir():
                if not path.is_dir():
                    continue
                pid_path = path / "pid"
                pid = None
                if pid_path.is_file():
                    try:
                        pid = int(pid_path.read_text(errors="replace").strip().split()[0])
                    except (OSError, ValueError, IndexError):
                        pid = None
                meta.setdefault(path.name, {})["pid"] = pid
                meta[path.name]["busy"] = bool(pid and pid_alive(pid))
        if HEADLESS_LOGS.is_dir():
            for path in HEADLESS_LOGS.glob("*.jsonl"):
                match = JSONL_NAME.match(path.name)
                if not match:
                    continue
                lane = match.group(1)
                meta.setdefault(lane, {})
                cursor = self.jsonl.setdefault(str(path), JsonlCursor())
                try:
                    cursor.consume(path, f"headless/logs/{path.name}")
                except Exception:
                    continue
        lane_items = []
        for lane, info in sorted(meta.items()):
            files = []
            for path_s, cursor in self.jsonl.items():
                name = Path(path_s).name
                match = JSONL_NAME.match(name)
                if match and match.group(1) == lane:
                    try:
                        mtime = Path(path_s).stat().st_mtime
                    except OSError:
                        mtime = 0
                    files.append((mtime, cursor, path_s))
            files.sort(key=lambda pair: pair[0])
            titles: deque = deque(maxlen=15)
            cost = 0.0
            has_cost = False
            tokens = 0
            lane_exit = None
            for _, cursor, _path in files:
                for item in cursor.titles:
                    if isinstance(item, tuple):
                        titles.append(item)
                    else:
                        titles.append((0.0, item))
                cost += cursor.cost
                has_cost = has_cost or cursor.has_cost
                tokens += cursor.tokens
            if files:
                # The previous session's exit must not look like the live one.
                lane_exit = files[-1][1].lane_exit
            activity = self.lane_file_activity(files[-1][2]) if files else {}
            activity_epoch = activity.get("epoch") or (files[-1][0] if files else None)
            last = None
            if activity_epoch:
                rel = board.human_short(max(0.0, time.time() - float(activity_epoch)))
                clock = board.clock_hhmm(activity_epoch)
                if rel and clock:
                    last = "%s (%s ET)" % (rel, clock)
                else:
                    last = fmt_et(activity_epoch)
            shown = []
            recent = 0
            now_ts = time.time()
            for when, label in titles:
                shown.append(label)
                if when and now_ts - float(when) <= 60:
                    recent += 1
            newest_first = list(reversed(shown[-15:]))
            lane_items.append({
                "lane": lane,
                "busy": bool(info.get("busy")),
                "pid": info.get("pid"),
                "session": info.get("session"),
                "branch": info.get("branch"),
                "actions": newest_first,
                "actions_per_min": recent,
                "action_bucket": insights.action_bucket(newest_first),
                "cost_usd": round(cost, 6) if has_cost else None,
                "tokens": tokens,
                "has_cost": has_cost,
                "last_activity": last,
                "activity_assistant": activity.get("assistant") or "",
                "activity_tool": activity.get("tool") or "",
                "activity_newest": activity.get("newest") or "",
                "activity_epoch": activity_epoch,
                "lane_exit": lane_exit,
                "history": info.get("history"),
                "_history_blob": info.get("history_blob") or info.get("history") or "",
                "_markers": [marker for _, cursor, _path in files for marker in cursor.markers],
            })
        lane_items.sort(key=lambda item: (0 if item["busy"] else 1, item["lane"]))
        with self.state_lock:
            self.lane_meta_items = lane_items
            self.lanes_as_of = time.time()
            self.lanes_error = None
    def lane_file_activity(self, path_s: str) -> dict:
        """Newest assistant sentence and tool call, cached on path mtime and size."""
        path = Path(path_s)
        try:
            st = path.stat()
        except OSError:
            return {}
        key = (st.st_mtime_ns, st.st_size)
        cached = self.lane_scans.get(path_s)
        if cached and cached[0] == key:
            return cached[1]
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return {}
        found = board.scan_lane_jsonl(text)
        result = {
            "assistant": redact_text(found.get("assistant") or "")[:500],
            "tool": redact_text(found.get("tool") or "")[:160],
            "newest": found.get("newest") or "",
            "epoch": st.st_mtime,
        }
        self.lane_scans[path_s] = (key, result)
        if len(self.lane_scans) > 80:
            self.lane_scans.pop(next(iter(self.lane_scans)))
        return result
    def read_ledger(self) -> None:
        entries = self.ledger_entries
        full_lines = None
        if LEDGER_PATH.exists():
            st = LEDGER_PATH.stat()
            key = (st.st_mtime_ns, st.st_size)
            if key != self.ledger_key:
                rows = []
                raw_lines = []
                for line in LEDGER_PATH.read_text(errors="replace").splitlines():
                    if not line.strip():
                        continue
                    raw_lines.append(line)
                    parts = line.split("\t")
                    while len(parts) < 5:
                        parts.append("")
                    when = parts[0]
                    label = when
                    try:
                        parsed = datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
                        label = parsed.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
                    except ValueError:
                        pass
                    rows.append({
                        "time_et": label,
                        "kind": parts[1],
                        "branch": parts[2],
                        "sha": parts[3],
                        "note": clip(parts[4], 600),
                    })
                entries = list(reversed(rows[-15:]))
                full_lines = raw_lines
                self.ledger_key = key
                self.ledger_entries = entries
        needs = []
        if NEEDS_DIR.is_dir():
            needs = sorted(path.name for path in NEEDS_DIR.glob("*.md"))
        with self.state_lock:
            self.ledger_entries = entries
            self.needs = needs
            if full_lines is not None:
                self.ledger_lines_full = full_lines
            self.ledger_as_of = time.time()
            self.ledger_error = None
    def job_git(self) -> float:
        if not site.adapter_enabled("git"):
            return 60.0
        code, out, err = run_cmd(
            ["git", "-C", str(REPO), "for-each-ref", "--format=%(refname:short)%09%(objectname)", "refs/remotes/origin"],
            10,
        )
        if code != 0:
            message = err.decode("utf-8", "replace").strip() or f"git exit {code}"
            self.set_source_error("git", "git: " + clip(message, 240))
            return 3
        refs = {}
        for line in out.decode("utf-8", "replace").splitlines():
            if "\t" not in line:
                continue
            name, sha = line.split("\t", 1)
            refs[name.strip()] = sha.strip()
        with self.state_lock:
            self.refs = refs
            self.refs_as_of = time.time()
            self.refs_error = None
        return 3
    def reload_config(self) -> dict:
        cfg = board.load_config(CONFIG_PATH)
        with self.state_lock:
            self.config = cfg
        return cfg
    def _load_labels(self) -> None:
        try:
            raw = json.loads(LABEL_CACHE.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict):
            self.fm_cache = {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    def _save_labels(self) -> None:
        LABEL_CACHE.parent.mkdir(exist_ok=True)
        tmp = LABEL_CACHE.with_suffix(".json.tmp")
        with self.label_lock:
            payload = json.dumps(self.fm_cache, indent=2, sort_keys=True)
        tmp.write_text(payload)
        tmp.replace(LABEL_CACHE)
    def remember_sha(self, sha: str | None) -> str | None:
        if not sha or not SHA_RE.match(sha):
            return None
        key = sha.lower()
        with self.label_lock:
            self.wanted_shas.add(key)
            subject = self.subjects.get(key)
        if subject:
            return subject
        return None
    def brief_pr(self, lane: str) -> int | None:
        if not lane or not BRIEF_DIR.is_dir():
            return None
        files = []
        try:
            files = [path for path in BRIEF_DIR.glob(lane + "*.md") if path.is_file()]
        except OSError:
            return None
        signature = []
        for path in files:
            try:
                signature.append((path.name, path.stat().st_mtime_ns))
            except OSError:
                continue
        signature.sort()
        key = tuple(signature)
        cached = self.brief_pr_cache.get(lane)
        if cached and cached[0] == key:
            return cached[1]
        found = None
        for path in files:
            match = re.search(r"-(\d{3,5})\.md$", path.name)
            if match:
                found = int(match.group(1))
                break
        if found is None:
            files.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
            for path in files[:3]:
                try:
                    text = path.read_text(errors="replace")[:4000]
                except OSError:
                    continue
                found = pr_from_text(path.name, text)
                if found:
                    break
        self.brief_pr_cache[lane] = (key, found)
        return found
    def note_fm(self, job: dict) -> None:
        key = job.get("key") or ""
        if not key:
            return
        with self.label_lock:
            if key in self.fm_cache or key in self.fm_queued:
                return
            self.fm_queued.add(key)
            self.fm_queue.append(job)
    def attach_fm(self, item: dict, key: str) -> None:
        with self.label_lock:
            cached = self.fm_cache.get(key) or {}
        label = cached.get("label")
        summary = cached.get("summary")
        item["fm_label"] = label or None
        item["fm_summary"] = summary or None
        item["fm_backend"] = cached.get("backend") if label else None
    def apply_cached_title(self, item: dict, key: str, summary: str, commit_epoch, must_keep: str = "") -> None:
        with self.label_lock:
            cached = self.fm_cache.get(key) or {}
        label = cached.get("label") or ""
        if label and must_keep and must_keep not in label:
            label = ""
        if label and board.generic_title(label):
            label = ""
        goal = cached.get("goal") or ""
        if goal and board.generic_title(goal):
            goal = ""
        item["fm_goal"] = goal or None
        if label:
            item["fm_label"] = label
            item["fm_summary"] = insights.apply_commit_clock(cached.get("summary") or summary, commit_epoch)
            item["fm_backend"] = cached.get("backend")
        else:
            item["fm_label"] = None
            item["fm_summary"] = summary
            item["fm_backend"] = None
    def pipeline_brief_exists(self, pr: int) -> bool:
        try:
            return (BRIEF_DIR / ("%d-pipeline.md" % pr)).is_file()
        except Exception:
            return False

    def brief_goal_for(self, pr: int | None) -> str:
        if not pr or not BRIEF_DIR.is_dir():
            return ""
        try:
            dir_mtime = BRIEF_DIR.stat().st_mtime_ns
        except OSError:
            dir_mtime = 0
        cache_key = ("pr-brief", pr, dir_mtime)
        with self.label_lock:
            cached = self.brief_text_cache.get(cache_key)
        if isinstance(cached, str):
            return cached
        try:
            files = [path for path in BRIEF_DIR.glob("*.md") if path.is_file()]
        except OSError:
            files = []
        chosen = insights.pick_brief_file(files, "", "", pr)
        goal = ""
        if chosen is not None:
            try:
                text = "\n".join(chosen.read_text(errors="replace").splitlines()[:60])
            except OSError:
                text = ""
            goal = insights.goal_line(text)
        with self.label_lock:
            self.brief_text_cache[cache_key] = goal
        return goal
    def pr_title(self, snap: dict, pr: int | None) -> str:
        if not pr:
            return ""
        for item in (snap.get("github") or {}).get("prs") or []:
            if item.get("number") == pr:
                return item.get("title") or ""
        return ""
    def lane_brief(self, lane: str, history: str, pr: int | None) -> dict:
        if not lane or not BRIEF_DIR.is_dir():
            return {}
        try:
            files = [path for path in BRIEF_DIR.glob(lane + "*.md") if path.is_file()]
        except OSError:
            return {}
        signature = []
        for path in files:
            try:
                st = path.stat()
            except OSError:
                continue
            signature.append((path.name, st.st_mtime_ns, st.st_size))
        signature.sort()
        cache_key = (lane, history, pr, tuple(signature))
        with self.label_lock:
            cached = self.brief_text_cache.get(cache_key)
        if cached is not None:
            return cached
        chosen = insights.pick_brief_file(files, lane, history, pr)
        payload = {}
        if chosen is not None:
            try:
                text = "\n".join(chosen.read_text(errors="replace").splitlines()[:60])
            except OSError:
                text = ""
            payload = {
                "name": chosen.name,
                "sha1": hashlib.sha1(text.encode("utf-8", "replace")).hexdigest(),
                "text": text,
                "goal": insights.goal_line(text),
            }
        with self.label_lock:
            self.brief_text_cache[cache_key] = payload
            if len(self.brief_text_cache) > 40:
                self.brief_text_cache.clear()
                self.brief_text_cache[cache_key] = payload
        return payload
    def needs_excerpt(self, branch: str, pr: int | None) -> str:
        if not NEEDS_DIR.is_dir():
            return ""
        token = ""
        if branch and not branch.startswith("/"):
            token = branch.split("/")[-1].lower()
        try:
            paths = [path for path in NEEDS_DIR.glob("*NEEDS.md") if path.is_file()]
        except OSError:
            return ""
        for path in paths:
            name = path.name.lower()
            if (pr and str(pr) in path.name) or (token and len(token) >= 4 and token in name):
                try:
                    return path.read_text(errors="replace")[:1500]
                except OSError:
                    return ""
        for path in paths:
            try:
                text = path.read_text(errors="replace")[:2048]
            except OSError:
                continue
            if pr and ("#%d" % pr) in text:
                return text[:1500]
            if token and len(token) >= 4 and token in text.lower():
                return text[:1500]
        return ""
    def refresh_lane_clocks(self) -> None:
        now = time.time()
        if now - self.worktrees_at > 300 or not self.worktrees:
            code, out, _err = run_cmd(
                ["git", "-C", str(REPO), "worktree", "list", "--porcelain"],
                12,
            )
            if code == 0:
                self.worktrees = parse_worktrees(out.decode("utf-8", "replace"))
                self.worktrees_at = now
        with self.state_lock:
            lanes = [
                {"lane": item.get("lane"), "branch": item.get("branch") or ""}
                for item in getattr(self, "lane_meta_items", [])
                if item.get("busy")
            ]
            refs = dict(self.refs)
        clocks = {}
        for lane in lanes:
            name = lane.get("lane") or ""
            branch = lane.get("branch") or ""
            path = None
            if branch.startswith("/") and Path(branch).is_dir():
                path = branch
            elif branch:
                path = self.worktrees.get(branch)
            epoch = None
            subject = ""
            if path:
                code, out, _err = run_cmd(
                    ["git", "-C", path, "log", "-1", "--format=%ct%x09%s"],
                    8,
                )
                epoch, subject = parse_git_log(code, out)
            if epoch is None and branch and not branch.startswith("/"):
                sha = refs.get("origin/" + branch) or ""
                if sha:
                    code, out, _err = run_cmd(
                        ["git", "-C", str(REPO), "log", "-1", "--format=%ct%x09%s", sha],
                        8,
                    )
                    epoch, subject = parse_git_log(code, out)
            clocks[name] = {"commit_epoch": epoch, "subject": subject}
        with self.state_lock:
            self.lane_clocks.update(clocks)
    def _pr_rows(self) -> list:
        with self.state_lock:
            gh_raw = list(self.gh_raw)
            comments = dict(self.gh_comments)
            refs = dict(self.refs)
        rows = []
        for pr in gh_raw:
            number = pr.get("number")
            branch = pr.get("headRefName") or ""
            local = refs.get("origin/" + branch) if branch else None
            summarized = summarize_pr(pr, comments.get(number), None, local)
            checks = summarized.get("checks") or {}
            state = summarized.get("review_decision") or ""
            if summarized.get("draft"):
                state = (state + " draft").strip()
            rows.append({
                "number": number,
                "title": summarized.get("title") or "",
                "head": summarized.get("head") or "",
                "updated_at": summarized.get("updated_at") or "",
                "state": state,
                "pending": checks.get("pending") or 0,
                "fail": checks.get("fail") or 0,
                "body": "",
            })
        return rows
    def telemetry_packet(self) -> dict:
        with self.state_lock:
            pipe_files = list(self.pipe_files)
            commands = list(self.mini_commands) if self.mini_as_of and time.time() - self.mini_as_of <= STALE_S["mini"] else []
            lanes_in = [dict(item) for item in getattr(self, "lane_meta_items", [])]
            mini_load = dict(self.mini_load)
            worker = dict(self.worker)
            ledger_lines = list(self.ledger_lines_full)
            clocks = {key: dict(value) for key, value in self.lane_clocks.items()}
        pr_rows = self._pr_rows()
        pr_by_number = {row["number"]: row for row in pr_rows if isinstance(row.get("number"), int)}
        runs, _basis, _med = build_runs(pipe_files, commands, time.time())
        extra_by_name = {item.get("name"): item.get("extras") or [] for item in pipe_files}
        for run in runs:
            run["pr"] = pr_from_text(run.get("name") or "")
            run["extras"] = extra_by_name.get(run.get("name")) or []
            run["label"] = compose_label(run.get("pr"), None, pipeline_outcome(run))
        lanes = []
        for item in lanes_in:
            item.pop("_markers", None)
            branch = item.get("branch") or ""
            name = item.get("lane") or ""
            pr = pr_from_text(branch, item.get("history") or "", name)
            if pr is None:
                pr = self.brief_pr(name)
            history = item.get("_history_blob") or item.get("history") or ""
            brief = self.lane_brief(name, history, pr) if item.get("busy") else {}
            clock = clocks.get(name) or {}
            pr_row = pr_by_number.get(pr) or {}
            lanes.append({
                "lane": name,
                "branch": branch,
                "busy": bool(item.get("busy")),
                "actions": item.get("actions") or [],
                "actions_per_min": item.get("actions_per_min") or 0,
                "bucket": item.get("action_bucket") or insights.action_bucket(item.get("actions") or []),
                "cost": item.get("cost_usd"),
                "pr": pr,
                "commit_epoch": clock.get("commit_epoch"),
                "title": insights.lane_title(name, pr, brief.get("goal") or ""),
                "brief_goal": brief.get("goal") or "",
                "brief_excerpt": brief.get("text") or "",
                "needs": "",
                "pr_title": pr_row.get("title") or "",
                "pr_body": "",
            })
        command = ""
        for proc in worker.get("processes") or []:
            if re.search(r"zai", proc.get("command") or "", re.I):
                command = proc.get("command") or ""
                break
        facts = insights.zai_title(command) if command else {}
        tails = []
        for item in (worker.get("runlogs") or []) + (worker.get("files") or []):
            tails.extend(item.get("tail") or [])
        load = worker.get("load") if isinstance(worker.get("load"), dict) else {}
        return {
            "now": time.time(),
            "mini_load": mini_load,
            "worker_load": load,
            "lanes": lanes,
            "runs": runs,
            "prs": pr_rows,
            "ledger_lines": ledger_lines,
            "zai": {
                "active": bool(worker.get("active")),
                "command": command,
                "title": facts.get("title") or "",
                "run_id": facts.get("run_id") or "",
                "units": facts.get("units") or "",
                "kind": facts.get("kind") or "",
                "model": facts.get("model") or "",
                "tails": tails[:40],
            },
        }
    def note_analyze(self, job: dict) -> None:
        key = job.get("job_key") or ""
        digest = job.get("input_hash") or ""
        if not key or not digest:
            return
        pending = {
            "job_key": key,
            "title": job.get("title") or key,
            "brief_goal": job.get("brief_goal") or "",
            "goal": "",
            "plan": [],
            "on_track": "",
            "risk": "",
            "would_do_differently": "",
            "pattern_notes": "",
            "backend": "",
            "model_error": "",
            "findings": job.get("findings") or [],
            "stats": job.get("stats") or {},
        }
        row = self.store.insight_row(key)
        if row and row.get("input_hash") == digest:
            merged = dict(row.get("payload") or {})
            merged["findings"] = pending["findings"]
            merged["stats"] = pending["stats"]
            merged["title"] = pending["title"] or merged.get("title")
            merged["brief_goal"] = pending["brief_goal"] or merged.get("brief_goal")
            self.store.save_insight(key, digest, merged)
            return
        if time.time() - self.analyze_attempt.get(key, 0.0) < 300:
            self.store.save_insight(key, digest, pending)
            return
        self.store.save_insight(key, digest, pending)
        self.analyze_attempt[key] = time.time()
        self.note_fm({
            "key": "analyze:%s|%s" % (key, digest[:12]),
            "priority": 8,
            "mode": "analyze",
            "timeout": 36,
            "job_key": key,
            "input_hash": digest,
            "context": job.get("context") or "",
            "sha": "",
            "subject": "",
            "job_type": "analyze",
            "outcome": "",
            "failing_step": "",
            "log_paths": [],
            "first_error": "",
        })
    def finish_analyze(self, job: dict) -> None:
        fm_key = job.get("key") or ""
        job_key = job.get("job_key") or ""
        digest = job.get("input_hash") or ""
        try:
            code, out, err = run_cmd(
                [str(FMLABEL)],
                float(job.get("timeout") or 36),
                json.dumps({
                    "mode": "analyze",
                    "context": redact_text(job.get("context") or ""),
                }).encode("utf-8"),
            )
            row = self.store.insight_row(job_key) or {}
            current = dict(row.get("payload") or {})
            if code != 0:
                message = err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip()
                current["model_error"] = clip(message or "fmlabel exit %d" % code, 240)
                current["backend"] = ""
                self.store.save_insight(job_key, digest or row.get("input_hash") or "", current)
                return
            try:
                parsed = json.loads(out.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                current["model_error"] = "fmlabel returned non-json"
                current["backend"] = ""
                self.store.save_insight(job_key, digest, current)
                return
            track = str(parsed.get("on_track") or "")
            if track not in ("yes", "at_risk", "off_track"):
                track = ""
            plan = parsed.get("plan") if isinstance(parsed.get("plan"), list) else []
            current.update({
                "goal": clip(redact_text(str(parsed.get("goal") or "")), 400),
                "plan": [clip(redact_text(str(item)), 180) for item in plan[:4]],
                "on_track": track,
                "risk": clip(redact_text(str(parsed.get("risk") or "")), 300),
                "would_do_differently": clip(redact_text(str(parsed.get("would_do_differently") or "")), 300),
                "pattern_notes": clip(redact_text(str(parsed.get("pattern_notes") or "")), 400),
                "backend": str(parsed.get("backend") or ""),
                "model_error": "",
            })
            self.store.save_insight(job_key, digest, current)
        finally:
            with self.label_lock:
                self.fm_queued.discard(fm_key)
    def aggregate_loop(self) -> None:
        next_git = time.time() + 2
        next_sample = time.time() + 5
        next_prune = time.time() + 3600
        next_pattern = time.time() + 25
        while not self.stop:
            now = time.time()
            try:
                if site.adapter_enabled("git") and now >= next_git:
                    self.refresh_lane_clocks()
                    next_git = time.time() + 60
                if now >= next_sample:
                    self.store.ingest(self.telemetry_packet())
                    next_sample = time.time() + 30
                if now >= next_prune:
                    self.store.prune()
                    next_prune = time.time() + 3600
                if (site.CURRENT.get("labels") or {}).get("enabled") is True and now >= next_pattern:
                    packet = self.telemetry_packet()
                    for lane in packet.get("lanes") or []:
                        if lane.get("busy"):
                            lane["needs"] = redact_text(self.needs_excerpt(lane.get("branch") or "", lane.get("pr")))
                    self.store.ingest(packet)
                    for job in insights.jobs_from_packet(packet, self.store):
                        self.note_analyze(job)
                    next_pattern = time.time() + 300
            except Exception as exc:
                sys.stderr.write("aggregate: %s: %s\n%s\n" % (
                    exc.__class__.__name__, exc, traceback.format_exc().rstrip(),
                ))
            time.sleep(1.0)
    def _insight_keys(self, snap: dict) -> list:
        keys = []
        for lane in (snap.get("lanes") or {}).get("items") or []:
            if lane.get("busy"):
                keys.append(lane.get("insight_key") or ("lane:" + str(lane.get("lane") or "")))
        for run in (snap.get("pipelines") or {}).get("runs") or []:
            if run.get("state") == "running":
                keys.append("pipeline:" + str(run.get("name") or ""))
        worker = snap.get("worker") or {}
        if worker.get("active") and not (worker.get("unreachable_since") or worker.get("error")):
            if worker.get("insight_key"):
                keys.append(worker["insight_key"])
        return keys
    def apply_labels(self, snap: dict, refs: dict) -> None:
        runs = (snap.get("pipelines") or {}).get("runs") or []
        for run in runs:
            pr = pr_from_text(run.get("name") or "")
            sha = run.get("head") or ""
            subject = self.remember_sha(sha)
            outcome = pipeline_outcome(run)
            run["pr"] = pr
            run["subject"] = subject
            run["outcome"] = outcome
            run["label"] = compose_label(pr, subject, outcome)
            run["chart_label"] = compose_label(pr, None, outcome)
            run["detail"] = " · ".join(part for part in (run.get("name"), run.get("head8")) if part)
            key = "pipeline:%s|%s" % (run.get("name") or "", outcome)
            self.note_fm({
                "key": key,
                "priority": 0,
                "sha": sha.lower() if sha else "",
                "subject": subject or "",
                "job_type": "Pipeline",
                "outcome": outcome,
                "failing_step": failing_step_word(run),
                "log_paths": step_log_paths(run),
                "first_error": "",
            })
            self.attach_fm(run, key)
            if run.get("state") == "running":
                pr_title = self.pr_title(snap, pr)
                brief_goal = self.brief_goal_for(pr)
                needs_goal = insights.goal_line(self.needs_excerpt("", pr)) if pr else ""
                purpose = board.pipeline_purpose(pr, pr_title, subject)
                goal = board.pipeline_goal(pr_title, subject, brief_goal, needs_goal)
                run["step_line"] = board.pipeline_step(run)
                digest = hashlib.sha1((purpose + "|" + goal).encode("utf-8", "replace")).hexdigest()[:12]
                tkey = "pipeline-title:%s|%s" % (run.get("name") or "", digest)
                self.note_fm({
                    "key": tkey,
                    "priority": -2,
                    "mode": "title",
                    "timeout": 22,
                    "lane": "pipeline",
                    "branch": "",
                    "pr": "" if pr is None else str(pr),
                    "brief": brief_goal or needs_goal or pr_title or subject or "",
                    "actions": "",
                    "clock": "no commit time in git log",
                    "bucket": "running",
                    "sha": sha.lower() if sha else "",
                    "subject": pr_title or subject or "",
                    "job_type": "Pipeline title",
                    "outcome": purpose,
                    "failing_step": "",
                    "log_paths": [],
                    "first_error": "",
                })
                self.apply_cached_title(run, tkey, goal, None)
                if pr and self.pipeline_brief_exists(pr):
                    # A per-run pipeline brief (pipeline-goal.sh) is authoritative; skip the FM rewrite.
                    head8 = (run.get("head8") or (sha or "")[:8])
                    run["purpose"] = board.usable_title(None, "#%d %s · pipeline" % (pr, head8) if head8 else purpose)
                    run["goal_line"] = board.usable_goal(brief_goal, goal)
                else:
                    run["purpose"] = board.usable_title(run.get("fm_label"), purpose)
                    run["goal_line"] = board.usable_goal(run.get("fm_goal"), goal)
        newest: dict[int, float] = {}
        for run in runs:
            group = run.get("pr") or 0
            newest[group] = max(newest.get(group, 0.0), run.get("start_epoch") or 0)
        runs.sort(key=lambda run: (
            0 if run.get("pr") else 1,
            -newest.get(run.get("pr") or 0, 0),
            -(run.get("start_epoch") or 0),
        ))

        for lane in (snap.get("lanes") or {}).get("items") or []:
            branch = lane.get("branch") or ""
            pr = pr_from_text(branch, lane.get("history") or "", lane.get("lane") or "")
            if pr is None:
                pr = self.brief_pr(lane.get("lane") or "")
            sha = ""
            if branch and not branch.startswith("/") and "\\" not in branch:
                sha = refs.get("origin/" + branch) or ""
            subject = self.remember_sha(sha)
            history_blob = lane.pop("_history_blob", "") or lane.get("history") or ""
            session = (lane.get("session") or "")[:8]
            lane["detail"] = " · ".join(part for part in (lane.get("lane"), branch, session) if part)
            lane["pr"] = pr
            lane["subject"] = subject
            if lane.get("busy"):
                brief = self.lane_brief(lane.get("lane") or "", history_blob, pr)
                with self.state_lock:
                    clock = dict(self.lane_clocks.get(lane.get("lane") or "") or {})
                epoch = clock.get("commit_epoch")
                bucket = lane.get("action_bucket") or "waiting"
                title = insights.lane_title(lane.get("lane") or "", pr, brief.get("goal") or subject or "")
                summary = insights.commit_summary(bucket, epoch)
                lane["outcome"] = bucket
                lane["label"] = title
                lane["insight_key"] = "lane:" + str(lane.get("lane") or "")
                key = "lane-title:%s|%s|%s" % (lane.get("lane") or "", brief.get("sha1") or "none", bucket)
                clock_line = summary if epoch else "no commit time in git log"
                self.note_fm({
                    "key": key,
                    "priority": -2,
                    "mode": "title",
                    "timeout": 22,
                    "lane": lane.get("lane") or "",
                    "branch": branch,
                    "pr": "" if pr is None else str(pr),
                    "brief": brief.get("text") or "",
                    "actions": redact_text("\n".join(lane.get("actions") or [])),
                    "clock": clock_line,
                    "bucket": bucket,
                    "sha": sha.lower() if sha else "",
                    "subject": brief.get("goal") or subject or "",
                    "job_type": "Lane title",
                    "outcome": title,
                    "failing_step": "",
                    "log_paths": [],
                    "first_error": "",
                })
                self.apply_cached_title(lane, key, summary, epoch)
                pr_title = self.pr_title(snap, pr)
                needs_goal = insights.goal_line(self.needs_excerpt(branch, pr))
                goal = board.pipeline_goal(pr_title, subject, brief.get("goal"), needs_goal)
                lane["purpose"] = board.usable_title(lane.get("fm_label"), title)
                lane["goal_line"] = board.usable_goal(lane.get("fm_goal"), goal)
            else:
                if lane.get("lane_exit") not in (None, ""):
                    outcome = "Grok Build lane passed" if lane.get("lane_exit") == 0 else "Grok Build lane failed"
                else:
                    outcome = "Grok Build lane idle"
                lane["outcome"] = outcome
                lane["label"] = compose_label(pr, subject, outcome)
                text = " ".join(lane.get("actions") or [])
                key = "lane:%s|%s" % (lane.get("lane") or "", outcome)
                self.note_fm({
                    "key": key,
                    "priority": 1,
                    "sha": sha.lower() if sha else "",
                    "subject": subject or "",
                    "job_type": "Grok Build lane",
                    "outcome": outcome,
                    "failing_step": "",
                    "log_paths": [],
                    "first_error": redact_text(clip(text, 200)) if outcome.endswith("failed") else "",
                })
                self.attach_fm(lane, key)
            lane["step_line"] = board.lane_activity(
                lane.get("activity_assistant"),
                lane.get("activity_tool"),
                lane.get("activity_newest"),
                bool(lane.get("busy")),
                lane.get("activity_epoch"),
                time.time(),
                lane.get("actions"),
                lane.get("action_bucket"),
            )

        for entry in (snap.get("ledger") or {}).get("entries") or []:
            sha = entry.get("sha") or ""
            if not SHA_RE.match(sha):
                sha = ""
            pr = pr_from_text(entry.get("branch") or "", entry.get("note") or "")
            subject = self.remember_sha(sha)
            outcome = cr_outcome(entry.get("kind") or "", entry.get("note") or "")
            entry["pr"] = pr
            entry["subject"] = subject
            entry["outcome"] = outcome
            entry["label"] = compose_label(pr, subject, outcome)
            entry["detail"] = " · ".join(part for part in (entry.get("sha"), entry.get("kind")) if part and part != "-")
            if sha or pr:
                key = "cr:%s|%s|%s" % (entry.get("time_et") or "", sha or entry.get("branch") or "", outcome)
                self.note_fm({
                    "key": key,
                    "priority": 4,
                    "sha": sha.lower() if sha else "",
                    "subject": subject or "",
                    "job_type": "CodeRabbit review",
                    "outcome": outcome,
                    "failing_step": "",
                    "log_paths": [],
                    "first_error": redact_text(clip(entry.get("note") or "", 200)),
                })
                self.attach_fm(entry, key)

        worker = snap.get("worker") or {}
        commands = [proc.get("command") or "" for proc in worker.get("processes") or []]
        tails = {}
        for item in (worker.get("runlogs") or []) + (worker.get("files") or []):
            tails.setdefault(item.get("path") or "", []).extend(item.get("tail") or [])
        labeled_runs = []
        for run in worker.get("runs") or []:
            path = run.get("path") or ""
            name = run.get("name") or Path(path).name
            text = "\n".join(tails.get(path, []))
            for log in worker.get("runlogs") or []:
                if name and name.replace(".sh", "") in (log.get("name") or ""):
                    text += "\n" + "\n".join(log.get("tail") or [])
            sha_match = re.search(r"\bcommit\s+([0-9a-f]{7,40})\b", text, re.I)
            sha = sha_match.group(1) if sha_match else ""
            pr = pr_from_text(name, path, text)
            subject = self.remember_sha(sha)
            outcome = zai_outcome(name, text, commands)
            item = dict(run)
            item["pr"] = pr
            item["subject"] = subject
            item["outcome"] = outcome
            item["label"] = compose_label(pr, subject, outcome)
            item["detail"] = name
            error = ""
            for line in text.splitlines():
                if STRONG_ERROR_RE.search(line) or WEAK_ERROR_RE.search(line):
                    error = redact_text(clip(line.strip(), 240))
                    break
            key = "zai:%s|%s" % (name, outcome)
            self.note_fm({
                "key": key,
                "priority": 2,
                "sha": sha.lower() if sha else "",
                "subject": subject or "",
                "job_type": "ZAI run",
                "outcome": outcome,
                "failing_step": "",
                "log_paths": [],
                "first_error": error,
            })
            self.attach_fm(item, key)
            labeled_runs.append(item)
        zai_command = ""
        for command in commands:
            if re.search(r"zai", command, re.I):
                zai_command = command
                break
        facts = insights.zai_title(zai_command) if zai_command else {}
        if facts.get("title"):
            summary = facts["title"].rstrip(".") + "."
            worker["label"] = facts["title"]
            worker["insight_key"] = "zai:" + str(facts.get("run_id") or "live")
            key = "zai-title:%s|%s|%s" % (
                facts.get("run_id") or "live",
                facts.get("units") or "",
                facts.get("kind") or "",
            )
            self.note_fm({
                "key": key,
                "priority": -2,
                "mode": "title",
                "timeout": 22,
                "lane": "Worker",
                "branch": "",
                "pr": "",
                "brief": redact_text(clip(zai_command, 500)),
                "actions": redact_text(clip(zai_command, 500)),
                "clock": "no commit time in git log",
                "bucket": facts.get("kind") or "run",
                "sha": "",
                "subject": facts["title"],
                "job_type": "ZAI title",
                "outcome": facts["title"],
                "failing_step": "",
                "log_paths": [],
                "first_error": "",
            })
            self.apply_cached_title(worker, key, summary, None, facts.get("run_id") or "")
            worker["purpose"] = facts["title"]
            worker["goal_line"] = board.usable_goal(
                worker.get("fm_goal"),
                board.zai_goal(facts.get("units") or "", facts.get("run_id") or "", facts.get("kind") or ""),
            )
            worker["step_line"] = board.zai_step(facts.get("units") or "")
            worker["zai_units"] = facts.get("units") or ""
            worker["zai_run"] = facts.get("run_id") or ""
            for item in labeled_runs:
                stem = Path(item.get("detail") or item.get("name") or "").stem
                if stem and stem in zai_command:
                    item["label"] = facts["title"]
                    self.apply_cached_title(item, key, summary, None, facts.get("run_id") or "")
        elif commands:
            title, goal, step_line = board.remote_tool_lines(commands)
            if title != "Worker job":
                worker["purpose"] = title
                worker["goal_line"] = goal
                worker["step_line"] = step_line
        worker["runs"] = labeled_runs

        for dev in (snap.get("devices") or {}).get("items") or []:
            if not dev.get("active"):
                dev["label"] = None
                dev["fm_label"] = None
                dev["fm_summary"] = None
                continue
            step = dev.get("step") or ""
            if re.search(r"TEST FAILED|XCTAssert|(?:^|\s)error:", step, re.I):
                outcome = "Device test failed"
            else:
                outcome = "Device test running"
            pr = pr_from_text(step, dev.get("name") or "")
            dev["pr"] = pr
            dev["outcome"] = outcome
            dev["label"] = compose_label(pr, None, outcome)
            dev["detail"] = dev.get("name") or ""
            key = "device:%s|%s" % (dev.get("udid") or dev.get("name") or "", outcome)
            self.note_fm({
                "key": key,
                "priority": 3,
                "sha": "",
                "subject": "",
                "job_type": "Device test",
                "outcome": outcome,
                "failing_step": dev.get("name") or "",
                "log_paths": [],
                "first_error": redact_text(clip(step, 240)),
            })
            self.attach_fm(dev, key)
            name = dev.get("name") or "Device"
            title, goal, step_line = board.device_lines(name, step)
            dev["purpose"] = board.usable_title(dev.get("fm_label"), title)
            dev["goal_line"] = board.usable_goal(dev.get("fm_goal"), goal)
            dev["step_line"] = step_line
    def fill_subjects(self) -> None:
        with self.label_lock:
            missing = [sha for sha in self.wanted_shas if sha not in self.subjects]
        for sha in missing[:30]:
            code, out, _err = run_cmd(
                ["git", "-C", str(REPO), "log", "-1", "--format=%s", sha],
                8,
            )
            subject = ""
            if code == 0:
                lines = out.decode("utf-8", "replace").strip().splitlines()
                subject = lines[0].strip() if lines else ""
            with self.label_lock:
                self.subjects[sha] = subject
    def pop_fm(self) -> dict | None:
        with self.label_lock:
            if not self.fm_queue:
                return None
            self.fm_queue.sort(key=lambda job: (job.get("priority", 9), job.get("key") or ""))
            return self.fm_queue.pop(0)
    def label_loop(self) -> None:
        if (site.CURRENT.get("labels") or {}).get("enabled") is not True:
            return
        announced = False
        while not self.stop:
            try:
                self.fill_subjects()
                if self.fm_down_until and time.time() < self.fm_down_until:
                    time.sleep(1.0)
                    continue
                job = self.pop_fm()
                if not job:
                    time.sleep(0.4)
                    continue
                if job.get("mode") == "analyze":
                    self.finish_analyze(job)
                    continue
                if not FMLABEL.is_file():
                    if not announced:
                        sys.stderr.write("fmlabel missing at %s; using deterministic labels\n" % FMLABEL)
                        announced = True
                    with self.label_lock:
                        self.fm_queued.discard(job["key"])
                    time.sleep(2.0)
                    continue
                sha = (job.get("sha") or "").lower()
                if sha:
                    with self.label_lock:
                        known = sha in self.subjects
                    if not known:
                        with self.label_lock:
                            self.wanted_shas.add(sha)
                        self.fill_subjects()
                with self.label_lock:
                    subject = self.subjects.get(sha) or job.get("subject") or ""
                first_error = job.get("first_error") or ""
                if not first_error:
                    first_error = first_error_in_paths(job.get("log_paths") or [])
                mode = job.get("mode") or "label"
                if mode == "title":
                    payload = {
                        "mode": "title",
                        "lane": job.get("lane") or "",
                        "branch": job.get("branch") or "",
                        "pr": job.get("pr") or "",
                        "brief": job.get("brief") or "",
                        "actions": job.get("actions") or "",
                        "clock": job.get("clock") or "",
                        "bucket": job.get("bucket") or "",
                    }
                    timeout = float(job.get("timeout") or 22)
                    word_limit = 22
                else:
                    payload = {
                        "subject": subject,
                        "job_type": job.get("job_type") or "",
                        "outcome": job.get("outcome") or "",
                        "failing_step": job.get("failing_step") or "",
                        "first_error": first_error,
                    }
                    timeout = 20
                    word_limit = 8
                code, out, err = run_cmd([str(FMLABEL)], timeout, json.dumps(payload).encode("utf-8"))
                key = job["key"]
                if code == 2:
                    sys.stderr.write("fmlabel unavailable: " + clip(err.decode("utf-8", "replace"), 240) + "\n")
                    self.fm_down_until = time.time() + 600
                    with self.label_lock:
                        self.fm_queue.insert(0, job)
                    continue
                if code != 0:
                    message = err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip()
                    with self.label_lock:
                        self.fm_cache[key] = {"error": clip(message or "fmlabel exit %d" % code, 240)}
                        self.fm_queued.discard(key)
                    self._save_labels()
                    continue
                try:
                    parsed = json.loads(out.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    with self.label_lock:
                        self.fm_cache[key] = {"error": "fmlabel returned non-json"}
                        self.fm_queued.discard(key)
                    self._save_labels()
                    continue
                label = clamp_words(redact_text(str(parsed.get("label") or "")), word_limit)
                summary = one_sentence(redact_text(str(parsed.get("summary") or "")))
                goal = one_sentence(redact_text(str(parsed.get("goal") or "")))
                if board.generic_title(label):
                    label = ""
                if not label and not goal:
                    with self.label_lock:
                        self.fm_cache[key] = {"error": "fmlabel returned an empty label"}
                        self.fm_queued.discard(key)
                    self._save_labels()
                    continue
                with self.label_lock:
                    self.fm_cache[key] = {
                        "label": label,
                        "summary": summary,
                        "goal": goal,
                        "backend": str(parsed.get("backend") or ""),
                    }
                    self.fm_queued.discard(key)
                self._save_labels()
            except Exception as exc:
                sys.stderr.write("label loop: %s: %s\n" % (exc.__class__.__name__, exc))
                time.sleep(1.0)
    def hold_for(self, number: int, lines: list[str]) -> str | None:
        pattern = re.compile(rf"#{number}(?!\d)")
        for line in reversed(lines):
            if pattern.search(line):
                return clip(line, 180)
        return None
    def build(self) -> dict:
        now = time.time()
        with self.state_lock:
            status_events = list(self.status_events)
            decision_file_events = list(self.decision_file_events)
            lane_items = [dict(item) for item in getattr(self, "lane_meta_items", [])]
            raws = list(self.status_raws)
            parsed = list(self.status_parsed)
            pipe_files = list(self.pipe_files)
            commands = list(self.mini_commands)
            usage_block = self.usage_block
            gh_raw = list(self.gh_raw)
            gh_comments = dict(self.gh_comments)
            refs = dict(self.refs)
            status_as_of = self.status_as_of
            pipe_as_of = self.pipe_as_of
            lanes_as_of = self.lanes_as_of
            ledger_as_of = self.ledger_as_of
            refs_as_of = self.refs_as_of
            gh_as_of = self.gh_as_of
            mini_as_of = self.mini_as_of
            worker_as_of = self.worker_as_of
            devices_as_of = self.devices_as_of
            errors = {
                "status": self.status_error,
                "pipelines": self.pipe_error,
                "lanes": self.lanes_error,
                "ledger": self.ledger_error,
                "git": self.refs_error,
                "github": self.gh_error,
                "mini": self.mini_error,
                "worker": self.worker_error,
                "devices": self.devices_error,
            }
            ledger_entries = list(self.ledger_entries)
            needs = list(self.needs)
            mini_processes = list(self.mini_processes)
            mini_slots = list(self.mini_slots)
            mini_hw = dict(self.mini_hw) if self.mini_hw else None
            mini_load = dict(self.mini_load)
            worker = dict(self.worker)
            worker_down = self.worker_down_since
            worker_recovering = self.worker_recovering
            worker_next = self.worker_next_try
            devices = [dict(item) for item in self.devices]
            devices_recovering = self.devices_recovering
            devices_next = self.devices_next_try
            rate = dict(self.gh_rate) if self.gh_rate else None
            backoff = self.gh_backoff_note
        # Activity must use current execution evidence, not an old device sample
        # or a build-for-testing destination that never launched the device.
        local_commands = list(commands) if mini_as_of and now - mini_as_of <= STALE_S["mini"] else []
        current_commands = list(local_commands)
        if worker_as_of and now - worker_as_of <= STALE_S["worker"] and not worker_down:
            current_commands += [p.get("command") or "" for p in worker.get("processes") or []]
        runtime_commands = [cmd for cmd in current_commands if device_runtime_command(cmd)]
        for dev in devices:
            aliases = device_aliases(dev.get("name") or "", dev.get("udid") or "", dev.get("identifier") or "")
            dev["active"] = bool(devices_as_of and now - devices_as_of <= STALE_S["devices"] and referenced(aliases, [target for cmd in runtime_commands for target in device_runtime_targets(cmd)]))
            if not dev["active"]:
                dev["step"] = None
        markers = list(status_events) + list(decision_file_events)
        public_lanes = []
        for lane in lane_items:
            markers.extend(lane.pop("_markers", []))
            public_lanes.append(lane)
        open_items = []
        for item in fold_decisions(markers):
            open_items.append({
                "id": item.get("id"),
                "question": item.get("question"),
                "source": item.get("source"),
                "time": item.get("time"),
            })
        feed = []
        for line in parsed[-30:]:
            feed.append({
                "time": line.get("time"),
                "who": line.get("who"),
                "text": line.get("text") if not line.get("unparsed") else line.get("raw"),
                "raw": line.get("raw"),
            })
        try:
            runs, eta_basis, med = build_runs(pipe_files, local_commands, now)
        except Exception as exc:
            log_exc("pipelines", exc)
            runs, eta_basis, med = [], "unavailable", None
            errors["pipelines"] = errors.get("pipelines") or ("pipelines: %s: %s" % (exc.__class__.__name__, exc))
        prs = []
        try:
            for index, pr in enumerate(sorted(gh_raw, key=queue_key)):
                number = pr.get("number")
                branch = pr.get("headRefName") or ""
                local = refs.get("origin/" + branch)
                comments = gh_comments.get(number) if index < 12 else None
                note_override = None
                if index >= 12:
                    note_override = "outside the first 12 in queue order; comments not fetched"
                summarized = summarize_pr(pr, comments, self.hold_for(number, raws) if isinstance(number, int) else None, local)
                if note_override:
                    summarized["comments"] = None
                    summarized["comments_note"] = note_override
                prs.append(summarized)
        except Exception as exc:
            log_exc("github", exc)
            errors["github"] = errors.get("github") or ("github: %s: %s" % (exc.__class__.__name__, exc))
        unreachable = fmt_et(worker_down) if worker_down else None
        worker_active = bool(worker.get("active")) and not worker_down
        snap = {
            "server_time": iso(now),
            "server_time_et": fmt_et(now),
            "decision_link": DECISION_LINK,
            "decisions": fresh(status_as_of, STALE_S["local"], errors["status"], open=open_items),
            "feed": fresh(status_as_of, STALE_S["local"], errors["status"], lines=feed),
            "pipelines": fresh(
                pipe_as_of,
                STALE_S["local"],
                errors["pipelines"],
                eta_basis=eta_basis,
                median_s=None if med is None else round(med, 1),
                runs=runs,
            ),
            "lanes": fresh(lanes_as_of, STALE_S["local"], errors["lanes"], items=public_lanes),
            "ledger": fresh(ledger_as_of, STALE_S["local"], errors["ledger"], entries=ledger_entries, needs=needs),
            "github": fresh(
                gh_as_of,
                STALE_S["github"],
                errors["github"],
                backoff=backoff,
                rate=rate,
                prs=prs,
            ),
            "refs": fresh(refs_as_of, STALE_S["git"], errors["git"], count=len(refs)),
            "mini": fresh(
                mini_as_of,
                STALE_S["mini"],
                errors["mini"],
                active=bool(mini_processes or mini_slots),
                processes=mini_processes,
                slots=mini_slots,
                hardware=mini_hw,
                load=mini_load,
                spec=spec_line((mini_hw or {}).get("config"), mini_load),
                codex=(usage_block or {}).get("codex"),
                zai=mini_zai(commands),
            ),
            "remote": self.remote_collector.snapshot(now),
            "utilization": usage_block,
            "worker": fresh(
                worker_as_of,
                STALE_S["worker"],
                errors["worker"],
                active=worker_active,
                unreachable_since=unreachable,
                processes=worker.get("processes") or [],
                files=worker.get("files") or [],
                runs=worker.get("runs") or [],
                runlogs=worker.get("runlogs") or [],
                build_product=worker.get("build_product"),
                hardware=worker.get("hardware"),
                load=worker.get("load"),
                recovering=board.recovering_text(worker_recovering, worker_next, now) or None,
                spec=spec_line(
                    (worker.get("hardware") or {}).get("config") if isinstance(worker.get("hardware"), dict) else None,
                    worker.get("load") if isinstance(worker.get("load"), dict) else None,
                ),
            ),
            "devices": fresh(
                devices_as_of,
                STALE_S["devices"],
                errors["devices"],
                items=devices,
                recovering=board.recovering_text(devices_recovering, devices_next, now) or None,
            ),
        }
        try:
            self.apply_labels(snap, refs)
        except Exception as exc:
            log_exc("labels", exc)
            if snap.get("pipelines") is not None:
                snap["pipelines"]["error"] = snap["pipelines"].get("error") or (
                    "labels: %s: %s" % (exc.__class__.__name__, exc)
                )
        try:
            self.annotate_overview(snap)
        except Exception as exc:
            log_exc("overview", exc)
            snap["overview"] = {"tiles": [], "error": "%s: %s" % (exc.__class__.__name__, exc)}
            snap["active"] = {"jobs": []}
        try:
            snap["insights"] = self.store.public_block(self._insight_keys(snap))
        except Exception as exc:
            log_exc("insights", exc)
            snap["insights"] = {"jobs": [], "error": "%s: %s" % (exc.__class__.__name__, exc)}
        snap.setdefault("mini", {})["name"] = site.mini_name()
        snap.setdefault("worker", {})["name"] = site.worker_name()
        try:
            snap["programs"] = self.programs.get()
        except Exception as exc:
            log_exc("programs", exc)
            snap["programs"] = {"schema_version": 1, "partial": True, "checkpoints": [],
                                "error": "Program evidence collector unavailable"}
        try:
            snap["executive_owner"] = self.executive_owner.get(now)
            snap["executive_owner"] = autotune_status.overlay(snap["executive_owner"], self.autotune_status.get(now))
            scope_config = site.CURRENT.get("release_scope") or {}
            snap["executive_owner"]["scope_mapping"] = release_scope.read(scope_config.get("path"), scope_config.get("sha256"), now)
        except Exception as exc:
            log_exc("executive-owner", exc)
            snap["executive_owner"] = {"stale": True, "error": "The Coordinator executive feed is unavailable"}
        try:
            snap["executive_brief"] = self.executive_brief.get(snap.get("executive_owner") or {}, now)
        except Exception as exc:
            log_exc("executive-brief", exc)
            snap["executive_brief"] = {"state": "unavailable", "draft": True, "error": "Apple summary is unavailable"}
        try:
            snap["executive_dashboard"] = executive.build_dashboard(snap, now=now)
        except Exception as exc:
            log_exc("executive", exc)
            snap["executive_dashboard"] = {
                "stale": True, "partial": True,
                "headline": "Executive summary unavailable",
                "summary": "Detailed source observations remain available below.",
                "error": "%s: %s" % (exc.__class__.__name__, exc),
            }
        return snap
    def publish(self) -> None:
        try:
            snapshot = self.build()
            encoded = json.dumps(snapshot, separators=(",", ":"), default=str)
        except Exception as exc:
            sys.stderr.write("publish: %s: %s\n%s\n" % (
                exc.__class__.__name__, exc, traceback.format_exc().rstrip(),
            ))
            return
        # Clocks move every poll. Skip the redact-and-send work until the
        # underlying sample changes, and still refresh as-of at least every 15s.
        key = _CLOCK_FIELDS.sub("", encoded)
        now = time.time()
        if key == self._content_key and now - self._content_at < 15:
            return
        self._content_key = key
        self._content_at = now
        try:
            raw = json.dumps(redact(snapshot), separators=(",", ":"), default=str).encode("utf-8")
        except Exception as exc:
            sys.stderr.write("publish: %s: %s\n%s\n" % (
                exc.__class__.__name__, exc, traceback.format_exc().rstrip(),
            ))
            return
        if b"\n" in raw:
            raw = raw.replace(b"\n", b" ")
        with self.cv:
            if raw == self.payload:
                return
            self.payload = raw
            self.version += 1
            self.cv.notify_all()
    def wait_snapshot(self, last: int, timeout: float) -> tuple[bytes, int]:
        with self.cv:
            if self.version == last:
                self.cv.wait(timeout)
            return self.payload, self.version

    def test_pool_body(self) -> bytes:
        """Cached pool snapshot. This does not query devices."""
        devices = []
        try:
            snap = json.loads(self.payload or b"{}")
        except json.JSONDecodeError:
            snap = {}
        block = snap.get("test_pool") if isinstance(snap, dict) else None
        if isinstance(block, dict) and isinstance(block.get("devices"), list):
            devices = block["devices"]
        body = {
            "generated_at": device_pool.iso_et(time.time()),
            "devices": devices,
        }
        return json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
    def annotate_overview(self, snap: dict) -> None:
        gh = snap.get("github") or {}
        try:
            live = live_work.collect([] if gh.get("stale") else gh.get("prs") or [])
        except Exception as exc:
            log_exc("live_work", exc)
            live = []
        live_actual = [row for row in live if row.get("pid") and not row.get("stale") and row.get("kind") not in ("PR activity", "Status unavailable")]
        devices = present_devices((snap.get("devices") or {}).get("items") or [])
        if snap.get("devices") is not None:
            snap["devices"]["items"] = devices
        used = set()

        def take(base: str) -> str:
            anchor = base
            number = 2
            while anchor in used:
                anchor = "%s-%d" % (base, number)
                number += 1
            used.add(anchor)
            return anchor

        for run in (snap.get("pipelines") or {}).get("runs") or []:
            run["anchor"] = take("pipe-" + slug(run.get("name") or "pipeline"))
        for lane in (snap.get("lanes") or {}).get("items") or []:
            lane["anchor"] = take("lane-" + slug(str(lane.get("lane") or "lane")))
        for run in (snap.get("worker") or {}).get("runs") or []:
            run["anchor"] = take("zai-" + slug(run.get("detail") or run.get("name") or "zai"))

        cfg = self.config or board.DEFAULT_CONFIG
        mini_cfg = cfg.get("mini") or {}
        worker_cfg = cfg.get("worker") or {}
        mini_host = mini_cfg.get("host") or ""
        mini_ip = mini_cfg.get("ip") or ""
        worker_host = self.resolved_ip or worker_cfg.get("ip") or worker_cfg.get("host") or ""

        mini = snap.get("mini") or {}
        mini["active"] = bool(mini.get("active") or live_actual)
        running = [
            run for run in ((snap.get("pipelines") or {}).get("runs") or [])
            if run.get("state") == "running"
        ]
        busy = [lane for lane in ((snap.get("lanes") or {}).get("items") or []) if lane.get("busy")]
        failing = False
        if running:
            run = max(running, key=lambda item: item.get("start_epoch") or 0)
            mini["job"] = run.get("purpose") or board.pipeline_purpose(run.get("pr"), None, run.get("subject"))
            mini["goal"] = run.get("goal_line") or board.pipeline_goal(None, run.get("subject"), None, None)
            mini["step"] = run.get("step_line") or board.pipeline_step(run)
        elif busy:
            lane = busy[0]
            mini["job"] = lane.get("purpose") or insights.lane_title(lane.get("lane") or "", lane.get("pr"), lane.get("subject") or "")
            mini["goal"] = lane.get("goal_line") or board.usable_goal(None, lane.get("subject") or lane.get("lane") or "the lane")
            mini["step"] = lane.get("step_line") or board.lane_activity(
                lane.get("activity_assistant"), lane.get("activity_tool"), lane.get("activity_newest"),
                True, lane.get("activity_epoch"), time.time(), lane.get("actions"), lane.get("action_bucket"),
            )
        elif live_actual:
            r = live_actual[0]
            mini["job"] = r["what"] + ((" " + r["pr"]) if r.get("pr") and r["pr"] not in r["what"] else "")
            mini["goal"] = ("Head %s" % r["head"]) if r.get("head") else r.get("kind") or ""
            mini["step"] = r["step"] + ((" · " + r["when"]) if r.get("when") else "")
        elif mini.get("active"):
            procs = mini.get("processes") or []
            mini["job"] = short_command(procs[0].get("command") if procs else "") or "Mini job"
            mini["goal"] = ""
            mini["step"] = "Running"
        else:
            mini["job"] = "Idle"
            mini["goal"] = ""
            mini["step"] = ""
        mini["job"] = board.usable_title(mini.get("job"), "Mini job" if mini.get("active") else "Idle")
        mini["failing"] = failing
        with self.state_lock:
            rfb_names = list(self.rfb_names)
        mini["connect"] = self.vnc_connect(rfb_names, mini_host, mini_ip, [mini_ip, mini_host, "localhost", "127.0.0.1", "::1"])

        worker = snap.get("worker") or {}
        offline = bool(worker.get("unreachable_since") or worker.get("error") or not worker.get("as_of"))
        if offline:
            worker["job"] = "Offline"
            worker["goal"] = ""
            worker["step"] = ""
            worker["failing"] = False
            worker["connect"] = None
        else:
            remote_runs = worker.get("runs") or []
            commands = [proc.get("command") or "" for proc in (worker.get("processes") or [])]

            def run_live(run: dict) -> bool:
                stem = Path(run.get("detail") or run.get("name") or "").stem
                return bool(stem) and any(stem in command for command in commands)

            live = [run for run in remote_runs if "running" in (run.get("outcome") or "") and run_live(run)]
            failed = [run for run in remote_runs if "failed" in (run.get("outcome") or "") and run_live(run)]
            if worker.get("purpose"):
                worker["job"] = worker["purpose"]
                worker["goal"] = worker.get("goal_line") or ""
                worker["step"] = worker.get("step_line") or "Running"
                worker["failing"] = bool(failed) and not live
            elif live or worker.get("active"):
                procs = worker.get("processes") or []
                command = procs[0].get("command") if procs else ""
                facts = insights.zai_title(command)
                if facts.get("title"):
                    worker["job"] = facts["title"]
                    worker["goal"] = board.zai_goal(facts.get("units") or "", facts.get("run_id") or "", facts.get("kind") or "run")
                    worker["step"] = board.zai_step(facts.get("units") or "")
                else:
                    title, goal, step_line = board.remote_tool_lines(
                        [proc.get("command") or "" for proc in procs]
                    )
                    worker["job"] = title
                    worker["goal"] = goal
                    worker["step"] = step_line
                worker["failing"] = bool(failed) and not live
            else:
                worker["job"] = "Idle"
                worker["goal"] = ""
                worker["step"] = ""
                worker["failing"] = False
            if worker.get("job") != "Idle":
                worker["job"] = board.usable_title(worker.get("job"), "Worker job")
                if worker.get("step"):
                    worker["step"] = board.scrub_step(worker["step"])
            worker_name = worker_cfg.get("host") or ""
            worker_ip = worker_cfg.get("ip") or ""
            worker["connect"] = self.vnc_connect(rfb_names, worker_name, worker_ip, None)

        if getattr(self, "pool", None) is not None:
            try:
                self.pool.attach_placeholders(devices, self.config, time.time())
            except Exception as exc:
                log_exc("pool", exc)
        snap["test_pool"] = {"devices": device_pool.public_devices(devices)}

        for dev in devices:
            reachable = board.device_reachable(dev.get("state"), bool(dev.get("placeholder")))
            if dev.get("active"):
                title, goal, step_line = board.device_lines(
                    dev.get("tile_name") or dev.get("name") or "Device",
                    dev.get("step") or "",
                )
                dev["job"] = board.usable_title(dev.get("fm_label"), title)
                dev["goal"] = board.usable_goal(dev.get("fm_goal"), goal)
                dev["step_line"] = step_line
            if reachable:
                dev["connect"] = {
                    "kind": "device",
                    "udid": dev.get("udid") or "",
                    "identifier": dev.get("identifier") or "",
                    "name": dev.get("tile_name") or dev.get("name") or "device",
                }
            else:
                dev["connect"] = None

        snap["overview"] = {"tiles": build_tiles(mini, worker, devices)}
        with self.state_lock:
            hw_probe = dict(self.hw_probe)
            hw_topo = dict(self.hw_topo)
            hw_hist = {k: list(v) for k, v in self.hw_history.items()}
        for tile in snap["overview"]["tiles"]:
            try:
                if tile.get("id") == "worker" and tile.get("status") != "offline":
                    watch = hwinfo.worker_watch(worker.get("processes") or [], worker.get("files") or [], time.time())
                    tile["watch"] = watch
                    if watch["job"]:
                        tile["job"] = watch["job"]
                        if tile.get("status") == "idle" and watch["loop_alive"]:
                            tile["pool_status"] = "Watching"
                    tile["goal"] = watch["last"] or tile.get("goal") or ""
                    tile["step"] = tile.get("step") or worker.get("step") or ""
                    # Only show the #2153 loop file time while that loop/gate is alive; otherwise
                    # show when Worker was last probed (the old loop files froze on Oct 30).
                    if watch["status_as_of"] and (watch["loop_alive"] or watch["gate_running"] or watch["building"]):
                        tile["status_as_of"] = fmt_et(watch["status_as_of"])
                    elif worker.get("as_of_et"):
                        tile["status_as_of"] = worker.get("as_of_et")
                    tile["status_stale"] = watch["stale"]
                    if not tile.get("job") or tile.get("job") == "Idle":
                        gates = [f for f in (worker.get("files") or []) if f.get("label") == "gate"]
                        if gates:
                            g = gates[0]
                            last = (g.get("tail") or [""])[-1].strip()
                            m = re.search(r"#?(\d{3,5})\D{0,3}([0-9a-f]{7,8})?", last) or re.search(r"(\d{3,5})", g.get("path") or "")
                            tile["job"] = "Idle"
                            tile["goal"] = "Last job: %s%s" % (Path(g.get("path") or "").parent.name, (" · " + last[:140]) if last else "")
                            try:
                                tile["step"] = "Finished %s" % fmt_et(float(g.get("mtime")))
                            except (TypeError, ValueError):
                                tile["step"] = "Finished (time unknown)"
                        else:
                            tile["job"] = "Idle"
                            tile["goal"] = "Last job: not reported (no gate-logs-*/summary.txt on Worker BuildDisk)"
                            tile["step"] = "Idle"
                if tile.get("id") == "mini":
                    full = tile.get("job") or ""
                    if len(full) > 60 or "/" in full:
                        tile["job_full"] = redact_text(full)
                        tile["job"] = board_short_job(full)
                if tile.get("id") in ("mini", "worker"):
                    tile["hw"] = hwinfo.mac_card(None if tile.get("status") == "offline" else hw_probe.get(tile["id"]),
                                                 hw_topo.get(tile["id"]), tile.get("job") or "", hw_hist.get(tile["id"]))
                else:
                    tile["hw"] = hwinfo.device_card(tile.get("product_type") or "", tile.get("storage_bytes"),
                                                    tile.get("device_state") or "", tile.get("os_label") or "", tile.get("job") or "")
            except Exception as exc:
                log_exc("hwcard", exc)
        jobs = []
        for run in running:
            title = run.get("purpose") or board.pipeline_purpose(run.get("pr"), None, run.get("subject"))
            jobs.append({
                "id": job_key("pipe-" + str(run.get("anchor") or run.get("pr") or title)),
                "name": board.usable_title(title, title),
                "goal": run.get("goal_line") or board.pipeline_goal(None, run.get("subject"), None, None),
                "step": run.get("step_line") or board.pipeline_step(run),
                "eta": run.get("eta") or "",
                "where": "Pipeline",
                "anchor": run.get("anchor"),
                "technical": technical_rows([
                    ("Pull request", ("#%s" % run.get("pr")) if run.get("pr") else ""),
                    ("Commit", run.get("head") or ""),
                    ("Pipeline", run.get("name") or ""),
                    ("Log", "; ".join(step_log_paths(run))),
                ]),
            })
        for lane in busy:
            title = lane.get("purpose") or insights.lane_title(lane.get("lane") or "", lane.get("pr"), lane.get("subject") or "")
            jobs.append({
                "id": job_key("lane-" + str(lane.get("lane") or lane.get("anchor") or title)),
                "name": board.usable_title(title, title),
                "goal": lane.get("goal_line") or "",
                "step": lane.get("step_line") or board.lane_activity(
                    lane.get("activity_assistant"), lane.get("activity_tool"), lane.get("activity_newest"),
                    True, lane.get("activity_epoch"), time.time(), lane.get("actions"), lane.get("action_bucket"),
                ),
                "eta": "",
                "where": lane.get("lane") or "Lane",
                "anchor": lane.get("anchor"),
                "technical": technical_rows([
                    ("Lane", lane.get("lane") or ""),
                    ("Session", lane.get("session") or ""),
                    ("Process", lane.get("pid") or ""),
                    ("Branch", lane.get("branch") or ""),
                    ("Pull request", ("#%s" % lane.get("pr")) if lane.get("pr") else ""),
                ]),
            })
        if worker.get("active") and not offline and worker.get("job") and worker.get("job") != "Idle":
            worker_pids = [
                str(proc.get("pid"))
                for proc in (worker.get("processes") or [])
                if isinstance(proc, dict) and proc.get("pid")
            ]
            worker_derived = ""
            for proc in (worker.get("processes") or []):
                command = (proc.get("command") or "") if isinstance(proc, dict) else ""
                if "DerivedData" in command:
                    worker_derived = command
                    break
            jobs.append({
                "id": "worker",
                "name": worker.get("job"),
                "goal": worker.get("goal") or "",
                "step": worker.get("step") or "Running",
                "eta": "",
                "where": "Worker",
                "anchor": "sys-worker",
                "technical": technical_rows([
                    ("Computer", site.worker_computer()),
                    ("Detail", worker.get("spec") or ""),
                    ("Process", ", ".join(worker_pids)),
                    ("DerivedData", worker_derived),
                ]),
            })
        for dev in devices:
            if not dev.get("active"):
                continue
            jobs.append({
                "id": job_key("dev-" + str(dev.get("anchor") or dev.get("name") or "device")),
                "name": dev.get("job") or dev.get("tile_name") or dev.get("name") or "Device",
                "goal": dev.get("goal") or dev.get("goal_line") or "",
                "step": dev.get("step_line") or "Running",
                "eta": "",
                "where": dev.get("tile_name") or dev.get("name") or "Device",
                "anchor": dev.get("anchor"),
                "technical": technical_rows([
                    ("Device", dev.get("name") or ""),
                    ("Identifier", dev.get("udid") or ""),
                ]),
            })
        represented_pids = {str(lane.get("pid")) for lane in busy if lane.get("pid")}
        for row in live_actual:
            pid = str(row["pid"])
            if pid in represented_pids:
                continue
            if row.get("pr") and any(str(run.get("pr") or "") == str(row["pr"]).lstrip("#") and run.get("head8") == row.get("head") for run in running):
                continue
            represented_pids.add(pid)
            jobs.append({
                "id": "live-" + pid,
                "name": row.get("what") or row.get("kind") or "Live work",
                "goal": "Goal: " + {"Autotune": "evaluate the current autotune configuration", "Native build": "compile the selected app and test targets", "CodeRabbit": "review the current code changes", "Mini gate": "complete the current validation gate", "Refresh lane": "advance the active change"}.get(row.get("kind"), row.get("what") or "complete the current work"),
                "step": row.get("step") or "Running; progress not reported",
                "eta": "",
                "where": "Primary-Mac",
                "anchor": "sys-mini",
                "technical": technical_rows([("Process", pid), ("Started", row.get("when") or ""), ("Commit", row.get("head") or "")]),
            })
        snap["active"] = {"jobs": jobs}
        titles = {}
        for pr in (snap.get("github") or {}).get("prs") or []:
            if isinstance(pr, dict) and pr.get("number") and pr.get("title"):
                titles[pr["number"]] = pr["title"]
        lists = board.overview_lists(
            jobs,
            (snap.get("pipelines") or {}).get("runs") or [],
            (snap.get("github") or {}).get("prs") or [],
            (snap.get("decisions") or {}).get("open") or [],
            titles,
        )
        overview = snap.get("overview")
        if not isinstance(overview, dict):
            overview = {}
            snap["overview"] = overview
        overview.update(lists)
        overview["live"] = live
        overview["working"] = [{"tone": "wait" if r.get("stale") or r.get("kind") == "Status unavailable" else "go", "text": live_work.line(r)} for r in live] + list(overview.get("working") or [])
        try:
            acc = accomplishments.CACHE.get()
        except Exception as exc:
            log_exc("accomplishments", exc)
            acc = None
        if isinstance(acc, dict) and acc.get("today"):
            overview["accomplishments"] = acc
            evidence = []
            for group in acc["today"].get("groups") or []:
                for item in group.get("items") or []:
                    evidence.append({"tone": "go", "kind": group.get("title"),
                                     "text": "%s · %s · %s" % (group.get("title"), item.get("time_et"), item.get("text"))})
            overview["done"] = evidence + list(overview.get("done") or [])
    def queue_job(self, name: str) -> None:
        with self.state_lock:
            self.force_jobs.add(name)
            if name == "worker":
                self.worker_fails = 0
                self.worker_recovering = False
                self.worker_next_try = 0.0
            elif name == "devices":
                self.devices_fails = 0
                self.devices_recovering = False
                self.devices_next_try = 0.0
    def action_reconnect(self) -> str:
        self.drop_worker_socket()
        self.queue_job("worker")
        return "Closed the Worker control socket and queued a probe."
    def action_refresh_devices(self) -> str:
        self.queue_job("devices")
        return "Queued a devicectl refresh."
    def action_save_settings(self, form: dict) -> str:
        posted = dict(form)
        posted.setdefault("resolve_mdns", "")
        cfg = board.apply_settings(self.config, posted)
        board.save_config(CONFIG_PATH, cfg)
        with self.state_lock:
            self.config = cfg
            self.resolved_at = 0.0
        self.queue_job("worker")
        return "Saved settings."
    def vnc_connect(self, names: list, host: str, fallback_ip: str, hide_on: list | None) -> dict | None:
        instance = board.match_rfb(names, host)
        href = board.vnc_href(instance, fallback_ip)
        if not href:
            return None
        info = {"kind": "vnc", "href": href}
        if instance:
            info["vncloc"] = "/vncloc/" + quote(instance, safe="")
        if hide_on:
            info["hide_on"] = hide_on
        return info
    def action_open_console(self, token: str) -> tuple[str, int]:
        # Device Hub restores the last selection. It cannot be told which device to show.
        with self.state_lock:
            items = [dict(item) for item in self.devices]
        return board.open_device_console(items, token, lambda cmd: run_cmd(cmd, 8))
    def settings_body(self, notice: str = "", csrf: str = "") -> bytes:
        with self.state_lock:
            cfg = self.config
            resolved = self.resolved_ip
            worker_note = board.recovering_text(self.worker_recovering, self.worker_next_try, time.time())
            devices_note = board.recovering_text(self.devices_recovering, self.devices_next_try, time.time())
        return board.settings_page(cfg, csrf or self.csrf, notice, resolved, worker_note, devices_note)
    def fast_loop(self) -> None:
        while not self.stop:
            started = time.monotonic()
            try:
                if site.adapter_enabled("local_system"):
                    self.sample_host_cpu()
            except Exception as exc:
                sys.stderr.write("cpu: %s\n" % exc)
            self.poll_local()
            self.publish()
            delay = site.poll_local() - (time.monotonic() - started)
            if delay > 0:
                time.sleep(delay)
    def hardware_read_failed(self, host: str, attempted: float) -> None:
        """Keep last-known hardware with its own failed inspection clock."""
        with self.state_lock:
            value = dict(self.hw_probe.get(host) or {})
            value.update(stale=True, error="Hardware inspection unavailable", attempted_at=attempted)
            self.hw_probe[host] = value

    def job_hwdetail(self) -> float:
        if not site.adapter_enabled("hardware_detail"):
            return 60.0
        """Per-cluster CPU, GPU, memory, swap, thermal and topology for the Mini and Worker (read-only, no sudo)."""
        from zai_devdashd.probes import _run_cmd, worker_socket_path
        try:
            script = hwinfo.PROBE.read_bytes()
        except OSError:
            for host in ("mini", "worker"):
                self.hardware_read_failed(host, time.time())
            return 60.0
        now = time.time()
        for host in ("mini", "worker"):
            topo = (now - self.hw_topo_at.get(host, 0)) > 6 * 3600 or host not in self.hw_topo
            arg = ["topo"] if topo else []
            if host == "mini":
                args = ["/usr/bin/nice", "-n", "10", "/usr/bin/python3", "-", *arg]
            else:
                try:
                    target = self.worker_target(False)
                except Exception:
                    self.hardware_read_failed(host, time.time())
                    continue
                args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "ControlMaster=auto",
                        "-o", "ControlPersist=60", "-o", "ControlPath=" + str(worker_socket_path()), target,
                        "/usr/bin/nice -n 10 /usr/bin/python3 - " + " ".join(arg)]
            code, out, _err = _run_cmd(args, 45 if topo else 15, script)
            data = hwinfo.parse_probe(out.decode("utf-8", "replace")) if code == 0 and out else None
            attempted = time.time()
            if not data or data.get("ok") is False:
                self.hardware_read_failed(host, attempted)
                continue
            with self.state_lock:
                if data:
                    data.update(as_of=attempted, attempted_at=attempted, stale=False, error=None)
                    if data.get("topology"):
                        self.hw_topo[host] = data["topology"]
                        self.hw_topo_at[host] = now
                    self.hw_probe[host] = data
                    if data.get("core_busy"):
                        hist = self.hw_history.setdefault(host, deque(maxlen=40))
                        hist.append(list(data["core_busy"]))
        return 15.0

    def job_usage(self) -> float:
        if not site.adapter_enabled("usage"):
            return 60.0
        # Initial transcript-cost scans may be slow; they must not hold up
        # machine, device, or GitHub observation in the shared polling worker.
        with self.state_lock:
            if self.usage_refreshing:
                return 60.0
            self.usage_refreshing = True
            commands = list(self.mini_commands)

        def collect_usage():
            try:
                block = self.usage_collector.collect(running_cwds=codex_cwds(commands))
                with self.state_lock:
                    self.usage_block = block
            except Exception as exc:
                log_exc("usage", exc)
            finally:
                with self.state_lock:
                    self.usage_refreshing = False

        worker = threading.Thread(target=collect_usage, name="mon-usage", daemon=True)
        self.usage_thread = worker
        try:
            worker.start()
        except Exception:
            with self.state_lock:
                self.usage_refreshing = False
            raise
        return 60.0

    def job_remote(self) -> float:
        if not site.adapter_enabled("remote"):
            return 60.0
        with self.state_lock:
            cfg = dict(self.config.get("remote") or {})
        self.remote_collector.poll(cfg)
        return float(cfg.get("probe_interval_s") or 60)

    def slow_loop(self) -> None:
        nxt = {"hw": 0.0, "load": 0.0, "mini": 0.0, "worker": 0.0, "git": 0.0, "devices": 0.0, "github": 0.0, "usage": 0.0, "hwdetail": 0.0, "remote": 0.0}
        order = ["hw", "load", "mini", "remote", "devices", "git", "worker", "github", "usage", "hwdetail"]
        intervals = site.poll_intervals()
        while not self.stop:
            now = time.time()
            with self.state_lock:
                forced = set(self.force_jobs)
                self.force_jobs.clear()
            for name in forced:
                if name in nxt:
                    nxt[name] = 0.0
            now = time.time()
            job = oldest_due_job(order, nxt, now)
            if job is None:
                time.sleep(min(1.0, max(0.1, min(nxt.values()) - now)))
                continue
            delay = intervals.get(job, 60)
            adapter = {"hw": "hardware_detail", "load": "local_system", "mini": "local_system",
                       "worker": "remote", "remote": "remote", "git": "git", "devices": "devices",
                       "github": "github", "usage": "usage", "hwdetail": "hardware_detail"}[job]
            if not site.adapter_enabled(adapter):
                nxt[job] = time.time() + max(1.0, delay)
                continue
            try:
                if job == "usage":
                    delay = self.job_usage()
                elif job == "hwdetail":
                    delay = self.job_hwdetail()
                elif job == "github":
                    delay = self.job_github()
                elif job == "hw":
                    delay = self.job_hw()
                elif job == "load":
                    delay = self.job_load()
                elif job == "mini":
                    delay = self.job_mini()
                elif job == "remote":
                    delay = self.job_remote()
                elif job == "worker":
                    delay = self.job_worker()
                elif job == "devices":
                    delay = self.job_devices()
                elif job == "git":
                    delay = self.job_git()
            except Exception as exc:
                source = "mini" if job in ("hw", "load") else job
                self.set_source_error(source, f"{job}: {exc.__class__.__name__}: {exc}")
                delay = intervals.get(job, 60)
            nxt[job] = time.time() + max(0.0, delay)
            self.publish()

CODEX_CWD_RE = re.compile(r"codex\s+exec\b.*?\s-C\s+(\S+)")


def board_short_job(command: str) -> str:
    """Short job name for a long command line: the script or tool name, not the path."""
    parts = (command or "").split()
    for part in parts:
        base = part.rsplit("/", 1)[-1]
        if base.endswith((".sh", ".py")) or base in ("xcodebuild", "codex", "grok", "zai-coder"):
            return clip(base, 60)
    return clip(Path(parts[0]).name if parts else command, 60)


def codex_cwds(commands: list) -> list:
    found = []
    for command in commands or []:
        m = CODEX_CWD_RE.search(command or "")
        if m:
            found.append(m.group(1))
    return found


def mini_zai(commands: list) -> dict | None:
    """ZAI run on the Mini, same match the Worker card uses; Codex lane prompts are skipped."""
    for command in commands or []:
        if re.search(r"zai", command or "", re.I) and not CODEX_CWD_RE.search(command or ""):
            facts = insights.zai_title(command)
            return {
                "title": facts.get("title") or "ZAI run",
                "run_id": facts.get("run_id") or "",
                "units": facts.get("units") or "",
                "command": redact_text(clip(command, 300)),
            }
    return None


def technical_rows(pairs: list[tuple[str, object]]) -> list[dict]:
    rows = []
    for label, value in pairs:
        text = " ".join(str(value or "").split())
        if text and text not in ("None", "—"):
            rows.append({"label": label, "value": text[:200]})
    return rows
def job_key(raw: str) -> str:
    text = _JOB_KEY.sub("-", str(raw or "").strip()).strip("-")[:96]
    return text or "item"
def word_prefix(text: str) -> tuple:
    words = re.findall(r"[A-Za-z0-9']+", text.lower())
    return tuple(words[:6])
def parse_log_when(text: str) -> datetime | None:
    match = WHEN_RE.match(text.strip())
    if not match:
        return None
    month, day, clock, year = match.groups()
    try:
        parsed = datetime.strptime(f"{month} {int(day)} {clock} {year}", "%b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return parsed.replace(tzinfo=ET)
def pr_from_text(*parts: str) -> int | None:
    for part in parts:
        if not part:
            continue
        match = re.search(r"eng-(\d{3,5})-", part)
        if match:
            return int(match.group(1))
        match = re.search(r"\(#(\d{3,5})\)", part)
        if match:
            return int(match.group(1))
        match = re.search(r"(?:^|[\s/(])#(\d{3,5})\b", part)
        if match:
            return int(match.group(1))
    return None
def tool_action_label(line: str) -> str:
    title = ""
    command = ""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        title = str(obj.get("title") or obj.get("toolName") or "tool")
        raw = obj.get("rawInput")
        if isinstance(raw, dict):
            command = str(raw.get("command") or raw.get("description") or "")
    if not title:
        title_match = re.search(r'"title"\s*:\s*"((?:\\.|[^"\\])*)"', line)
        name_match = re.search(r'"toolName"\s*:\s*"((?:\\.|[^"\\])*)"', line)
        title = title_match.group(1) if title_match else (name_match.group(1) if name_match else "tool")
        if "\\" in title:
            title = title.encode("utf-8").decode("unicode_escape", "replace")
    text = title if not command else "%s: %s" % (title, clip(command, 100))
    return redact_text(clip(text, 160))
def parse_worktrees(text: str) -> dict:
    mapping = {}
    path = ""
    for line in text.splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1].strip()
        elif line.startswith("branch ") and path:
            ref = line.split(" ", 1)[1].strip()
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/"):]
            if ref:
                mapping[ref] = path
    return mapping
def parse_git_log(code: int, out: bytes) -> tuple:
    if code != 0:
        return None, ""
    lines = out.decode("utf-8", "replace").strip().splitlines()
    if not lines:
        return None, ""
    parts = lines[0].split("\t", 1)
    try:
        epoch = int(parts[0])
    except ValueError:
        return None, ""
    subject = parts[1].strip() if len(parts) > 1 else ""
    return epoch, subject
def compose_label(pr: int | None, subject: str | None, outcome: str) -> str:
    parts = []
    if pr:
        parts.append("#%d" % pr)
    if subject:
        parts.append(subject.rstrip(".").strip())
    if outcome:
        parts.append(outcome)
    return " · ".join(parts)
def pipeline_outcome(run: dict) -> str:
    steps = run.get("steps") or []
    state = run.get("state") or ""
    failed = [STEP_WORDS.get(step["name"], step["name"]) for step in steps if step.get("state") == "fail"]
    if state == "running":
        current = next((step for step in steps if step.get("state") == "run"), None)
        if current:
            return STEP_WORDS.get(current["name"], current["name"]) + " running"
        return "Pipeline running"
    if state == "failed" and failed:
        return " + ".join(failed) + " failed"
    if state == "failed":
        return "Pipeline failed"
    if state == "passed":
        return "Pipeline passed"
    if state == "stopped":
        return "Pipeline stopped"
    return "Pipeline " + (state or "unknown")
def failing_step_word(run: dict) -> str:
    for step in run.get("steps") or []:
        if step.get("state") == "fail":
            return STEP_WORDS.get(step["name"], step["name"])
    for step in run.get("steps") or []:
        if step.get("state") == "run":
            return STEP_WORDS.get(step["name"], step["name"])
    return ""
def step_log_paths(run: dict) -> list[str]:
    stem = run.get("name") or ""
    prefix = stem[: -len("-pipeline")] if stem.endswith("-pipeline") else stem
    names = [step["name"] for step in (run.get("steps") or []) if step.get("state") == "fail"]
    if not names:
        names = [step["name"] for step in (run.get("steps") or []) if step.get("state") == "run"]
    paths = []
    pipeline_log = PIPELINE_DIR / (stem + ".log")
    if pipeline_log.is_file():
        paths.append(str(pipeline_log))
    for name in names:
        suffix = STEP_LOG_SUFFIX.get(name)
        if not suffix:
            continue
        path = PIPELINE_DIR / (prefix + suffix)
        if path.is_file():
            paths.append(str(path))
    return paths
def first_error_in_file(path: Path) -> str | None:
    strong = None
    strong_follow = None
    weak = None
    try:
        with path.open("r", errors="replace") as handle:
            read = 0
            previous_banner = False
            for line in handle:
                read += len(line)
                stripped = line.strip()
                if not stripped:
                    if read > 2_000_000:
                        break
                    continue
                if strong is None and STRONG_ERROR_RE.search(stripped):
                    strong = stripped
                    previous_banner = stripped.lower().startswith("failing tests")
                    if not previous_banner:
                        break
                elif previous_banner and strong_follow is None:
                    strong_follow = stripped
                    break
                elif weak is None and WEAK_ERROR_RE.search(stripped) and "passed" not in stripped.lower():
                    weak = stripped
                if read > 2_000_000:
                    break
    except OSError:
        return None
    chosen = strong_follow or strong or weak
    if not chosen:
        return None
    return redact_text(clip(chosen, 240))
def first_error_in_paths(paths: list[str]) -> str:
    for raw in paths:
        found = first_error_in_file(Path(raw))
        if found:
            return found
    return ""
def cr_outcome(kind: str, note: str) -> str:
    lowered = (note or "").lower()
    if "changes_requested" in lowered or "changes requested" in lowered:
        return "CodeRabbit review changes requested"
    if lowered.startswith("requested"):
        return "CodeRabbit review requested"
    if lowered.startswith("skipped"):
        return "CodeRabbit review skipped"
    if kind and kind != "-":
        return "CodeRabbit review " + kind
    return "CodeRabbit review"
def zai_outcome(script: str, text: str, commands: list[str]) -> str:
    stem = Path(script).stem
    if any(stem and stem in command for command in commands):
        return "ZAI run running"
    if re.search(r"exit\s*=\s*0\b|\bexit 0\b", text):
        return "ZAI run passed"
    if re.search(r"exit\s*=\s*[1-9]|\*\* TEST|XCTAssert|(?:^|\s)error:", text, re.I):
        return "ZAI run failed"
    return "ZAI run finished"
def clamp_words(text: str, limit: int) -> str:
    return " ".join((text or "").split()[:limit])
def one_sentence(text: str) -> str:
    flat = " ".join((text or "").split())
    match = re.match(r"^.*?[.!?](?:\s|$)", flat)
    if match:
        return match.group(0).strip()
    return clip(flat, 240)
def question_text(rest: str) -> str:
    return re.sub(r"^\[D-[A-Za-z0-9_-]+\]\s*:?\s*", "", rest.strip()).strip()
def classify_marker(line: str) -> dict | None:
    """Return an open or close event, or None. A line can theoretically be both."""
    opened = None
    open_match = OPEN_RE.search(line)
    if open_match:
        tag_match = TAG_RE.search(line)
        question = question_text(open_match.group(2)) or line.strip()
        opened = {
            "kind": "open",
            "marker": open_match.group(1),
            "tag": tag_match.group(1) if tag_match else None,
            "question": question,
            "prefix": word_prefix(question),
            "raw": line.strip(),
        }
    closed = None
    res_match = RES_RE.search(line)
    if res_match:
        closed = {"kind": "resolved", "prefix": word_prefix(res_match.group(1)), "raw": line.strip()}
    elif DECIDED_RE.search(line):
        tag_match = TAG_RE.search(line)
        closed = {
            "kind": "decided",
            "tag": tag_match.group(1) if tag_match else None,
            "raw": line.strip(),
        }
    if opened and closed:
        return {"kind": "both", "open": opened, "close": closed}
    return opened or closed
def fold_decisions(events: list[dict]) -> list[dict]:
    events = sorted(events, key=lambda item: (item.get("when") or 0, item.get("seq") or 0))
    open_items: list[dict] = []
    for event in events:
        payload = event
        close = None
        if event.get("kind") == "both":
            payload = event["open"]
            close = event["close"]
        if payload.get("kind") == "open":
            item = {
                "id": payload.get("tag"),
                "question": payload.get("question") or payload.get("raw"),
                "prefix": payload.get("prefix") or (),
                "source": event.get("source"),
                "time": event.get("time"),
                "when": event.get("when") or 0,
                "raw": payload.get("raw"),
                "closed": False,
            }
            open_items.append(item)
        target_close = close or (payload if payload.get("kind") in ("resolved", "decided") else None)
        if not target_close:
            continue
        if target_close.get("kind") == "decided" and target_close.get("tag"):
            for item in reversed(open_items):
                if not item["closed"] and item.get("id") == target_close["tag"]:
                    item["closed"] = True
                    break
        elif target_close.get("kind") == "resolved" and target_close.get("prefix"):
            for item in reversed(open_items):
                if not item["closed"] and item.get("prefix") == target_close["prefix"]:
                    item["closed"] = True
                    break
    return [item for item in open_items if not item["closed"]]
def parse_status(text: str) -> tuple[list[dict], list[str]]:
    current_date = None
    parsed = []
    raws = []
    for index, line in enumerate(text.splitlines()):
        raw = line.rstrip()
        if not raw.strip():
            continue
        raws.append(raw)
        match = STATUS_RE.match(raw.strip())
        if not match:
            parsed.append({"raw": raw, "who": None, "text": raw, "time": None, "i": index, "unparsed": True})
            continue
        if match.group(1):
            current_date = match.group(1)
        clock = match.group(2)
        when = None
        label = None
        if current_date:
            try:
                when_dt = datetime.strptime(f"{current_date} {clock}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
                when = when_dt.timestamp()
                label = when_dt.strftime("%Y-%m-%d %H:%M ET")
            except ValueError:
                when = None
        parsed.append({
            "raw": raw,
            "who": match.group(3).strip(),
            "text": match.group(4),
            "time": label,
            "when": when,
            "i": index,
            "unparsed": False,
        })
    return parsed, raws
def decision_events_from_lines(lines: list[dict], source: str, default_when: float) -> list[dict]:
    events = []
    for line in lines:
        marker = classify_marker(line.get("raw") or "")
        if not marker:
            continue
        events.append({
            **marker,
            "source": source,
            "when": line.get("when") or default_when,
            "time": line.get("time") or fmt_et(default_when),
            "seq": line.get("i") or 0,
        })
    return events
def parse_pipeline_text(name: str, text: str, mtime: float) -> dict:
    start_raw = None
    end_raw = None
    head = None
    exits = []
    extras = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        start_match = START_RE.match(stripped)
        if start_match:
            start_raw = start_match.group(1).strip()
            head = start_match.group(2)
            continue
        end_match = END_RE.match(stripped)
        if end_match:
            end_raw = end_match.group(1).strip()
            continue
        exit_match = EXIT_RE.match(stripped)
        if exit_match:
            exits.append((exit_match.group(1), int(exit_match.group(2))))
            continue
        extras.append(clip(stripped, 200))
    return {
        "name": name,
        "head": head,
        "start_raw": start_raw,
        "end_raw": end_raw,
        "start": parse_log_when(start_raw) if start_raw else None,
        "end": parse_log_when(end_raw) if end_raw else None,
        "exits": exits,
        "extras": extras[:6],
        "mtime": mtime,
    }
def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
def pipeline_plan(exits):
    found = dict(exits)
    display = list(CANON_STEPS)
    if "PHONE_BFT" not in found:
        aliases = [name for name in found if name != "MAC_BFT" and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*_BFT", name)]
        phone = aliases[0] if len(aliases) == 1 else None
        if phone:
            display[display.index("PHONE_BFT")] = phone
    display.extend(name for name in found if name not in display)
    return found, display


def pipeline_process_alive(stem, commands):
    expected = {stem, stem + ".sh"}
    for command in commands:
        try:
            args = shlex.split(command)
        except ValueError:
            continue
        if not args:
            continue
        executable = Path(args[0]).name
        if executable in expected:
            return True
        if executable not in ("sh", "bash", "zsh", "dash", "ksh"):
            continue
        for arg in args[1:]:
            if arg.startswith("-"):
                if "c" in arg[1:]:
                    break
                continue
            if Path(arg).name in expected:
                return True
            break
    return False


def build_runs(files: list[dict], commands: list[str], now: float) -> tuple[list[dict], str, float | None]:
    completed = []
    for item in files:
        recorded, expected = pipeline_plan(item.get("exits") or [])
        if item.get("start") and item.get("end") and all(recorded.get(step) == 0 for step in expected):
            completed.append((item["end"].timestamp(), (item["end"] - item["start"]).total_seconds()))
    completed.sort(key=lambda pair: pair[0], reverse=True)
    sample = [duration for _, duration in completed[:ETA_N]]
    med = median(sample)
    if sample:
        basis = f"basis: median of last {len(sample)} runs"
    else:
        basis = "basis: median of last 0 runs"
    today = datetime.fromtimestamp(now, ET).date()
    runs = []
    for item in files:
        found, display = pipeline_plan(item["exits"])
        ended = item.get("end") is not None
        stem = item["name"]
        alive = pipeline_process_alive(stem, commands)
        recent = (now - item["mtime"]) < 30 * 60
        running = (not ended) and alive
        start = item.get("start")
        end = item.get("end")
        mtime_dt = datetime.fromtimestamp(item["mtime"], ET)
        completed_names = [step for step in display if step in found]
        step_seconds = {}
        if start and end and completed_names:
            each = (end - start).total_seconds() / len(completed_names)
            for step in completed_names:
                step_seconds[step] = each
            step_basis = (
                f"estimated: equal split of START to END across {len(completed_names)} "
                "completed steps (no per-step timestamps)"
            )
        elif start and completed_names:
            span = (mtime_dt - start).total_seconds()
            each = span / len(completed_names) if completed_names else 0
            for step in completed_names:
                step_seconds[step] = each
            step_basis = (
                "estimated: equal split of START to file mtime across completed steps "
                "(no per-step timestamps)"
            )
        else:
            step_basis = "no timestamps to estimate step durations"
        # A missing result does not prove that stage started (e.g. Integration tests
        # can explicitly await authorization while the pipeline wrapper lives).
        running_step = None
        if start and end:
            elapsed = (end - start).total_seconds()
        elif start and running:
            # `now` is a unix epoch. `start` is a timezone-aware datetime.
            elapsed = max(0.0, now - start.timestamp())
        elif start:
            elapsed = max(0.0, (mtime_dt - start).total_seconds())
        else:
            elapsed = None
        if any(code != 0 for code in found.values()):
            state = "failed"
        elif ended and all(step in found and found[step] == 0 for step in display):
            state = "passed"
        elif running:
            state = "running"
        elif ended:
            state = "incomplete" if found else "stopped"
        elif recent:
            state = "unconfirmed"
        else:
            state = "stopped"
        if running:
            state = "running"
        eta = None
        if running and med is not None and elapsed is not None:
            eta = max(0.0, med - elapsed)
        start_date = start.astimezone(ET).date() if start else mtime_dt.date()
        steps = []
        for step in display:
            if step in found:
                code = found[step]
                step_state = "pass" if code == 0 else "fail"
                exit_code = code
            elif step == running_step:
                step_state = "run"
                exit_code = None
            else:
                step_state = "pending"
                exit_code = None
            steps.append({
                "name": step,
                "word": STEP_WORDS.get(step, step),
                "state": step_state,
                "exit": exit_code,
                "duration_s": round(step_seconds[step], 1) if step in step_seconds else None,
            })
        done = sum(1 for step in display if step in found)
        missing = [STEP_WORDS.get(step, step) for step in display if step not in found]
        head = item.get("head") or ""
        runs.append({
            "name": stem,
            "head": head,
            "head8": head[:8],
            "state": state,
            "today": start_date == today,
            "elapsed_s": None if elapsed is None else round(elapsed, 1),
            "elapsed": human_dur(elapsed),
            "eta_s": None if eta is None else round(eta, 1),
            "eta": human_dur(eta) if eta is not None else None,
            "step_basis": step_basis,
            "progress_done": done,
            "progress_total": len(display),
            "verification_note": ("No recorded result for: " + ", ".join(missing)) if state == "incomplete" else ("Recent log activity; matching pipeline process not observed" if state == "unconfirmed" else ("Pipeline process observed; current stage not reported" if state == "running" else "")),
            "steps": steps,
            "extras": item.get("extras") or [],
            "alive_via": "process" if alive else None,
            "start_epoch": start.timestamp() if start else item["mtime"],
        })
    runs.sort(key=lambda run: (0 if run["today"] else 1, -(run["start_epoch"] or 0)))
    return runs, basis, med
def check_bucket(item: dict) -> str:
    conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
    status = str(item.get("status") or "").upper()
    if conclusion in ("SUCCESS",):
        return "pass"
    if conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "ERROR", "STARTUP_FAILURE", "ACTION_REQUIRED"):
        return "fail"
    if conclusion in ("SKIPPED", "NEUTRAL"):
        return "skipped"
    if status in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED") or conclusion in ("", "PENDING", "EXPECTED"):
        return "pending"
    return "pending"
def author_login(review: dict) -> str:
    author = review.get("author") or review.get("user") or {}
    if isinstance(author, dict):
        return str(author.get("login") or author.get("name") or "")
    return str(author or "")
def summarize_pr(pr: dict, comments: list | None, hold: str | None, local_sha: str | None) -> dict:
    reviews = list(pr.get("latestReviews") or [])
    reviews.sort(key=lambda review: review.get("submittedAt") or review.get("createdAt") or "", reverse=True)
    coderabbit = None
    others = []
    seen = set()
    for review in reviews:
        login = author_login(review)
        state = review.get("state") or ""
        if "coderabbit" in login.lower():
            if coderabbit is None:
                coderabbit = state or "COMMENTED"
            continue
        if login in seen:
            continue
        if state in ("APPROVED", "CHANGES_REQUESTED"):
            seen.add(login)
            others.append({"author": login, "state": state})
    counts = {"pass": 0, "fail": 0, "pending": 0, "skipped": 0}
    failed_names = []
    rollup = pr.get("statusCheckRollup") or []
    if isinstance(rollup, dict):
        rollup = rollup.get("nodes") or rollup.get("contexts") or []
    if isinstance(rollup, list):
        for check in rollup:
            if not isinstance(check, dict):
                continue
            bucket = check_bucket(check)
            counts[bucket] = counts.get(bucket, 0) + 1
            if bucket == "fail":
                name = check.get("name") or check.get("context") or "check"
                if len(failed_names) < 8:
                    failed_names.append(str(name))
    head = pr.get("headRefOid") or ""
    shown_comments = None
    note = None
    if comments is None:
        note = "review comments not fetched yet"
    else:
        shown = []
        ordered = sorted(comments, key=lambda item: item.get("created_at") or item.get("updated_at") or "", reverse=True)
        for comment in ordered[:3]:
            user = comment.get("user") or {}
            login = user.get("login") if isinstance(user, dict) else ""
            body = comment.get("body") or ""
            body = re.sub(r"\s+", " ", body).strip()
            shown.append({
                "author": login or "",
                "snippet": body[:200],
                "url": comment.get("html_url") or "",
            })
        shown_comments = shown
    number = pr.get("number")
    return {
        "number": number,
        "title": pr.get("title") or "",
        "url": pr.get("url") or "",
        "head": pr.get("headRefName") or "",
        "sha": head[:8],
        "draft": bool(pr.get("isDraft")),
        "review_decision": pr.get("reviewDecision") or "",
        "coderabbit": coderabbit,
        "others": others,
        "checks": counts,
        "failed_names": failed_names,
        "comments": shown_comments,
        "comments_note": note,
        "hold": hold,
        "local_sha": (local_sha or "")[:8] or None,
        "local_full": local_sha,
        "updated_at": pr.get("updatedAt"),
    }
def extract_cost(obj: dict) -> tuple[float, bool]:
    if isinstance(obj.get("total_cost_usd"), (int, float)):
        return float(obj["total_cost_usd"]), True
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    for key in ("cost_usd", "total_cost", "cost"):
        if isinstance(obj.get(key), (int, float)):
            return float(obj[key]), True
        if isinstance(usage.get(key), (int, float)):
            return float(usage[key]), True
    model_usage = obj.get("modelUsage")
    if isinstance(model_usage, dict):
        total = 0.0
        found = False
        for value in model_usage.values():
            if isinstance(value, dict) and isinstance(value.get("costUSD"), (int, float)):
                total += float(value["costUSD"])
                found = True
        if found:
            return total, True
    return 0.0, False
def extract_tokens(obj: dict) -> int:
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    if isinstance(usage.get("total_tokens"), (int, float)):
        return int(usage["total_tokens"])
    total = 0
    for key in ("input_tokens", "output_tokens"):
        if isinstance(usage.get(key), (int, float)):
            total += int(usage[key])
    return total
class JsonlCursor:
    def __init__(self) -> None:
        self.offset = 0
        self.mtime = 0.0
        self.size = 0
        self.complete = False
        self.buf = ""
        self.text = ""
        self.titles: deque = deque(maxlen=15)
        self.cost = 0.0
        self.has_cost = False
        self.tokens = 0
        self.lane_exit = None
        self.markers: list[dict] = []
        self.seen: set[str] = set()
        self.seq = 0

    def reset(self) -> None:
        self.__init__()

    def flush_text(self, source: str, when: float) -> None:
        if self.text:
            self.scan_text(self.text, source, when)
            self.text = ""

    def scan_text(self, text: str, source: str, when: float) -> None:
        if not any(token in text for token in ("DECISION:", "NEEDS OWNER:", "RESOLVED:", "DECIDED:")):
            return
        for line in text.splitlines():
            if not any(token in line for token in ("DECISION:", "NEEDS OWNER:", "RESOLVED:", "DECIDED:")):
                continue
            key = line.strip()
            if not key or key in self.seen:
                continue
            self.seen.add(key)
            marker = classify_marker(key)
            if not marker:
                continue
            self.seq += 1
            self.markers.append({
                **marker,
                "source": source,
                "when": when,
                "time": fmt_et(when),
                "seq": self.seq,
            })

    def handle(self, line: str, source: str, when: float) -> None:
        if not line.strip():
            return
        type_match = re.search(r'"type"\s*:\s*"([^"]+)"', line)
        kind = type_match.group(1) if type_match else ""
        if kind in ("thought", "tool_call_update", "available_commands", "usage", "auto_compact_started", "auto_compact_completed"):
            self.flush_text(source, when)
            return
        if kind == "text":
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return
            data = obj.get("data")
            if isinstance(data, str):
                self.text += data
            return
        if kind == "tool_call":
            self.flush_text(source, when)
            self.titles.append((when, tool_action_label(line)))
            return
        if kind == "end" or ("lane_exit" in line and "type" not in line[:40]):
            self.flush_text(source, when)
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return
            if isinstance(obj, dict) and "lane_exit" in obj and obj.get("type") is None:
                try:
                    self.lane_exit = int(obj.get("lane_exit"))
                except (TypeError, ValueError):
                    self.lane_exit = obj.get("lane_exit")
                return
            if isinstance(obj, dict) and obj.get("type") == "end":
                cost, found = extract_cost(obj)
                if found:
                    self.cost += cost
                    self.has_cost = True
                self.tokens += extract_tokens(obj)
                if "lane_exit" in obj:
                    self.lane_exit = obj.get("lane_exit")
            return
        self.flush_text(source, when)

    def consume(self, path: Path, source: str) -> bool:
        try:
            st = path.stat()
        except OSError:
            return False
        if self.complete and st.st_mtime == self.mtime and st.st_size == self.size:
            return False
        if st.st_size < self.offset:
            self.reset()
        incremental = self.offset > 0
        try:
            with path.open("r", errors="replace") as handle:
                handle.seek(self.offset)
                # Historical lane logs are a few MB. Read a file in one pass so
                # cost and lane_exit are not stuck on the first megabyte. Later
                # growth is small and still capped so a runaway log cannot stall.
                chunk = handle.read(8_000_000)
                self.offset = handle.tell()
        except OSError:
            return False
        self.buf += chunk
        if len(self.buf) > 5_000_000 and "\n" not in self.buf:
            self.buf = ""
        when = time.time() if incremental else 0.0
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            try:
                self.handle(line, source, when)
            except Exception:
                continue
        self.scan_text(self.text, source, when)
        try:
            st2 = path.stat()
        except OSError:
            st2 = st
        if self.offset >= st2.st_size:
            self.complete = True
            self.mtime = st2.st_mtime
            self.size = st2.st_size
        else:
            self.complete = False
        return True
def self_test() -> None:
    """Pure synthetic parser smoke; no files, Engine or workload acceptance."""
    assert oldest_due_job(["first", "second"], {"first": 2, "second": 1}, 3) == "second"
    assert pr_from_text("synthetic #17") == 17
    assert not parse_worktrees("")
    assert parse_status("2001-06-06 09:00 ET Example: DECISION: Review the example")[0]


class _LazyEngine:
    """Importing parser helpers must not allocate databases or probe the host."""
    def __init__(self):
        object.__setattr__(self, "_value", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def _get(self):
        with self._lock:
            if self._value is None:
                object.__setattr__(self, "_value", Engine())
            return self._value

    def __getattr__(self, name):
        return getattr(self._get(), name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._get(), name, value)


ENGINE = _LazyEngine()
