"""Independent domain acceptance for registered, source-bound program closeouts.

Every receipt is generated synthetic parser data with unrelated identities; no actual acceptance is reproduced.
Native source, binaries, logs, prompts and account metadata are not included.
All collection uses temporary fixtures and injected read-only source responses.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from zai_devdashd import programs
from zai_devdashd.executive import build_dashboard

QUALIFIED = "0e60e3ecfa390a1f801bfc1c6bdd6419e8aa1d4a"
PUBLISHED = "2cf3828d2bab145c686729f0114c590bf223ace1"
MERGE = "c149dfd1ef31c044bd427aa40883d85bd5677c23"
DOCS = "eb9f456050937dbcc07cc245c1406ca6eedf51a5"
BASE = "synthetic/foundation"
ISSUES = {101, 102, 103, 104, 105}
FIXTURES = Path(__file__).parent / "fixtures" / "programs" / "synthetic"


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


ACCEPTED = epoch("2001-06-06T14:32:31Z")
NOW = ACCEPTED + 300
COMMITS = {
    "qualified": {"sha": QUALIFIED, "committed_at": epoch("2001-06-06T13:52:50Z")},
    "published": {"sha": PUBLISHED, "committed_at": epoch("2001-06-06T14:02:40Z")},
    "merge": {"sha": MERGE, "committed_at": epoch("2001-06-06T14:28:00Z")},
    "docs": {"sha": DOCS, "committed_at": ACCEPTED},
}


def github_snapshot():
    return {
        "parent": {"number": 11, "state": "OPEN", "isDraft": True, "headRefOid": DOCS,
                   "baseRefName": "main"},
        "pr": {"number": 13, "state": "MERGED", "isDraft": False, "headRefOid": PUBLISHED,
               "baseRefName": BASE, "mergeCommit": {"oid": MERGE}, "mergedAt": "2001-06-06T14:28:00Z"},
        "canonical_issue": {"number": 12, "state": "OPEN", "stateReason": None},
        "issues": [{"number": n, "state": "CLOSED", "stateReason": "COMPLETED"} for n in sorted(ISSUES)],
    }


class Campaign:
    def __init__(self, tmp_path):
        self.receipts = tmp_path / "accepted-receipts"
        shutil.copytree(FIXTURES, self.receipts)
        self.definition = {
            "id": "p1-up1-correctness-20010606", "kind": "checkpoint_closeout_v1", "expected_checkout_pins": 3,
            "title": "P1 UP1 correctness accepted on the isolated foundation",
            "program": "UP1/P1 foundations", "scope": "v1.x",
            "objective": "Exercise a synthetic receipt schema without performing native work.",
            "impact": "Synthetic parser story only; no actual product acceptance.",
            "next_step": "Keep parent feature rollout and production qualification separate.",
            "release_boundary": "Synthetic parser facts never establish real release acceptance.",
            "receipt_root": str(self.receipts),
            "lineage": {"repository_path": str(tmp_path / "repository"), "qualified": QUALIFIED,
                        "published": PUBLISHED, "merge": MERGE, "docs": DOCS},
            "github": {"repository": "example-owner/example-project", "parent_pr": 11,
                       "canonical_issue": 12, "pr": 13},
            "receipts": [{"id": f.name, "path": f.name, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
                         for f in sorted(self.receipts.glob("*.json"))],
        }
        (tmp_path / "repository").mkdir()
        self.registry = tmp_path / "registry.json"
        self.write_registry()

    def write_registry(self):
        self.registry.write_text(json.dumps({"schema_version": 1, "programs": [self.definition]}))

    def change(self, name, update, repin=False):
        path = self.receipts / name
        document = json.loads(path.read_text())
        update(document)
        path.write_text(json.dumps(document))
        if repin:
            entry = next(r for r in self.definition["receipts"] if r["id"] == name)
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.write_registry()

    def collect(self, now=NOW, previous=None, current=None, git_verified=True, github_error=False):
        def git_reader(lineage):
            assert lineage["qualified"] == QUALIFIED
            assert lineage["docs"] == DOCS
            return {"verified": git_verified, "commits": copy.deepcopy(COMMITS),
                    "error": None if git_verified else "Source lineage is not verified"}

        def github_reader(definition, issue_numbers):
            if github_error:
                raise RuntimeError("SENSITIVE_GITHUB_FAILURE")
            return copy.deepcopy(github_snapshot() if current is None else current)

        return programs.collect(self.registry, now=now, previous=previous,
                                git_reader=git_reader, github_reader=github_reader)


@pytest.fixture
def campaign(tmp_path):
    return Campaign(tmp_path)


def checkpoint(feed):
    assert len(feed["checkpoints"]) == 1
    return feed["checkpoints"][0]


def platform(checkpoint, family):
    return next(p for p in checkpoint["facts"]["platforms"] if p["id"] == family)


def executive_state(feed):
    counts = {"merged": 0, "opened": 1, "gates": 0, "shipped": 0}
    opened = {"kind": "opened", "key": "opened:13", "pr": 13,
              "text": "#13 P1 correctness", "day": "2001-06-06", "time_et": "8:16 AM ET",
              "current_state": "MERGED", "state_observed_at": NOW, "source_kind": "github"}
    block = {"summary": "V1/main scoped workflow counts", "counts": counts,
             "groups": [{"kind": "opened", "items": [opened]}]}
    return {"programs": feed, "overview": {"accomplishments": {
        "observed_at": NOW, "stale": False, "partial": False,
        "today": copy.deepcopy(block), "week": copy.deepcopy(block),
        "source_status": {"github": {"state": "available", "observed_at": NOW}},
        "coverage": {"merged_bases": ["main", "main"]},
    }}}


def test_complete_registered_p1_preserves_source_scope_and_independent_qualifications(campaign):
    feed = campaign.collect()
    cp = checkpoint(feed)
    assert cp["state"] == "verified" and cp["accepted"] is True and cp["stale"] is False
    assert cp["scope"] == "v1.x" and cp["facts"]["v1_promoted"] is False
    assert cp["lineage"]["qualified"] == QUALIFIED and cp["lineage"]["published"] == PUBLISHED
    assert cp["lineage"]["merge"] == MERGE and cp["lineage"]["docs"] == DOCS
    assert cp["lineage"]["verified"] is True
    assert cp["accepted_at"] == ACCEPTED and cp["accepted_at_source"] == "git docs commit"
    mac, simulator = platform(cp, "macos"), platform(cp, "ios_simulator")
    assert mac["build"] in ("12.0", "1.12.0") and mac["signed"] is True and mac["compile_only"] is False
    assert {k: mac["tests"].get(k) for k in ("passed", "suites", "new", "failed", "skipped")} == {"passed": 7, "suites": 2, "new": 3, "failed": 0, "skipped": 0}
    assert simulator["build"] in ("13.0", "1.13.0")
    assert simulator["signed"] is False and simulator["compile_only"] is True and simulator["tests"] is None
    assert set(simulator["architectures"]) == {"arm64", "x86_64"}
    assert simulator["pinned_checkouts"] == 3 and simulator["warnings"] == 2
    assert mac["machine"] == "Mini" and simulator["machine"] == "Worker"
    assert mac["occurred_at"] < ACCEPTED and simulator["occurred_at"] < ACCEPTED
    closures = cp["facts"]["closures"]
    assert closures["count"] == 5 and {i["number"] for i in closures["issues"]} == ISSUES
    assert all(i["state"].lower() == "closed" for i in closures["issues"])
    assert cp["current"]["parent_pr"]["number"] == 11 and cp["current"]["parent_pr"]["draft"] is True
    assert cp["current"]["canonical_issue"]["number"] == 12
    assert cp["current"]["canonical_issue"]["state"].lower() == "open"
    assert cp["current"]["pr"]["number"] == 13 and cp["current"]["pr"]["merge_commit"] == MERGE
    assert {i["number"] for i in cp["current"]["issues"]} == ISSUES
    assert {k: cp["coverage"][k] for k in ("expected", "present", "valid")} == {"expected": 14, "present": 14, "valid": 14}
    assert feed["coverage"]["registered"] == 1


@pytest.mark.parametrize("name", ["signed-units-1.acceptance.json", "simulator-artifact-acceptance.json"])
def test_missing_required_acceptance_never_becomes_a_verified_checkpoint(campaign, name):
    (campaign.receipts / name).unlink()
    cp = checkpoint(campaign.collect())
    assert cp["state"] != "verified" and cp["coverage"]["present"] == 13
    assert cp["coverage"]["valid"] < cp["coverage"]["expected"] and cp["error"]


def test_receipt_content_changed_without_repin_is_rejected_even_if_facts_still_parse(campaign):
    path = campaign.receipts / "signed-units-1.acceptance.json"
    path.write_text(path.read_text() + " ")
    cp = checkpoint(campaign.collect())
    assert cp["state"] != "verified" and cp["coverage"]["valid"] < 14 and cp["error"]


@pytest.mark.parametrize("name,key,value", [
    ("simulator-artifact-acceptance.json", "source", "f" * 40),
    ("merge-acceptance.receipt.json", "qualifiedProductSource", "f" * 40),
    ("final-closeout-source.receipt.json", "productBytesUnchanged", False),
    ("merge-acceptance.receipt.json", "v1Promoted", True),
    ("signed-units-1.acceptance.json", "passed", 6),
    ("candidate3-source-native.acceptance.json", "newTestsExecutedPassed", 2),
    ("deepsource-final.receipt.json", "swiftIssues", 1),
    ("codex-review3.receipt.json", "verdict", "CHANGES_REQUIRED"),
    ("final-ci.receipt.json", "combinedStatus", "failure"),
])
def test_repin_does_not_make_contradictory_acceptance_evidence_valid(campaign, name, key, value):
    campaign.change(name, lambda document: document.update({key: value}), repin=True)
    cp = checkpoint(campaign.collect())
    assert cp["state"] != "verified" and cp["error"]


def test_git_lineage_must_be_proved_beyond_receipt_boolean_claims(campaign):
    cp = checkpoint(campaign.collect(git_verified=False))
    assert cp["state"] != "verified" and cp["lineage"]["verified"] is False


def test_github_initial_failure_is_unknown_current_state_with_historical_result_clocks(campaign):
    cp = checkpoint(campaign.collect(github_error=True))
    assert cp["current"]["state"] == "unavailable" and cp["current"]["observed_at"] is None
    assert cp["current"]["error"] and cp["current"]["v1_promoted"] is None
    assert cp["accepted_at"] == ACCEPTED
    assert platform(cp, "ios_simulator")["compile_only"] is True
    assert "SENSITIVE_GITHUB_FAILURE" not in json.dumps(cp)


def test_github_later_failure_retains_current_last_success_clock_without_fresh_verdict(campaign):
    first = campaign.collect()
    cp = checkpoint(campaign.collect(now=NOW + 120, previous=first, github_error=True))
    assert cp["current"]["state"] == "stale" and cp["current"]["observed_at"] == NOW
    assert cp["current"]["attempted_at"] == NOW + 120 and cp["current"]["error"]
    assert cp["accepted_at"] == ACCEPTED
    assert platform(cp, "macos")["occurred_at"] == platform(checkpoint(first), "macos")["occurred_at"]
    assert platform(cp, "ios_simulator")["occurred_at"] == platform(checkpoint(first), "ios_simulator")["occurred_at"]


def test_reopened_issue_changes_current_attention_without_rewriting_accepted_history(campaign):
    first = campaign.collect()
    current = github_snapshot()
    next(i for i in current["issues"] if i["number"] == 102).update(state="OPEN", stateReason="REOPENED")
    cp = checkpoint(campaign.collect(now=NOW + 120, previous=first, current=current))
    assert cp["current"]["state"] == "attention" and 102 in cp["current"]["reopened_issues"]
    assert cp["accepted_at"] == ACCEPTED and cp["facts"]["closures"]["count"] == 5
    assert cp["facts"]["v1_promoted"] is False


def test_wrong_current_pr_merge_or_target_is_attention_not_false_current_acceptance(campaign):
    current = github_snapshot()
    current["pr"]["mergeCommit"]["oid"] = "f" * 40
    current["pr"]["baseRefName"] = "main"
    cp = checkpoint(campaign.collect(current=current))
    assert cp["current"]["state"] == "attention" and cp["current"]["v1_promoted"] is not False
    assert cp["accepted_at"] == ACCEPTED and cp["facts"]["v1_promoted"] is False


def test_repeat_poll_and_restart_keep_one_event_and_original_result_clocks(campaign):
    first = campaign.collect()
    later = campaign.collect(now=NOW + 120, previous=first)
    restarted = campaign.collect(now=NOW + 240)
    for feed in (later, restarted):
        cp = checkpoint(feed)
        assert cp["event_id"] == checkpoint(first)["event_id"]
        assert cp["accepted_at"] == ACCEPTED
        assert [p["occurred_at"] for p in cp["facts"]["platforms"]] == [p["occurred_at"] for p in checkpoint(first)["facts"]["platforms"]]
        assert cp["observed_at"] > checkpoint(first)["observed_at"]


def test_projection_is_pure_and_keeps_program_acceptance_separate_from_v1_workflow(campaign, monkeypatch):
    feed = campaign.collect()
    state = executive_state(feed)
    before = copy.deepcopy(state)
    def forbidden(*args, **kwargs):
        pytest.fail("Pure program projection attempted I/O or started a worker")
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("threading.Thread.start", forbidden)
    view = build_dashboard(state, now=NOW)
    assert view == build_dashboard(state, now=NOW) and state == before
    assert view["readiness"] == "unverified"
    assert {g["id"] for g in view["goals"]} == {"integration", "qualification", "distribution"}
    assert view["achievements"]["today"]["counts"] == before["overview"]["accomplishments"]["today"]["counts"]
    section = view["programs"]
    assert len(section["checkpoints"]) == 1 and section["checkpoints"][0]["scope"] == "v1.x"
    assert section["achievements"]["today"]["count"] == section["achievements"]["week"]["count"] == 1
    event = section["achievements"]["today"]["completed"][0]
    assert event["accepted_at"] == ACCEPTED and event["evidence_id"] == "program:" + checkpoint(feed)["id"]
    assert event["kind"] == "program_checkpoint"


def test_original_acceptance_day_and_event_identity_survive_polling_and_restart(campaign):
    first = campaign.collect()
    restarted = campaign.collect(now=NOW + 120)
    initial_view = build_dashboard(executive_state(first), now=NOW)
    repeated_view = build_dashboard(executive_state(restarted), now=NOW + 120)
    for view in (initial_view, repeated_view):
        window = view["programs"]["achievements"]["today"]
        assert window["count"] == 1 and len(window["completed"]) == 1
    a, b = (view["programs"]["achievements"]["today"]["completed"][0] for view in (initial_view, repeated_view))
    assert a["evidence_id"] == b["evidence_id"] and a["accepted_at"] == b["accepted_at"] == ACCEPTED
    # Tomorrow's successful verification is not a new completion today.
    tomorrow = NOW + 86400
    future = campaign.collect(now=tomorrow)
    view = build_dashboard(executive_state(future), now=tomorrow)
    assert view["programs"]["achievements"]["today"]["count"] == 0
    assert view["programs"]["achievements"]["week"]["count"] == 1


def test_original_pr_opening_keeps_historical_count_but_current_merged_state(campaign):
    view = build_dashboard(executive_state(campaign.collect()), now=NOW)
    today = view["achievements"]["today"]
    assert today["counts"]["opened"] == 1 and today["counts"]["merged"] == 0
    opened = next(row for row in today["started"] if row.get("pr") == 13)
    assert opened["kind"] == "opened" and opened["current_state"].upper() == "MERGED"
    assert opened["state_observed_at"] == NOW
    assert view["programs"]["checkpoints"][0]["current"]["pr"]["state"].lower() == "merged"


def test_later_missing_local_receipt_retains_aged_verified_facts_and_exposes_failure(campaign):
    first = campaign.collect()
    old = checkpoint(first)
    (campaign.receipts / "simulator-artifact-acceptance.json").unlink()
    retained = checkpoint(campaign.collect(now=NOW + 120, previous=first))
    assert retained["state"] != "verified" and retained["stale"] is True and retained["error"]
    assert retained["verified_at"] == old["verified_at"]
    assert retained["accepted_at"] == old["accepted_at"]
    assert retained["attempted_at"] == NOW + 120
    assert platform(retained, "ios_simulator")["occurred_at"] == platform(old, "ios_simulator")["occurred_at"]
    assert platform(retained, "macos")["tests"]["passed"] == 7


def test_unknown_current_github_does_not_erase_verified_historical_program_acceptance(campaign):
    feed = campaign.collect(github_error=True)
    view = build_dashboard(executive_state(feed), now=NOW)
    cp = view["programs"]["checkpoints"][0]
    assert cp["historical_accepted"] is True
    assert cp["current"]["state"] == "unavailable" and cp["current"]["observed_at"] is None
    assert view["programs"]["achievements"]["today"]["count"] == 1
    assert view["readiness"] == "unverified"


@pytest.mark.parametrize("name", [
    "signed-build-2.artifacts.json", "signed-units-1.acceptance.json",
    "candidate3-source-native.acceptance.json", "simulator-artifact-acceptance.json",
    "deepsource-final.receipt.json", "codex-review3.receipt.json",
])
def test_repin_cannot_replace_missing_source_attribution(campaign, name):
    campaign.change(name, lambda document: document.pop("source"), repin=True)
    cp = checkpoint(campaign.collect())
    assert cp["state"] != "verified" and cp["error"]


def test_closeout_cannot_precede_the_native_result_it_claims_to_accept(campaign):
    after_closeout = datetime.fromtimestamp(ACCEPTED + 60, timezone.utc).isoformat()
    def later_result(document):
        document["finishedAt"] = after_closeout
        document["observedAt"] = after_closeout
    campaign.change("simulator-artifact-acceptance.json", later_result, repin=True)
    cp = checkpoint(campaign.collect())
    assert cp["state"] != "verified" and cp["error"]


def test_empty_registry_does_not_claim_zero_completed_goals_across_the_program(tmp_path):
    registry = tmp_path / "empty-registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "programs": []}))
    def forbidden(*args, **kwargs):
        pytest.fail("Empty registry tried to inspect unregistered work")
    feed = programs.collect(registry, now=NOW, git_reader=forbidden, github_reader=forbidden)
    assert feed["coverage"]["registered"] == 0
    section = build_dashboard(executive_state(feed), now=NOW)["programs"]
    assert section["achievements"]["today"]["count"] is None
    assert section["achievements"]["week"]["count"] is None
