"""Site configuration. Values live in a TOML file, not in this source.

Python 3.9 has no tomllib. The reader accepts the subset this project writes:
comments, dotted [tables], strings, integers, booleans, arrays, and inline
tables (used for per-device quiet hours).
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

_GROUP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")

DEFAULT_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 8780},
    "state": {"dir": ""},
    "service": {"label": "org.zai.devdashd", "allow_restart": False},
    "adapters": {"local_system": True, "git": False, "github": False,
                 "remote": False, "devices": False, "usage": False,
                 "reviewers": False, "hardware_detail": False},
    "executive_feed": {"producer_id": ""},
    "telemetry": {"usage_dir": "", "reviewer_rollup": "", "codex_sessions_dir": ""},
    "labels": {"enabled": False},
    "executive_ai": {"enabled": False, "model": "system"},
    "autotune_monitor": {"status_path": "", "policy_sha256": "", "producer_chat_id": "", "historical_valid": -1},
    "release_scope": {"path": "", "sha256": ""},
    "mini": {"name": "Local host", "host": "", "ip": "", "probe_interval_s": 5},
    "worker": {
        "name": "Worker",
        "computer": "Worker",
        "host": "",
        "ip": "",
        "user": "",
        "resolve_mdns": True,
        "probe_interval_s": 10,
    },
    "remote": {"enabled": False, "name": "Remote host", "computer": "", "host": "",
                "probe_interval_s": 60, "stale_s": 180},
    "devices": {"probe_interval_s": 30, "numbers": []},
    "pool": {"notify_command": ""},
    "github": {"bin": "gh", "repo": "", "integration_branch": ""},
    "links": {"decision_link": ""},
    # v2 phase 1. CodeRabbit limits: https://docs.coderabbit.ai/management/rate-limits
    "coderabbit": {"cli_per_hour": 0, "full_per_hour": 0, "weekly_full_target": 0, "weekly_full_slowdown": 0, "cli_resets": ""},
    "workflow": {"diesel_windows": [], "cloud_agent_cap": 0, "ultra_resets": "",
                 "bugbot_pilot_start": "", "bugbot_pilot_days": 0},
    "xcode_cloud": {"included_hours": 0, "hours_used_manual": -1},
    "copilot": {"overage_usd": 0},
    # xAI Management API (read-only). key_file is a 0600 file outside the repo; never put the key in this TOML.
    "xai": {"key_file": "", "team_id": "", "poll_s": 900},
    "poll": {
        "local_s": 3,
        "load_s": 4,
        "mini_s": 5,
        "worker_s": 10,
        "git_s": 3,
        "devices_s": 30,
        "github_s": 90,
        "hw_s": 3600,
        "stale_local_s": 45,
        "stale_git_s": 45,
        "stale_mini_s": 45,
        "stale_worker_s": 60,
        "stale_devices_s": 120,
        "stale_github_s": 180,
    },
    "paths": {
        "status": "",
        "decisions": "",
        "pipeline_dir": "",
        "headless_state": "",
        "headless_logs": "",
        "headless_locks": "",
        "ledger": "",
        "needs": "",
        "briefs": "",
        "repo": "",
        "slot_dir": "",
        "programs_registry": "",
        "executive_owner": "",
        "scratch": "",
    },
    "trust": {"dir": ""},
    "developer": {"dir": ""},
    "queue": {"order": []},
    "auth_group": "zai-devdashd",
    "allow_localhost_unauth": True,
    "session_ttl_s": 12 * 3600,
}

CURRENT = copy.deepcopy(DEFAULT_CONFIG)
CONFIG_FILE = Path.home() / ".config" / "zai-devdashd" / "config.toml"
_LOADED = False

# Filled by apply() for the modules that still use these names.
ROOT = Path.home() / "Library" / "Application Support" / "zai-devdashd"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATUS_PATH = Path()
DECISIONS_PATH = Path()
PIPELINE_DIR = Path()
HEADLESS_STATE = Path()
HEADLESS_LOGS = Path()
HEADLESS_LOCKS = Path()
LEDGER_PATH = Path()
NEEDS_DIR = Path()
REPO = Path()
SLOT_DIR = Path()
BRIEF_DIR = Path()
FMLABEL = Path()
LABEL_CACHE = Path()
HW_CACHE = Path()
GH = "gh"
REPO_SLUG = ""
DECISION_LINK = ""
QUEUE = []
QUEUE_INDEX = {}
PORT = 8780
HOST = "127.0.0.1"
DEVICE_NUMBERS = ()
CONFIG_PATH = CONFIG_FILE


def _clip_host(value: str | None, fallback: str) -> str:
    text = (value or "").strip()
    if not text or len(text) > 200 or re.search(r"[\s@/\\]", text):
        return fallback
    return text


def _clip_user(value: str | None, fallback: str) -> str:
    text = (value or "").strip()
    if not text or len(text) > 64 or not re.match(r"^[A-Za-z0-9._-]+$", text):
        return fallback
    return text


def _interval(value, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(600, max(5, number))


def _auth_group(value, fallback: str) -> str:
    text = str(value or "").strip()
    if _GROUP_RE.match(text):
        return text
    return fallback


def _session_ttl(value, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(7 * 24 * 3600, max(300, number))


def _flag(value, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def parse_toml(text: str) -> dict:
    """Read the small TOML subset used by config.example.toml."""
    root: dict = {}
    current = root

    def split_commas(body: str) -> list:
        parts = []
        buf = []
        quote = ""
        depth = 0
        for char in body:
            if quote:
                buf.append(char)
                if char == quote:
                    quote = ""
                continue
            if char in ("'", '"'):
                quote = char
                buf.append(char)
                continue
            if char in "{[":
                depth += 1
                buf.append(char)
                continue
            if char in "}]":
                depth = max(0, depth - 1)
                buf.append(char)
                continue
            if char == "," and depth == 0:
                parts.append("".join(buf).strip())
                buf = []
                continue
            buf.append(char)
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
        return parts

    def parse_inline(raw: str) -> dict:
        body = raw[1:-1].strip()
        if not body:
            return {}
        out = {}
        for part in split_commas(body):
            if "=" not in part:
                raise ValueError("inline table entry")
            key, value = part.split("=", 1)
            out[key.strip()] = parse_value(value.strip())
        return out

    def parse_value(raw: str):
        if raw in ("true", "false"):
            return raw == "true"
        if raw.startswith('"') or raw.startswith("'"):
            quote = raw[0]
            if len(raw) < 2 or raw[-1] != quote:
                raise ValueError("unclosed string")
            body = raw[1:-1]
            if quote == "'":
                return body
            return (
                body.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
        if raw.startswith("{") and raw.endswith("}"):
            return parse_inline(raw)
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            if not inner:
                return []
            return [parse_value(part) for part in split_commas(inner)]
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        if re.fullmatch(r"-?\d+\.\d+", raw):
            return float(raw)
        raise ValueError("unsupported value")

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]") and "=" not in line:
            parts = [part.strip() for part in line[1:-1].split(".")]
            current = root
            for part in parts:
                nxt = current.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    current[part] = nxt
                current = nxt
            continue
        if "=" not in line:
            raise ValueError("line %d is not a key" % lineno)
        key, raw_value = line.split("=", 1)
        try:
            current[key.strip()] = parse_value(raw_value.strip())
        except ValueError as exc:
            raise ValueError("line %d: %s" % (lineno, exc)) from exc
    return root


def _toml_string(value: str) -> str:
    return '"%s"' % (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join("%s = %s" % (key, _toml_value(item)) for key, item in value.items()) + "}"
    raise TypeError(type(value).__name__)


def _dump_level(data: dict, prefix: str) -> str:
    scalars = []
    tables = []
    for key in sorted(data):
        if isinstance(data[key], dict):
            tables.append(key)
        else:
            scalars.append(key)
    lines = []
    if prefix:
        lines.append("[%s]" % prefix)
    for key in scalars:
        lines.append("%s = %s" % (key, _toml_value(data[key])))
    chunks = []
    if lines:
        chunks.append("\n".join(lines))
    for name in tables:
        child = name if not prefix else "%s.%s" % (prefix, name)
        chunks.append(_dump_level(data[name], child).rstrip("\n"))
    return "\n\n".join(chunk for chunk in chunks if chunk) + "\n"


def dump_toml(data: dict) -> str:
    return _dump_level(data, "").rstrip() + "\n"


def _merge(base: dict, raw: dict) -> dict:
    out = copy.deepcopy(base)
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _clip_text(value, limit: int) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    return text[:limit]


def _clock_hhmm(value) -> str:
    text = str(value or "").strip()
    if text == "24:00" or re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
        return text
    return ""


def _zone_name(value) -> str:
    text = str(value or "").strip() or "America/New_York"
    if len(text) > 64 or not re.fullmatch(r"[A-Za-z0-9_+-][A-Za-z0-9_/+-]{0,63}", text):
        return "America/New_York"
    try:
        ZoneInfo(text)
    except Exception:
        return "America/New_York"
    return text


def _sanitize_profile(raw) -> dict:
    """Keep a device profile's label and quiet-hour windows. Drop anything else."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    label = _clip_text(raw.get("label"), 40)
    if label:
        out["label"] = label
    windows = []
    for item in raw.get("quiet_hours") or []:
        if not isinstance(item, dict):
            continue
        start = _clock_hhmm(item.get("start"))
        end = _clock_hhmm(item.get("end"))
        if not start or not end:
            continue
        windows.append({
            "start": start,
            "end": end,
            "tz": _zone_name(item.get("tz")),
            "note": _clip_text(item.get("note"), 80),
        })
    if windows:
        out["quiet_hours"] = windows
    return out


