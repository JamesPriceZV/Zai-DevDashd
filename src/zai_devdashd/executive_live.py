"""Read-only executive owner feed and evidence-derived operation estimates.

No workload dispatch, model calls, receipt creation or source-clock rewriting.
The named Coordinator owns this feed; its reports remain owner reports.
"""
from __future__ import annotations

import copy
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import time

SCHEMA = "zai-devdashd.executive-owner.v1"
MAX_BYTES = 512 * 1024
FRESH_S = 600
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")


def _text(value, limit=400):
    return value[:limit] if isinstance(value, str) and not any(ord(c) < 32 for c in value) else ""


def _rows(value, limit):
    return value[:limit] if isinstance(value, list) else []


def _dict(value):
    return value if isinstance(value, dict) else {}


def _clock(value, now):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            value = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if type(value) not in (int, float):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= now + 300 else None


def _future_clock(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        stamp = parsed.timestamp()
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def _identity(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _binding(value):
    value = _dict(value)
    source = value.get("source_sha")
    digest = value.get("receipt_sha256")
    return (isinstance(source, str) and SHA.fullmatch(source) is not None
            and isinstance(digest, str) and DIGEST.fullmatch(digest) is not None)


def _history(history, operation, now):
    """Use successful, comparable, unique source-bound owner records only."""
    samples, seen = [], set()
    for row in _rows(history, 256):
        row = _dict(row)
        if row.get("outcome") != "passed" or not _binding(row):
            continue
        if any(not operation.get(k) or row.get(k) != operation.get(k)
               for k in ("kind", "machine", "profile")):
            continue
        start, end = _clock(row.get("started_at"), now), _clock(row.get("completed_at"), now)
        if start is None or end is None or not 0 < end - start <= 86400:
            continue
        key = row["receipt_sha256"]
        if key not in seen:
            samples.append(end - start)
            seen.add(key)
    return sorted(samples)


def estimate(operation, history, now):
    """A duration estimate is distinct from admission and a finish-time estimate."""
    terminal = operation.get("state")
    if terminal in ("completed", "cancelled", "failed"):
        return {"state": terminal, "samples": 0, "confidence": "not applicable",
                "basis": "Terminal owner-reported operation; no future work is forecast",
                "remaining_low_s": None, "remaining_high_s": None, "finish_low": None, "finish_high": None,
                "condition": {"completed": "Completion recorded; no remaining work is estimated",
                              "cancelled": "Cancelled; no completion is scheduled",
                              "failed": "The operation stopped; a new plan is needed before estimating completion"}[terminal]}
    samples = _history(history, operation, now)
    result = {"state": "unavailable", "samples": len(samples), "confidence": "insufficient history",
              "basis": "Successful owner-recorded runs with the same operation, machine and profile",
              "remaining_low_s": None, "remaining_high_s": None,
              "finish_low": None, "finish_high": None,
              "condition": "Comparable completed run history is not connected"}
    if len(samples) < 3:
        return result
    low = statistics.median(samples)
    high = samples[math.ceil(.9 * len(samples)) - 1]
    confidence = "moderate" if len(samples) >= 5 else "low"
    if high > low * 2:
        confidence = "low"
    elapsed = 0
    if operation.get("state") == "running":
        start = _clock(operation.get("started_at"), now)
        if start is None:
            result["condition"] = "The operation's actual start time is unreported"
            return result
        elapsed = max(0, now - start)
    result.update(state="conditional", confidence=confidence,
                  remaining_low_s=max(0, low - elapsed), remaining_high_s=max(0, high - elapsed),
                  condition="After required dependencies and scheduling are confirmed")
    # Exceeding a historic range is uncertainty, not an automatic imminent finish.
    if elapsed >= high:
        result.update(state="overrun", confidence="low", remaining_low_s=None,
                      remaining_high_s=None, condition="The run has exceeded the comparable historical range")
    return result


def project(document, now, *, producer=""):
    document = _dict(document)
    if not producer or document.get("schema") != SCHEMA or document.get("producer_chat_id") != producer:
        raise ValueError("Executive owner contract is unavailable")
    observed = _clock(document.get("observed_at"), now)
    if observed is None:
        raise ValueError("Executive owner clock is invalid")
    stale = now - observed > FRESH_S
    candidate = _dict(document.get("candidate"))
    candidate_sha = candidate.get("sha")
    candidate_sha = candidate_sha if isinstance(candidate_sha, str) and SHA.fullmatch(candidate_sha) else None
    features, checks, operations, blockers = [], [], [], []
    feature_ids, check_ids = set(), set()
    for raw in _rows(document.get("features"), 64):
        raw = _dict(raw)
        if not _identity(raw.get("id")) or raw["id"] in feature_ids:
            continue
        feature_ids.add(raw["id"])
        state = raw.get("state")
        state = state if state in ("planned", "in_progress", "integrated", "accepted", "blocked") else "unreported"
        # Completion claims need a bound source record; supplied 'verified' flags are ignored.
        if state in ("integrated", "accepted") and not _binding(raw):
            state = "unreported"
        features.append({"id": raw["id"], "title": _text(raw.get("title"), 180),
                         "benefit": _text(raw.get("benefit")), "state": state,
                         "source_sha": raw.get("source_sha") if _binding(raw) else None,
                         "receipt_sha256": raw.get("receipt_sha256") if _binding(raw) else None,
                         "reported_at": _clock(raw.get("reported_at"), now)})
    for raw in _rows(document.get("release_checks"), 64):
        raw = _dict(raw)
        if not _identity(raw.get("id")) or raw["id"] in check_ids:
            continue
        check_ids.add(raw["id"])
        current = bool(candidate_sha and raw.get("source_sha") == candidate_sha)
        state = raw.get("state")
        state = state if state in ("pending", "running", "passed", "failed", "blocked", "not_required") else "unreported"
        bound = _binding(raw)
        if state == "passed" and (not current or not bound):
            state = "other_revision" if bound else "unreported"
        checks.append({"id": raw["id"], "title": _text(raw.get("title"), 180),
                       "stage": _text(raw.get("stage"), 80), "state": state,
                       "required": raw.get("required") is True, "current_candidate": current,
                       "source_sha": raw.get("source_sha") if bound else None,
                       "receipt_sha256": raw.get("receipt_sha256") if bound else None,
                       "reported_at": _clock(raw.get("reported_at"), now)})
    seen = set()
    for raw in _rows(document.get("operations"), 64):
        raw = _dict(raw)
        if not _identity(raw.get("id")) or raw["id"] in seen:
            continue
        seen.add(raw["id"])
        state = raw.get("state")
        state = state if state in ("queued", "scheduled", "running", "completed", "failed", "blocked", "cancelled") else "unreported"
        maintenance_receipt = (raw.get("kind") in ("storage", "maintenance")
                               and isinstance(raw.get("receipt_sha256"), str)
                               and bool(DIGEST.fullmatch(raw["receipt_sha256"])))
        if state == "completed" and not (_binding(raw) or maintenance_receipt):
            state = "unreported"
        row = {k: _text(raw.get(k), 180) for k in ("id", "kind", "machine", "profile", "title", "owner", "next_action")}
        row.update(state=state, depends_on=[v for v in _rows(raw.get("depends_on"), 16) if _identity(v)],
                   started_at=_clock(raw.get("started_at"), now),
                   scheduled_at=_future_clock(raw.get("scheduled_at")) if raw.get("schedule_confirmed") is True else None,
                   source_sha=raw.get("source_sha") if _binding(raw) else None,
                   receipt_sha256=raw.get("receipt_sha256") if _binding(raw) or maintenance_receipt else None)
        row["estimate"] = estimate(dict(raw, state=state), document.get("history"), now)
        operations.append(row)
    by_id = {row["id"]: row for row in operations}
    visiting, resolved = set(), set()

    def finish(row):
        identity = row["id"]
        if identity in resolved:
            return row["state"] == "completed" or row["estimate"].get("finish_high") is not None
        if identity in visiting:
            return False
        visiting.add(identity)
        e = row["estimate"]
        if row["state"] == "completed":
            resolved.add(identity); visiting.remove(identity)
            return True
        ready = row["state"] in ("scheduled", "running") and not stale
        starts_low = [now, row.get("scheduled_at") or now]
        starts_high = list(starts_low)
        for dependency in row["depends_on"]:
            parent = by_id.get(dependency)
            if parent is None or not finish(parent):
                ready = False
            elif parent["state"] != "completed":
                starts_low.append(parent["estimate"]["finish_low"])
                starts_high.append(parent["estimate"]["finish_high"])
        if row["state"] == "scheduled" and row.get("scheduled_at") is None:
            ready = False
        if row["state"] == "scheduled" and row.get("scheduled_at") is not None and row["scheduled_at"] < now:
            ready = False
            e["condition"] = "The scheduled start has passed; an updated schedule is needed"
        if ready and e["state"] == "conditional":
            e.update(state="estimated", finish_low=max(starts_low) + e["remaining_low_s"],
                     finish_high=max(starts_high) + e["remaining_high_s"], condition="Based on the confirmed schedule and dependency estimates")
        elif stale:
            e.update(finish_low=None, finish_high=None, condition="Owner observations need refreshing")
        visiting.remove(identity); resolved.add(identity)
        return e.get("finish_high") is not None

    for row in operations:
        finish(row)
    for raw in _rows(document.get("blockers"), 32):
        raw = _dict(raw)
        if _identity(raw.get("id")):
            blockers.append({k: _text(raw.get(k)) for k in ("id", "title", "owner", "impact", "next_action")})
    complete_scope = (document.get("scope_complete") is True and len(features) == len(_rows(document.get("features"), 65))
                      and bool(features) and len({f["id"] for f in features}) == len(features))
    required = [row for row in checks if row["required"]]
    inventory = _dict(document.get("inventory"))
    groups = ("common", "B", "C", "D", "conditional")
    inventory_valid = all(type(inventory.get(k)) is int and 0 <= inventory[k] <= 1000 for k in groups)
    inventory_valid = inventory_valid and type(inventory.get("total")) is int and inventory["total"] == sum(inventory[k] for k in groups)
    forecasts = []
    for raw in _rows(document.get("planning_forecasts"), 8):
        raw = _dict(raw)
        start, end = _date(raw.get("start_date")), _date(raw.get("end_date"))
        if not _identity(raw.get("id")) or start is None or end is None or end < start:
            continue
        forecasts.append({"id": raw["id"], "label": _text(raw.get("label"), 180),
                          "start_date": start.isoformat(), "end_date": end.isoformat(),
                          "confidence": raw.get("confidence") if raw.get("confidence") in ("low", "moderate", "high") else "unreported",
                          "basis": "Coordinator planning forecast; conditional, not an empirical completion estimate",
                          "conditions": [_text(v, 200) for v in _rows(raw.get("conditions"), 8) if _text(v, 200)]})
    proposed = _dict(document.get("proposed_candidate"))
    proposed_sha = proposed.get("sha")
    return {"schema": SCHEMA, "producer_chat_id": producer, "observed_at": observed,
            "stale": stale, "authority": "Coordinator owner report; source bindings are displayed, not independent acceptance",
            "candidate": {"sha": candidate_sha, "build": _text(candidate.get("build"), 40), "title": _text(candidate.get("title"), 180)},
            "proposed_candidate": {"sha": proposed_sha if isinstance(proposed_sha, str) and SHA.fullmatch(proposed_sha) else None,
                                   "build": _text(proposed.get("build"), 40), "title": _text(proposed.get("title"), 180)},
            "scope_label": _text(document.get("scope_label"), 180) or "Current delivery requirements",
            "inventory": {k: inventory[k] for k in groups + ("total",)} if inventory_valid else None,
            "planning_forecasts": forecasts,
            "features": features, "feature_scope_complete": complete_scope,
            "integrated_features": sum(row["state"] in ("integrated", "accepted") for row in features),
            "accepted_features": sum(row["state"] == "accepted" for row in features),
            "feature_total": len(features) if complete_scope else None,
            "release_checks": checks, "required_checks": len(required) if document.get("release_scope_complete") is True and required and len(checks) == len(_rows(document.get("release_checks"), 65)) else None,
            "current_checks_reported_passed": sum(row["state"] == "passed" and row["current_candidate"] for row in required),
            "readiness": "Owner-recorded checks; final release approval remains separate",
            "operations": operations, "blockers": blockers,
            "marketing_facts": [{"id": _text(_dict(v).get("id"), 96), "text": _text(_dict(v).get("text"), 600)}
                                for v in _rows(document.get("marketing_facts"), 32) if _identity(_dict(v).get("id"))]}


def read_document(path, *, private=True, limit=MAX_BYTES):
    path = Path(path)
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise ValueError("Executive feed path is not canonical")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        permission_mask = 0o077 if private else 0o022
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & permission_mask or before.st_size > limit:
            raise ValueError("Executive feed custody or size is invalid")
        raw = bytearray()
        while len(raw) <= limit:
            part = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        after = os.fstat(descriptor)
        if len(raw) > limit or any(getattr(before, k) != getattr(after, k) for k in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
            raise ValueError("Executive feed changed while reading")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate executive field")
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(value, dict):
            raise ValueError("Executive document must be one JSON object")
        return bytes(raw), value
    finally:
        os.close(descriptor)


class Cache:
    def __init__(self, path, *, producer=""):
        self.path = Path(path)
        self.producer = producer
        self.last = None
        self.result = None
        self.attempted = 0

    def get(self, now=None):
        now = time.time() if now is None else now
        if not self.producer:
            return {"schema": SCHEMA, "features": [], "release_checks": [], "operations": [], "blockers": [],
                    "stale": True, "error": "Executive producer is not configured", "observed_at": None}
        if now - self.attempted < 5 and self.result is not None:
            result = copy.deepcopy(self.result)
            result["stale"] = bool(result.get("stale") or now - result.get("observed_at", 0) > FRESH_S)
            return _expire_estimates(result)
        self.attempted = now
        try:
            raw, document = read_document(self.path)
            result = project(document, now, producer=self.producer)
            result.update(input_sha256=hashlib.sha256(raw).hexdigest(), error=None, attempted_at=now)
            self.last = copy.deepcopy(result)
            self.result = copy.deepcopy(result)
            return result
        except (OSError, ValueError, TypeError, OverflowError):
            result = copy.deepcopy(self.last) if self.last else {"schema": SCHEMA, "features": [], "release_checks": [], "operations": [], "blockers": []}
            result.update(stale=True, error="The Coordinator executive feed is not connected or could not be validated", attempted_at=now)
            self.result = _expire_estimates(result)
            return copy.deepcopy(self.result)


def _expire_estimates(result):
    if result.get("stale"):
        for operation in result.get("operations", []):
            forecast = operation.get("estimate", {})
            forecast.update(finish_low=None, finish_high=None, state="unavailable", condition="Owner observations need refreshing")
    return result
