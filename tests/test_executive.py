import copy
from datetime import datetime, timezone

import pytest

from zai_devdashd.executive import build_dashboard

NOW = datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("bases", [None, ["release/candidate", "develop"]])
def test_workflow_description_does_not_invent_branch_coverage(bases):
    source = state()
    if bases is not None:
        source["overview"]["accomplishments"]["coverage"] = {"merged_bases": bases}
    summary = build_dashboard(source, now=NOW)["workflow_scope"]["summary"]
    assert "merges into main" not in summary
    assert "main or main" not in summary
    assert "recorded source scopes" in summary
    assert "missing branch coverage remains unconfirmed" in summary


def fresh(**fields):
    return {"observed_at": NOW - 5, "as_of_et": "2026-11-04 07:59 ET",
            "stale": False, "error": None, **fields}


def state():
    items = [
        {"kind": "opened", "key": "open:1", "pr": 1, "text": "#1 proposed work",
         "day": "2026-11-04", "time_et": "7:45 AM ET"},
        {"kind": "merged", "key": "merge:2", "pr": 2, "merge_commit": "abc12345",
         "text": "#2 accepted change", "day": "2026-11-04", "time_et": "7:50 AM ET"},
        {"kind": "gates", "key": "gate:2:abc12345", "pr": 2, "head": "abc12345",
         "text": "#2 exact-head gate green", "source": "mission/status.md", "line": 120,
         "source_updated_at": NOW - 3600, "evidence_type": "full_exact_head_gate",
         "day": "2026-11-04", "time_et": "7:52 AM ET"},
        {"kind": "shipped", "key": "upload:9000.0:macos", "build": "9000.0",
         "platform": "macOS", "text": "TestFlight 9000.0 uploaded for macOS",
         "day": "2026-11-04", "time_et": "7:55 AM ET"},
    ]
    groups = [{"kind": row["kind"], "items": [row]} for row in items]
    counts = {"merged": 1, "opened": 1, "gates": 1, "shipped": 1}
    acc = fresh(today={"summary": "Recorded events", "groups": groups, "counts": counts},
                week={"label": "Week of Mon Nov 2", "summary": "Recorded events",
                      "days": [{"groups": groups}], "counts": counts},
                source_status={"github": {"state": "available", "observed_at": NOW - 5},
                               "testflight": {"state": "available"},
                               "gates": {"state": "available"}})
    return {
        "overview": {"accomplishments": acc, "tiles": []},
        "mini": fresh(active=True, processes=[{"command": "DO NOT COPY RAW COMMAND"}],
                      load={"cpu": 14, "gpu": 30}, hardware={"title": "Mac mini M5 Pro", "cores": 18}),
        "worker": fresh(active=False), "remote": fresh(name="Remote-Mac", state="online",
                      hardware={"cpu": "Intel i5", "arch": "x86_64"}, load={"load_1m": 4.2}),
        "devices": fresh(items=[
            {"name": "Tablet101", "device_type": "iPad", "active": False, "state": "connected"},
            {"name": "Phone102", "device_type": "iPhone", "active": False, "state": "connected"},
        ]),
        "pipelines": fresh(runs=[]), "github": fresh(prs=[]),
    }


def goal(result, key):
    return next(row for row in result["goals"] if row["id"] == key)


def platform(result, key):
    return next(row for row in result["platforms"] if row["id"] == key)


def test_completed_and_started_events_have_useful_distinct_impacts_and_identity():
    result = build_dashboard(state(), NOW)
    today = result["achievements"]["today"]
    assert [row["kind"] for row in today["started"]] == ["opened"]
    assert {row["kind"] for row in today["completed"]} == {"merged", "gates", "shipped"}
    rows = {row["kind"]: row for row in today["completed"]}
    assert rows["merged"]["merge_commit"] == "abc12345"
    assert "head" not in rows["merged"]
    assert rows["gates"]["head"] == "abc12345"
    assert rows["gates"]["source"] == "mission/status.md" and rows["gates"]["line"] == 120
    assert rows["gates"]["source_updated_at"] == NOW - 3600
    assert rows["shipped"]["build"] == "9000.0"
    assert "head" not in rows["shipped"]
    assert "processing" in rows["shipped"]["impact"]
    assert today["started"][0]["goal"] == "Integrate approved changes"
    assert "proposed" in today["started"][0]["impact"]
    assert "not qualify the whole release" in rows["gates"]["impact"]


