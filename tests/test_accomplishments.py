"""Independent public parser stories; every identity and event below is synthetic."""
from datetime import datetime, timedelta
import hashlib

from zai_devdashd import accomplishments as acc
from zai_devdashd import labels as board

ET = acc.ET
SYNTHETIC_MONDAY = datetime(2025, 3, 10, 16, 45, tzinfo=ET)
SYNTHETIC_BUILD = "7100.4"


def synthetic_sha(label):
    """Deterministic fixture-only git-shaped identity, unrelated to source history."""
    return hashlib.sha256(("zai-devdashd synthetic accomplishments v1:" + label).encode()).hexdigest()[:40]


class SyntheticDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(acc.time.time(), tz)


def test_gate_and_tf_parsing_and_week(tmp_path):
    # A duplicate Monday gate, a Sunday pre-DST gate, two successful platform
    # uploads and one failed upload are independent parser fixtures, not a run log.
    status = tmp_path / "synthetic-status.md"
    primary, base = synthetic_sha("monday-gate"), synthetic_sha("monday-base")
    prep_from, prep_to = synthetic_sha("sunday-start"), synthetic_sha("sunday-end")
    gate = f"2025-03-10 14:07 EDT synthetic full exact-head gate #4101 {primary} vs base {base}: rc=0 UNIT EXIT 0 fixture=duplicate\n"
    status.write_text(gate + gate +
        f"2025-03-09 01:20 EST L-PREP-4102: GATE PASS synthetic range {prep_from}..{prep_to} fixture=week-boundary\n"
        "2025-03-10 14:11 EDT synthetic gate #4103 rc=3 UNIT EXIT 3\n")
    tf = tmp_path / "synthetic-uploads"
    tf.mkdir()
    (tf / "sample-run.log").write_text(
        f"Mon Mar 10 13:02:17 EDT 2025 BUILD={SYNTHETIC_BUILD}\n"
        "Mon Mar 10 13:17:41 EDT 2025 ios UPLOAD EXIT 0\n"
        "Mon Mar 10 13:31:09 EDT 2025 macos UPLOAD EXIT 0\n"
        "Mon Mar 10 13:37:55 EDT 2025 macos UPLOAD EXIT 7\n")
    now = SYNTHETIC_MONDAY
    since = acc.window_start(now)
    items = acc.gate_items(since, status) + acc.tf_items(since, tf)
    items.append({"kind": "merged", "when": datetime(2025, 3, 9, 9, 12, tzinfo=ET), "key": "merged:4104",
                  "pr": 4104, "text": "#4104 · synthetic Sunday merge"})
    out = acc.build(items, now)
    assert out["today"]["counts"] == {"merged": 0, "shipped": 2, "opened": 0, "gates": 1}
    assert out["week"]["counts"]["merged"] == 0  # Monday Mar 10 excludes Sunday.
    assert out["rolling"]["counts"] == {"merged": 1, "shipped": 2, "opened": 0, "gates": 2}
    texts = [i["text"] for g in out["today"]["groups"] for i in g["items"]]
    assert f"TestFlight {SYNTHETIC_BUILD} uploaded for iOS" in texts
    assert [d["label"] for d in out["rolling"]["days"]] == ["Mon Mar 10", "Sun Mar 9"]
    assert now.utcoffset() == timedelta(hours=-4)
    assert datetime(2025, 3, 9, 1, 20, tzinfo=ET).utcoffset() == timedelta(hours=-5)


def test_fail_label_names_the_check_and_head():
    pr = {"number": 4101, "title": "Synthetic failed checks", "sha": synthetic_sha("check-label"), "checks": {"fail": 2, "pending": 0},
          "failed_names": ["DeepSource: Shell", "DeepSource: Swift"], "hold": ""}
    row = board.pr_wait(pr)
    assert "DeepSource: Shell, DeepSource: Swift" in row["text"] and synthetic_sha("check-label")[:8] in row["text"]
    assert "A check failed" not in row["text"]


