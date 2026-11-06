"""Test-pool eligibility. The clock is injected. No devicectl calls."""

import io
import json
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from zai_devdashd import config as site
from zai_devdashd import pool

ET = ZoneInfo("America/New_York")
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def example_config() -> dict:
    return {"devices": {key: {"label": label, "quiet_hours": [{"start": "20:00", "end": "24:00", "tz": "America/New_York", "note": "Reserved example window"}]}
                        for key, label in (("s103", "Phone103"), ("s104", "Phone104"))}, "pool": {"notify_command": ""}}


def at(year, month, day, hour, minute) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=ET).timestamp()


def phone(name, udid, state, **extra) -> dict:
    item = {
        "name": name,
        "tile_name": "",
        "udid": udid,
        "identifier": "CORE-" + udid,
        "state": state,
        "model": "iPhone",
        "os_label": "iOS 27",
        "pairing_state": "paired",
        "trust_state": "",
        "active": False,
    }
    item.update(extra)
    return item


def healthy(_device_id, _work_dir):
    return {"developer_mode": True, "lock": "unlocked", "battery_pct": 80, "charging": False}


def ready_store(tmp_path, name, udid, moment, runner, cfg=None):
    cfg = example_config() if cfg is None else cfg
    store = pool.PoolStore(tmp_path / (udid + ".db"))
    item = phone(name, udid, "connected")
    store.observe([item], cfg, moment - pool.AVAILABLE_S, runner=runner, work_dir=tmp_path)
    store.observe([item], cfg, moment, runner=runner, work_dir=tmp_path)
    return store, item


def test_quiet_hours_round_trip_from_example():
    parsed = example_config()
    for key in ("s103", "s104"):
        window = parsed["devices"][key]["quiet_hours"][0]
        assert window == {
            "start": "20:00",
            "end": "24:00",
            "tz": "America/New_York",
            "note": "Reserved example window",
        }
    assert parsed["devices"]["s103"]["label"] == "Phone103"
    assert parsed["devices"]["s104"]["label"] == "Phone104"
    assert parsed["pool"]["notify_command"] == ""
    again = site.parse_toml(site.dump_toml(parsed))
    assert again["devices"]["s104"]["quiet_hours"][0]["note"] == "Reserved example window"
    assert again["devices"]["s103"]["quiet_hours"][0]["tz"] == "America/New_York"
    kept = site._sanitize(site._merge(site.DEFAULT_CONFIG, parsed))
    saved = site.apply_settings(kept, {"devices_interval": "45"})
    dumped = site.parse_toml(site.dump_toml(site._sanitize(site._merge(site.DEFAULT_CONFIG, saved))))
    assert dumped["devices"]["s103"]["quiet_hours"][0]["start"] == "20:00"
    assert dumped["devices"]["s104"]["quiet_hours"][0]["end"] == "24:00"
    assert dumped["pool"]["notify_command"] == ""


def test_transitions_keep_last_five(tmp_path):
    store = pool.PoolStore(tmp_path / "events.db")
    cfg = example_config()
    states = [
        "connected",
        "available (paired)",
        "unavailable",
        "connected",
        "available (paired)",
        "unavailable",
    ]
    item = phone("Solaritablet105", "UDID307", states[0])
    start = 1_700_000_000
    for index, state in enumerate(states):
        item["state"] = state
        store.observe([item], cfg, start + index * 10, runner=healthy, work_dir=tmp_path)
    assert [event["state"] for event in item["pool_events"]] == states[1:]
    assert item["pool_events"][0]["ts"].endswith("-05:00") or item["pool_events"][0]["ts"].endswith("-04:00")


def test_event_log_keeps_thirty_days(tmp_path):
    store = pool.PoolStore(tmp_path / "retain.db")
    cfg = example_config()
    now = 1_800_000_000.0
    store._db.execute(
        "INSERT INTO device_events (device_key, name, udid, state, ts) VALUES (?, ?, ?, ?, ?)",
        ("udid:OLD", "old", "OLD", "connected", now - 31 * 86400),
    )
    store._db.execute(
        "INSERT INTO device_events (device_key, name, udid, state, ts) VALUES (?, ?, ?, ?, ?)",
        ("udid:KEEP", "keep", "KEEP", "connected", now - 29 * 86400),
    )
    store._db.commit()
    store.observe([phone("Solaritablet105", "UDID307", "connected")], cfg, now, runner=healthy, work_dir=tmp_path)
    old = store._db.execute("SELECT COUNT(*) AS n FROM device_events WHERE device_key = ?", ("udid:OLD",)).fetchone()["n"]
    kept = store._db.execute("SELECT COUNT(*) AS n FROM device_events WHERE device_key = ?", ("udid:KEEP",)).fetchone()["n"]
    assert old == 0
    assert kept == 1


