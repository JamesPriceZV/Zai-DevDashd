"""Collector bounds, cache independence and fail-closed evidence retention."""
import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from zai_devdashd import programs

NOW = 1793810000.0
LINEAGE = {key: digit * 40 for key, digit in zip(("qualified", "published", "merge", "docs"), "1234")}
GITHUB = {"repository": "example/product", "parent_pr": 11, "canonical_issue": 12, "pr": 13}


def write_registry(tmp_path, definitions):
    path = tmp_path / "programs.json"
    path.write_text(json.dumps({"schema_version": 1, "programs": definitions}))
    return path


def definition(tmp_path):
    return {"id": "checkpoint", "kind": "checkpoint_closeout_v1", "expected_checkout_pins": 3, "title": "Accepted checkpoint",
            "scope": "v1.x", "program": "Foundations", "receipt_root": str(tmp_path),
            "lineage": dict(LINEAGE, repository_path=str(tmp_path)), "github": GITHUB, "receipts": []}


def previous():
    return {"schema_version": 1, "state": "available", "observed_at": NOW - 20, "attempted_at": NOW - 20,
            "partial": False, "error": None,
            "coverage": {"registered": 1, "verified": 1, "pending": 0, "expected_receipts": 14,
                         "present_receipts": 14, "valid_receipts": 14, "errors": []},
            "checkpoints": [{"id": "checkpoint", "accepted": True, "state": "verified", "stale": False,
                             "lineage": dict(LINEAGE, verified=True), "facts": {"closures": {"issues": []}},
                             "github_identity": GITHUB, "event_id": "stable-event", "accepted_base": "foundation",
                             "accepted_at": NOW - 100, "occurred_at": NOW - 200,
                             "observed_at": NOW - 20, "verified_at": NOW - 20,
                             "current": {"state": "available", "stale": False, "observed_at": NOW - 25},
                             "evidence": [], "coverage": {"expected": 14, "present": 14, "valid": 14}}],
            "sources": [], "lanes": []}


def unavailable(*args, **kwargs):
    raise RuntimeError("SENSITIVE_CALLBACK_FAILURE")


def test_pending_adapter_preserves_inventory_without_reading_source(tmp_path, monkeypatch):
    path = write_registry(tmp_path, [{"id": "build-b", "kind": "pending_adapter", "title": "Build B",
                                     "owner": "Release integrator", "scope": "v1",
                                     "objective": "Qualify the current candidate",
                                     "sources": [{"kind": "controller_state", "path": "/unread/source.json"}]}])
    real = programs._read_json
    monkeypatch.setattr(programs, "_read_json",
                        lambda p, limit: real(p, limit) if Path(p) == path else pytest.fail("pending adapter read a source"))
    result = programs.collect(path, now=NOW, git_reader=unavailable, github_reader=unavailable)
    assert result["coverage"]["registered"] == result["coverage"]["pending"] == 1
    assert result["coverage"]["verified"] == 0 and result["partial"]
    row = result["checkpoints"][0]
    assert row["state"] == "pending" and row["accepted"] is False and row["facts"] == {}
    assert result["sources"][0]["owner"] == "Release integrator"


@pytest.mark.parametrize("body", [
    {"schema_version": True, "programs": []},
    {"schema_version": 2, "programs": []},
    {"schema_version": 1, "programs": [{"id": "same"}, {"id": "same"}]},
    {"schema_version": 1, "programs": [{"id": str(i)} for i in range(programs.MAX_PROGRAMS + 1)]},
])
def test_invalid_registry_never_reports_current_verified_zero_or_leaks_body(tmp_path, body):
    body["credential"] = "SENSITIVE_REGISTRY_BODY"
    path = tmp_path / "programs.json"
    path.write_text(json.dumps(body))
    result = programs.collect(path, now=NOW)
    assert result["state"] == "unavailable" and result["coverage"]["verified"] is None
    assert "SENSITIVE" not in json.dumps(result)


def test_global_registry_failure_separates_historic_coverage_and_original_current_clock(tmp_path):
    old = previous()
    result = programs.collect(tmp_path / "missing.json", now=NOW, previous=old)
    assert result["coverage"]["verified"] is None
    assert result["historic_coverage"]["coverage"]["verified"] == 1
    assert result["historic_coverage"]["observed_at"] == old["observed_at"]
    row = result["checkpoints"][0]
    assert row["accepted"] is True and row["stale"] is True and row["state"] == "stale"
    assert row["current"]["stale"] is True and row["current"]["observed_at"] == NOW - 25
    assert old["checkpoints"][0]["stale"] is False


def test_changed_lineage_id_cannot_inherit_old_accepted_facts(tmp_path):
    program = definition(tmp_path)
    program["lineage"]["qualified"] = "5" * 40
    path = write_registry(tmp_path, [program])
    result = programs.collect(path, now=NOW, previous=previous(), github_reader=unavailable)
    row = result["checkpoints"][0]
    assert row["accepted"] is False and row["facts"] == {} and row["lineage"]["verified"] is False


def test_same_lineage_retains_historic_facts_stale_on_receipt_failure(tmp_path):
    path = write_registry(tmp_path, [definition(tmp_path)])
    row = programs.collect(path, now=NOW, previous=previous(), github_reader=unavailable)["checkpoints"][0]
    assert row["accepted"] is True and row["state"] == "stale"
    assert row["accepted_at"] == NOW - 100 and row["verified_at"] == NOW - 20
    assert row["current"]["state"] == "stale" and row["current"]["observed_at"] == NOW - 25


