import copy
import threading
from zai_devdashd import aggregator as agg
from zai_devdashd import config as site


def test_current_live_job_drives_host_and_active_cards_consistently(monkeypatch):
    engine = object.__new__(agg.Engine)
    engine.config = copy.deepcopy(site.DEFAULT_CONFIG)
    engine.state_lock = threading.Lock()
    engine.resolved_ip = ""
    engine.rfb_names = []
    engine.hw_probe = {}
    engine.hw_topo = {}
    engine.hw_history = {}
    engine.pool = None
    engine.vnc_connect = lambda *args: None
    row = {"kind": "CodeRabbit", "what": "CodeRabbit CLI preview", "pr": "", "head": "", "step": "reviewing", "when": "started now", "pid": 123}
    monkeypatch.setattr(agg.live_work, "collect", lambda *args: [row])
    monkeypatch.setattr(agg.accomplishments.CACHE, "get", lambda: None)
    snap = {"mini": {"active": False}, "worker": {}, "devices": {"items": []}, "pipelines": {"runs": []}, "lanes": {"items": []}, "github": {"prs": []}}
    engine.annotate_overview(snap)
    assert snap["mini"]["active"] is True
    assert any(j["id"] == "live-123" for j in snap["active"]["jobs"])
    assert all(j["goal"].startswith("Goal:") for j in snap["active"]["jobs"] if j.get("goal"))
    tile = next(t for t in snap["overview"]["tiles"] if t["id"] == "mini")
    assert tile["status"] != "idle"


def test_stale_cached_process_is_not_a_current_active_card(monkeypatch):
    engine = object.__new__(agg.Engine)
    engine.config = copy.deepcopy(site.DEFAULT_CONFIG)
    engine.state_lock = threading.Lock()
    engine.resolved_ip = ""
    engine.rfb_names = []
    engine.hw_probe = {}
    engine.hw_topo = {}
    engine.hw_history = {}
    engine.pool = None
    engine.vnc_connect = lambda *args: None
    row = {"kind": "CodeRabbit", "what": "CodeRabbit CLI preview", "pr": "", "head": "", "step": "last known", "when": "old", "pid": 123, "stale": True}
    monkeypatch.setattr(agg.live_work, "collect", lambda *args: [row])
    monkeypatch.setattr(agg.accomplishments.CACHE, "get", lambda: None)
    snap = {"mini": {"active": False}, "worker": {}, "devices": {"items": []}, "pipelines": {"runs": []}, "lanes": {"items": []}, "github": {"prs": []}}
    engine.annotate_overview(snap)
    assert not snap["active"]["jobs"]
    assert snap["overview"]["working"][0]["tone"] == "wait"