def test_future_records_do_not_count_as_today():
    now = SYNTHETIC_MONDAY
    items = [{"kind": "opened", "when": datetime(2025, 3, 11, 2, 15, tzinfo=ET), "key": "opened:4110", "text": "future"}]
    assert acc.build(items, now)["today"]["counts"]["opened"] == 0


def test_github_failure_is_unavailable_not_empty(monkeypatch):
    import subprocess
    monkeypatch.setattr(acc.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "private token"))
    try:
        acc._gh(["pr", "list"])
    except acc.SourceUnavailable as exc:
        assert "private" not in str(exc)
    else:
        raise AssertionError("failed GitHub read was accepted as no achievements")


def test_cache_failure_retains_github_evidence_and_original_source_clock(monkeypatch):
    cache = acc.Cache()
    now = SYNTHETIC_MONDAY
    original_clock = now.timestamp()
    cache.value = acc.build([{"kind": "merged", "when": now, "key": "merged:4110", "text": "#4110", "source_kind": "github"}], now,
                            {"github": {"state": "available", "observed_at": original_clock, "error": None}})
    monkeypatch.setattr(acc, "collect", lambda: (_ for _ in ()).throw(acc.SourceUnavailable("GitHub unavailable")))
    monkeypatch.setattr(acc, "tf_items", lambda *a: [])
    monkeypatch.setattr(acc, "gate_items", lambda *a: [])
    cache._run()
    assert cache.value["today"]["counts"]["merged"] is None
    assert next(r for r in cache.value["records"] if r["kind"] == "merged")["key"] == "merged:4110"
    assert cache.value["source_status"]["github"]["observed_at"] == original_clock
    assert cache.value["source_status"]["github"]["state"] == "stale"
    assert cache.value["stale"] is True and cache.value["partial"] is True and cache.value["error"]


def test_first_github_failure_reports_unknown_counts_with_local_receipts(monkeypatch):
    cache = acc.Cache()
    monkeypatch.setattr(acc, "collect", lambda: (_ for _ in ()).throw(acc.SourceUnavailable("GitHub unavailable")))
    monkeypatch.setattr(acc, "tf_items", lambda *a: [])
    monkeypatch.setattr(acc, "gate_items", lambda *a: [])
    cache._run()
    assert cache.value["today"]["counts"]["merged"] is None
    assert cache.value["week"]["counts"]["opened"] is None
    assert cache.value["today"]["counts"]["shipped"] == 0
    assert cache.value["error"] and cache.value["partial"]


def test_empty_success_and_truncated_github_results_are_not_complete(monkeypatch):
    import json
    import subprocess
    for output in ("", json.dumps([{}] * 1000)):
        monkeypatch.setattr(acc.subprocess, "run", lambda *a, _out=output, **kw: subprocess.CompletedProcess(a, 0, _out, ""))
        try:
            acc._gh(["pr", "list"])
        except acc.SourceUnavailable:
            pass
        else:
            raise AssertionError("incomplete GitHub results were accepted")
    monkeypatch.setattr(acc.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "[]", ""))
    assert acc._gh(["pr", "list"]) == []


def test_overdue_cache_is_flagged_without_falsifying_source_timestamp():
    import time
    from datetime import timedelta
    cache = acc.Cache()
    old = SYNTHETIC_MONDAY - timedelta(minutes=20)
    cache.value = acc.build([], old)
    cache.at = time.time()
    value = cache.get()
    assert value["stale"] and value["partial"]
    assert value["as_of_et"] == old.strftime("%Y-%m-%d %H:%M ET")
    assert cache.value["stale"] is False


def test_first_github_failure_day_summaries_keep_git_counts_unknown(monkeypatch):
    from datetime import timedelta
    cache = acc.Cache()
    recorded = SYNTHETIC_MONDAY - timedelta(seconds=1)
    gate = {"kind": "gates", "when": recorded, "key": "gate:4110:synthetic", "text": "#4110 green gate"}
    monkeypatch.setattr(acc, "collect", lambda: (_ for _ in ()).throw(acc.SourceUnavailable("GitHub unavailable")))
    monkeypatch.setattr(acc, "tf_items", lambda *a: [])
    monkeypatch.setattr(acc, "gate_items", lambda *a: [gate])
    cache._run()
    for name in ("week", "rolling"):
        day = cache.value[name]["days"][0]
        assert day["counts"]["merged"] is None and day["counts"]["opened"] is None
        assert day["counts"]["gates"] == 1
        assert "GitHub merges/openings unavailable" in day["summary"]
        assert "1 green gates recorded locally" in day["summary"]
        assert "0 PRs" not in day["summary"]
        assert day["groups"][0]["kind"] == "gates" and len(day["groups"][0]["items"]) == 1