def test_offline_threshold_is_ten_minutes(tmp_path):
    cfg = example_config()

    def drive(span):
        store = pool.PoolStore(tmp_path / ("off-%s.db" % span))
        item = phone("Solaritablet105", "U" + str(span), "unavailable")
        store.observe([item], cfg, 10_000, runner=healthy, work_dir=tmp_path)
        item["state"] = "connected"
        store.observe([item], cfg, 10_000 + span, runner=healthy, work_dir=tmp_path)
        store.observe([item], cfg, 10_000 + span + pool.AVAILABLE_S, runner=healthy, work_dir=tmp_path)
        return item

    short = drive(pool.OFFLINE_S - 1)
    assert short["pool_status"] == ""
    assert short["pool_eligible"] is False
    long = drive(pool.OFFLINE_S)
    assert long["pool_status"] == "In test pool"
    assert long["pool_eligible"] is True


def test_available_threshold_is_two_minutes(tmp_path):
    cfg = example_config()
    store = pool.PoolStore(tmp_path / "avail.db")
    item = phone("Solaritablet105", "UDID-AVAIL", "connected")
    store.observe([item], cfg, 50_000, runner=healthy, work_dir=tmp_path)
    store.observe([item], cfg, 50_000 + pool.AVAILABLE_S - 1, runner=healthy, work_dir=tmp_path)
    assert item["pool_status"] == ""
    store.observe([item], cfg, 50_000 + pool.AVAILABLE_S, runner=healthy, work_dir=tmp_path)
    assert item["pool_status"] == "In test pool"


def test_startup_without_offline_history_uses_first_seen(tmp_path):
    _store, item = ready_store(tmp_path, "Solaritablet105", "UDID-FIRST", 80_000, healthy)
    assert item["pool_status"] == "In test pool"
    assert "offline" not in " ".join(item["pool_reasons"])


def test_quiet_hours_s103_and_s104_are_dst_safe(tmp_path):
    parsed = example_config()
    winter = datetime(2026, 1, 15, 20, 0, tzinfo=ET)
    summer = datetime(2026, 7, 15, 20, 0, tzinfo=ET)
    assert winter.utcoffset() != summer.utcoffset()
    samples = (
        (2026, 1, 15, 19, 59, None),
        (2026, 1, 15, 20, 0, "Reserved example window"),
        (2026, 1, 15, 23, 59, "Reserved example window"),
        (2026, 1, 16, 0, 0, None),
        (2026, 7, 15, 19, 59, None),
        (2026, 7, 15, 20, 0, "Reserved example window"),
        (2026, 7, 15, 23, 59, "Reserved example window"),
        (2026, 7, 16, 0, 0, None),
    )
    for key in ("s103", "s104"):
        windows = parsed["devices"][key]["quiet_hours"]
        for year, month, day, hour, minute, note in samples:
            found = pool.quiet_note(windows, datetime(year, month, day, hour, minute, tzinfo=ET))
            assert found == note, (key, hour, minute, found)
    calls = []

    def counting(device_id, work_dir):
        calls.append(device_id)
        return healthy(device_id, work_dir)

    for name, udid in (("Phone103", "SYNTHETIC-103"), ("Phone104", "SYNTHETIC-104")):
        store = pool.PoolStore(tmp_path / (udid + "-quiet.db"))
        item = phone(name, udid, "connected")
        moment = at(2026, 1, 15, 19, 59)
        store.observe([item], parsed, moment - pool.AVAILABLE_S, runner=counting, work_dir=tmp_path)
        before = len(calls)
        store.observe([item], parsed, moment, runner=counting, work_dir=tmp_path)
        assert item["pool_status"] == "In test pool"
        assert item["pool_quiet"] is False
        store.observe([item], parsed, at(2026, 1, 15, 20, 0), runner=counting, work_dir=tmp_path)
        assert item["pool_status"] == "Quiet hours: Reserved example window"
        assert item["pool_quiet"] is True
        assert item["pool_eligible"] is False
        assert len(calls) == before + 1
        store.observe([item], parsed, at(2026, 1, 15, 23, 59), runner=counting, work_dir=tmp_path)
        assert item["pool_status"] == "Quiet hours: Reserved example window"
        store.observe([item], parsed, at(2026, 1, 16, 0, 0), runner=counting, work_dir=tmp_path)
        assert item["pool_status"] == "In test pool"
        summer_at = at(2026, 7, 15, 20, 0)
        store.observe([item], parsed, summer_at, runner=counting, work_dir=tmp_path)
        assert item["pool_status"] == "Quiet hours: Reserved example window"


