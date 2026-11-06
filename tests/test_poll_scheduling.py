from zai_devdashd.aggregator import oldest_due_job


def test_slow_short_cadence_probes_do_not_starve_linked_host():
    order = ["load", "mini", "remote"]
    deadlines = dict.fromkeys(order, 0)
    now = 0
    served = []
    durations = {"load": 8, "mini": 8, "remote": 3}
    intervals = {"load": 4, "mini": 5, "remote": 60}
    for _ in range(3):
        job = oldest_due_job(order, deadlines, now)
        served.append(job)
        now += durations[job]
        deadlines[job] = now + intervals[job]
    assert served == ["load", "mini", "remote"]


def test_poll_scheduler_waits_and_preserves_equal_deadline_order():
    order = ["mini", "remote"]
    assert oldest_due_job(order, {"mini": 10, "remote": 10}, 9) is None
    assert oldest_due_job(order, {"mini": 10, "remote": 10}, 10) == "mini"
    assert oldest_due_job(order, {"mini": 20, "remote": 10}, 20) == "remote"



def test_import_is_inert_and_lazy_singleton_forwards_shutdown_writes(monkeypatch):
    from zai_devdashd import aggregator
    assert aggregator.ENGINE.__dict__["_value"] is None
    created = []
    class InertEngine:
        stop = False
        def __init__(self): created.append(self)
    monkeypatch.setattr(aggregator, "Engine", InertEngine)
    proxy = aggregator._LazyEngine()
    assert created == []
    proxy.stop = True
    assert len(created) == 1 and created[0].stop is True and proxy.stop is True
    assert "stop" not in proxy.__dict__


def test_disabled_optional_jobs_return_before_using_any_host_or_collector(monkeypatch):
    from types import SimpleNamespace
    from zai_devdashd import aggregator, config
    monkeypatch.setitem(config.CURRENT, "adapters", {})
    inert = SimpleNamespace()
    for name in ("job_git", "job_usage", "job_remote", "job_hwdetail"):
        assert getattr(aggregator.Engine, name)(inert) == 60.0
    assert aggregator.Engine.poll_local(inert) is None


def test_fast_loop_does_not_observe_host_when_local_adapter_disabled(monkeypatch):
    from types import SimpleNamespace
    from zai_devdashd import aggregator, config
    monkeypatch.setitem(config.CURRENT, "adapters", {"local_system": False})
    def forbidden(): raise AssertionError("disabled host observer called")
    inert = SimpleNamespace(stop=False, sample_host_cpu=forbidden)
    inert.poll_local = lambda: aggregator.Engine.poll_local(inert)
    inert.publish = lambda: setattr(inert, "stop", True)
    monkeypatch.setattr(aggregator.time, "sleep", lambda _: None)
    aggregator.Engine.fast_loop(inert)
    assert inert.stop is True


def test_slow_loop_filters_all_disabled_jobs_before_dispatch(monkeypatch):
    from types import SimpleNamespace
    import threading
    from zai_devdashd import aggregator, config
    monkeypatch.setitem(config.CURRENT, "adapters", {"local_system": False})
    inert = SimpleNamespace(stop=False, state_lock=threading.Lock(), force_jobs=set())
    seen = []
    admitted = config.adapter_enabled
    def observe_adapter(name):
        seen.append(name)
        if len(seen) == 10: inert.stop = True
        return admitted(name)
    monkeypatch.setattr(config, "adapter_enabled", observe_adapter)
    aggregator.Engine.slow_loop(inert)
    assert set(seen) == {"local_system", "remote", "git", "devices", "github", "usage", "hardware_detail"}