def _sanitize(cfg: dict) -> dict:
    base = DEFAULT_CONFIG
    worker = cfg.get("worker") if isinstance(cfg.get("worker"), dict) else {}
    mini = cfg.get("mini") if isinstance(cfg.get("mini"), dict) else {}
    devices = cfg.get("devices") if isinstance(cfg.get("devices"), dict) else {}
    server = cfg.get("server") if isinstance(cfg.get("server"), dict) else {}
    cfg["worker"] = worker
    cfg["mini"] = mini
    cfg["devices"] = devices
    cfg["server"] = server
    worker["host"] = _clip_host(worker.get("host"), "")
    worker["ip"] = _clip_host(worker.get("ip"), "")
    worker["user"] = _clip_user(worker.get("user"), "")
    worker["name"] = _clip_host(worker.get("name"), base["worker"]["name"]) or base["worker"]["name"]
    worker["computer"] = _clip_host(worker.get("computer"), worker["name"]) or worker["name"]
    worker["resolve_mdns"] = _flag(worker.get("resolve_mdns"), True)
    worker["probe_interval_s"] = _interval(worker.get("probe_interval_s"), 10)
    mini["host"] = _clip_host(mini.get("host"), "")
    mini["ip"] = _clip_host(mini.get("ip"), "")
    mini["name"] = _clip_host(mini.get("name"), base["mini"]["name"]) or base["mini"]["name"]
    mini["probe_interval_s"] = _interval(mini.get("probe_interval_s"), 5)
    remote = cfg.get("remote") if isinstance(cfg.get("remote"), dict) else {}
    peer = str(remote.get("host") or "").strip()
    valid_peer = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", peer))
    try:
        stale_s = int(remote.get("stale_s") or 180)
    except (TypeError, ValueError):
        stale_s = 180
    cfg["remote"] = {
        "enabled": _flag(remote.get("enabled"), False) and valid_peer,
        "host": peer if valid_peer else "",
        "name": _clip_text(remote.get("name"), 64) or "Remote host",
        "computer": _clip_host(remote.get("computer"), ""),
        "include_account": remote.get("include_account") is True,
        "probe_interval_s": max(30, _interval(remote.get("probe_interval_s"), 60)),
        "stale_s": min(3600, max(60, stale_s)),
    }
    profiles = {}
    for key, value in list(devices.items()):
        if key in ("probe_interval_s", "numbers") or not isinstance(value, dict):
            continue
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", str(key)):
            continue
        profile = _sanitize_profile(value)
        if profile:
            profiles[key] = profile
    interval = _interval(devices.get("probe_interval_s"), 30)
    numbers = devices.get("numbers") if isinstance(devices.get("numbers"), list) else list(base["devices"]["numbers"])
    devices.clear()
    devices["probe_interval_s"] = interval
    devices["numbers"] = [str(item) for item in numbers if re.fullmatch(r"\d{2,6}", str(item or ""))]
    devices.update(profiles)
    pool = cfg.get("pool") if isinstance(cfg.get("pool"), dict) else {}
    command = _clip_text(pool.get("notify_command"), 200)
    cfg["pool"] = {"notify_command": command}
    try:
        port = int(server.get("port") or 8780)
    except (TypeError, ValueError):
        port = 8780
    server["port"] = port if 1 <= port <= 65535 else 8780
    server["host"] = _clip_host(server.get("host"), "127.0.0.1") or "127.0.0.1"
    cfg["auth_group"] = _auth_group(cfg.get("auth_group"), base["auth_group"])
    cfg["allow_localhost_unauth"] = _flag(cfg.get("allow_localhost_unauth"), True)
    cfg["session_ttl_s"] = _session_ttl(cfg.get("session_ttl_s"), base["session_ttl_s"])
    service = cfg.get("service") if isinstance(cfg.get("service"), dict) else {}
    label = str(service.get("label") or "org.zai.devdashd")
    if not re.fullmatch(r"org\.zai\.devdashd(?:\.[A-Za-z0-9_-]{1,64})?", label):
        label = "org.zai.devdashd"
    cfg["service"] = {"label": label, "allow_restart": service.get("allow_restart") is True}
    adapters = cfg.get("adapters") if isinstance(cfg.get("adapters"), dict) else {}
    cfg["adapters"] = {key: adapters.get(key, value) is True for key, value in base["adapters"].items()}
    label_options = cfg.get("labels") if isinstance(cfg.get("labels"), dict) else {}
    cfg["labels"] = {"enabled": label_options.get("enabled") is True}
    order = ((cfg.get("queue") or {}).get("order") if isinstance(cfg.get("queue"), dict) else []) or []
    clean = []
    for item in order:
        try:
            clean.append(int(item))
        except (TypeError, ValueError):
            continue
    cfg.setdefault("queue", {})["order"] = clean
    return cfg