def test_no_aggregate_release_percentage_from_merged_prs_or_process_activity():
    s = state()
    s["github"]["prs"] = [{"number": 1, "checks": {"pass": 10, "fail": 0}, "sha": "abc12345"}]
    result = build_dashboard(s, NOW)
    assert result["readiness"] == "unverified"
    assert all(row["readiness"] == "unverified" and "progress" not in row for row in result["goals"])
    assert "Current candidate processing" in goal(result, "distribution")["summary"]
    assert all(row["status"] != "passed" for row in result["platforms"])


def test_tablet101_is_ipad_and_phone102_is_iphone_despite_legacy_phone_label():
    s = state()
    s["pipelines"]["runs"] = [{
        "name": "gate", "head": "12345678", "steps": [
            {"name": "PHONE_BFT", "device": "Tablet101", "word": "phone build", "state": "pass", "exit": 0},
            {"name": "Phone102_BFT", "state": "pass", "exit": 0},
            {"name": "MAC_UNIT", "state": "fail", "exit": 65},
        ]}]
    result = build_dashboard(s, NOW)
    ipad = platform(result, "ipados")
    phone = platform(result, "ios")
    assert ipad["builds"]["evidence"][0]["label"] == "PHONE_BFT"
    assert ipad["builds"]["evidence"][0]["value"] == "Build for testing"
    assert phone["builds"]["evidence"][0]["label"] == "Phone102_BFT"
    assert ipad["tests"]["state"] == phone["tests"]["state"] == "unreported"
    assert platform(result, "macos")["tests"]["evidence"][0]["state"] == "failed"


def test_missing_inventory_does_not_guess_platform_from_historical_device_number():
    s = state()
    s["devices"]["items"] = []
    s["pipelines"]["runs"] = [{"name": "old", "steps": [
        {"name": "PHONE_BFT", "state": "pass"}, {"name": "DIESEL", "state": "pass"},
        {"name": "REL", "state": "pass"}]}]
    result = build_dashboard(s, NOW)
    assert all(p["builds"]["state"] == "unreported" and p["tests"]["state"] == "unreported"
               for p in result["platforms"])
    assert len(result["coverage"]["unassigned_platform_stages"]) == 3


def test_explicit_runtime_test_identity_is_separate_from_build_and_launch():
    s = state()
    s["devices"]["items"][1].update(active=True, step="private raw command")
    s["pipelines"]["runs"] = [{"name": "gate", "steps": [
        {"name": "Phone102_TEST", "state": "pass", "exit": 0},
        {"name": "DIESEL", "device": "Tablet101", "state": "fail", "exit": 65}]}]
    result = build_dashboard(s, NOW)
    phone = platform(result, "ios")
    assert phone["tests"]["state"] == "runtime_active"
    assert phone["tests"]["evidence"][0]["state"] == "runtime_active"
    assert "not a passed test" in phone["tests"]["evidence"][0]["note"]
    assert "private raw command" not in str(result)
    assert platform(result, "ipados")["tests"]["evidence"][0]["state"] == "failed"


def test_failed_stages_count_as_recorded_without_becoming_passed_progress():
    s = state()
    s["pipelines"]["runs"] = [{"name": "mixed", "head": "12345678", "state": "running", "steps": [
        {"name": "MAC_BFT", "state": "pass", "exit": 0},
        {"name": "MAC_UNIT", "state": "fail", "exit": 65},
        {"name": "Phone102_BFT", "state": "pending", "exit": None}]}]
    result = build_dashboard(s, NOW)
    progress = platform(result, "macos")["builds"]["evidence"][0]["progress"]
    assert progress["done"] == 2 and progress["passed"] == 1 and progress["total"] == 3
    assert "percent" not in progress
    assert platform(result, "ios")["builds"]["evidence"][0]["state"] == "not_recorded"
    assert platform(result, "ios")["builds"]["state"] != "active"


@pytest.mark.parametrize("change", [{"stale": True}, {"error": "inspection failed"},
                                   {"observed_at": NOW - 999}])
def test_stale_cached_device_and_machine_activity_cannot_claim_live_execution(change):
    s = state()
    s["mini"].update(change)
    s["devices"].update(change)
    s["devices"]["items"][1]["active"] = True
    result = build_dashboard(s, NOW)
    assert result["machines"][0]["state"] == "unknown"
    assert platform(result, "ios")["members"][0]["runtime_active"] is False
    assert platform(result, "ios")["tests"]["state"] != "active"


