"""Read-only, policy-bound AutoTune metadata; no liveness or acceptance authority."""
from __future__ import annotations

import copy
import hashlib
import re
import time

from zai_devdashd.executive_live import read_document, _clock, _text

DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _count(value):
    return value if type(value) is int and 0 <= value <= 1000000 else None


def project(document, policy, now, *, producer="", historical_valid=None):
    if (not isinstance(document, dict) or not isinstance(policy, str)
            or not DIGEST.fullmatch(policy) or document.get("policySHA256") != policy):
        raise ValueError("AutoTune policy does not match the assigned producer")
    if not producer or document.get("producer_chat_id") != producer:
        raise ValueError("AutoTune producer does not match the configured identity")
    raw_status = document.get("status")
    if not isinstance(raw_status, str) or len(raw_status) > 200 or any(ord(c) < 32 for c in raw_status):
        raise ValueError("AutoTune status is malformed")
    publication = _clock(document.get("lastProgressLocal"), now)
    if publication is None:
        raise ValueError("AutoTune publication clock is unavailable")
    status = raw_status.lower()
    if status.startswith("halted"):
        state, title = "blocked", "AutoTune paused for an inspection check"
    elif status.startswith("waiting"):
        state, title = "queued", "AutoTune waiting for resources"
    elif status.startswith("running"):
        state, title = "running", "AutoTune controller reports work in progress"
    elif status in ("complete", "completed", "finished"):
        state, title = "completed", "AutoTune controller finished; result qualification is separate"
    else:
        state, title = "unreported", "AutoTune controller status needs interpretation"
    counts = document.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    job = document.get("actualJob")
    job = job if isinstance(job, dict) else {}
    number = _count(job.get("job", job.get("number", job.get("index"))))
    reason = document.get("blockReason")
    reason = reason if isinstance(reason, str) else ""
    public_reason = ("A process could not be inspected. The runner stopped without automatically retrying."
                     if reason.startswith("Unknown process visibility") else
                     "The runner reports a blocking condition; its owner needs to review the original record."
                     if reason else "")
    if reason.startswith("memory below admitted floor"):
        title = "AutoTune paused by its memory guard"
        public_reason = "The memory guard stopped the run; no benchmark result is implied."
        if document.get("ownedClosure") is not True:
            public_reason += " Cleanup has not been confirmed."
    # legacy lastProgressLocal is a publication clock, including unchanged loops.
    return {"producer_chat_id": producer, "policy_sha256": policy,
            "state": state, "title": title, "status_publication_at": publication,
            "stale": now - publication > 120, "publication_age_s": max(0, now - publication),
            "actual_progress_at": None, "legacy_publication_clock": True,
            "checked_at": now, "reported_job": number,
            "reported_job_id": _text(job.get("id"), 96) or None,
            "new_valid": _count(document.get("completed")),
            # Policy-bound baseline, never aggregate-minus-new: optional counts can lag.
            "historical_valid": _count(historical_valid),
            "recorded_valid_total": _count(counts.get("valid")),
            "historical_failed": _count(counts.get("historicalFailed")),
            "new_failed": _count(counts.get("newFailed")),
            "recorded_attempts": _count(counts.get("attempted")),
            "counts_may_lag": True, "inflight_in_counts": False,
            "native_execution_verified": False, "benchmark_acceptance": "Not established by controller metadata",
            "blocker": public_reason, "error": None}


class Cache:
    def __init__(self, path, policy, producer, historical_valid=None):
        self.path, self.policy, self.producer = path, policy, producer
        self.historical_valid = _count(historical_valid)
        self.last = None
        self.attempted = 0

    def get(self, now=None):
        now = time.time() if now is None else now
        if not self.path or not self.producer or not isinstance(self.policy, str) or not DIGEST.fullmatch(self.policy):
            return {"state": "unreported", "error": "The current AutoTune status contract is not connected"}
        if self.last is not None and now - self.attempted < 5:
            return copy.deepcopy(self.last)
        self.attempted = now
        try:
            raw, document = read_document(self.path, private=False, limit=65536)
            result = project(document, self.policy, now, producer=self.producer, historical_valid=self.historical_valid)
            result["status_sha256"] = hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, TypeError, OverflowError):
            result = {"state": "unreported", "checked_at": now,
                      "error": "AutoTune metadata could not be read or its policy did not match",
                      "native_execution_verified": False}
        self.last = copy.deepcopy(result)
        return result


def overlay(owner, observed):
    """A newer controller report replaces operational intent, never plan clocks."""
    result = copy.deepcopy(owner)
    result.setdefault("operational_sources", {})["autotune"] = copy.deepcopy(observed)
    if observed.get("error"):
        return result
    rows = result.setdefault("operations", [])
    row = next((item for item in rows if item.get("id") == "autotune"), None)
    if row is None:
        row = {"id": "autotune", "kind": "automation", "owner": "AutoTune executor", "depends_on": [],
               "state": "unreported", "title": "AutoTune status needs a fresh publication"}
        rows.append(row)
    row["controller"] = copy.deepcopy(observed)
    row["controller_applied"] = False
    active = observed["state"] in ("queued", "running")
    older_than_terminal = (row.get("state") in ("completed", "cancelled", "failed")
                           and observed.get("status_publication_at", 0) <= result.get("observed_at", 0))
    if active and (observed.get("stale") or older_than_terminal):
        return result
    row["owner_reported_state"] = row.get("state")
    row.update(state=observed["state"], title=observed["title"], controller_applied=True,
               next_action=observed.get("blocker") or row.get("next_action") or "Follow the controller's recorded next step")
    row["estimate"] = {"state": "unavailable", "samples": 0, "confidence": "insufficient history",
                       "finish_low": None, "finish_high": None,
                       "condition": "Controller metadata supplies state; comparable admitted run history is needed for an estimate"}
    return result