def _local_sources(monkeypatch, uploads, status):
    original_tf, original_gate = acc.tf_items, acc.gate_items
    monkeypatch.setattr(acc, "tf_items", lambda since, *_: original_tf(since, uploads))
    monkeypatch.setattr(acc, "gate_items", lambda since, *_: original_gate(since, status))
    monkeypatch.setattr(acc, "github_items", lambda since: [])


def test_rows_preserve_original_receipt_identity_and_scope(tmp_path, monkeypatch):
    now = SYNTHETIC_MONDAY
    head = synthetic_sha("source-identity-gate")
    status = tmp_path / "status.md"
    status.write_text("2025-03-10 14:07 EDT synthetic full exact-head gate #4101 %s: rc=0 UNIT EXIT 0\n" % head)
    tf = tmp_path / "tf"
    tf.mkdir()
    log = tf / "build-run.log"
    log.write_text(f"BUILD={SYNTHETIC_BUILD}\nMon Mar 10 13:17:41 EDT 2025 ios UPLOAD EXIT 0\n")
    _local_sources(monkeypatch, tf, status)
    out = acc.collect(now)
    rows = {r["kind"]: r for r in out["records"]}
    gate, upload = rows["gates"], rows["shipped"]
    assert gate["pr"] == 4101 and gate["head"] == head
    assert gate["evidence_type"] == "full_exact_head_gate"
    assert gate["source"] == str(status) and gate["source_updated_at"] == status.stat().st_mtime
    assert upload["build"] == SYNTHETIC_BUILD and upload["platform"] == "iOS"
    assert upload["source"] == str(log) and upload["source_updated_at"] == log.stat().st_mtime
    assert upload["evidence_type"] == "upload_exit_zero" and "head" not in upload
    assert all(r["key"] == r["evidence_id"] and r["occurred_at"] and "goal_id" not in r for r in rows.values())
    assert out["coverage"]["week_starts"] == "Monday"
    assert "tester availability unverified" in out["coverage"]["testflight"]


def test_github_merge_commit_is_preserved_without_claiming_pr_head(monkeypatch):
    now = SYNTHETIC_MONDAY
    commit = synthetic_sha("merged-commit")
    replies = iter([[{"number": 4110, "title": "A change", "mergedAt": "2025-03-10T18:21:00Z",
                     "mergeCommit": {"oid": commit}, "baseRefName": "main"}], []])
    monkeypatch.setattr(acc, "_gh", lambda args: next(replies))
    row = acc.build(acc.github_items(acc.window_start(now)), now)["records"][0]
    assert row["pr"] == 4110 and row["merge_commit"] == commit and row["base"] == "main"
    assert row["source_kind"] == "github" and "head" not in row


def test_missing_local_sources_are_unknown_not_zero(tmp_path, monkeypatch):
    now = SYNTHETIC_MONDAY
    _local_sources(monkeypatch, tmp_path / "missing-tf", tmp_path / "missing-status.md")
    monkeypatch.setattr(acc, "github_items", lambda since: [{"kind": "opened", "when": now, "key": "opened:4110", "pr": 4110, "text": "#4110"}])
    out = acc.collect(now)
    assert out["partial"] and out["stale"]
    for name in ("today", "week", "rolling"):
        section = out[name]
        assert section["counts"]["shipped"] is None and section["counts"]["gates"] is None
        assert "TestFlight uploads unavailable" in section["summary"]
        for day in section.get("days") or []:
            assert day["counts"]["shipped"] is None and day["counts"]["gates"] is None
            assert "0 TestFlight" not in day["summary"]
    for name in ("testflight", "gates"):
        source = out["source_status"][name]
        assert source["state"] == "unavailable" and source["observed_at"] is None
        assert source["attempted_at"] and source["error"] and source["source_updated_at"] is None