def test_cached_achievement_preserves_old_source_clock_and_null_counts():
    s = state()
    acc = s["overview"]["accomplishments"]
    acc.update(observed_at=NOW - 7200, stale=True, error="GitHub unavailable")
    acc["week"]["counts"]["merged"] = None
    acc["source_status"]["github"] = {"state": "stale", "observed_at": NOW - 7200}
    result = build_dashboard(s, NOW)
    assert result["generated_at"] == NOW
    assert result["as_of_et"] == acc["as_of_et"]
    assert result["achievements"]["week"]["observed_at"] == NOW - 7200
    assert result["achievements"]["week"]["counts"]["merged"] is None
    assert goal(result, "integration")["evidence"][0]["value"] == "Unknown"
    assert result["coverage"]["source_status"]["github"]["observed_at"] == NOW - 7200
    assert result["stale"] and result["partial"]


def test_pr_impediments_retain_head_and_stale_status_without_claiming_release_blocker():
    s = state()
    s["github"].update(stale=True, prs=[
        {"number": 7, "title": "Proposed fix", "sha": "abc12345", "checks": {"fail": 1},
         "failed_names": ["Swift"], "review_decision": "CHANGES_REQUESTED"},
        {"number": 8, "hold": "Historic broad hold", "checks": {"fail": 0}}])
    result = build_dashboard(s, NOW)
    assert len(result["blockers"]) == 1
    impediment = result["blockers"][0]
    assert impediment["head"] == "abc12345"
    assert impediment["status"] == "stale"
    assert "exact-head coverage is unreported" in impediment["reason"]
    assert "whole release is not established" in impediment["note"]
    assert goal(result, "integration")["status"] != "attention"


def test_projection_is_deterministic_does_not_mutate_input_or_copy_raw_processes(monkeypatch):
    s = state()
    before = copy.deepcopy(s)
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("Projection attempted file access"))
    first = build_dashboard(s, NOW)
    assert first == build_dashboard(s, datetime.fromtimestamp(NOW, timezone.utc))
    assert s == before
    assert "DO NOT COPY RAW COMMAND" not in str(first)
    assert {m["kind"] for m in first["machines"]} == {"mac"}
    assert len(first["machines"]) == 3


def test_empty_state_keeps_readiness_and_counts_unknown():
    result = build_dashboard({}, NOW)
    assert result["stale"] and result["partial"]
    assert result["achievements"]["today"]["counts"] is None
    assert result["readiness"] == "unverified"
    assert all(row["evidence"][0]["count"] is None for row in result["goals"])
    assert all(m["state"] == "unknown" for m in result["machines"])


def test_individual_achievement_source_clock_and_freshness_survive_partial_collection():
    s = state()
    acc = s["overview"]["accomplishments"]
    acc.update(partial=True)
    acc["source_status"]["testflight"] = {"state": "stale", "observed_at": NOW - 7200,
                                             "as_of_et": "2026-11-04 06:00 ET"}
    result = build_dashboard(s, NOW)
    rows = {r["kind"]: r for r in result["achievements"]["today"]["completed"]}
    assert rows["shipped"]["stale"] is True
    assert rows["shipped"]["evidence"][0]["observed_at"] == NOW - 7200
    assert rows["merged"]["stale"] is False
    assert result["partial"] is True


def test_all_mac_hosts_are_in_the_macos_group():
    state = {"mini": {"observed_at": 1000, "active": True},
             "worker": {"observed_at": 1000, "active": False},
             "remote": {"observed_at": 1000}}
    result = build_dashboard(state, now=1001)
    mac = next(group for group in result["platforms"] if group["id"] == "macos")
    assert {row["source"] for row in mac["members"]} == {"mini", "worker", "remote"}
    assert next(row for row in mac["members"] if row["source"] == "mini")["activity"] == "Activity observed"


def test_coderabbit_request_is_visible_without_claiming_release_blocker():
    state = {"github": {"observed_at": 1000, "prs": [{
        "number": 42, "title": "Current change", "coderabbit": "CHANGES_REQUESTED",
        "review_decision": "", "sha": "abc12345", "checks": {}}]}}
    result = build_dashboard(state, now=1001)
    assert len(result["blockers"]) == 1
    row = result["blockers"][0]
    assert "CodeRabbit" in row["reason"] and "exact-head coverage is unreported" in row["reason"]
    assert row["head"] == "abc12345" and "whole release is not established" in row["note"]


