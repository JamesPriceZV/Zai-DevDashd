"""Reviewer telemetry panel: reads an explicitly configured rollup.json. Nothing is computed or invented here; missing stays missing."""
from __future__ import annotations

import calendar
import json
import time
from pathlib import Path
from zai_devdashd import config as site

ROLLUP = site.optional_path("reviewer_rollup", section="telemetry")
ORDER = ["Bugbot", "CodeRabbit", "Codex", "DeepSource"]
ROLES = {name: "Reported %s review metadata" % name for name in ORDER}


def _ago(iso, now):
    try:
        t = calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None
    return max(0, now - t)


def build(now=None, path=None):
    if path is None and not site.adapter_enabled("reviewers"):
        return {"state": "unconfigured", "note": "Reviewer adapter is disabled", "observed_at": None, "rows": []}
    path = site.optional_path("reviewer_rollup", section="telemetry") if path is None else Path(path)
    now = now or time.time()
    try:
        r = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"state": "no data yet", "note": "Collector has not written rollup.json yet."}
    out = {"state": "live", "generated_at": r.get("generated_at"), "age_s": _ago(r.get("generated_at"), now),
           "prs_scanned": r.get("prs_scanned"), "findings_total": r.get("findings_total"), "since": r.get("since"),
           "method": r.get("method"), "deepsource": r.get("deepsource"), "recent": r.get("recent") or [], "cards": []}
    for name in ORDER:
        v = (r.get("reviewers") or {}).get(name) or {}
        v = dict(v, name=name, role=ROLES[name], thin=(v.get("findings") or 0) < 20)
        out["cards"].append(v)
    return out