def test_empty_readable_local_sources_are_verified_zero(tmp_path, monkeypatch):
    now = SYNTHETIC_MONDAY
    tf = tmp_path / "tf"
    tf.mkdir()
    status = tmp_path / "status.md"
    status.write_text("No recognized gate receipt here.\n")
    _local_sources(monkeypatch, tf, status)
    out = acc.collect(now)
    assert out["today"]["counts"]["shipped"] == out["today"]["counts"]["gates"] == 0
    assert not out["partial"]
    assert out["source_status"]["testflight"]["coverage"]["files_found"] == 0
    assert out["source_status"]["gates"]["coverage"]["files_read"] == 1


def test_partial_upload_read_keeps_receipts_but_count_unknown(tmp_path, monkeypatch):
    now = SYNTHETIC_MONDAY
    tf = tmp_path / "tf"
    tf.mkdir()
    good, bad = tf / "good-run.log", tf / "bad-run.log"
    good.write_text(f"BUILD={SYNTHETIC_BUILD}\nMon Mar 10 13:17:41 EDT 2025 ios UPLOAD EXIT 0\n")
    bad.write_text("unreadable")
    status = tmp_path / "status.md"
    status.write_text("")
    _local_sources(monkeypatch, tf, status)
    original = type(bad).read_text
    def read(path, *args, **kwargs):
        if path == bad:
            raise PermissionError("SENSITIVE_EXCEPTION_TEXT")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(bad), "read_text", read)
    out = acc.collect(now)
    source = out["source_status"]["testflight"]
    assert source["state"] == "partial" and source["coverage"]["files_failed"] == 1
    assert source["coverage"]["recorded_items"] == 1 and out["today"]["counts"]["shipped"] is None
    assert len(out["today"]["groups"][0]["items"]) == 1
    failed = next(f for f in source["files"] if not f["read"])
    assert failed["source_updated_at"] == bad.stat().st_mtime
    assert "SENSITIVE_EXCEPTION_TEXT" not in str(out)


def test_unreadable_gate_and_upload_listing_are_explicit(tmp_path, monkeypatch):
    now = SYNTHETIC_MONDAY
    tf, status = tmp_path / "tf", tmp_path / "status.md"
    tf.mkdir()
    status.write_text("not readable")
    _local_sources(monkeypatch, tf, status)
    original_read, original_iter = type(status).read_text, type(tf).iterdir
    def read(path, *args, **kwargs):
        if path == status:
            raise PermissionError("SENSITIVE_EXCEPTION_TEXT")
        return original_read(path, *args, **kwargs)
    def entries(path):
        if path == tf:
            raise PermissionError("SENSITIVE_EXCEPTION_TEXT")
        return original_iter(path)
    monkeypatch.setattr(type(status), "read_text", read)
    monkeypatch.setattr(type(tf), "iterdir", entries)
    out = acc.collect(now)
    assert out["today"]["counts"]["shipped"] is None and out["today"]["counts"]["gates"] is None
    assert out["source_status"]["gates"]["source_updated_at"] == status.stat().st_mtime
    assert out["source_status"]["testflight"]["source_updated_at"] == tf.stat().st_mtime
    assert "SENSITIVE_EXCEPTION_TEXT" not in str(out)


def test_local_cache_failure_retains_receipts_and_original_source_times(tmp_path, monkeypatch):
    now = SYNTHETIC_MONDAY
    status, tf = tmp_path / "status.md", tmp_path / "tf"
    status.write_text("2025-03-10 14:07 EDT synthetic full exact-head gate #4101 %s: rc=0 UNIT EXIT 0\n" % synthetic_sha("retained-local-gate"))
    tf.mkdir()
    _local_sources(monkeypatch, tf, status)
    good = acc.collect(now)
    old_status = good["source_status"]["gates"]
    cache = acc.Cache()
    cache.value = good
    status.unlink()
    failed = acc.collect(now)
    monkeypatch.setattr(acc, "collect", lambda: failed)
    cache._run()
    kept = cache.value["source_status"]["gates"]
    assert kept["state"] == "stale"
    assert kept["observed_at"] == old_status["observed_at"]
    assert kept["source_updated_at"] == old_status["source_updated_at"]
    assert kept["failed_attempt"]["state"] == "unavailable" and kept["attempted_at"]
    assert cache.value["today"]["counts"]["gates"] is None
    assert cache.value["records"][0]["head"] == synthetic_sha("retained-local-gate")
    assert kept["coverage"]["recorded_items"] == 1


