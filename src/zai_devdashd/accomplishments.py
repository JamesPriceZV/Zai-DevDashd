"""Finished work from real evidence: GitHub merges/opens, TestFlight upload logs, green gate records.

Read-only. gh runs with its own stored login; no token is read or printed here.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
from zai_devdashd import config as site
STATUS_MD = site.optional_path("status")
TF_DIR = site.optional_path("uploads")
REFRESH_S = 300
KINDS = ("merged", "shipped", "opened", "gates")
KIND_TITLE = {"merged": "Merged into configured branches", "shipped": "Shipped (TestFlight)", "opened": "Opened PRs", "gates": "Gates passed"}

_GATE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d) E[SD]?T (.*?(?:full exact-head gate #(\d+) ([0-9a-f]{7,40})\b.*rc=0 UNIT EXIT 0.*"
    r"|L-PREP-(\d+): GATE PASS.*?([0-9a-f]{7,40})?\b.*))$")
_TF_RE = re.compile(r"^(\w{3} \w{3} +\d+ [\d:]+) E[SD]T (\d{4}) (ios|macos) UPLOAD EXIT 0\s*$")
_BUILD_RE = re.compile(r"BUILD=([\d.]+)")


def window_start(now: datetime) -> datetime:
    """Rolling 7 days, from local midnight 6 days before today, ET."""
    today = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=6)


def week_start(now: datetime) -> datetime:
    today = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=today.weekday())


def _iso(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(ET)
    except (ValueError, AttributeError):
        return None


class SourceUnavailable(RuntimeError):
    pass


def _gh(args: list[str]) -> list:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceUnavailable("GitHub achievements lookup unavailable") from exc
    if out.returncode != 0:
        raise SourceUnavailable("GitHub achievements lookup failed")
    try:
        data = json.loads(out.stdout)
    except ValueError as exc:
        raise SourceUnavailable("Invalid GitHub achievements response") from exc
    if not isinstance(data, list) or len(data) >= 1000:
        raise SourceUnavailable("GitHub achievements response is incomplete")
    return data


def github_items(since: datetime) -> list[dict]:
    cfg = site.CURRENT.get("github") or {}
    repository, integration = cfg.get("repo", ""), cfg.get("integration_branch", "")
    if not site.adapter_enabled("github") or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise SourceUnavailable("GitHub achievement adapter is not configured")
    bases = set(filter(None, ("main", integration)))
    day = since.astimezone(timezone.utc).strftime("%Y-%m-%d")
    items = []
    for pr in _gh(["pr", "list", "-R", repository, "--state", "merged", "--search", "merged:>=%s" % day,
                   "--limit", "1000", "--json", "number,title,mergedAt,mergeCommit,baseRefName"]):
        when = _iso(pr.get("mergedAt") or "")
        if not when or when < since or pr.get("baseRefName") not in bases:
            continue
        merge_commit = ((pr.get("mergeCommit") or {}).get("oid") or "")
        sha = merge_commit[:8]
        items.append({"kind": "merged", "when": when, "key": "merged:%s" % pr["number"], "pr": pr["number"],
                      "merge_commit": merge_commit, "base": pr["baseRefName"], "title": pr.get("title") or "",
                      "source_kind": "github", "source": "GitHub merged PRs", "url": "https://github.com/%s/pull/%s" % (repository, pr["number"]),
                      "text": "#%s · %s → %s%s" % (pr["number"], pr.get("title", "")[:100], pr["baseRefName"],
                                                  (" as %s" % sha) if sha else "")})
    for pr in _gh(["pr", "list", "-R", repository, "--state", "all", "--search", "created:>=%s" % day,
                   "--limit", "1000", "--json", "number,title,createdAt,state"]):
        when = _iso(pr.get("createdAt") or "")
        if not when or when < since:
            continue
        items.append({"kind": "opened", "when": when, "key": "opened:%s" % pr["number"], "pr": pr["number"],
                      "title": pr.get("title") or "", "source_kind": "github", "source": "GitHub opened PRs",
                      "current_state": str(pr.get("state", "")).lower() if pr.get("state") in ("OPEN", "MERGED", "CLOSED") else "unknown",
                      "state_observed_at": time.time(),
                      "url": "https://github.com/%s/pull/%s" % (repository, pr["number"]),
                      "text": "#%s · %s (%s)" % (pr["number"], pr.get("title", "")[:100], str(pr.get("state", "")).lower())})
    return items


class SourceItems(list):
    """List-compatible receipts with the outcome of their bounded source read."""

    def __init__(self, items=(), status=None):
        super().__init__(items)
        self.status = status or {}


def _status(source, since, attempted, files, error=None, updated=None):
    failed = sum(not row["read"] for row in files)
    readable = len(files) - failed
    state = "available" if not error and not failed else ("partial" if readable else "unavailable")
    return {"state": state, "source": str(source), "observed_at": attempted if state == "available" else None,
            "attempted_at": attempted, "source_updated_at": updated, "error": error,
            "files": files, "coverage": {"since": since.isoformat(), "files_found": len(files),
                                         "files_read": readable, "files_failed": failed}}


def _read_receipt(path):
    updated = None
    try:
        updated = path.stat().st_mtime
        lines = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return [], {"path": str(path), "source_updated_at": updated, "read": False,
                    "error": "%s reading achievement source" % exc.__class__.__name__}
    return lines, {"path": str(path), "source_updated_at": updated, "read": True, "error": None}


def tf_items(since: datetime, tf_dir: Path = TF_DIR) -> list[dict]:
    attempted = time.time()
    items, files, updated = [], [], None
    try:
        updated = tf_dir.stat().st_mtime
        logs = sorted(path for path in tf_dir.iterdir() if path.name.endswith("-run.log"))
    except OSError as exc:
        error = "%s accessing TestFlight upload directory" % exc.__class__.__name__
        return SourceItems(status=_status(tf_dir, since, attempted, files, error, updated))
    for log in logs:
        build = ""
        lines, metadata = _read_receipt(log)
        files.append(metadata)
        for number, line in enumerate(lines, 1):
            b = _BUILD_RE.search(line)
            if b:
                build = b.group(1)
            m = _TF_RE.match(line.strip())
            if not m:
                continue
            try:
                when = datetime.strptime("%s %s" % (m.group(1), m.group(2)), "%a %b %d %H:%M:%S %Y").replace(tzinfo=ET)
            except ValueError:
                continue
            if when < since:
                continue
            plat = "iOS" if m.group(3) == "ios" else "macOS"
            identity = build or "%s:%d" % (log.name, number)
            items.append({"kind": "shipped", "when": when, "key": "tf:%s:%s" % (identity, plat),
                          "build": build or None, "platform": plat, "source_kind": "testflight",
                          "source": str(log), "source_updated_at": metadata["source_updated_at"],
                          "evidence_type": "upload_exit_zero", "line": number,
                          "text": "TestFlight %s uploaded for %s" % (build or "build", plat)})
    error = "Some TestFlight upload logs could not be read" if any(not row["read"] for row in files) else None
    status = _status(tf_dir, since, attempted, files, error, updated)
    status["coverage"]["file_pattern"] = "*-run.log"
    status["source_updated_at_kind"] = "directory_mtime"
    return SourceItems(items, status)


def gate_items(since: datetime, status_md: Path = STATUS_MD) -> list[dict]:
    attempted = time.time()
    items = []
    lines, metadata = _read_receipt(status_md)
    for number, line in enumerate(lines, 1):
        m = _GATE_RE.match(line.strip())
        if not m:
            continue
        try:
            when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        except ValueError:
            continue
        if when < since:
            continue
        if m.group(3):
            pr, head, what = m.group(3), m.group(4), "full exact-head gate green"
            extra = re.search(r"vs base ([0-9a-f]{7,40})", line)
            if extra:
                what += " vs base %s" % extra.group(1)[:8]
            evidence_type = "full_exact_head_gate"
        else:
            pr, head = m.group(5), m.group(6) or ""
            push = re.search(r"\.\.([0-9a-f]{7,40})", line)
            if push:
                head = push.group(1)
            what, evidence_type = "prep gate passed", "prep_gate"
        sha = head[:8]
        items.append({"kind": "gates", "when": when, "key": "gate:%s:%s:%s" % (pr, sha, what[:4]), "pr": int(pr),
                      "head": head or None, "evidence_type": evidence_type, "source_kind": "gates",
                      "source": str(status_md), "source_updated_at": metadata["source_updated_at"], "line": number,
                      "text": "#%s %s — %s" % (pr, sha, what)})
    status = _status(status_md, since, attempted, [metadata], metadata["error"], metadata["source_updated_at"])
    status["source_updated_at_kind"] = "file_mtime"
    return SourceItems(items, status)


def dedupe(items: list[dict]) -> list[dict]:
    seen = {}
    for item in sorted(items, key=lambda i: i["when"]):
        seen.setdefault(item["key"], item)  # keep the first time each thing happened
    return sorted(seen.values(), key=lambda i: i["when"], reverse=True)


def _row(item: dict) -> dict:
    row = {"kind": item["kind"], "tone": "go", "time_et": item["when"].strftime("%-I:%M %p ET"),
           "day": item["when"].strftime("%Y-%m-%d"), "text": item["text"],
           "occurred_at": item["when"].isoformat(), "evidence_id": item["key"], "key": item["key"]}
    for field in ("pr", "head", "merge_commit", "build", "platform", "base", "title", "url",
                  "source_kind", "source", "source_updated_at", "line", "evidence_type",
                  "current_state", "state_observed_at"):
        if item.get(field) is not None and item.get(field) != "":
            row[field] = item[field]
    return row


def _counts(items: list[dict]) -> dict:
    return {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}


def _summary(counts: dict) -> str:
    labels = {"merged": "PRs merged", "shipped": "TestFlight uploads", "opened": "PRs opened", "gates": "green gates"}
    if any(counts.get(k) is None for k in KINDS):
        parts = []
        if counts.get("merged") is None and counts.get("opened") is None:
            parts.append("GitHub merges/openings unavailable")
            keys = ("shipped", "gates")
        else:
            keys = KINDS
        for k in keys:
            value = counts.get(k)
            suffix = "recorded locally" if k in ("shipped", "gates") else "recorded"
            parts.append((labels[k] + " unavailable") if value is None else "%d %s %s" % (value, labels[k], suffix))
        return "; ".join(parts) + "."
    parts = ["%d PR%s merged" % (counts["merged"], "" if counts["merged"] == 1 else "s"),
             "%d TestFlight upload%s" % (counts["shipped"], "" if counts["shipped"] == 1 else "s"),
             "%d PR%s opened" % (counts["opened"], "" if counts["opened"] == 1 else "s"),
             "%d green gate%s" % (counts["gates"], "" if counts["gates"] == 1 else "s")]
    return ", ".join(parts) + "."


def _by_day(items: list[dict]) -> list[dict]:
    days: dict[str, list] = {}
    for i in items:
        days.setdefault(i["when"].strftime("%Y-%m-%d"), []).append(i)
    out = []
    for day in sorted(days, reverse=True):
        rows = days[day]
        label = rows[0]["when"].strftime("%a %b %-d")
        out.append({"day": day, "label": label, "summary": _summary(_counts(rows)), "counts": _counts(rows),
                    "groups": [{"kind": k, "title": KIND_TITLE[k], "items": [_row(r) for r in rows if r["kind"] == k]}
                               for k in KINDS if any(r["kind"] == k for r in rows)]})
    return out


def build(items: list[dict], now: datetime, source_status: dict | None = None) -> dict:
    now = now.astimezone(ET)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    items = dedupe([item for item in items if item["when"] <= now])
    today = [i for i in items if i["when"] >= today0]
    week = [i for i in items if i["when"] >= week_start(now)]
    rolling = [i for i in items if i["when"] >= window_start(now)]
    value = {
        "as_of_et": now.strftime("%Y-%m-%d %H:%M ET"),
        "observed_at": now.timestamp(),
        "stale": False,
        "partial": False,
        "error": None,
        "today": {"summary": _summary(_counts(today)), "counts": _counts(today),
                  "groups": [{"kind": k, "title": KIND_TITLE[k], "items": [_row(i) for i in today if i["kind"] == k]}
                             for k in KINDS if any(i["kind"] == k for i in today)]},
        "week": {"label": "Week of %s" % week_start(now).strftime("%a %b %-d"), "summary": _summary(_counts(week)),
                 "counts": _counts(week), "days": _by_day(week)},
        "rolling": {"label": "Last 7 days (since %s)" % window_start(now).strftime("%a %b %-d"),
                    "summary": _summary(_counts(rolling)), "counts": _counts(rolling), "days": _by_day(rolling)},
        "sources": "GitHub merges and new PRs (gh), TestFlight upload logs in tf-triggers, green gates in mission/status.md",
        "records": [_row(item) for item in rolling],
        "source_status": copy.deepcopy(source_status or {}),
        "coverage": {"timezone": "America/New_York", "week_starts": "Monday", "rolling_since": window_start(now).isoformat(),
                     "merged_bases": list(dict.fromkeys(filter(None, ("main", (site.CURRENT.get("github") or {}).get("integration_branch"))))), "opened_bases": "all", "github_limit_per_query": 1000,
                     "testflight": "Successful upload log receipts only; processing and tester availability unverified",
                     "gates": "Recognized explicit success receipts only; not all builds or tests"},
    }
    problems = []
    for source, kinds in (("github", ("merged", "opened")), ("testflight", ("shipped",)), ("gates", ("gates",))):
        status = value["source_status"].get(source)
        if not status:
            continue
        status.setdefault("coverage", {})["recorded_items"] = sum(1 for item in rolling if item["kind"] in kinds)
        if status.get("state") != "available":
            problems.append(status.get("error") or (source + " achievement source is " + str(status.get("state"))))
            for name in ("today", "week", "rolling"):
                for section in [value[name], *(value[name].get("days") or [])]:
                    for kind in kinds:
                        section["counts"][kind] = None
                    section["summary"] = _summary(section["counts"])
    if problems:
        value.update(stale=True, partial=True, error="; ".join(problems))
    return value


def _source_status(items, source, now):
    status = copy.deepcopy(getattr(items, "status", None))
    return status or {"state": "available", "source": source, "observed_at": now.timestamp(),
                      "attempted_at": now.timestamp(), "source_updated_at": None, "error": None}


def _local_collect(now, github=None, github_error=None):
    since = window_start(now)
    if site.adapter_enabled("git"):
        uploads = tf_items(since, site.optional_path("uploads"))
        gates = gate_items(since, site.optional_path("status"))
    else:
        uploads = SourceItems(status={"state": "unavailable", "observed_at": None, "error": "Local evidence adapter is disabled"})
        gates = SourceItems(status={"state": "unavailable", "observed_at": None, "error": "Local evidence adapter is disabled"})
    status = {"testflight": _source_status(uploads, "testflight", now), "gates": _source_status(gates, "gates", now),
              "github": {"state": "unavailable" if github_error else "available", "source": "GitHub PR queries",
                         "observed_at": None if github_error else now.timestamp(), "attempted_at": now.timestamp(),
                         "source_updated_at": None, "error": github_error}}
    return build(list(github or []) + uploads + gates, now, status)


def collect(now: datetime | None = None) -> dict:
    now = now or datetime.now(ET)
    if not site.adapter_enabled("github"):
        return _local_collect(now, github_error="GitHub adapter is disabled")
    return _local_collect(now, github_items(window_start(now)))


def _retain_local_evidence(value, previous):
    """Keep earlier verified receipts after a local read failure, with their original ages."""
    failed = {name for name in ("testflight", "gates")
              if (value.get("source_status", {}).get(name) or {}).get("state") != "available"}
    if not failed or not previous:
        return value
    records = list(value.get("records") or [])
    statuses = copy.deepcopy(value["source_status"])
    for name in failed:
        old = (previous.get("source_status") or {}).get(name) or {}
        retained = [row for row in previous.get("records") or [] if row.get("source_kind") == name]
        records.extend(retained)
        if old.get("observed_at") is not None:
            failed_status = statuses[name]
            statuses[name] = dict(copy.deepcopy(old), state="stale", attempted_at=failed_status.get("attempted_at"),
                                  error=failed_status.get("error"), failed_attempt=failed_status)
    items = [dict(row, when=_iso(row["occurred_at"])) for row in records if _iso(row.get("occurred_at"))]
    return build(items, datetime.fromtimestamp(value["observed_at"], ET), statuses)


def _github_failure(previous, now, error):
    """Refresh independent local sources while retaining only aged GitHub evidence."""
    value = _local_collect(now, github_error=error)
    records = list(value["records"])
    records.extend(row for row in (previous or {}).get("records") or []
                   if row.get("kind") in ("merged", "opened"))
    statuses = value["source_status"]
    old_github = ((previous or {}).get("source_status") or {}).get("github") or {}
    if old_github.get("observed_at") is not None:
        statuses["github"] = dict(copy.deepcopy(old_github), state="stale", error=error,
                                  attempted_at=now.timestamp())
    items = [dict(row, when=_iso(row["occurred_at"])) for row in records if _iso(row.get("occurred_at"))]
    return build(items, now, statuses)


class Cache:
    """Refresh in the background so a slow gh call never blocks the snapshot."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: dict | None = None
        self.at = 0.0
        self.busy = False

    def _run(self) -> None:
        try:
            value = collect()
        except Exception as exc:  # keep the last good value
            value = None
            err = "%s: %s" % (exc.__class__.__name__, exc)
        attempted = time.time()
        with self.lock:
            if value is not None:
                self.value = _retain_local_evidence(value, self.value)
            else:
                # A failed GitHub read must not freeze independent upload/gate reads.
                value = _github_failure(self.value, datetime.now(ET), err)
                self.value = _retain_local_evidence(value, self.value)
                self.value["attempted_at"] = attempted
            self.at = attempted
            self.busy = False

    def get(self) -> dict | None:
        if not site.adapter_enabled("github") and not site.adapter_enabled("git"):
            return _local_collect(datetime.now(ET), github_error="Achievement adapters are disabled")
        with self.lock:
            if not self.busy and time.time() - self.at > REFRESH_S:
                self.busy = True
                threading.Thread(target=self._run, daemon=True, name="accomplishments").start()
            value = copy.deepcopy(self.value)
            if value and time.time() - (value.get("observed_at") or 0) > REFRESH_S * 2:
                value.update(stale=True, partial=True)
                value["error"] = value.get("error") or "Achievement sources have not refreshed"
                for status in value.get("source_status", {}).values():
                    if status.get("state") == "available":
                        status["state"] = "stale"
                        status["error"] = "Achievement source has not refreshed"
            return value


CACHE = Cache()
