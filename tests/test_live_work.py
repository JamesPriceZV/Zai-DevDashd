from datetime import datetime
import subprocess

from zai_devdashd import live_work as live

NOW = datetime(2026, 11, 3, 20, 0, tzinfo=live.ET)


def proc(pid, ppid, cmd):
    return {"pid": pid, "ppid": ppid, "cmd": cmd, "start": NOW}


def test_native_builds_are_reported_without_guessing_progress():
    rows = live._native_build_rows([
        proc(10, 1, "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild build-for-testing -derivedDataPath /scratch/foundations"),
        proc(11, 1, "/bin/bash -c echo xcodebuild build"),
    ], NOW)
    assert len(rows) == 1 and rows[0]["pid"] == 10
    assert rows[0]["step"] == "xcodebuild build-for-testing"
    assert rows[0]["progress"] is None
    assert "foundations" in rows[0]["what"]


def test_native_build_already_represented_by_parent_is_not_duplicated():
    procs = [proc(5, 1, "/bin/bash gate.sh"), proc(6, 5, "/bin/bash build-slot.sh"),
             proc(7, 6, "/usr/bin/xcodebuild build -derivedDataPath /scratch/gate")]
    assert live._native_build_rows(procs, NOW, [{"pid": 5}]) == []
    assert len(live._native_build_rows(procs, NOW, [{"pid": 99}])) == 1


def test_absolute_autotune_script_is_recognized_and_uses_its_own_run(monkeypatch):
    procs = [proc(10, 1, "/Applications/Xcode.app/Python /scratch/actual/autotune.py"),
             proc(11, 1, "/bin/sh -c echo Python autotune.py")]
    rows = live._autotune_rows(procs, NOW)
    assert len(rows) == 1 and rows[0]["pid"] == 10
    assert "actual" in rows[0]["what"]


def test_failed_process_inspection_preserves_stale_rows_and_warns(monkeypatch):
    previous = {"kind": "Native build", "what": "Native build", "pr": "", "head": "", "step": "xcodebuild build", "when": "", "pid": 10}
    monkeypatch.setattr(live, "_cache", {"t": 0, "rows": [previous], "error": None})
    monkeypatch.setattr(live, "_ps", lambda: (_ for _ in ()).throw(live.InspectionUnavailable()))
    rows = live.collect([])
    assert rows[0]["pid"] == 10 and rows[0]["stale"] is True
    assert rows[-1]["kind"] == "Status unavailable"
    assert live.collect([])[-1]["kind"] == "Status unavailable"


def test_ps_failure_is_not_an_empty_success(monkeypatch):
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "private stderr"))
    try:
        live._ps()
    except live.InspectionUnavailable as exc:
        assert "private" not in str(exc)
    else:
        raise AssertionError("failed ps was accepted")