def test_changed_typed_github_identity_does_not_reuse_other_current_snapshot(tmp_path):
    program = definition(tmp_path)
    program["github"] = dict(GITHUB, parent_pr=999)
    path = write_registry(tmp_path, [program])
    row = programs.collect(path, now=NOW, previous=previous(), github_reader=unavailable)["checkpoints"][0]
    assert row["current"]["state"] == "unavailable" and row["current"]["observed_at"] is None


def test_source_json_size_duplicate_fields_and_symlinks_fail_closed(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text('{"result":"pass","result":"fail"}')
    with pytest.raises(ValueError):
        programs._read_json(path, 100)
    path.write_text("x" * 101)
    with pytest.raises(programs.EvidenceError):
        programs._read_json(path, 100)
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        programs._read_json(alias, 1000)


@pytest.mark.parametrize("value", [True, "not a clock", float("nan"), float("inf"), -1, 10 ** 10000])
def test_invalid_clock_values_are_unknown(value):
    assert programs._epoch(value) is None


def test_defaults_are_evaluated_at_construction_from_current_config(tmp_path, monkeypatch):
    from zai_devdashd import config
    monkeypatch.setattr(config, "programs_registry", lambda: tmp_path / "first.json", raising=False)
    monkeypatch.setattr(config, "CURRENT", {"github": {"bin": "/first/gh"}})
    first = programs.Cache()
    monkeypatch.setattr(config, "programs_registry", lambda: tmp_path / "second.json")
    monkeypatch.setattr(config, "CURRENT", {"github": {"bin": "/second/gh"}})
    second = programs.Cache()
    assert first.registry_path.name == "first.json" and first.gh_path == "/first/gh"
    assert second.registry_path.name == "second.json" and second.gh_path == "/second/gh"


def test_cache_get_is_nonblocking_single_worker_throttled_and_returns_copies(tmp_path):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def collector(path, **kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(2)
        finished.set()
        return previous()
    cache = programs.Cache(tmp_path / "programs.json", clock=lambda: NOW, collector=collector)
    try:
        first = cache.get()
        assert first["state"] == "pending" and first["coverage"]["registered"] is None
        assert started.wait(1)
        before = time.monotonic()
        assert cache.get()["state"] == "pending"
        assert time.monotonic() - before < 0.1 and len(calls) == 1
    finally:
        release.set()
    assert finished.wait(1)
    for _ in range(100):
        value = cache.get()
        if value["state"] == "available":
            break
        time.sleep(0.001)
    value["checkpoints"][0]["facts"]["changed"] = True
    assert "changed" not in cache.get()["checkpoints"][0]["facts"]
    assert len(calls) == 1


def test_unexpected_worker_failure_retains_history_with_unknown_current_coverage(tmp_path):
    cache = programs.Cache(tmp_path / "programs.json", clock=lambda: NOW, collector=unavailable)
    cache._run(NOW, previous())
    value = cache.get()
    assert value["coverage"]["verified"] is None
    assert value["checkpoints"][0]["current"]["state"] == "stale"
    assert value["checkpoints"][0]["current"]["observed_at"] == NOW - 25
    assert "SENSITIVE" not in json.dumps(value)


def test_overdue_cache_cannot_claim_current_verified_coverage(tmp_path):
    cache = programs.Cache(tmp_path / "programs.json", clock=lambda: NOW + 1000)
    cache._value, cache._attempted, cache._busy = previous(), NOW - 10, True
    value = cache.get()
    assert value["coverage"]["verified"] is None
    assert value["checkpoints"][0]["current"]["stale"] is True
    assert value["checkpoints"][0]["accepted_at"] == NOW - 100


def test_command_output_limit_reaps_only_its_owned_process(monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"x" * 100)
    os.close(write_fd)
    class Child:
        stdout = os.fdopen(read_fd, "rb", buffering=0)
        killed = False
        def poll(self): return None
        def kill(self): self.killed = True
        def wait(self, timeout=None): return 0
    child = Child()
    monkeypatch.setattr(programs.subprocess, "Popen", lambda *a, **k: child)
    with pytest.raises(programs.EvidenceError, match="output bound"):
        programs._command(["git", "read-only"], time.monotonic() + 1, limit=16)
    assert child.killed and child.stdout.closed


@pytest.mark.parametrize("payload", [b"null", b"[]", b'{"data":null}', b'{"data":{"repository":null}}'])
def test_malformed_graphql_response_is_source_failure(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(programs, "_command", lambda *a, **k: payload)
    with pytest.raises(programs.EvidenceError, match="GitHub"):
        programs._github(definition(tmp_path), [], "gh", time.monotonic() + 1)


def test_unavailable_receipt_root_has_unknown_present_counts_not_fresh_zero(tmp_path):
    program = definition(tmp_path)
    program["receipt_root"] = str(tmp_path / "not-mounted-or-absent")
    path = write_registry(tmp_path, [program])
    result = programs.collect(path, now=NOW, github_reader=unavailable)
    assert result["coverage"]["expected_receipts"] == len(programs.REQUIRED)
    assert result["coverage"]["present_receipts"] is None
    assert result["coverage"]["valid_receipts"] is None


def test_unregistered_required_receipts_do_not_reduce_expected_checkpoint_denominator(tmp_path):
    path = write_registry(tmp_path, [definition(tmp_path)])
    result = programs.collect(path, now=NOW, github_reader=unavailable)
    assert result["coverage"]["expected_receipts"] == len(programs.REQUIRED)
    assert result["checkpoints"][0]["accepted"] is False
