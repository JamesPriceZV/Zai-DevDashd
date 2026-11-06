"""Hardware freshness is independent of healthy process polling."""
import json
import threading
from zai_devdashd import aggregator, hwinfo, probes


def test_failed_probe_keeps_original_clock_and_marks_retained_values(monkeypatch):
    engine = object.__new__(aggregator.Engine)
    engine.state_lock = threading.Lock()
    engine.hw_topo = {"mini": {}, "worker": {}}
    engine.hw_topo_at = {"mini": 1000, "worker": 1000}
    engine.hw_history = {}
    engine.hw_probe = {host: {"as_of": 990, "core_busy": [25], "disks": [{"name": "Startup", "as_of": 989}]}
                       for host in ("mini", "worker")}
    engine.worker_target = lambda _flag: "worker"
    monkeypatch.setattr(aggregator.time, "time", lambda: 1001)
    monkeypatch.setattr(probes, "_run_cmd", lambda *args: (1, b"", b"SENSITIVE_EXCEPTION_TEXT"))
    monkeypatch.setattr(probes, "worker_socket_path", lambda: __import__("pathlib").Path("/synthetic/worker.sock"))
    engine.job_hwdetail()
    for host in ("mini", "worker"):
        value = engine.hw_probe[host]
        assert value["as_of"] == 990 and value["attempted_at"] == 1001
        assert value["stale"] and value["error"] == "Hardware inspection unavailable"
        card = hwinfo.mac_card(value, {})
        assert card["stale"] and card["as_of"] == 990
        assert card["disks"][0]["stale"] and card["disks"][0]["as_of"] == 989
        assert card["disks_status"]["partial"]
    assert "SENSITIVE_EXCEPTION_TEXT" not in json.dumps(engine.hw_probe)


def test_successful_probe_clears_failed_inspection_and_ages_independently(monkeypatch):
    engine = object.__new__(aggregator.Engine)
    engine.state_lock = threading.Lock()
    engine.hw_topo = {"mini": {}, "worker": {}}
    engine.hw_topo_at = {"mini": 1000, "worker": 1000}
    engine.hw_history = {}
    engine.hw_probe = {"mini": {"as_of": 990, "stale": True, "error": "old"}}
    engine.worker_target = lambda _flag: "worker"
    clock = [1001]
    monkeypatch.setattr(aggregator.time, "time", lambda: clock[0])
    monkeypatch.setattr(probes, "_run_cmd", lambda *args: (0, b'HWJSON {"ok":true,"core_busy":[50]}', b""))
    monkeypatch.setattr(probes, "worker_socket_path", lambda: __import__("pathlib").Path("/synthetic/worker.sock"))
    engine.job_hwdetail()
    value = engine.hw_probe["mini"]
    assert value["as_of"] == 1001 and value["error"] is None and not value["stale"]
    assert not hwinfo.mac_card(value, {})["stale"]
    clock[0] = 1047
    assert hwinfo.mac_card(value, {})["stale"]
    assert value["as_of"] == 1001 and value["stale"] is False


def test_publish_ignores_age_only_updates_but_sends_stale_and_source_changes(monkeypatch):
    engine = object.__new__(aggregator.Engine)
    engine.cv = threading.Condition()
    engine.version = 0
    engine.payload = b"{}"
    engine._content_key = ""
    engine._content_at = 0
    clock = [100.0]
    source = {"observed_at": 90, "stale": False}
    monkeypatch.setattr(aggregator.time, "time", lambda: clock[0])
    engine.build = lambda: {
        "server_time": str(clock[0]),
        "executive_dashboard": {"generated_at": clock[0],
                                "machines": [{"age_s": clock[0] - source["observed_at"], **source}]}}
    engine.publish()
    assert engine.version == 1
    clock[0] = 102.5
    engine.publish()
    assert engine.version == 1
    source["stale"] = True
    engine.publish()
    assert engine.version == 2
    clock[0] = 118
    engine.publish()
    assert engine.version == 3
    source["observed_at"] = 91
    engine.publish()
    assert engine.version == 4


import pytest
from zai_devdashd import config as _site

@pytest.fixture(autouse=True)
def _synthetic_adapter_configuration(monkeypatch):
    monkeypatch.setitem(_site.CURRENT, "adapters", {"hardware_detail": True, "remote": True})
    monkeypatch.setitem(_site.CURRENT, "github", {"repo": "example/project", "integration_branch": "main"})