def program_feed(accepted_at=NOW - 1800):
    return {
        "schema_version": 1, "observed_at": NOW - 2, "attempted_at": NOW - 2,
        "coverage": {"registered": 1, "expected_receipts": 2, "present_receipts": 2,
                     "valid_receipts": 2, "verified": 1, "errors": []},
        "checkpoints": [{
            "id": "foundation-checkpoint", "title": "Accepted foundation checkpoint",
            "program": "Future foundation", "scope": "v1.x", "state": "verified", "accepted": True,
            "stale": False, "summary": "Acceptance is limited to the registered foundation.",
            "objective": "Establish the registered foundation",
            "impact": "This preserves the accepted foundation for subsequent product work.",
            "next_step": "Resolve the independent parent follow-up.",
            "release_boundary": "Foundation only; current v1 release approval remains separate.",
            "observed_at": NOW - 2, "verified_at": NOW - 2, "attempted_at": NOW - 2,
            "occurred_at": NOW - 7200, "accepted_at": accepted_at,
            "lineage": {"qualified": "1" * 40, "published": "2" * 40,
                        "merge": "3" * 40, "docs": "4" * 40, "verified": True,
                        "commits": {"docs": {"sha": "4" * 40, "committed_at": accepted_at}}},
            "coverage": {"expected": 2, "present": 2, "valid": 2},
            "facts": {"v1_promoted": False, "platforms": [
                {"id": "macos", "title": "Signed Mac and hosted tests", "machine": "Build Mac",
                 "build": "9001.0", "signed": True, "compile_only": False,
                 "tests": {"passed": 9, "suites": 2, "new": 3, "failed": 0, "skipped": 0},
                 "occurred_at": NOW - 7200},
                {"id": "ios_simulator", "title": "Simulator compile/link", "machine": "Compiler Mac",
                 "build": "9002.0", "signed": False, "compile_only": True,
                 "architectures": ["arm64", "x86_64"], "pinned_checkouts": 4,
                 "warnings": 7, "tests": None, "occurred_at": NOW - 7100},
            ], "closures": {"count": 2, "issues": [{"number": 20, "state": "CLOSED"}]}},
            "evidence": [{"id": "qualified-receipt", "kind": "native", "sha256": "a" * 64,
                          "state": "verified", "occurred_at": NOW - 7200, "source": "registered-receipt"}],
            "current": {"state": "available", "observed_at": NOW - 3, "attempted_at": NOW - 3,
                        "error": None, "parent_pr": {"number": 10, "state": "OPEN", "draft": True},
                        "pr": {"number": 11, "state": "MERGED"}, "canonical_issue": {"number": 12, "state": "OPEN"},
                        "reopened_issues": [], "v1_promoted": None},
        }], "lanes": [],
    }


def test_program_acceptance_is_additive_scoped_and_uses_original_acceptance_clock():
    s = state()
    old_counts = copy.deepcopy(s["overview"]["accomplishments"])
    s["programs"] = program_feed()
    result = build_dashboard(s, NOW)
    programs = result["programs"]
    cp = programs["checkpoints"][0]
    assert cp["accepted_verified"] and cp["historical_accepted"]
    assert cp["scope"] == "v1.x" and cp["facts"]["v1_promoted"] is False
    assert programs["achievements"]["today"]["count"] == 1
    event = programs["achievements"]["today"]["completed"][0]
    assert event["evidence_id"] == "program:foundation-checkpoint"
    assert event["accepted_at"] == NOW - 1800
    assert event["occurred_at"] == NOW - 7200 and event["verified_at"] == NOW - 2
    assert event["current_observed_at"] == NOW - 3
    assert event["impact"] == cp["impact"] and event["goal"] == cp["objective"]
    assert result["achievements"]["today"]["counts"] == old_counts["today"]["counts"]
    assert result["achievements"]["week"]["counts"] == old_counts["week"]["counts"]
    assert result["readiness"] == "unverified"
    assert [g["id"] for g in result["goals"]] == ["integration", "qualification", "distribution"]


def test_program_platform_results_never_turn_simulator_compilation_into_device_tests():
    s = state()
    s["programs"] = program_feed()
    result = build_dashboard(s, NOW)
    mac = platform(result, "macos")["program_evidence"][0]
    sim = platform(result, "ios")["program_evidence"][0]
    assert mac["signed"] is True and mac["tests"]["passed"] == 9
    assert not mac["compile_only"]
    assert sim["signed"] is False and sim["compile_only"] is True
    assert sim["tests"] is None and sim["architectures"] == ["arm64", "x86_64"]
    assert "runtime and device tests unverified" in sim["summary"]
    assert platform(result, "ios")["tests"]["state"] == "unreported"
    assert platform(result, "ipados")["program_evidence"] == []