def test_lock_episode_notifies_once_and_survives_quiet_hours(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("empty notify command must not spawn a process")

    monkeypatch.setattr(pool.subprocess, "run", boom)
    sent = []

    def notifier(command, message):
        sent.append((command, message))

    def locked(_device_id, _work_dir):
        return {"developer_mode": True, "lock": "locked", "battery_pct": 90, "charging": True}

    cfg = example_config()
    store = pool.PoolStore(tmp_path / "lock.db")
    item = phone("Phone103", "SYNTHETIC-103", "connected")
    moment = at(2026, 1, 15, 19, 50)
    store.observe([item], cfg, moment - pool.AVAILABLE_S, runner=healthy, work_dir=tmp_path, notifier=notifier)
    store.observe([item], cfg, moment, runner=locked, work_dir=tmp_path, notifier=notifier)
    assert item["pool_status"] == "Needs unlock"
    assert item["pool_banner"] == "Phone103 needs unlocking to join the test pool"
    assert store.notify_count() == 1
    assert sent == [("", "Phone103 needs unlocking to join the test pool")]
    store.observe([item], cfg, moment + 30, runner=locked, work_dir=tmp_path, notifier=notifier)
    assert store.notify_count() == 1
    assert len(sent) == 1
    store.observe([item], cfg, at(2026, 1, 15, 20, 0), runner=locked, work_dir=tmp_path, notifier=notifier)
    assert item["pool_status"] == "Quiet hours: Reserved example window"
    assert item["pool_banner"] == "Phone103 needs unlocking to join the test pool"
    assert store.notify_count() == 1
    store.observe([item], cfg, at(2026, 1, 16, 0, 0), runner=locked, work_dir=tmp_path, notifier=notifier)
    assert item["pool_status"] == "Needs unlock"
    assert store.notify_count() == 1
    unlocked_at = at(2026, 1, 16, 0, 0) + pool.HEALTH_INTERVAL_S
    store.observe([item], cfg, unlocked_at, runner=healthy, work_dir=tmp_path, notifier=notifier)
    assert item["pool_status"] == "In test pool"
    assert item["pool_banner"] == ""
    relock_at = unlocked_at + pool.HEALTH_INTERVAL_S
    store.observe([item], cfg, relock_at, runner=locked, work_dir=tmp_path, notifier=notifier)
    assert store.notify_count() == 2
    assert len(sent) == 2
    item["state"] = "unavailable"
    offline_at = relock_at + 10
    store.observe([item], cfg, offline_at, runner=locked, work_dir=tmp_path, notifier=notifier)
    assert item["pool_status"] == "Offline"
    assert item["pool_banner"] == ""
    assert store.notify_count() == 2
    item["state"] = "connected"
    back = offline_at + pool.OFFLINE_S
    store.observe([item], cfg, back, runner=locked, work_dir=tmp_path, notifier=notifier)
    store.observe([item], cfg, back + pool.AVAILABLE_S, runner=locked, work_dir=tmp_path, notifier=notifier)
    assert item["pool_status"] == "Needs unlock"
    assert store.notify_count() == 3
    assert sent[-1][1] == "Phone103 needs unlocking to join the test pool"


def test_failed_check_keeps_a_good_cache_and_unknowns_do_not_fail(tmp_path):
    cfg = example_config()
    store = pool.PoolStore(tmp_path / "health.db")
    item = phone("Solaritablet105", "UDID-H", "connected")
    start = 90_000

    def unknown(_device_id, _work_dir):
        return {"developer_mode": None, "lock": "unknown", "battery_pct": None, "charging": None}

    store.observe([item], cfg, start, runner=unknown, work_dir=tmp_path)
    ready = start + pool.AVAILABLE_S
    store.observe([item], cfg, ready, runner=unknown, work_dir=tmp_path)
    assert item["pool_status"] == "In test pool"

    def failed(_device_id, _work_dir):
        return None

    stale = ready + pool.HEALTH_INTERVAL_S
    store.observe([item], cfg, stale, runner=failed, work_dir=tmp_path)
    assert item["pool_status"] == "In test pool"

    def off(_device_id, _work_dir):
        return {"developer_mode": False, "lock": "unlocked", "battery_pct": 90, "charging": False}

    off_at = stale + pool.HEALTH_INTERVAL_S
    store.observe([item], cfg, off_at, runner=off, work_dir=tmp_path)
    assert item["pool_status"] == "Needs attention: Developer Mode is off"

    def low(_device_id, _work_dir):
        return {"developer_mode": True, "lock": "unlocked", "battery_pct": 20, "charging": False}

    low_at = off_at + pool.HEALTH_INTERVAL_S
    store.observe([item], cfg, low_at, runner=low, work_dir=tmp_path)
    assert item["pool_status"] == "Needs attention: battery under 30%"

    def plugged(_device_id, _work_dir):
        return {"developer_mode": True, "lock": "unlocked", "battery_pct": 20, "charging": True}

    store.observe([item], cfg, low_at + pool.HEALTH_INTERVAL_S, runner=plugged, work_dir=tmp_path)
    assert item["pool_status"] == "In test pool"

    fresh = pool.PoolStore(tmp_path / "nostatus.db")
    blank = phone("Solaritablet105", "UDID-BLANK", "connected")
    fresh.observe([blank], cfg, start, runner=failed, work_dir=tmp_path)
    fresh.observe([blank], cfg, start + pool.AVAILABLE_S, runner=failed, work_dir=tmp_path)
    assert blank["pool_status"] == "Candidate (checking)"


def test_active_device_skips_health_and_keeps_status(tmp_path):
    calls = []

    def counting(device_id, work_dir):
        calls.append(device_id)
        return healthy(device_id, work_dir)

    cfg = example_config()
    store = pool.PoolStore(tmp_path / "active.db")
    item = phone("Tablet101", "UDID337", "connected")
    store.observe([item], cfg, 1000, runner=counting, work_dir=tmp_path)
    store.observe([item], cfg, 1000 + pool.AVAILABLE_S, runner=counting, work_dir=tmp_path)
    assert item["pool_status"] == "In test pool"
    assert len(calls) == 1
    item["active"] = True

    def locked(_device_id, _work_dir):
        calls.append("locked")
        return {"developer_mode": True, "lock": "locked", "battery_pct": 80, "charging": False}

    store.observe([item], cfg, 1000 + pool.AVAILABLE_S + pool.HEALTH_INTERVAL_S, runner=locked, work_dir=tmp_path)
    assert calls == ["CORE-UDID337"]
    assert item["pool_status"] == "In test pool"


def test_one_health_check_per_poll_and_two_minute_cache(tmp_path):
    calls = []

    def counting(device_id, _work_dir):
        calls.append(device_id)
        return healthy(device_id, _work_dir)

    cfg = example_config()
    store = pool.PoolStore(tmp_path / "one.db")
    first = phone("Solaritablet105", "ONE", "connected")
    second = phone("Device173", "TWO", "connected")
    start = 5_000.0
    store.observe([first, second], cfg, start, runner=counting, work_dir=tmp_path)
    store.observe([first, second], cfg, start + pool.AVAILABLE_S, runner=counting, work_dir=tmp_path)
    assert len(calls) == 1
    assert first["pool_status"] == "In test pool"
    assert second["pool_status"] == "Candidate (checking)"
    store.observe([first, second], cfg, start + pool.AVAILABLE_S + 1, runner=counting, work_dir=tmp_path)
    assert len(calls) == 2
    assert second["pool_status"] == "In test pool"
    store.observe([first, second], cfg, start + pool.AVAILABLE_S + 30, runner=counting, work_dir=tmp_path)
    assert len(calls) == 2
    store.observe(
        [first, second], cfg, start + pool.AVAILABLE_S + pool.HEALTH_INTERVAL_S, runner=counting, work_dir=tmp_path,
    )
    assert len(calls) == 3


def test_untrusted_device_needs_attention(tmp_path):
    cfg = example_config()
    store = pool.PoolStore(tmp_path / "trust.db")
    item = phone("Solaritablet105", "UDID-TRUST", "connected", trust_state="untrusted")
    store.observe([item], cfg, 100, runner=healthy, work_dir=tmp_path)
    store.observe([item], cfg, 100 + pool.AVAILABLE_S, runner=healthy, work_dir=tmp_path)
    assert item["pool_status"] == "Needs attention: not paired"


def test_api_test_pool_requires_auth(monkeypatch):
    from types import SimpleNamespace
    from zai_devdashd import server
    from zai_devdashd.server import Handler

    # Exercise the HTTP auth path without creating a collector or observing hosts.
    monkeypatch.setattr(server, "ENGINE", SimpleNamespace(
        config={"allow_localhost_unauth": False},
        gate=SimpleNamespace(session=lambda token: None),
    ))
    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/api/test-pool"
    handler.requestline = "GET /api/test-pool HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.client_address = ("203.0.113.50", 9)
    handler.headers = EmailMessage()
    handler.close_connection = False
    handler.rfile = io.BytesIO()
    handler.wfile = io.BytesIO()
    handler.do_GET()
    status = handler.wfile.getvalue().split(b"\r\n", 1)[0]
    assert status == b"HTTP/1.1 401 Unauthorized"


def test_since_label_today_yesterday_and_dst():
    now = datetime(2026, 10, 29, 18, 23, tzinfo=ET)
    assert pool.since_label("2026-10-29T18:23:00-04:00", now) == "6:23 PM ET"
    assert pool.since_label("2026-10-28T18:23:00-04:00", now) == "Oct 28, 6:23 PM ET"
    assert pool.since_label("2026-10-29T09:05:00-04:00", now) == "9:05 AM ET"
    spring = datetime(2026, 3, 8, 12, 0, tzinfo=ET)
    before = datetime(2026, 3, 8, 1, 30, tzinfo=ET)
    after = datetime(2026, 3, 8, 3, 30, tzinfo=ET)
    assert before.utcoffset() != after.utcoffset()
    assert pool.since_label(before.isoformat(timespec="seconds"), spring) == "1:30 AM ET"
    assert pool.since_label(after.isoformat(timespec="seconds"), spring) == "3:30 AM ET"
    yesterday = datetime(2026, 3, 7, 18, 23, tzinfo=ET)
    assert pool.since_label(yesterday.isoformat(timespec="seconds"), spring) == "Mar 7, 6:23 PM ET"
    assert pool.since_label("", spring) == ""


def test_public_block_matches_the_api_shape():
    now = datetime(2026, 10, 29, 18, 23, tzinfo=ET)
    rows = pool.public_devices([{
        "tile_name": "Phone103",
        "udid": "UDID",
        "identifier": "CORE",
        "model": "iPhone 16 Pro",
        "os_label": "iOS 27.0",
        "state": "connected",
        "pool_status": "In test pool",
        "pool_eligible": True,
        "pool_reasons": [],
        "pool_since": "2026-01-15T18:00:00-05:00",
        "pool_quiet": False,
    }], now)
    assert rows == [{
        "name": "Phone103",
        "udid": "UDID",
        "coredevice_id": "CORE",
        "model": "iPhone 16 Pro",
        "os": "iOS 27.0",
        "status": "In test pool",
        "eligible": True,
        "reasons": [],
        "since": "2026-01-15T18:00:00-05:00",
        "since_label": "Jan 15, 6:00 PM ET",
        "quiet_hours_active": False,
    }]
    body = json.dumps({"generated_at": rows[0]["since"], "devices": rows})
    assert "eligible" in body


def test_extract_health_reads_nested_details_and_ignores_missing_battery():
    details = {
        "result": {
            "properties": {
                "developerModeStatus": "enabled",
                "battery": {"level": 0.4, "batteryState": "charging"},
            }
        }
    }
    lock_payload = {"result": {"lockState": "locked"}}
    health = pool.extract_health(details, lock_payload)
    assert health["developer_mode"] is True
    assert health["lock"] == "locked"
    assert health["battery_pct"] == 40
    assert health["charging"] is True
    sparse = pool.extract_health({"result": {}}, None)
    assert sparse["developer_mode"] is None
    assert sparse["lock"] == "unknown"
    assert sparse["battery_pct"] is None
