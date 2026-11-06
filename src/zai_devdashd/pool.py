"""Read-only test-pool eligibility for phones and iPads.

A device that is already connected the first time it is seen has no offline
history. The 10-minute offline gate applies only after an offline stretch
was actually recorded. The 2-minute available clock starts at first_seen, so
that device can become a candidate once it has stayed reachable for 2 minutes.

`devicectl` on this Xcode has `device info details` and `device info lockState`.
It has no `device info battery` subcommand. Battery, when present, is read from
the details JSON. Unknown battery does not fail a candidate. Unknown lock is
not treated as unlocked and does not become "Needs unlock".

Health queries are allow-listed to those two info subcommands. An ACTIVE device
(a test command referenced it) is not queried; its last status is kept.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OFFLINE_S = 600
AVAILABLE_S = 120
HEALTH_INTERVAL_S = 120
HEALTH_TIMEOUT_S = 15
RETAIN_S = 30 * 24 * 3600
ET = ZoneInfo("America/New_York")
REACHABLE = ("connected", "available (paired)")
_DEVICE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_INFO_KINDS = ("details", "lockState")
_DIGITS = re.compile(r"(\d{3})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_events (
    id INTEGER PRIMARY KEY,
    device_key TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    udid TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS device_events_key_ts ON device_events(device_key, ts);
CREATE TABLE IF NOT EXISTS device_state (
    device_key TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    udid TEXT NOT NULL DEFAULT '',
    coredevice_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    os TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    since REAL NOT NULL,
    first_seen REAL NOT NULL,
    had_offline INTEGER NOT NULL DEFAULT 0,
    offline_span_s REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    status_since REAL NOT NULL,
    reasons TEXT NOT NULL DEFAULT '[]',
    eligible INTEGER NOT NULL DEFAULT 0,
    quiet_hours_active INTEGER NOT NULL DEFAULT 0,
    health_json TEXT NOT NULL DEFAULT '',
    health_at REAL,
    lock_episode TEXT NOT NULL DEFAULT '',
    profile_key TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notify_events (
    id INTEGER PRIMARY KEY,
    device_key TEXT NOT NULL,
    episode TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
"""


def qualifies(offline_s: float, available_s: float, had_offline: bool) -> bool:
    """True when the device has been reachable long enough to be checked.

    Without a recorded offline stretch, only the 2-minute available clock
    applies (the device was already connected when first seen). After an
    offline stretch, that stretch must be at least 10 minutes and the device
    must then stay reachable for 2 minutes. 599s offline does not qualify.
    119s available does not qualify.
    """
    if available_s < AVAILABLE_S:
        return False
    if not had_offline:
        return True
    return offline_s >= OFFLINE_S


def _minutes(hhmm: str) -> int:
    if hhmm == "24:00":
        return 24 * 60
    hour, minute = hhmm.split(":", 1)
    return int(hour) * 60 + int(minute)