def test_first_github_and_local_failure_do_not_publish_any_zero(tmp_path, monkeypatch):
    cache = acc.Cache()
    _local_sources(monkeypatch, tmp_path / "missing-tf", tmp_path / "missing-status.md")
    monkeypatch.setattr(acc, "collect", lambda: (_ for _ in ()).throw(acc.SourceUnavailable("GitHub unavailable")))
    cache._run()
    for name in ("today", "week", "rolling"):
        assert all(v is None for v in cache.value[name]["counts"].values())
        assert "0 " not in cache.value[name]["summary"]
    assert all(s["state"] == "unavailable" for s in cache.value["source_status"].values())


def test_repeated_first_failure_never_claims_a_successful_source_read(tmp_path, monkeypatch):
    cache = acc.Cache()
    _local_sources(monkeypatch, tmp_path / "missing-tf", tmp_path / "missing-status.md")
    monkeypatch.setattr(acc, "collect", lambda: (_ for _ in ()).throw(acc.SourceUnavailable("GitHub unavailable")))
    cache._run()
    original = cache.value["observed_at"]
    cache._run()
    assert cache.value["observed_at"] >= original  # collection advances; no successful source clock is fabricated
    assert cache.value["source_status"]["github"]["state"] == "unavailable"
    assert cache.value["source_status"]["github"]["observed_at"] is None
    assert all(v is None for v in cache.value["today"]["counts"].values())


def test_each_github_outage_poll_reads_new_uploads_and_gates(tmp_path, monkeypatch):
    from datetime import timedelta
    now = SYNTHETIC_MONDAY
    initial = now - timedelta(minutes=5)
    clock = [initial.timestamp()]
    monkeypatch.setattr(acc.time, "time", lambda: clock[0])
    tf, status = tmp_path / "tf", tmp_path / "status.md"
    tf.mkdir()
    log = tf / "build-run.log"
    log.write_text("")
    status.write_text("")
    _local_sources(monkeypatch, tf, status)
    github = {"kind": "merged", "when": initial - timedelta(seconds=1), "key": "merged:4110", "pr": 4110,
              "source_kind": "github", "merge_commit": synthetic_sha("outage-merge"), "text": "#4110 merged"}
    monkeypatch.setattr(acc, "github_items", lambda since: [github])
    cache = acc.Cache()
    cache.value = acc.collect(initial)
    original_github = dict(cache.value["source_status"]["github"])
    old_local_clock = cache.value["source_status"]["gates"]["observed_at"]
    monkeypatch.setattr(acc, "collect", lambda: (_ for _ in ()).throw(acc.SourceUnavailable("GitHub unavailable")))
    for expected in (1, 2):
        clock[0] = now.timestamp() + expected
        with log.open("a") as fh:
            fh.write("BUILD=7200.%d\n%s ios UPLOAD EXIT 0\n" % (expected, now.strftime("%a %b %d %H:%M:%S %Z %Y")))
        with status.open("a") as fh:
            fh.write("%s EDT synthetic full exact-head gate #%d %s: rc=0 UNIT EXIT 0\n" % (now.strftime("%Y-%m-%d %H:%M"), 4200 + expected, synthetic_sha("outage-gate-%d" % expected)))
        cache._run()
        value = cache.value
        gh = value["source_status"]["github"]
        assert gh["state"] == "stale" and gh["observed_at"] == original_github["observed_at"]
        assert gh["attempted_at"] >= original_github["attempted_at"]
        assert gh["coverage"]["recorded_items"] == 1
        assert value["today"]["counts"]["merged"] is None and value["today"]["counts"]["opened"] is None
        for name in ("today", "week", "rolling"):
            assert value[name]["counts"]["shipped"] == expected
            assert value[name]["counts"]["gates"] == expected
        for source in ("testflight", "gates"):
            fresh = value["source_status"][source]
            assert fresh["state"] == "available" and fresh["observed_at"] == clock[0]
            assert fresh["observed_at"] > old_local_clock
        assert len([r for r in value["records"] if r["kind"] == "merged"]) == 1
        assert len([r for r in value["records"] if r["kind"] == "gates"]) == expected
        assert len([r for r in value["records"] if r["kind"] == "shipped"]) == expected

    # A concurrent local failure retains only that source's aged evidence too.
    original_gate = dict(cache.value["source_status"]["gates"])
    status.unlink()
    clock[0] += 1
    cache._run()
    value = cache.value
    assert value["today"]["counts"]["gates"] is None
    assert value["today"]["counts"]["shipped"] == 2
    assert value["source_status"]["gates"]["state"] == "stale"
    assert value["source_status"]["gates"]["observed_at"] == original_gate["observed_at"]
    assert value["source_status"]["gates"]["source_updated_at"] == original_gate["source_updated_at"]
    assert value["source_status"]["github"]["observed_at"] == original_github["observed_at"]
    assert len([r for r in value["records"] if r["kind"] == "gates"]) == 2


