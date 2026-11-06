"""Bounded read-only accepted-program evidence. No producer writes or workloads."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import selectors
import stat
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from zai_devdashd import config as site

SCHEMA_VERSION = 1
MAX_REGISTRY = 256 * 1024
MAX_RECEIPT = 1024 * 1024
MAX_PROGRAMS = 16
MAX_RECEIPTS = 32
REFRESH_S = 120
CYCLE_S = 30
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DOCS = {"README.md", "CHANGELOG.md"}
REQUIRED = (
    "final-closeout-source.receipt.json", "merge-acceptance.receipt.json",
    "closeout-source-equivalence.receipt.json", "signed-build-2.artifacts.json",
    "signed-build-2.receipt.json", "signed-units-1.acceptance.json",
    "signed-units-1.summary.json", "signed-units-1.receipt.json",
    "candidate3-source-native.acceptance.json", "simulator-artifact-acceptance.json",
    "final-issue-closure-audit.json", "final-ci.receipt.json",
    "deepsource-final.receipt.json", "codex-review3.receipt.json",
)


class EvidenceError(Exception):
    def __init__(self, message, receipt=None):
        super().__init__(message)
        self.receipt = receipt


def _text(value, length=512):
    return value[:length] if isinstance(value, str) and not any(ord(c) < 32 for c in value) else None


def _int(value, low=0, high=1000000):
    return value if type(value) is int and low <= value <= high else None


def _epoch(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if type(value) not in (float, int):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _need(condition, receipt):
    if not condition:
        raise EvidenceError("Receipt semantics invalid: " + receipt, receipt)


def _clock(value, now, receipt):
    result = _epoch(value)
    _need(result is not None and result <= now + 300, receipt)
    return result


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _read_json(path, limit):
    path = Path(path)
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise EvidenceError("Source path is not canonical")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise EvidenceError("Source size/type bound")
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(content) > limit or any(getattr(before, f) != getattr(after, f) for f in fields):
            raise EvidenceError("Source changed or exceeded bound")
        return bytes(content), json.loads(content, object_pairs_hook=_pairs)
    finally:
        os.close(descriptor)


def _command(arguments, deadline, limit=MAX_RECEIPT):
    left = min(5, deadline - time.monotonic())
    if left <= 0:
        raise EvidenceError("Read-only command deadline")
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
                       GH_PROMPT_DISABLED="1", GIT_PAGER="cat")
    child = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=environment, bufsize=0)
    output, selector = bytearray(), selectors.DefaultSelector()
    end = time.monotonic() + left
    try:
        selector.register(child.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise EvidenceError("Read-only command deadline")
            for key, _ in selector.select(remaining):
                data = os.read(key.fileobj.fileno(), min(65536, limit + 1 - len(output)))
                if not data:
                    selector.unregister(key.fileobj)
                else:
                    output.extend(data)
                    if len(output) > limit:
                        raise EvidenceError("Read-only command output bound")
        remaining = end - time.monotonic()
        if remaining <= 0 or child.wait(timeout=max(0.001, remaining)) != 0:
            raise EvidenceError("Read-only command unavailable")
        return bytes(output)
    finally:
        selector.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        child.stdout.close()


def _git(lineage, deadline):
    if not site.adapter_enabled("git"):
        raise EvidenceError("Git evidence adapter is disabled")
    repository = Path(lineage["repository_path"])
    if not repository.is_absolute() or repository.resolve(strict=True) != repository or not repository.is_dir():
        raise EvidenceError("Git repository unavailable")
    prefix = ["/usr/bin/git", "--no-optional-locks", "--no-replace-objects",
              "-c", "core.fsmonitor=false", "-C", str(repository)]
    names = ("qualified", "published", "merge", "docs")
    commits = {}
    raw = _command(prefix + ["show", "-s", "--format=%H%x09%T%x09%ct"] +
                   [lineage[n] for n in names], deadline, 16384).decode()
    by_sha = {}
    for row in raw.splitlines():
        fields = row.split("\t")
        if len(fields) != 3 or not SHA.fullmatch(fields[0]) or not SHA.fullmatch(fields[1]):
            raise EvidenceError("Git commit metadata invalid")
        by_sha[fields[0]] = {"sha": fields[0], "tree": fields[1], "committed_at": _epoch(int(fields[2]))}
    for name in names:
        if lineage[name] not in by_sha:
            raise EvidenceError("Git commit missing")
        commits[name] = by_sha[lineage[name]]
    for left, right in (("qualified", "published"), ("published", "merge"), ("merge", "docs")):
        _command(prefix + ["merge-base", "--is-ancestor", lineage[left], lineage[right]], deadline, 1024)
    if commits["published"]["tree"] != commits["merge"]["tree"]:
        raise EvidenceError("Accepted merge tree differs from published candidate")
    for right in ("published", "docs"):
        changed = _command(prefix + ["diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                                    "--name-only", lineage["qualified"], lineage[right], "--"], deadline, 131072)
        if not set(changed.decode().splitlines()).issubset(DOCS):
            raise EvidenceError("Product bytes differ across closeout lineage")
    return {"verified": True, "commits": commits, "merge_tree": commits["merge"]["tree"]}


def _github(program, issues, gh_path, deadline):
    if not site.adapter_enabled("github"):
        raise EvidenceError("GitHub evidence adapter is disabled")
    configured = program["github"]
    owner, name = configured["repository"].split("/")
    selections = [
        "parent:pullRequest(number:%d){number state isDraft headRefOid baseRefName}" % configured["parent_pr"],
        "pr:pullRequest(number:%d){number state isDraft headRefOid baseRefName mergeCommit{oid} mergedAt}" % configured["pr"],
        "canonical_issue:issue(number:%d){number state stateReason}" % configured["canonical_issue"],
    ]
    selections += ["i%d:issue(number:%d){number state stateReason}" % (i, number)
                   for i, number in enumerate(issues)]
    query = "query{repository(owner:%s,name:%s){%s}}" % (json.dumps(owner), json.dumps(name), " ".join(selections))
    response = json.loads(_command([gh_path, "api", "graphql", "-f", "query=" + query], deadline))
    if not isinstance(response, dict) or response.get("errors"):
        raise EvidenceError("Current GitHub reconciliation unavailable")
    data = response.get("data")
    result = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise EvidenceError("Current GitHub reconciliation unavailable")
    result["issues"] = [result.pop("i%d" % i) for i in range(len(issues))]
    return result


def _current(raw, program, issue_numbers, expected_base, now):
    if not isinstance(raw, dict):
        raise EvidenceError("Current GitHub reconciliation unavailable")
    configured = program["github"]
    result = {"state": "available", "observed_at": now, "attempted_at": now, "error": None,
              "v1_promoted": None, "issues": [], "reopened_issues": [], "stale": False}
    for source, target, number in (("parent", "parent_pr", configured["parent_pr"]),
                                   ("pr", "pr", configured["pr"])):
        row = raw.get(source)
        if not isinstance(row, dict) or row.get("number") != number or type(row.get("number")) is not int:
            raise EvidenceError("Current GitHub identity unavailable")
        state = str(row.get("state") or "").upper()
        head, base = row.get("headRefOid"), _text(row.get("baseRefName"), 200)
        if state not in ("OPEN", "CLOSED", "MERGED") or not SHA.fullmatch(str(head)) or not base:
            raise EvidenceError("Current GitHub PR metadata unavailable")
        result[target] = {"number": number, "state": state.lower(), "head": head, "base": base}
        if source == "parent":
            if type(row.get("isDraft")) is not bool:
                raise EvidenceError("Current parent PR draft state unavailable")
            result[target]["draft"] = row["isDraft"]
        else:
            merge = (row.get("mergeCommit") or {}).get("oid")
            merged_at = _epoch(row.get("mergedAt"))
            result[target].update(merge_commit=merge if SHA.fullmatch(str(merge)) else None,
                                  merged_at=merged_at)
            lineage = program["lineage"]
            if state != "MERGED" or merge != lineage["merge"] or head != lineage["published"] or base != expected_base:
                result["state"] = "attention"
    canonical = raw.get("canonical_issue")
    if not isinstance(canonical, dict) or type(canonical.get("number")) is not int or canonical.get("number") != configured["canonical_issue"]:
        raise EvidenceError("Current canonical issue identity unavailable")
    if str(canonical.get("state")).upper() not in ("OPEN", "CLOSED"):
        raise EvidenceError("Current canonical issue state unavailable")
    result["canonical_issue"] = {"number": canonical["number"], "state": canonical["state"].lower()}
    received = raw.get("issues")
    if not isinstance(received, list) or len(received) != len(issue_numbers):
        raise EvidenceError("Current issue coverage unavailable")
    mapped = {item.get("number"): item for item in received if isinstance(item, dict) and type(item.get("number")) is int}
    if set(mapped) != set(issue_numbers) or len(mapped) != len(received):
        raise EvidenceError("Current issue coverage unavailable")
    for number in issue_numbers:
        item = mapped[number]
        state = str(item.get("state") or "").lower()
        if state not in ("open", "closed"):
            raise EvidenceError("Current issue state unavailable")
        result["issues"].append({"number": number, "state": state,
                                 "reason": _text(item.get("stateReason"), 64)})
        if state == "open":
            result["reopened_issues"].append(number)
    if result["reopened_issues"]:
        result["state"] = "attention"
    return result


def _closeout(program, docs, now, git_reader):
    lineage = program["lineage"]
    q, p, m, d = [lineage[key] for key in ("qualified", "published", "merge", "docs")]
    bound_sources = {"signed-build-2.artifacts.json", "signed-build-2.receipt.json",
                     "signed-units-1.acceptance.json", "signed-units-1.receipt.json",
                     "candidate3-source-native.acceptance.json", "simulator-artifact-acceptance.json",
                     "deepsource-final.receipt.json", "codex-review3.receipt.json"}
    for name, receipt in docs.items():
        if name in bound_sources or "source" in receipt:
            _need(receipt.get("source") == q, name)
    def receipt(name):
        return docs[name]
    name = "merge-acceptance.receipt.json"
    merge = receipt(name)
    _need(type(merge.get("pr")) is int and merge["pr"] == program["github"]["pr"] and
          merge.get("merged") is True and merge.get("mergeCommit") == m and
          merge.get("qualifiedProductSource") == q and merge.get("reviewedPublishedHead") == p and
          merge.get("identicalToPublishedCandidate") is True and merge.get("qualifiedSourceIsAncestor") is True and
          merge.get("v1Promoted") is False and SHA.fullmatch(str(merge.get("mergeTree"))) and
          _text(merge.get("base"), 200), name)
    _clock(merge.get("observedAt"), now, name)
    for name, fields in (
        ("closeout-source-equivalence.receipt.json", {"qualifiedProductSource": q, "publishedCandidate": p}),
        ("final-closeout-source.receipt.json", {"acceptedMerge": m, "closeoutHead": d, "qualifiedProductSource": q})):
        row = receipt(name)
        _need(all(row.get(key) == value for key, value in fields.items()) and row.get("productBytesUnchanged") is True, name)
        delta = row.get("delta", row.get("deltaFromQualifiedSource"))
        _need(isinstance(delta, list) and set(delta).issubset(DOCS), name)

    name = "signed-build-2.artifacts.json"
    artifacts = receipt(name)
    versions, signatures = artifacts.get("versions"), artifacts.get("signatures")
    _need(isinstance(versions, list) and versions and all(isinstance(v, dict) for v in versions), name)
    build = versions[0].get("build")
    _need(isinstance(build, str) and re.fullmatch(r"[0-9]+\.[0-9]+", build) and
          all(v.get("build") == build and v.get("version") == "1." + build for v in versions), name)
    _need(isinstance(signatures, list) and len(signatures) >= 3 and all(isinstance(s, dict) for s in signatures), name)
    roles = {s.get("name") for s in signatures}
    teams = {s.get("team") for s in signatures}
    _need({"app", "helper"}.issubset(roles) and any(str(role).startswith("embedded-helper") for role in roles) and
          len(teams) == 1 and all(_text(team, 64) for team in teams) and
          all(type(s.get("verificationExitCode")) is int and s["verificationExitCode"] == 0 for s in signatures), name)

    name = "signed-build-2.receipt.json"
    build_run = receipt(name)
    _need(type(build_run.get("exitCode")) is int and build_run["exitCode"] == 0, name)
    build_at = _clock(build_run.get("finishedAt"), now, name)
    name = "signed-units-1.acceptance.json"
    units = receipt(name)
    passed, suites = _int(units.get("passed"), 1), _int(units.get("suites"), 1)
    _need(passed is not None and suites is not None and suites <= passed and units.get("build") == build and
          _int(units.get("failed")) == 0 and _int(units.get("skipped")) == 0, name)
    name = "signed-units-1.summary.json"
    summary = receipt(name)
    devices = summary.get("devicesAndConfigurations")
    _need(summary.get("result") == "Passed" and _int(summary.get("passedTests"), 1) == passed and
          _int(summary.get("totalTestCount"), 1) == passed and _int(summary.get("failedTests")) == 0 and
          _int(summary.get("skippedTests")) == 0 and isinstance(devices, list) and devices and
          all(isinstance(v, dict) and (v.get("device") or {}).get("platform") == "macOS" for v in devices), name)
    unit_result_at = _clock(summary.get("finishTime"), now, name)
    name = "signed-units-1.receipt.json"
    unit_run = receipt(name)
    _need(type(unit_run.get("exitCode")) is int and unit_run["exitCode"] == 0, name)
    unit_at = _clock(unit_run.get("finishedAt"), now, name)
    _need(unit_result_at <= unit_at <= unit_result_at + 300, name)
    name = "candidate3-source-native.acceptance.json"
    native = receipt(name)
    new, expected = _int(native.get("newTestsExecutedPassed"), 1), _int(native.get("newTestsExpected"), 1)
    names, scoped, reused, rechecked = (native.get("newTestNames"), _int(native.get("lintScopedFiles"), 1),
                                      _int(native.get("unchangedSincePriorFullLint")), native.get("changedFilesNarrowLintZero"))
    _need(new == expected and new is not None and new <= passed and _int(native.get("totalTestsExecutedPassed"), 1) == passed and
          _int(native.get("skipped")) == 0 and isinstance(names, list) and len(names) == new and
          len(set(names)) == new and all(isinstance(n, str) for n in names) and
          isinstance(rechecked, list) and all(isinstance(n, str) for n in rechecked) and
          scoped is not None and reused is not None and scoped == reused + len(rechecked), name)

    name = "simulator-artifact-acceptance.json"
    sim = receipt(name)
    app, xcresult, source_state, checks = (sim.get("app") or {}, sim.get("xcresult") or {},
                                          sim.get("sourceState") or {}, sim.get("checks") or {})
    architectures = app.get("architectures")
    pins, warnings = _int(source_state.get("pinnedCheckouts"), 1), _int(xcresult.get("warningCount"))
    required_checks = {"nativeExitZero", "xcresultSucceeded", "exactSource", "onlyThreeAuthorizedIdentityChanges",
                       "canonicalProjectIdentity", "appPlistIdentity", "dualArchitecture",
                       "packageManifestUnchanged", "allCheckoutPinsUnchanged"}
    _need(sim.get("result") == "PASS" and type(sim.get("nativeExitCode")) is int and sim["nativeExitCode"] == 0 and
          sim.get("pair") == app.get("CFBundleVersion") and sim.get("pair") != build and
          isinstance(sim.get("pair"), str) and re.fullmatch(r"[0-9]+\.[0-9]+", sim["pair"]) and
          isinstance(architectures, list) and set(architectures) == {"arm64", "x86_64"} and len(architectures) == 2 and
          str(app.get("sdk")).startswith("iphonesimulator") and source_state.get("head") == q and
          pins is not None and warnings is not None and xcresult.get("status") == "succeeded" and
          _int(xcresult.get("errorCount")) == 0 and isinstance(checks, dict) and checks and
          required_checks.issubset(checks) and all(value is True for value in checks.values()) and
          pins == program.get("expected_checkout_pins") and
          "unsigned" in str(sim.get("scope")).lower() and "compile" in str(sim.get("scope")).lower(), name)
    sim_at = _clock(sim.get("finishedAt"), now, name)
    name = "codex-review3.receipt.json"
    review = receipt(name)
    _need(review.get("verdict") == "PASS" and _int(review.get("files"), 1) is not None and
          _int(review.get("reviewCycle"), 1, 3) is not None and review.get("introducedActionableFindings") == [] and
          _int(review.get("manifestMismatches")) == 0 and review.get("cleanDetachedAtCompletion") is True and
          review.get("baseIsAncestor") is True, name)
    review_at = _clock(review.get("completedAt"), now, name)
    name = "deepsource-final.receipt.json"
    ds = receipt(name)
    _need(ds.get("swiftVerdict") == "PASSED" and _int(ds.get("swiftIssues")) == 0 and
          _int(ds.get("allLanguageIssueCounts")) == 0 and ds.get("suppressionsAdded") is False and
          ds.get("ignoredFindings") is False, name)
    ds_at = _clock(ds.get("observedAt"), now, name)
    name = "final-ci.receipt.json"
    ci = receipt(name)
    statuses, check_runs = ci.get("statuses"), ci.get("checks")
    _need(ci.get("publishedHead") == p and ci.get("qualifiedProductSource") == q and
          ci.get("productBytesUnchanged") is True and ci.get("combinedStatus") == "success" and
          isinstance(statuses, list) and statuses and all(isinstance(s, dict) and s.get("state") == "success" for s in statuses) and
          isinstance(check_runs, list) and check_runs and
          all(isinstance(c, dict) and c.get("status") == "completed" and
              c.get("conclusion") in ("success", "skipped", "neutral") for c in check_runs) and
          (ci.get("completedLifecycleRun") or {}).get("source") == q and
          (ci.get("completedLifecycleRun") or {}).get("conclusion") == "success", name)
    ci_at = _clock(ci.get("observedAt"), now, name)
    name = "final-issue-closure-audit.json"
    closure = receipt(name)
    issue_rows = closure.get("issues")
    _need(isinstance(issue_rows, list) and issue_rows and len(issue_rows) <= 64 and
          all(isinstance(i, dict) and _int(i.get("number"), 1) is not None and
              i.get("state") == "closed" and i.get("stateReason") == "completed" and
              i.get("closureValid") is True for i in issue_rows), name)
    _need(len({i["number"] for i in issue_rows}) == len(issue_rows), name)
    configured = program["github"]
    parent = closure.get("parent%d" % configured["parent_pr"]) or {}
    child = closure.get("child%d" % configured["pr"]) or {}
    _need(closure.get("canonical%d" % configured["canonical_issue"]) == "open" and parent.get("state") == "open" and
          parent.get("draft") is True and parent.get("head") == m and child.get("merged") is True and
          child.get("merge") == m and child.get("base") == merge["base"], name)
    closure_at = _clock(closure.get("observedAt"), now, name)

    try:
        verified = git_reader(lineage)
    except Exception:
        raise EvidenceError("Git lineage verification unavailable") from None
    if not isinstance(verified, dict) or verified.get("verified") is not True:
        raise EvidenceError("Git lineage verification unavailable")
    commits = verified.get("commits") or {}
    for key in ("qualified", "published", "merge", "docs"):
        row = commits.get(key) or {}
        if row.get("sha") != lineage[key] or _epoch(row.get("committed_at")) is None:
            raise EvidenceError("Git lineage commit clock unavailable")
    if verified.get("merge_tree") is not None and verified["merge_tree"] != merge["mergeTree"]:
        raise EvidenceError("Git merge tree differs from acceptance receipt")
    accepted_at = _clock(commits["docs"]["committed_at"], now, "Git docs commit")
    if accepted_at < max(build_at, unit_at, sim_at, review_at, ds_at, ci_at, closure_at):
        raise EvidenceError("Acceptance clock predates a required terminal gate")
    platforms = [
        {"id": "macos", "title": "Mac signed build and hosted tests", "machine": "Mini",
         "build": build, "signed": True, "compile_only": False,
         "tests": {"passed": passed, "suites": suites, "new": new, "failed": units["failed"], "skipped": units["skipped"]},
         "occurred_at": unit_result_at},
        {"id": "ios_simulator", "title": "iOS Simulator compile/link", "machine": "Worker",
         "build": sim["pair"], "signed": False, "compile_only": True, "tests": None,
         "architectures": sorted(architectures), "pinned_checkouts": pins, "warnings": warnings,
         "occurred_at": sim_at},
    ]
    facts = {
        "v1_promoted": merge["v1Promoted"], "platforms": platforms,
        "review": {"verdict": review["verdict"], "files": review["files"], "cycle": review["reviewCycle"], "occurred_at": review_at},
        "lint": {"scoped_files": scoped, "reused_full_lint_files": reused, "rechecked_files": len(rechecked)},
        "deepsource": {"verdict": ds["swiftVerdict"], "issues": ds["swiftIssues"],
                       "all_language_issues": ds["allLanguageIssueCounts"], "occurred_at": ds_at},
        "ci": {"state": ci["combinedStatus"], "statuses": [{"name": _text(s.get("context"), 160), "state": s["state"]} for s in statuses],
               "checks": [{"name": _text(c.get("name"), 160), "state": c["conclusion"]} for c in check_runs], "occurred_at": ci_at},
        "closures": {"count": len(issue_rows), "issues": [{"number": i["number"], "state": i["state"], "reason": i["stateReason"]} for i in issue_rows],
                     "occurred_at": closure_at},
    }
    public_lineage = {key: lineage[key] for key in ("qualified", "published", "merge", "docs")}
    public_lineage.update(verified=True, commits={key: {"sha": commits[key]["sha"],
                           "committed_at": _epoch(commits[key]["committed_at"])} for key in public_lineage})
    return facts, public_lineage, accepted_at, max(build_at, unit_result_at, sim_at), merge["base"]


def _pending(now, state="pending", error=None):
    return {"schema_version": 1, "state": state, "observed_at": None, "attempted_at": now,
            "partial": True, "error": error, "coverage": {"registered": None, "verified": None, "pending": None,
            "expected_receipts": None, "present_receipts": None, "valid_receipts": None, "errors": []},
            "checkpoints": [], "lanes": [], "sources": []}


def _failed_feed(previous, now, error):
    result = copy.deepcopy(previous) if previous else _pending(now, "unavailable")
    if previous:
        result["historic_coverage"] = {"observed_at": previous.get("observed_at"),
                                       "coverage": copy.deepcopy(previous.get("coverage"))}
    result.update(partial=True, attempted_at=now, state="unavailable", error=error)
    result["coverage"] = {"registered": None, "verified": None, "pending": None,
                          "expected_receipts": None, "present_receipts": None,
                          "valid_receipts": None, "errors": [error]}
    for checkpoint in result["checkpoints"]:
        checkpoint.update(stale=True, state="stale", attempted_at=now, error=error)
        if isinstance(checkpoint.get("current"), dict):
            checkpoint["current"].update(state="stale", stale=True)
    return result


def _program_metadata(program):
    keys = ("id", "title", "program", "scope", "kind", "owner", "objective", "impact", "next_step", "release_boundary")
    return {key: _text(program.get(key), 512 if key not in ("id", "kind", "scope") else 96) for key in keys}


def _sources(program):
    result = []
    for source in (program.get("sources") or [])[:16]:
        if isinstance(source, dict) and source.get("kind") in ("codex_chat", "json_receipt", "status_file", "controller_state"):
            result.append({key: value for key in ("kind", "id", "path")
                           if (value := _text(source.get(key), 512)) is not None})
    return result


def _definition(program):
    if not isinstance(program, dict) or not isinstance(program.get("id"), str) or not ID.fullmatch(program["id"]):
        raise EvidenceError("Program identity invalid")
    if program.get("kind") != "checkpoint_closeout_v1":
        return
    if _int(program.get("expected_checkout_pins"), 1, 10000) is None:
        raise EvidenceError("Configured checkout pin count is required")
    lineage, github = program.get("lineage"), program.get("github")
    if not isinstance(lineage, dict) or not isinstance(github, dict):
        raise EvidenceError("Program lineage/GitHub definition missing")
    if program.get("scope") != "v1.x" or any(not SHA.fullmatch(str(lineage.get(k))) for k in ("qualified", "published", "merge", "docs")):
        raise EvidenceError("Program scope/lineage invalid")
    if not REPOSITORY.fullmatch(str(github.get("repository"))) or any(
            _int(github.get(k), 1) is None for k in ("parent_pr", "canonical_issue", "pr")):
        raise EvidenceError("Program GitHub identities invalid")


def _checkpoint(program, now, previous, deadline, git_reader, github_reader, gh_path):
    checkpoint = dict(_program_metadata(program), state="pending", accepted=False, stale=False,
                      facts={}, lineage={"verified": False}, evidence=[], current=None, observed_at=None, verified_at=None,
                      attempted_at=now, accepted_at=None, occurred_at=None, error=None,
                      coverage={"expected": len(REQUIRED) if program.get("kind") == "checkpoint_closeout_v1" else 0,
                                "present": None, "valid": None},
                      github_identity=copy.deepcopy(program.get("github")))
    if program.get("kind") != "checkpoint_closeout_v1":
        checkpoint.update(state="pending", error="Evidence adapter not implemented",
                          coverage={"expected": 0, "present": 0, "valid": 0})
        return checkpoint
    docs, evidence = {}, []
    expected = program.get("receipts")
    issue_numbers, expected_base = [], None
    try:
        if not isinstance(expected, list) or len(expected) > MAX_RECEIPTS:
            raise EvidenceError("Receipt registry bound")
        if len({r.get("id") for r in expected if isinstance(r, dict)}) != len(expected):
            raise EvidenceError("Receipt registry identities invalid")
        by_id = {r["id"]: r for r in expected}
        if set(by_id) - set(REQUIRED):
            raise EvidenceError("Unsupported receipt for this adapter version")
        checkpoint["coverage"]["registered_receipts"] = len(expected)
        root = Path(program["receipt_root"])
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            raise EvidenceError("Receipt root unavailable")
        checkpoint["coverage"].update(present=0, valid=0)
        for name, entry in by_id.items():
            if time.monotonic() >= deadline:
                raise EvidenceError("Evidence collection deadline")
            relative = Path(entry.get("path", ""))
            item = {"id": _text(name, 160), "kind": "json_receipt", "sha256": entry.get("sha256") if DIGEST.fullmatch(str(entry.get("sha256"))) else None,
                    "source": _text(relative.name, 160), "state": "unavailable"}
            evidence.append(item)
            if (not isinstance(name, str) or not ID.fullmatch(name) or not DIGEST.fullmatch(str(entry.get("sha256"))) or
                    relative.is_absolute() or not relative.parts or ".." in relative.parts):
                raise EvidenceError("Receipt definition invalid")
            path = root / relative
            try:
                if os.path.lexists(path):
                    checkpoint["coverage"]["present"] += 1
                content, value = _read_json(path, MAX_RECEIPT)
                if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    item["state"] = "hash_mismatch"
                    continue
                if not isinstance(value, dict):
                    item["state"] = "invalid"
                    continue
                item["state"] = "valid"
                docs[name] = value
                checkpoint["coverage"]["valid"] += 1
            except (OSError, ValueError, EvidenceError, RecursionError):
                continue
        checkpoint["evidence"] = evidence
        if not set(REQUIRED).issubset(by_id) or not set(REQUIRED).issubset(docs) or len(docs) != len(expected):
            raise EvidenceError("Required pinned receipts unavailable or invalid")
        facts, lineage, accepted_at, occurred_at, expected_base = _closeout(
            program, docs, now, git_reader or (lambda row: _git(row, deadline)))
        checkpoint.update(state="verified", accepted=True, observed_at=now, verified_at=now, facts=facts,
                          lineage=lineage, accepted_at=accepted_at, occurred_at=occurred_at,
                          accepted_at_source="git docs commit", accepted_base=expected_base, event_id=program["id"] + ":" + lineage["qualified"] + ":" + lineage["merge"])
        issue_numbers = [issue["number"] for issue in facts["closures"]["issues"]]
    except (EvidenceError, OSError, KeyError, TypeError, ValueError, RecursionError, OverflowError) as error:
        message = str(error) if isinstance(error, EvidenceError) else "Accepted evidence validation unavailable"
        if isinstance(error, EvidenceError) and error.receipt in docs:
            checkpoint["coverage"]["valid"] -= 1
            for item in evidence:
                if item["id"] == error.receipt:
                    item["state"] = "invalid"
        coverage = checkpoint["coverage"]
        same_lineage = previous and all((previous.get("lineage") or {}).get(key) == program["lineage"][key]
                                          for key in ("qualified", "published", "merge", "docs"))
        if same_lineage and previous.get("accepted"):
            retained = {key: copy.deepcopy(previous[key]) for key in (
                "accepted", "facts", "lineage", "accepted_at", "accepted_at_source", "accepted_base", "occurred_at", "observed_at", "verified_at", "event_id") if key in previous}
            checkpoint.update(retained, state="stale", stale=True, error=message,
                              historic_evidence=copy.deepcopy(previous.get("evidence", [])))
            issue_numbers = [issue["number"] for issue in checkpoint["facts"]["closures"]["issues"]]
        else:
            checkpoint.update(state="partial" if coverage["valid"] else "unavailable", stale=True, error=message)
        checkpoint["evidence"] = evidence

    try:
        raw = (github_reader(program, issue_numbers) if github_reader else
               _github(program, issue_numbers, gh_path, deadline))
        if expected_base is None:
            expected_base = checkpoint.get("accepted_base")
        if expected_base is None and "merge-acceptance.receipt.json" in docs:
            expected_base = docs["merge-acceptance.receipt.json"].get("base")
        checkpoint["current"] = _current(raw, program, issue_numbers, expected_base, now)
    except Exception:
        old = ((previous or {}).get("current") or {}) if (previous or {}).get("github_identity") == program.get("github") else {}
        if old.get("observed_at") is not None:
            checkpoint["current"] = dict(copy.deepcopy(old), state="stale", stale=True, attempted_at=now,
                                         error="Current GitHub reconciliation unavailable")
        else:
            checkpoint["current"] = {"state": "unavailable", "observed_at": None, "attempted_at": now,
                                     "stale": True, "error": "Current GitHub reconciliation unavailable",
                                     "parent_pr": None, "pr": None, "canonical_issue": None,
                                     "issues": None, "reopened_issues": None, "v1_promoted": None}
    return checkpoint


def _defaults(registry_path, gh_path):
    from zai_devdashd import config as site
    if registry_path is None:
        registry_path = site.programs_registry() if hasattr(site, "programs_registry") else site.state_dir() / "programs.json"
    if gh_path is None:
        gh_path = ((site.CURRENT.get("github") or {}).get("bin") or "gh")
    return Path(registry_path).expanduser().absolute(), gh_path


def collect(registry_path=None, *, now=None, previous=None, git_reader=None, github_reader=None, gh_path=None):
    now = time.time() if now is None else now
    path, gh_path = _defaults(registry_path, gh_path)
    deadline = time.monotonic() + CYCLE_S
    try:
        content, registry = _read_json(path, MAX_REGISTRY)
        if not isinstance(registry, dict) or type(registry.get("schema_version")) is not int or registry["schema_version"] != 1:
            raise EvidenceError("Program registry schema unavailable")
        definitions = registry.get("programs")
        if not isinstance(definitions, list) or len(definitions) > MAX_PROGRAMS:
            raise EvidenceError("Program registry count bound")
        for definition in definitions:
            _definition(definition)
        if len({p["id"] for p in definitions}) != len(definitions):
            raise EvidenceError("Duplicate program identity")
    except (EvidenceError, OSError, ValueError, TypeError, RecursionError, KeyError):
        return _failed_feed(previous, now, "Program registry unavailable or invalid")
    old = {p.get("id"): p for p in (previous or {}).get("checkpoints", [])}
    checkpoints = [_checkpoint(program, now, old.get(program["id"]), deadline, git_reader, github_reader, gh_path)
                   for program in definitions]
    def count_receipts(key):
        values = [p["coverage"][key] for p in checkpoints]
        return None if any(value is None for value in values) else sum(values)
    coverage = {"registered": len(definitions), "verified": sum(p["state"] == "verified" for p in checkpoints),
                "pending": sum(p["state"] == "pending" for p in checkpoints),
                "expected_receipts": count_receipts("expected"),
                "present_receipts": count_receipts("present"),
                "valid_receipts": count_receipts("valid"),
                "errors": [p["error"] for p in checkpoints if p.get("error")]}
    partial = any(p["state"] != "verified" or (p.get("current") or {}).get("state") not in ("available", "attention") for p in checkpoints)
    return {"schema_version": 1, "state": "available" if definitions else "unconfigured", "observed_at": now,
            "attempted_at": now, "partial": partial, "error": None,
            "registry_sha256": hashlib.sha256(content).hexdigest(), "coverage": coverage,
            "checkpoints": checkpoints, "lanes": [], "sources": [
                dict(_program_metadata(program), state=checkpoint["state"], sources=_sources(program),
                     boundary="Registered evidence source; only implemented and verified adapters establish accepted facts.")
                for program, checkpoint in zip(definitions, checkpoints)]}


class Cache:
    """One throttled background worker; reads never run probes or wait on I/O."""
    def __init__(self, registry_path=None, *, gh_path=None, clock=None, refresh_s=REFRESH_S, collector=None):
        self.registry_path, self.gh_path = _defaults(registry_path, gh_path)
        self.clock = clock or time.time
        self.refresh_s = max(10, min(3600, refresh_s))
        self.collector = collector or collect
        self._lock = threading.Lock()
        self._value = None
        self._attempted = None
        self._busy = False

    def _run(self, now, previous):
        try:
            value = self.collector(self.registry_path, now=now, previous=previous, gh_path=self.gh_path)
        except Exception:
            value = _failed_feed(previous, now, "Evidence collector unavailable")
        with self._lock:
            self._value, self._busy = value, False

    def get(self):
        now, launch = self.clock(), None
        with self._lock:
            if not self._busy and (self._attempted is None or now < self._attempted or now - self._attempted >= self.refresh_s):
                self._busy, self._attempted = True, now
                launch = threading.Thread(target=self._run, args=(now, copy.deepcopy(self._value)), daemon=True, name="program-evidence")
            value = copy.deepcopy(self._value) if self._value is not None else _pending(now)
            if value.get("observed_at") is not None and now - value["observed_at"] > self.refresh_s * 3:
                value = _failed_feed(value, self._attempted, "Evidence refresh overdue")
        if launch:
            try:
                launch.start()
            except (RuntimeError, OSError):
                with self._lock:
                    self._busy = False
                value.update(partial=True, error="Evidence worker could not start")
        return value