def quiet_note(windows, when: datetime) -> str | None:
    """Return the note when `when` falls inside a quiet-hour window.

    The window is [start, end) on the clock of `tz`. 24:00 is the next
    midnight, so 23:59 matches and 00:00 does not. ZoneInfo supplies the
    offset, including EDT versus EST.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=ET)
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        start = str(window.get("start") or "")
        end = str(window.get("end") or "")
        if not start or not end:
            continue
        try:
            zone = ZoneInfo(str(window.get("tz") or "America/New_York"))
            start_m = _minutes(start)
            end_m = _minutes(end)
        except Exception:
            continue
        local = when.astimezone(zone)
        now_m = local.hour * 60 + local.minute
        if start_m <= end_m:
            inside = start_m <= now_m < end_m
        else:
            inside = now_m >= start_m or now_m < end_m
        if inside:
            return str(window.get("note") or "quiet hours")
    return None


def profiles_from(cfg: dict | None) -> dict:
    devices = (cfg or {}).get("devices") if isinstance((cfg or {}).get("devices"), dict) else {}
    return {key: value for key, value in devices.items() if isinstance(value, dict)}


def profile_for(item: dict, profiles: dict) -> tuple[str, dict]:
    for text in (item.get("tile_name"), item.get("name"), item.get("anchor")):
        match = _DIGITS.search(str(text or ""))
        if not match:
            continue
        key = "s" + match.group(1)
        found = profiles.get(key)
        if isinstance(found, dict):
            return key, found
    return "", {}


def device_label(item: dict, profile: dict) -> str:
    label = str((profile or {}).get("label") or "").strip()
    if label:
        return label
    for text in (item.get("tile_name"), item.get("name")):
        match = _DIGITS.search(str(text or ""))
        if match:
            return "S" + match.group(1)
    return str(item.get("name") or "Device")


def notify_command(cfg: dict | None) -> str:
    pool = (cfg or {}).get("pool") if isinstance((cfg or {}).get("pool"), dict) else {}
    return str(pool.get("notify_command") or "")


def storage_key(item: dict) -> str:
    udid = str(item.get("udid") or "").strip()
    if udid:
        return "udid:" + udid
    identifier = str(item.get("identifier") or "").strip()
    if identifier:
        return "id:" + identifier
    name = re.sub(r"\s+", " ", str(item.get("name") or "").strip().lower())
    return "name:" + name[:80]


def query_id(item: dict) -> str:
    for key in ("identifier", "udid"):
        text = str(item.get(key) or "").strip()
        if _DEVICE_ID.match(text):
            return text
    return ""


def paired_ok(pairing_state: str, trust_state: str, list_state: str):
    """True when paired and trusted. None when the list did not say."""
    trust = (trust_state or "").strip().lower()
    if trust and trust not in ("trusted",):
        return False
    pairing = (pairing_state or "").strip().lower()
    if pairing == "paired":
        return True
    if pairing:
        return False
    if list_state in REACHABLE:
        return True
    return None


def _as_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("1", "true", "on", "yes", "enabled", "enable"):
        return True
    if text in ("0", "false", "off", "no", "disabled", "disable"):
        return False
    return None


def _lock_word(value) -> str:
    if isinstance(value, bool):
        return "locked" if value else "unlocked"
    text = str(value or "").strip().lower()
    if text in ("locked", "true", "1", "yes"):
        return "locked"
    if text in ("unlocked", "false", "0", "no"):
        return "unlocked"
    return "unknown"


def _percent(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 100:
        return None
    if number <= 1:
        return number * 100.0
    return number


def _charging(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("charging", "charged", "full", "true", "1", "yes"):
        return True
    if text in ("unplugged", "discharging", "not charging", "false", "0", "no"):
        return False
    return None


def _walk_scalars(node, want: set[str], found: dict, parent: str, depth: int) -> None:
    if depth > 14 or node is None:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            low = str(key).lower()
            if low in want and low not in found and not isinstance(value, (dict, list)):
                found[low] = value
            _walk_scalars(value, want, found, low, depth + 1)
        return
    if isinstance(node, list):
        for item in node[:40]:
            _walk_scalars(item, want, found, parent, depth + 1)


def _battery(payload: dict | None):
    percent = None
    charging = None
    if not isinstance(payload, dict):
        return None, None

    def walk(node, parent: str, depth: int) -> None:
        nonlocal percent, charging
        if depth > 14 or node is None:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                low = str(key).lower()
                parent_battery = "battery" in parent or "battery" in low
                if isinstance(value, (dict, list)):
                    walk(value, low, depth + 1)
                    continue
                if percent is None and (
                    low in ("batterylevel", "batterypercent", "batterypercentage")
                    or (parent_battery and low in ("percent", "percentage", "level"))
                ):
                    percent = value
                if charging is None and low in ("charging", "ischarging", "batterystate"):
                    charging = value
            return
        if isinstance(node, list):
            for item in node[:40]:
                walk(item, parent, depth + 1)

    walk(payload, "", 0)
    return percent, charging


def extract_health(details: dict | None, lock_payload: dict | None) -> dict:
    """Pull developer mode, lock, and battery out of devicectl JSON.

    The walker accepts both the current `properties` shape and the deprecated
    hardwareProperties / deviceProperties / connectionProperties shape.
    """
    found: dict = {}
    _walk_scalars(details, {"developermodestatus", "developermode"}, found, "", 0)
    developer = _as_bool(found.get("developermodestatus", found.get("developermode")))
    lock_found: dict = {}
    _walk_scalars(lock_payload, {"lockstate", "locked", "islocked", "devicelocked"}, lock_found, "", 0)
    if not lock_found:
        _walk_scalars(details, {"lockstate", "locked", "islocked", "devicelocked"}, lock_found, "", 0)
    raw_lock = None
    for key in ("lockstate", "locked", "islocked", "devicelocked"):
        if key in lock_found:
            raw_lock = lock_found[key]
            break
    raw_pct, raw_charge = _battery(details if isinstance(details, dict) else None)
    return {
        "developer_mode": developer,
        "lock": _lock_word(raw_lock) if raw_lock is not None else "unknown",
        "battery_pct": _percent(raw_pct),
        "charging": _charging(raw_charge),
    }


def interpret(health: dict, paired) -> dict:
    """Map a cached health sample to a tile status. Unknown does not fail."""
    if paired is False:
        return _decision("Needs attention: not paired", False, ["not paired"], "end")
    if isinstance(health, dict) and health.get("developer_mode") is False:
        return _decision("Needs attention: Developer Mode is off", False, ["Developer Mode is off"], "end")
    lock = (health or {}).get("lock") or "unknown"
    if lock == "locked":
        return _decision("Needs unlock", False, ["locked"], "open")
    percent = (health or {}).get("battery_pct")
    charging = (health or {}).get("charging")
    if percent is not None and percent < 30 and charging is not True:
        return _decision("Needs attention: battery under 30%", False, ["battery under 30%"], "end")
    return _decision("In test pool", True, [], "end")


def _decision(status: str, eligible: bool, reasons: list, episode: str, needs_check: bool = False) -> dict:
    return {
        "status": status,
        "eligible": eligible,
        "reasons": reasons,
        "needs_check": needs_check,
        "episode": episode,
    }


def classify(
    reachable: bool,
    active: bool,
    had_offline: bool,
    offline_span_s: float,
    available_s: float,
    quiet: str | None,
    paired,
    health: dict | None,
    health_fresh: bool,
    previous_status: str,
) -> dict:
    quiet_on = bool(quiet)
    if not reachable:
        decision = _decision("Offline", False, ["offline"], "end")
        decision["quiet"] = quiet_on
        return decision
    if quiet:
        decision = _decision("Quiet hours: %s" % quiet, False, [quiet], "keep")
        decision["quiet"] = True
        return decision
    ready = qualifies(offline_span_s, available_s, had_offline)
    if not ready:
        reasons = []
        if had_offline and offline_span_s < OFFLINE_S:
            reasons.append("offline stretch under 10 minutes")
        elif available_s < AVAILABLE_S:
            reasons.append("available under 2 minutes")
        decision = _decision("", False, reasons, "end")
        decision["quiet"] = False
        return decision
    if active:
        status = previous_status or ""
        if status == "Needs unlock" or status.startswith("Quiet hours:"):
            episode = "keep"
        else:
            episode = "end"
        decision = _decision(status, status == "In test pool", ["test running"], episode)
        decision["quiet"] = False
        return decision
    if isinstance(health, dict):
        decision = interpret(health, paired)
        decision["quiet"] = False
        decision["needs_check"] = not health_fresh
        if decision["status"] == "Needs unlock":
            decision["episode"] = "open"
        return decision
    decision = _decision("Candidate (checking)", False, ["checking"], "end", needs_check=True)
    decision["quiet"] = False
    return decision


def iso_et(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch), ET).isoformat(timespec="seconds")


_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _as_et(value):
    """An aware America/New_York datetime, or None when value is empty."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        when = value
    elif isinstance(value, (int, float)):
        when = datetime.fromtimestamp(float(value), ET)
    else:
        text = str(value).strip()
        if not text:
            return None
        when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=ET)
    return when.astimezone(ET)