def test_side_branch_merge_is_scoped_but_opening_keeps_current_state_and_clock(monkeypatch):
    # A synthetic topic-branch merge is not an integration merge, while its
    # opening record retains the event time and subsequently observed state.
    now = datetime(2025, 3, 12, 14, 30, tzinfo=ET)
    opened = "2025-03-12T15:10:00Z"
    replies = iter([
        [{"number": 4120, "title": "Synthetic side-branch change", "mergedAt": "2025-03-12T17:05:00Z",
          "mergeCommit": {"oid": synthetic_sha("side-branch-merge")}, "baseRefName": "examples/parallel-topic"}],
        [{"number": 4120, "title": "Synthetic side-branch change", "createdAt": opened, "state": "MERGED"}],
    ])
    monkeypatch.setattr(acc, "_gh", lambda args: next(replies))
    monkeypatch.setattr(acc.time, "time", lambda: now.timestamp())
    value = acc.build(acc.github_items(acc.window_start(now)), now)
    assert value["today"]["counts"]["merged"] == 0
    assert value["today"]["counts"]["opened"] == 1
    row = value["records"][0]
    assert row["current_state"] == "merged"
    assert row["state_observed_at"] == now.timestamp()
    assert acc._iso(row["occurred_at"]) == acc._iso(opened)
    assert "0 PRs merged" in value["today"]["summary"]
    assert "main/crash" not in value["today"]["summary"]


import pytest
from zai_devdashd import config as _site


@pytest.mark.parametrize("integration_branch", ["release/stable", "trunk", ""])
def test_merge_summary_keeps_unconfirmed_branch_scope_out_of_the_claim(monkeypatch, integration_branch):
    monkeypatch.setitem(_site.CURRENT, "github", {
        "repo": "example/project", "integration_branch": integration_branch,
    })
    for merged, phrase in ((1, "1 PR merged"), (None, "PRs merged unavailable")):
        summary = acc._summary({"merged": merged, "shipped": 0, "opened": 0, "gates": 0})
        assert phrase in summary
        assert "main/crash" not in summary
        assert "into main" not in summary
        assert "release/stable" not in summary and "trunk" not in summary

@pytest.fixture(autouse=True)
def _synthetic_adapter_configuration(monkeypatch):
    monkeypatch.setattr(acc.time, "time", lambda: SYNTHETIC_MONDAY.timestamp())
    monkeypatch.setattr(acc, "datetime", SyntheticDateTime)
    monkeypatch.setitem(_site.CURRENT, "adapters", {"git": True, "github": True})
    monkeypatch.setitem(_site.CURRENT, "github", {"repo": "example/project", "integration_branch": "main"})