def default_config_path() -> Path:
    raw = os.environ.get("ZAI_DEVDASHD_CONFIG") or ""
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "zai-devdashd" / "config.toml"


def state_dir() -> Path:
    raw = ((CURRENT.get("state") or {}).get("dir") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "Library" / "Application Support" / "zai-devdashd"


def adapter_enabled(name: str) -> bool:
    """External integrations require an explicit true value in site config."""
    adapters = CURRENT.get("adapters") or {}
    return adapters.get(name, name == "local_system") is True


def optional_path(name: str, section: str = "paths") -> Path:
    """Blank inputs point at an absent private sentinel, never the process cwd."""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name) or section not in ("paths", "telemetry"):
        raise ValueError("Invalid optional input name")
    values = CURRENT.get(section)
    value = values.get(name) if isinstance(values, dict) else None
    raw = value.strip() if isinstance(value, str) else ""
    return Path(raw).expanduser() if raw else state_dir() / "unconfigured" / section / name


def programs_registry() -> Path:
    """Read-only checkpoint registry; private receipt paths never enter settings HTML."""
    raw = ((CURRENT.get("paths") or {}).get("programs_registry") or "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "programs.json"


def executive_owner() -> Path:
    """Private, read-only Coordinator report; no workload control authority."""
    raw = ((CURRENT.get("paths") or {}).get("executive_owner") or "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "executive-owner.json"


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def probe_script() -> Path:
    return package_dir() / "resources" / "build_host_probe.sh"


def static_dir() -> Path:
    return package_dir() / "static"


def trust_dir() -> Path:
    raw = ((CURRENT.get("trust") or {}).get("dir") or "").strip()
    return Path(raw).expanduser() if raw else state_dir() / "trust"


def trust_file(name: str) -> Path:
    return trust_dir() / name


def developer_dir() -> str:
    return ((CURRENT.get("developer") or {}).get("dir") or "").strip()


def listen_port() -> int:
    raw = os.environ.get("ZAI_DEVDASHD_PORT")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if 1 <= value <= 65535:
            return value
    return int((CURRENT.get("server") or {}).get("port") or 8780)


def poll_local() -> float:
    return float((CURRENT.get("poll") or {}).get("local_s") or 3)


def poll_intervals() -> dict:
    poll = CURRENT.get("poll") or {}
    return {
        "hw": float(poll.get("hw_s") or 3600),
        "load": float(poll.get("load_s") or 4),
        "mini": float(poll.get("mini_s") or 5),
        "worker": float(poll.get("worker_s") or 10),
        "remote": float((CURRENT.get("remote") or {}).get("probe_interval_s") or 60),
        "git": float(poll.get("git_s") or 3),
        "devices": float(poll.get("devices_s") or 30),
        "github": float(poll.get("github_s") or 90),
    }


def poll_github() -> float:
    return float((CURRENT.get("poll") or {}).get("github_s") or 90)


def mini_name() -> str:
    return ((CURRENT.get("mini") or {}).get("name") or "Mini")


def worker_name() -> str:
    return ((CURRENT.get("worker") or {}).get("name") or "Worker")


def worker_computer() -> str:
    worker = CURRENT.get("worker") or {}
    return worker.get("computer") or worker.get("name") or "Worker"


def mini_host() -> str:
    return ((CURRENT.get("mini") or {}).get("host") or "")


def mini_ip() -> str:
    return ((CURRENT.get("mini") or {}).get("ip") or "")


def apply(create_dirs: bool = False) -> None:
    """Publish path constants for the modules split out of the old monitor."""
    global ROOT, STATIC_DIR, STATUS_PATH, DECISIONS_PATH, PIPELINE_DIR
    global HEADLESS_STATE, HEADLESS_LOGS, HEADLESS_LOCKS, LEDGER_PATH, NEEDS_DIR
    global REPO, SLOT_DIR, BRIEF_DIR, FMLABEL, LABEL_CACHE, HW_CACHE
    global GH, REPO_SLUG, DECISION_LINK, QUEUE, QUEUE_INDEX, PORT, HOST
    global DEVICE_NUMBERS, CONFIG_PATH
    paths = CURRENT.get("paths") or {}
    root = state_dir()
    ROOT = root
    STATIC_DIR = static_dir()
    STATUS_PATH = optional_path("status")
    DECISIONS_PATH = optional_path("decisions")
    PIPELINE_DIR = optional_path("pipeline_dir")
    HEADLESS_STATE = optional_path("headless_state")
    HEADLESS_LOGS = optional_path("headless_logs")
    HEADLESS_LOCKS = optional_path("headless_locks")
    LEDGER_PATH = optional_path("ledger")
    NEEDS_DIR = optional_path("needs")
    REPO = optional_path("repo")
    SLOT_DIR = optional_path("slot_dir")
    BRIEF_DIR = optional_path("briefs")
    FMLABEL = root / "bin" / "fmlabel"
    LABEL_CACHE = root / "cache" / "labels.json"
    HW_CACHE = root / "cache" / "hardware.json"
    GH = ((CURRENT.get("github") or {}).get("bin") or "gh")
    REPO_SLUG = ((CURRENT.get("github") or {}).get("repo") or "")
    DECISION_LINK = ((CURRENT.get("links") or {}).get("decision_link") or "")
    QUEUE = list(((CURRENT.get("queue") or {}).get("order") or []))
    QUEUE_INDEX = {number: index for index, number in enumerate(QUEUE)}
    PORT = listen_port()
    HOST = ((CURRENT.get("server") or {}).get("host") or "127.0.0.1")
    DEVICE_NUMBERS = tuple((CURRENT.get("devices") or {}).get("numbers") or [])
    CONFIG_PATH = CONFIG_FILE
    if create_dirs:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for folder in ("tmp", "logs", "cache", "data", "certs", "bin"):
            (root / folder).mkdir(mode=0o700, parents=True, exist_ok=True)


INSTALL_NAMES = (
    "ROOT", "STATIC_DIR", "STATUS_PATH", "DECISIONS_PATH", "PIPELINE_DIR",
    "HEADLESS_STATE", "HEADLESS_LOGS", "HEADLESS_LOCKS", "LEDGER_PATH", "NEEDS_DIR",
    "REPO", "SLOT_DIR", "BRIEF_DIR", "FMLABEL", "LABEL_CACHE", "HW_CACHE",
    "GH", "REPO_SLUG", "DECISION_LINK", "QUEUE", "QUEUE_INDEX", "PORT", "HOST",
    "DEVICE_NUMBERS", "CONFIG_PATH",
)


def install(module_globals: dict) -> None:
    apply()
    for name in INSTALL_NAMES:
        module_globals[name] = globals()[name]


def load(path: Path | None = None) -> dict:
    global CURRENT, CONFIG_FILE, _LOADED
    CONFIG_FILE = Path(path).expanduser() if path else default_config_path()
    raw = {}
    if CONFIG_FILE.is_file():
        raw = parse_toml(CONFIG_FILE.read_text(encoding="utf-8"))
    CURRENT = _sanitize(_merge(DEFAULT_CONFIG, raw))
    _LOADED = True
    apply(create_dirs=True)
    return copy.deepcopy(CURRENT)


def ensure_loaded() -> dict:
    if not _LOADED:
        return load(default_config_path())
    apply()
    return copy.deepcopy(CURRENT)


def load_config(path: Path | None = None) -> dict:
    """Reload the TOML the settings page and the probes share."""
    target = Path(path) if path else CONFIG_FILE
    if not _LOADED or Path(path or CONFIG_FILE) != CONFIG_FILE:
        return load(target)
    if target.is_file():
        raw = parse_toml(target.read_text(encoding="utf-8"))
        global CURRENT
        CURRENT = _sanitize(_merge(DEFAULT_CONFIG, raw))
        apply()
    return copy.deepcopy(CURRENT)


def save_config(path: Path, cfg: dict) -> None:
    global CURRENT
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    CURRENT = _sanitize(_merge(DEFAULT_CONFIG, cfg or {}))
    apply()
    text = dump_toml(CURRENT)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def apply_settings(cfg: dict, form: dict) -> dict:
    nxt = copy.deepcopy(cfg or DEFAULT_CONFIG)
    worker = nxt.setdefault("worker", {})
    mini = nxt.setdefault("mini", {})
    devices = nxt.setdefault("devices", {})
    if "worker_host" in form:
        worker["host"] = _clip_host(form.get("worker_host"), worker.get("host") or "")
    if "worker_ip" in form:
        worker["ip"] = _clip_host(form.get("worker_ip"), worker.get("ip") or "")
    if "worker_user" in form:
        worker["user"] = _clip_user(form.get("worker_user"), worker.get("user") or "")
    if "worker_interval" in form:
        worker["probe_interval_s"] = _interval(form.get("worker_interval"), worker.get("probe_interval_s") or 10)
    if "devices_interval" in form:
        devices["probe_interval_s"] = _interval(
            form.get("devices_interval"), devices.get("probe_interval_s") or 30
        )
    if "mini_host" in form:
        mini["host"] = _clip_host(form.get("mini_host"), mini.get("host") or "")
    if "mini_ip" in form:
        mini["ip"] = _clip_host(form.get("mini_ip"), mini.get("ip") or "")
    if "resolve_mdns" in form:
        worker["resolve_mdns"] = str(form.get("resolve_mdns") or "").lower() in ("1", "true", "on", "yes")
    return nxt