def test_current_github_failure_does_not_revoke_historical_program_acceptance():
    s = state()
    s["programs"] = program_feed()
    cp = s["programs"]["checkpoints"][0]
    cp["current"].update(state="unavailable", error="GitHub unavailable", observed_at=NOW - 5000)
    cp["current"]["reopened_issues"] = [20]
    result = build_dashboard(s, NOW)["programs"]
    out = result["checkpoints"][0]
    assert out["accepted_verified"] and result["achievements"]["today"]["count"] == 1
    assert out["facts"]["closures"]["issues"][0]["state"] == "CLOSED"
    assert out["current"]["reopened_issues"] == [20] and out["current"]["parent_pr"]["draft"] is True
    assert out["current"]["observed_at"] == NOW - 5000


@pytest.mark.parametrize("change", [
    {"state": "unavailable", "error": "Receipt missing"},
    {"state": "partial", "error": "Receipt digest mismatch"},
    {"state": "stale", "stale": True, "error": "Registered evidence unavailable"},
])
def test_missing_tampered_or_stale_program_evidence_is_never_fresh_acceptance(change):
    s = state()
    s["programs"] = program_feed()
    s["programs"]["checkpoints"][0].update(change)
    s["programs"]["checkpoints"][0]["coverage"].update(present=1, valid=0)
    result = build_dashboard(s, NOW)["programs"]
    assert result["state"] == "partial"
    assert not result["checkpoints"][0]["accepted_verified"]
    assert result["achievements"]["today"]["count"] is None
    assert result["checkpoints"][0]["error"] == change["error"]


def test_product_acceptance_does_not_move_to_today_when_receipts_are_reverified():
    s = state()
    accepted = datetime(2026, 11, 1, 15, tzinfo=timezone.utc).timestamp()  # prior calendar week
    s["programs"] = program_feed(accepted)
    first = build_dashboard(s, NOW)
    assert first["programs"]["achievements"]["today"]["count"] == 0
    assert first["programs"]["achievements"]["week"]["count"] == 0
    s["programs"]["checkpoints"][0]["verified_at"] = NOW
    second = build_dashboard(s, NOW)
    assert second["programs"]["achievements"] == first["programs"]["achievements"]


def test_unknown_program_registry_and_missing_acceptance_time_remain_unknown():
    result = build_dashboard(state(), NOW)["programs"]
    assert result["state"] == "unavailable"
    assert result["achievements"]["today"]["count"] is None
    s = state()
    s["programs"] = program_feed(None)
    result = build_dashboard(s, NOW)["programs"]
    assert result["achievements"]["today"]["count"] is None
    assert result["achievements"]["today"]["completed"] == []


@pytest.mark.parametrize("current", ["MERGED", "CLOSED", "OPEN"])
def test_historical_opening_current_state_is_independent_and_clocked(current):
    s = state()
    item = s["overview"]["accomplishments"]["today"]["groups"][0]["items"][0]
    item.update(current_state=current, state_observed_at=NOW - 90)
    result = build_dashboard(s, NOW)["achievements"]["today"]["started"][0]
    assert result["event_status"] == "historical_opening"
    assert result["current"]["state"] == current.lower()
    assert result["current"]["observed_at"] == NOW - 90
    assert result["current_state"] == current and result["state_observed_at"] == NOW - 90
    assert result["at"] == "2026-11-04 7:45 AM ET"
    if current != "OPEN":
        assert "outstanding" not in result["impact"] or "not an outstanding" in result["impact"]


def test_only_trusted_legacy_collector_state_suffix_is_used():
    s = state()
    item = s["overview"]["accomplishments"]["today"]["groups"][0]["items"][0]
    item.update(text="#1 proposed work (merged)", source="GitHub opened PRs", source_kind="github")
    out = build_dashboard(s, NOW)["achievements"]["today"]["started"][0]
    assert out["current"]["state"] == "merged"
    item["source"] = "Untrusted title"
    out = build_dashboard(s, NOW)["achievements"]["today"]["started"][0]
    assert out["current"]["state"] == "unreported"


def test_program_projection_is_pure_and_repeat_calls_do_not_duplicate_acceptances(monkeypatch):
    s = state()
    s["programs"] = program_feed()
    before = copy.deepcopy(s)
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("Program projection attempted I/O"))
    first = build_dashboard(s, NOW)
    second = build_dashboard(s, NOW)
    assert first == second and s == before
    assert len(first["programs"]["achievements"]["today"]["completed"]) == 1


