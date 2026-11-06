import threading
import time
from types import SimpleNamespace
from zai_devdashd import probes, aggregator


def test_expired_github_phase_recovers_without_refreshing_success_time(monkeypatch):
    calls = []
    engine = SimpleNamespace(gh_phase="comments", gh_cycle=100, gh_i=7, gh_backoff_until=0, gh_as_of=90)
    engine.set_source_error = lambda *args: calls.append("error")
    engine.github_rate = lambda: calls.append("rate") or 0
    monkeypatch.setattr(probes.time, "time", lambda: 161)
    assert probes.ProbeMixin.job_github(engine) == 5
    assert engine.gh_phase == "rate" and engine.gh_i == 0 and engine.gh_as_of == 90
    assert probes.ProbeMixin.job_github(engine) == 0
    assert calls == ["error", "rate"]


def test_recovered_github_phase_still_respects_backoff(monkeypatch):
    engine = SimpleNamespace(gh_phase="rate", gh_cycle=100, gh_i=0, gh_backoff_until=500)
    engine.github_rate = lambda: (_ for _ in ()).throw(AssertionError("must respect backoff"))
    monkeypatch.setattr(probes.time, "time", lambda: 200)
    assert probes.ProbeMixin.job_github(engine) == 30


def test_slow_usage_does_not_block_polling_or_launch_duplicate_collectors(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def collect(**kwargs):
        calls.append(kwargs)
        entered.set()
        release.wait(2)
        return {"rows": ["fresh"], "as_of": 123}
    monkeypatch.setattr(aggregator.site, "adapter_enabled", lambda name: name == "usage")
    engine = SimpleNamespace()
    engine.state_lock = threading.Lock()
    engine.mini_commands = []
    engine.usage_refreshing = False
    engine.usage_block = {"rows": ["previous"], "as_of": 90}
    engine.usage_collector = SimpleNamespace(collect=collect)
    started = time.monotonic()
    try:
        assert aggregator.Engine.job_usage(engine) == 60
        assert time.monotonic() - started < 0.5
        assert entered.wait(1)
        aggregator.Engine.job_usage(engine)
        assert len(calls) == 1 and engine.usage_block["as_of"] == 90
    finally:
        release.set()
        engine.usage_thread.join(2)
    assert engine.usage_block["as_of"] == 123 and not engine.usage_refreshing


def test_failed_usage_keeps_previous_snapshot_and_releases_admission(monkeypatch):
    def collect(**kwargs):
        raise RuntimeError("fixture failure")
    monkeypatch.setattr(aggregator.site, "adapter_enabled", lambda name: name == "usage")
    monkeypatch.setattr(aggregator, "log_exc", lambda *args: None)
    engine = SimpleNamespace()
    engine.state_lock = threading.Lock()
    engine.mini_commands = []
    engine.usage_refreshing = False
    engine.usage_block = {"as_of": 90}
    engine.usage_collector = SimpleNamespace(collect=collect)
    aggregator.Engine.job_usage(engine)
    engine.usage_thread.join(2)
    assert engine.usage_block == {"as_of": 90} and not engine.usage_refreshing