def since_label(value, now=None) -> str:
    """Readable Eastern time for the Since column.

    Today in America/New_York is "6:23 PM ET". Any other day is
    "Oct 28, 6:23 PM ET". The hour has no leading zero. `now` is a
    datetime, epoch, or ISO string so tests can pin the clock. The API
    field `since` stays the raw ISO string.
    """
    when = _as_et(value)
    if when is None:
        return ""
    current = _as_et(now) if now is not None else datetime.now(ET)
    if current is None:
        current = datetime.now(ET)
    hour = when.hour % 12 or 12
    clock = "%d:%02d %s ET" % (hour, when.minute, "AM" if when.hour < 12 else "PM")
    if when.date() == current.date():
        return clock
    return "%s %d, %s" % (_MONTHS[when.month - 1], when.day, clock)


def emit_notify(command: str, message: str) -> None:
    """Run the configured hook. An empty command sends nothing."""
    text = (command or "").strip()
    if not text:
        return
    try:
        args = shlex.split(text)
    except ValueError:
        return
    if not args:
        return
    args.append(message)
    try:
        subprocess.run(
            args,
            shell=False,
            timeout=5,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _info_json(device_id: str, kind: str, work_dir: Path):
    if kind not in _INFO_KINDS or not _DEVICE_ID.match(device_id or ""):
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", device_id)[:40]
    out = work_dir / ("pool-%s-%s.json" % (kind, safe))
    try:
        out.unlink()
    except OSError:
        pass
    try:
        completed = subprocess.run(
            [
                "xcrun", "devicectl", "device", "info", kind,
                "--device", device_id,
                "--quiet",
                "--timeout", str(HEALTH_TIMEOUT_S),
                "--json-output", str(out),
            ],
            timeout=HEALTH_TIMEOUT_S,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not out.is_file():
        return None
    try:
        payload = json.loads(out.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return {"result": payload}


def default_health(device_id: str, work_dir: Path):
    """One details query plus one lockState query. No battery subcommand."""
    details = _info_json(device_id, "details", Path(work_dir))
    if details is None:
        return None
    lock_payload = _info_json(device_id, "lockState", Path(work_dir))
    return extract_health(details, lock_payload)


def _blank_row(key: str, now: float, state: str) -> dict:
    reachable = state in REACHABLE
    return {
        "device_key": key,
        "name": "",
        "udid": "",
        "coredevice_id": "",
        "model": "",
        "os": "",
        "state": state,
        "since": now,
        "first_seen": now,
        "had_offline": 0 if reachable else 1,
        "offline_span_s": 0.0,
        "status": "",
        "status_since": now,
        "reasons": [],
        "eligible": 0,
        "quiet_hours_active": 0,
        "health": None,
        "health_at": None,
        "lock_episode": "",
        "profile_key": "",
        "updated_at": now,
    }


def _apply_state(row: dict, new_state: str, now: float) -> bool:
    old = row.get("state") or ""
    if old == new_state:
        return False
    old_up = old in REACHABLE
    new_up = new_state in REACHABLE
    if old and old_up and not new_up:
        row["had_offline"] = 1
        row["since"] = now
    elif old and not old_up and new_up:
        row["had_offline"] = 1
        row["offline_span_s"] = max(0.0, now - float(row.get("since") or now))
        row["since"] = now
    row["state"] = new_state
    return True


class PoolStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _load_rows(self) -> dict[str, dict]:
        found = {}
        for record in self._db.execute("SELECT * FROM device_state"):
            health = None
            raw = record["health_json"] or ""
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    health = parsed
            try:
                reasons = json.loads(record["reasons"] or "[]")
            except json.JSONDecodeError:
                reasons = []
            found[record["device_key"]] = {
                "device_key": record["device_key"],
                "name": record["name"],
                "udid": record["udid"],
                "coredevice_id": record["coredevice_id"],
                "model": record["model"],
                "os": record["os"],
                "state": record["state"],
                "since": record["since"],
                "first_seen": record["first_seen"],
                "had_offline": int(record["had_offline"] or 0),
                "offline_span_s": float(record["offline_span_s"] or 0),
                "status": record["status"] or "",
                "status_since": record["status_since"],
                "reasons": reasons if isinstance(reasons, list) else [],
                "eligible": int(record["eligible"] or 0),
                "quiet_hours_active": int(record["quiet_hours_active"] or 0),
                "health": health,
                "health_at": record["health_at"],
                "lock_episode": record["lock_episode"] or "",
                "profile_key": record["profile_key"] or "",
                "updated_at": record["updated_at"],
            }
        return found

    def _events(self, device_key: str) -> list[dict]:
        records = self._db.execute(
            "SELECT state, ts FROM device_events WHERE device_key = ? ORDER BY ts DESC, id DESC LIMIT 5",
            (device_key,),
        ).fetchall()
        return [{"state": record["state"], "ts": iso_et(record["ts"])} for record in reversed(records)]

    def _insert_event(self, row: dict, now: float) -> None:
        self._db.execute(
            "INSERT INTO device_events (device_key, name, udid, state, ts) VALUES (?, ?, ?, ?, ?)",
            (row["device_key"], row.get("name") or "", row.get("udid") or "", row.get("state") or "", now),
        )

    def _save_row(self, row: dict) -> None:
        self._db.execute(
            """
            INSERT INTO device_state (
                device_key, name, udid, coredevice_id, model, os, state, since, first_seen,
                had_offline, offline_span_s, status, status_since, reasons, eligible,
                quiet_hours_active, health_json, health_at, lock_episode, profile_key, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_key) DO UPDATE SET
                name = excluded.name,
                udid = excluded.udid,
                coredevice_id = excluded.coredevice_id,
                model = excluded.model,
                os = excluded.os,
                state = excluded.state,
                since = excluded.since,
                first_seen = excluded.first_seen,
                had_offline = excluded.had_offline,
                offline_span_s = excluded.offline_span_s,
                status = excluded.status,
                status_since = excluded.status_since,
                reasons = excluded.reasons,
                eligible = excluded.eligible,
                quiet_hours_active = excluded.quiet_hours_active,
                health_json = excluded.health_json,
                health_at = excluded.health_at,
                lock_episode = excluded.lock_episode,
                profile_key = excluded.profile_key,
                updated_at = excluded.updated_at
            """,
            (
                row["device_key"], row.get("name") or "", row.get("udid") or "",
                row.get("coredevice_id") or "", row.get("model") or "", row.get("os") or "",
                row.get("state") or "", row["since"], row["first_seen"], int(row.get("had_offline") or 0),
                float(row.get("offline_span_s") or 0), row.get("status") or "", row.get("status_since") or row["since"],
                json.dumps(row.get("reasons") or []), 1 if row.get("eligible") else 0,
                1 if row.get("quiet_hours_active") else 0,
                json.dumps(row["health"]) if isinstance(row.get("health"), dict) else "",
                row.get("health_at"), row.get("lock_episode") or "", row.get("profile_key") or "",
                row.get("updated_at") or row["since"],
            ),
        )

    def _open_episode(self, row: dict, label: str, message: str, command: str, now: float, notifier) -> None:
        if row.get("lock_episode"):
            return
        episode = "%s:%d" % (row["device_key"], int(now))
        cur = self._db.execute(
            "INSERT OR IGNORE INTO notify_events (device_key, episode, name, message, ts) VALUES (?, ?, ?, ?, ?)",
            (row["device_key"], episode, label, message, now),
        )
        if cur.rowcount:
            row["lock_episode"] = episode
            notifier(command, message)

    def observe(self, items, cfg, now: float, runner=None, work_dir: Path | None = None, notifier=None) -> None:
        """Record transitions for this poll and attach pool fields onto `items`.

        At most one health check runs, and only for a time-ready candidate that
        is not ACTIVE. A failed check leaves a good cache in place.
        """
        runner = default_health if runner is None else runner
        notifier = emit_notify if notifier is None else notifier
        profiles = profiles_from(cfg)
        command = notify_command(cfg)
        when = datetime.fromtimestamp(now, ET)
        seen = []
        with self._lock:
            rows = self._load_rows()
            self._db.execute("DELETE FROM device_events WHERE ts < ?", (now - RETAIN_S,))
            for item in items or []:
                if not isinstance(item, dict) or item.get("placeholder"):
                    continue
                key = storage_key(item)
                state = str(item.get("state") or "unavailable")
                row = rows.get(key)
                changed = False
                if row is None:
                    row = _blank_row(key, now, state)
                    changed = True
                    rows[key] = row
                else:
                    changed = _apply_state(row, state, now)
                profile_key, profile = profile_for(item, profiles)
                row["name"] = str(item.get("name") or row.get("name") or "")
                row["udid"] = str(item.get("udid") or "")
                row["coredevice_id"] = str(item.get("identifier") or "")
                row["model"] = str(item.get("model") or "")
                row["os"] = str(item.get("os_label") or item.get("os") or "")
                row["profile_key"] = profile_key
                row["updated_at"] = now
                if changed:
                    self._insert_event(row, now)
                seen.append((item, row, profile))
            seen_keys = {row["device_key"] for _item, row, _profile in seen}
            for key, row in rows.items():
                if key in seen_keys:
                    continue
                if _apply_state(row, "unavailable", now):
                    row["updated_at"] = now
                    self._insert_event(row, now)
                    self._finish(row, cfg, now, when, command, notifier, profiles)
                    self._save_row(row)
            prepared = []
            for item, row, profile in seen:
                decision = self._classify_row(item, row, profile, when, now)
                prepared.append({
                    "item": item,
                    "row": row,
                    "profile": profile,
                    "decision": decision,
                    "query_id": query_id(item),
                })
            pending = [
                entry for entry in prepared
                if entry["decision"].get("needs_check") and entry["query_id"] and not item_active(entry["item"])
            ]
            pending.sort(key=lambda entry: (entry["row"].get("health_at") is not None, entry["row"].get("health_at") or 0))
            if pending and work_dir is not None:
                chosen = pending[0]
                try:
                    result = runner(chosen["query_id"], Path(work_dir))
                except Exception:
                    result = None
                if isinstance(result, dict):
                    chosen["row"]["health"] = result
                    chosen["row"]["health_at"] = now
                    chosen["decision"] = self._classify_row(
                        chosen["item"], chosen["row"], chosen["profile"], when, now
                    )
            for entry in prepared:
                self._apply_decision(entry["row"], entry["decision"], entry["profile"], entry["item"], command, now, notifier)
                self._paint(entry["item"], entry["row"], entry["profile"])
                self._save_row(entry["row"])
            self._db.commit()

    def _finish(self, row, cfg, now, when, command, notifier, profiles) -> None:
        profile = profiles.get(row.get("profile_key") or "") if row.get("profile_key") else {}
        if not isinstance(profile, dict):
            profile = {}
        note = quiet_note(profile.get("quiet_hours"), when) if profile else None
        decision = classify(False, False, True, 0, 0, note, None, row.get("health"), False, row.get("status") or "")
        self._apply_decision(row, decision, profile, {"name": row.get("name") or "", "tile_name": ""}, command, now, notifier)

    def _classify_row(self, item, row, profile, when, now) -> dict:
        state = row.get("state") or ""
        reachable = state in REACHABLE
        note = quiet_note((profile or {}).get("quiet_hours"), when) if profile else None
        if reachable:
            available_s = max(0.0, now - float(row.get("since") or now))
        else:
            available_s = 0.0
        health_at = row.get("health_at")
        fresh = health_at is not None and (now - float(health_at)) < HEALTH_INTERVAL_S
        paired = paired_ok(
            str(item.get("pairing_state") or ""),
            str(item.get("trust_state") or ""),
            state,
        )
        return classify(
            reachable,
            bool(item.get("active")),
            bool(row.get("had_offline")),
            float(row.get("offline_span_s") or 0),
            available_s,
            note,
            paired,
            row.get("health") if isinstance(row.get("health"), dict) else None,
            fresh,
            row.get("status") or "",
        )

    def _apply_decision(self, row, decision, profile, item, command, now, notifier) -> None:
        status = decision.get("status") or ""
        if status != (row.get("status") or ""):
            row["status_since"] = now
        row["status"] = status
        row["reasons"] = list(decision.get("reasons") or [])
        row["eligible"] = 1 if decision.get("eligible") and status == "In test pool" else 0
        row["quiet_hours_active"] = 1 if decision.get("quiet") else 0
        episode = decision.get("episode") or "end"
        if status == "Needs unlock":
            label = device_label(item, profile or {})
            message = "%s needs unlocking to join the test pool" % label
            self._open_episode(row, label, message, command, now, notifier)
        elif episode == "end":
            row["lock_episode"] = ""

    def _paint(self, item, row, profile) -> None:
        label = device_label(item, profile or {})
        status = row.get("status") or ""
        item["pool_status"] = status
        item["pool_eligible"] = bool(row.get("eligible")) and status == "In test pool"
        item["pool_reasons"] = list(row.get("reasons") or [])
        item["pool_since"] = iso_et(row.get("status_since"))
        item["pool_quiet"] = bool(row.get("quiet_hours_active"))
        item["pool_events"] = self._events(row["device_key"])
        item["pool_name"] = label
        episode = row.get("lock_episode") or ""
        show = bool(episode) and (status == "Needs unlock" or status.startswith("Quiet hours:"))
        item["pool_episode"] = episode if show else ""
        item["pool_banner"] = ("%s needs unlocking to join the test pool" % label) if show else ""

    def attach_placeholders(self, devices, cfg, now: float) -> None:
        """Fill pool fields for tiles whose phone is absent from this poll."""
        profiles = profiles_from(cfg)
        when = datetime.fromtimestamp(now, ET)
        with self._lock:
            for dev in devices or []:
                if not isinstance(dev, dict) or dev.get("pool_status"):
                    continue
                if not dev.get("placeholder"):
                    continue
                profile_key, profile = profile_for(dev, profiles)
                row = None
                if profile_key:
                    record = self._db.execute(
                        "SELECT device_key FROM device_state WHERE profile_key = ? ORDER BY updated_at DESC LIMIT 1",
                        (profile_key,),
                    ).fetchone()
                    if record:
                        row = self._load_rows().get(record["device_key"])
                if row and (row.get("state") or "") not in REACHABLE:
                    self._paint(dev, row, profile)
                    continue
                note = quiet_note((profile or {}).get("quiet_hours"), when) if profile else None
                dev["pool_status"] = "Offline"
                dev["pool_eligible"] = False
                dev["pool_reasons"] = ["offline"]
                dev["pool_since"] = ""
                dev["pool_quiet"] = bool(note)
                dev["pool_events"] = self._events(row["device_key"]) if row else []
                dev["pool_name"] = device_label(dev, profile or {})
                dev["pool_episode"] = ""
                dev["pool_banner"] = ""

    def notify_count(self, device_key: str | None = None) -> int:
        with self._lock:
            if device_key:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM notify_events WHERE device_key = ?",
                    (device_key,),
                ).fetchone()
            else:
                row = self._db.execute("SELECT COUNT(*) AS n FROM notify_events").fetchone()
            return int(row["n"])


def item_active(item: dict) -> bool:
    return bool(item.get("active"))


def public_devices(devices, now=None) -> list[dict]:
    """JSON rows for GET /api/test-pool. No device commands."""
    rows = []
    for dev in devices or []:
        if not isinstance(dev, dict):
            continue
        reachable = (not dev.get("placeholder")) and (dev.get("state") in REACHABLE)
        status = dev.get("pool_status") or ""
        if not status:
            if not reachable:
                status = "Offline"
            elif dev.get("active"):
                status = "Active"
            else:
                status = "Idle"
        reasons = dev.get("pool_reasons") if isinstance(dev.get("pool_reasons"), list) else []
        since = dev.get("pool_since") or ""
        rows.append({
            "name": dev.get("tile_name") or dev.get("pool_name") or dev.get("name") or "",
            "udid": dev.get("udid") or "",
            "coredevice_id": dev.get("identifier") or "",
            "model": dev.get("model") or "",
            "os": dev.get("os_label") or dev.get("os") or "",
            "status": status,
            "eligible": bool(dev.get("pool_eligible")) and status == "In test pool",
            "reasons": [str(item) for item in reasons],
            "since": since,
            "since_label": since_label(since, now),
            "quiet_hours_active": bool(dev.get("pool_quiet")),
        })
    return rows