def test_structured_current_state_wins_over_legacy_opening_suffix():
    s = state()
    row = s["overview"]["accomplishments"]["today"]["groups"][0]["items"][0]
    row.update(text="#1 proposed work (open)", source="GitHub opened PRs", source_kind="github",
               current_state="MERGED", state_observed_at=NOW - 7)
    event = build_dashboard(s, NOW)["achievements"]["today"]["started"][0]
    assert event["current"]["state"] == "merged"
    assert event["current"]["source"] == "structured source state"
    assert event["current_state"] == "MERGED"


def test_partial_registered_coverage_exposes_known_verified_lower_bound():
    s = state()
    s["programs"] = program_feed()
    s["programs"]["coverage"].update(registered=3, verified=1, pending=2)
    s["programs"]["checkpoints"] += [
        {"id": "pending-a", "state": "pending_adapter", "accepted": False},
        {"id": "pending-b", "state": "pending_adapter", "accepted": False},
    ]
    result = build_dashboard(s, NOW)["programs"]
    view = result["achievements"]["today"]
    assert view["count"] is None and view["known_count"] == 1
    assert "1 recorded acceptance" in view["summary"] and view["verified_count"] == 1 and view["retained_count"] == 0
    assert result["coverage"]["pending"] == 2
    pending = result["checkpoints"][1]
    assert pending["status_label"] == "Evidence connection pending"
    assert pending["platforms"] == [] and not pending["historical_accepted"]


def test_program_acceptance_requires_explicit_owner_acceptance_flag():
    s = state()
    s["programs"] = program_feed()
    del s["programs"]["checkpoints"][0]["accepted"]
    cp = build_dashboard(s, NOW)["programs"]["checkpoints"][0]
    assert not cp["accepted_verified"] and not cp["historical_accepted"]
    assert cp["platforms"] == []


def test_program_source_failure_downgrades_cached_current_availability_without_retiming():
    s = state()
    s["programs"] = program_feed()
    s["programs"].update(stale=True, error="Registry source unavailable")
    result = build_dashboard(s, NOW)["programs"]
    cp = result["checkpoints"][0]
    assert cp["historical_accepted"] and not cp["accepted_verified"]
    assert cp["current"]["state"] == "stale" and cp["current"]["stale"]
    assert cp["current"]["reported_state"] == "available"
    assert cp["current"]["observed_at"] == NOW - 3
    assert cp["accepted_at"] == NOW - 1800 and cp["verified_at"] == NOW - 2
    assert result["achievements"]["today"]["count"] is None


def test_unconfigured_registry_is_not_a_complete_zero_product_history():
    s = state()
    s["programs"] = {"state": "unconfigured", "observed_at": NOW, "checkpoints": [],
                     "coverage": {"registered": 0, "verified": 0, "pending": 0}}
    out = build_dashboard(s, NOW)["programs"]
    assert out["state"] == "unconfigured" and out["partial"]
    assert out["achievements"]["today"]["count"] is None
    assert out["achievements"]["week"]["count"] is None


def test_pending_adapter_kind_is_normalized_without_invented_pass_facts():
    s = state()
    s["programs"] = program_feed()
    s["programs"]["coverage"].update(registered=2, pending=1)
    s["programs"]["checkpoints"].append({"id": "pending-native", "kind": "pending_adapter",
                                         "state": "pending", "accepted": False, "facts": {}})
    cp = build_dashboard(s, NOW)["programs"]["checkpoints"][1]
    assert cp["state"] == "pending_adapter" and cp["status_label"] == "Evidence connection pending"
    assert not cp["accepted_verified"] and not cp["historical_accepted"] and cp["platforms"] == []


def test_retained_acceptance_summary_never_calls_stale_receipts_currently_verified():
    s = state()
    s["programs"] = program_feed()
    s["programs"]["checkpoints"][0].update(state="stale", stale=True, error="Digest mismatch")
    view = build_dashboard(s, NOW)["programs"]["achievements"]["today"]
    assert view["count"] is None and view["known_count"] == 1
    assert view["verified_count"] == 0 and view["retained_count"] == 1
    assert "verification needs rechecking" in view["summary"]
    assert "currently verified" not in view["summary"]


def test_actual_javascript_status_classifier_is_fail_closed_without_a_browser():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("browser_executive.py")
    spec = importlib.util.spec_from_file_location("executive_browser_unit_checks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.status_classifier_checks()
    assert result["status"] == "PASS" and result["checks"] >= 100
