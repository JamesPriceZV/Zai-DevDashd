"""Pure executive view of existing dashboard evidence; no probes or workloads."""
from __future__ import annotations

import copy
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
GOALS = {
    "integration": "Integrate approved changes",
    "qualification": "Qualify builds on Mac, iPhone and iPad",
    "distribution": "Deliver a TestFlight candidate",
}
KINDS = {"opened": "integration", "merged": "integration",
         "gates": "qualification", "shipped": "distribution"}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=ET).timestamp() if value.tzinfo is None else value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def _meta(block, now, threshold):
    block = _dict(block)
    at = _epoch(block.get("observed_at"))
    if at is None:
        at = _epoch(block.get("as_of"))
    age = max(0, now - at) if at is not None else None
    stale = bool(block.get("stale") or block.get("error") or age is None or age > threshold)
    return {"observed_at": at, "as_of_et": block.get("as_of_et") or (datetime.fromtimestamp(at, ET).strftime("%Y-%m-%d %H:%M:%S ET") if at is not None else None),
            "stale": stale, "error": block.get("error"), "age_s": age}


def _evidence(label, value, meta, **fields):
    return dict(label=label, value=value, as_of_et=meta.get("as_of_et"),
                observed_at=meta.get("observed_at"), stale=meta.get("stale", True), **fields)


def _achievement(row, acc_meta):
    kind = row.get("kind")
    scope = KINDS.get(kind)
    if not scope:
        return None
    impacts = {
        "opened": "A change was proposed for review; opening does not establish integration or validation.",
        "merged": "Accepted code joined the recorded integration branch; release qualification is a separate gate.",
        "gates": "The recorded validation gate passed for its receipt and head; it does not qualify the whole release.",
        "shipped": "The build was handed to Apple; processing and tester availability remain unverified.",
    }
    result = {
        "title": row.get("text") or "Recorded " + str(kind),
        "goal": GOALS[scope], "goal_id": scope, "impact": impacts[kind],
        "at": " ".join(str(row.get(k) or "") for k in ("day", "time_et")).strip(),
        "kind": kind, "stale": acc_meta["stale"],
        "product_goal": "Not associated with a product milestone by this evidence source",
        "evidence": [_evidence("Recorded event", row.get("text") or kind, acc_meta,
                               source=row.get("source") or row.get("source_kind") or kind)],
    }
    if kind == "opened":
        current = _opening_state(row, acc_meta)
        result["current"] = current
        result["event_status"] = "historical_opening"
        state_text = current["state"]
        result["impact"] = {
            "merged": "This change was opened during the period and was later recorded as merged. Its opening is historical; release qualification remains separate.",
            "closed": "This change was opened during the period and was later recorded as closed. It is not an outstanding proposal at that source observation.",
            "open": "This change was opened during the period and was still open at the stated source observation. Acceptance is not established by the opening event.",
        }.get(state_text, "A change was proposed and opened during this period. Its current PR state is unreported; the opening does not establish an outstanding task.")
    # Preserve source identity without inventing a head for a merge or upload.
    for key in ("evidence_id", "key", "pr", "head", "merge_commit", "build",
                "platform", "source", "source_kind", "source_updated_at", "occurred_at",
                "base", "url", "line", "evidence_type", "current_state", "state_observed_at"):
        if key in row:
            result[key] = copy.deepcopy(row[key])
    return result


def _window(block, acc_meta, source_status, now):
    block = _dict(block)
    groups = _rows(block.get("groups"))
    for day in _rows(block.get("days")):
        groups += _rows(day.get("groups"))
    completed, started, seen = [], [], set()
    for group in groups:
        for original in _rows(group.get("items")):
            row = dict(original, kind=original.get("kind") or group.get("kind"))
            identity = row.get("key") or row.get("evidence_id") or (
                row.get("kind"), row.get("day"), row.get("time_et"), row.get("text"))
            if str(identity) in seen:
                continue
            seen.add(str(identity))
            source_key = {"opened": "github", "merged": "github", "shipped": "testflight", "gates": "gates"}.get(row["kind"])
            status = _dict(source_status.get(source_key))
            meta = _meta(status, now, 720) if status.get("observed_at") is not None else dict(acc_meta)
            if status.get("state") in ("stale", "unavailable"):
                meta["stale"] = True
            item = _achievement(row, meta)
            if item:
                (started if row["kind"] == "opened" else completed).append(item)
    return {"label": block.get("label"), "summary": block.get("summary") or "Evidence unavailable",
            "counts": copy.deepcopy(block.get("counts")),
            "completed": completed, "started": started, **acc_meta}


def _platform(device):
    kind = " ".join(str(device.get(k) or "") for k in
                    ("device_type", "product_type", "model", "os_label")).lower()
    if "ipad" in kind:
        return "ipados"
    if "iphone" in kind:
        return "ios"
    return None


def _aliases(device):
    values = [device.get(k) for k in ("name", "tile_name", "pool_name", "udid", "identifier")]
    out = {str(value).upper() for value in values if value}
    return out


def _stage_platform(step, run, device_map):
    name = str(step.get("name") or "").upper()
    explicit = str(step.get("platform") or run.get("platform") or "").lower()
    if explicit in ("macos", "ios", "ipados"):
        return explicit
    if name.startswith("MAC_"):
        return "macos"
    token = name.split("_", 1)[0]
    if token in device_map:
        return device_map[token]
    for identity in (step.get("device"), run.get("device")):
        if str(identity or "").upper() in device_map:
            return device_map[str(identity).upper()]
    return None


def _platforms(state, now):
    devices = _dict(state.get("devices"))
    dm = _meta(devices, now, 120)
    pipes = _dict(state.get("pipelines"))
    pm = _meta(pipes, now, 45)
    groups = {key: {"id": key, "title": title, "members": [], "builds": [], "tests": []}
              for key, title in (("macos", "Mac"), ("ios", "iPhone"), ("ipados", "iPad"))}
    device_map, unassigned = {}, []
    for dev in _rows(devices.get("items")):
        family = _platform(dev)
        if not family:
            continue
        device_map.update({alias: family for alias in _aliases(dev)})
        active = bool(dev.get("active")) and not dm["stale"]
        groups[family]["members"].append({
            "name": dev.get("tile_name") or dev.get("name") or family,
            "model": dev.get("model"), "os": dev.get("os_label") or dev.get("os"),
            "state": "runtime_active" if active else ("unknown" if dm["stale"] else "no_runtime_observed"),
            "runtime_active": active, "connection": dev.get("state"), **dm,
        })
        if active:
            groups[family]["tests"].append(_evidence(
                "Device runtime activity", "Runtime activity observed; result unreported",
                dm, state="runtime_active", source="devices.active",
                note="Runtime activity can be an app launch; it is not a passed test."))
    for run in _rows(pipes.get("runs")):
        steps = _rows(run.get("steps"))
        recorded = [s for s in steps if s.get("state") in ("pass", "fail")]
        progress = {"mode": "recorded_stages", "done": len(recorded), "total": len(steps),
                    "passed": sum(s.get("state") == "pass" for s in steps),
                    "label": "Recorded results, including failures; not release readiness"}
        for step in steps:
            family = _stage_platform(step, run, device_map)
            if not family:
                unassigned.append({"run": run.get("name"), "stage": step.get("name"),
                                   "reason": "Platform identity not explicit in current evidence"})
                continue
            name = str(step.get("name") or "")
            # Build-for-testing produces a build. Only explicit test stage identities
            # enter the test group; historical generic DIESEL/REL labels stay unknown.
            category = "tests" if name.upper() == "DIESEL" or name.upper().endswith(("_UNIT", "_TEST", "_TESTS", "_DIESEL")) else "builds"
            groups[family][category].append(_evidence(
                name, "Build for testing" if name.upper().endswith("_BFT") else name, pm,
                state={"pass": "passed", "fail": "failed", "run": "active"}.get(step.get("state"), "not_recorded"),
                exit=step.get("exit"), head=run.get("head") or run.get("head8"),
                run=run.get("name"), run_started_at=run.get("start_epoch"),
                source="pipelines.runs.steps", progress=progress))
    for group in groups.values():
        for category in ("builds", "tests"):
            rows = group[category]
            active = any(r.get("state") == "active" and not r["stale"] for r in rows)
            runtime_active = any(r.get("state") == "runtime_active" and not r["stale"] for r in rows)
            group[category] = {
                "state": "active" if active else ("runtime_active" if runtime_active else ("recorded" if rows else "unreported")),
                "summary": "%d recorded observation%s; results apply to the named run/head, not the current release." %
                           (len(rows), "" if len(rows) == 1 else "s") if rows else "No attributable evidence reported.",
                "evidence": rows[:8], "coverage": {"observations": len(rows), "shown": min(8, len(rows)),
                                                   "limited": len(rows) > 8},
                "stale": all(row["stale"] for row in rows) if rows else True,
            }
        group.update(status="active" if any(group[k]["state"] in ("active", "runtime_active") for k in ("builds", "tests"))
                     else "unverified", summary="Build results and runtime/test observations are separate; release readiness is unverified.")
    return list(groups.values()), unassigned


def _machines(state, now):
    tiles = {r.get("id"): r for r in _rows(_dict(state.get("overview")).get("tiles"))}
    rows = []
    for key, name, seconds in (("mini", "Primary-Mac", 45), ("worker", "Build-Mac", 60),
                               ("remote", "Remote-Mac", 180)):
        host = _dict(state.get(key))
        meta = _meta(host, now, seconds)
        load = _dict(host.get("load"))
        hw = _dict(host.get("hardware"))
        offline = host.get("state") in ("unreachable", "disabled", "waiting") or host.get("unreachable_since")
        rows.append({
            "id": key, "name": host.get("name") or _dict(tiles.get(key)).get("name") or name,
            "kind": "mac", "os_family": "macOS",
            "state": "unknown" if meta["stale"] else ("unreachable" if offline else "online"),
            "activity": "Unknown" if meta["stale"] else
                        ("Activity observed" if host.get("active") else "No assigned workload reported"),
            "hardware": {k: copy.deepcopy(hw[k]) for k in ("title", "model", "cpu", "cores",
                         "memory_gb", "memory_bytes", "os", "arch") if k in hw},
            "performance": {k: copy.deepcopy(load[k]) for k in
                            ("cpu", "gpu", "load_1m", "disk_free_bytes", "disk_total_bytes") if k in load},
            "source": key, **meta,
        })
    return rows


def _blockers(state, now):
    gh = _dict(state.get("github"))
    meta = _meta(gh, now, 180)
    rows = []
    for pr in _rows(gh.get("prs")):
        reasons = []
        checks = _dict(pr.get("checks"))
        if checks.get("fail"):
            reasons.append("Failed checks: " + ", ".join(str(v) for v in (pr.get("failed_names") or ["names unreported"])))
        if checks.get("pending"):
            reasons.append("%s check(s) pending" % checks["pending"])
        if pr.get("review_decision") == "CHANGES_REQUESTED":
            reasons.append("GitHub review decision requests changes; reviewer exact-head coverage is unreported")
        elif pr.get("coderabbit") == "CHANGES_REQUESTED":
            reasons.append("Last recorded CodeRabbit review requests changes; exact-head coverage is unreported")
        if pr.get("draft"):
            reasons.append("Pull request remains a draft")
        if not reasons:
            continue
        rows.append({"title": "#%s · %s" % (pr.get("number"), pr.get("title") or "Pull request"),
                     "reason": "; ".join(reasons), "goal": GOALS["integration"],
                     "next_step": "Resolve the reported PR impediment and recheck its current head.",
                     "status": "stale" if meta["stale"] else "reported",
                     "head": pr.get("sha"), "href": pr.get("url"),
                     "note": "PR impediment only; impact on the whole release is not established.",
                     **meta})
    return rows



def _opening_state(row, meta):
    """A historical opening is separate from the source's later PR state."""
    raw = str(row.get("current_state") or "").upper()
    origin = "structured source state"
    if raw not in ("OPEN", "MERGED", "CLOSED"):
        raw = ""
        # Only the legacy collector's generated suffix is accepted as a fallback.
        if row.get("source_kind") == "github" and row.get("source") == "GitHub opened PRs":
            match = re.search(r"\s\((open|merged|closed)\)$", str(row.get("text") or ""), re.I)
            if match:
                raw = match.group(1).upper()
                origin = "legacy GitHub collector state suffix"
    observed = _epoch(row.get("state_observed_at"))
    if observed is None:
        observed = meta.get("observed_at")
    return {
        "state": raw.lower() if raw else "unreported",
        "observed_at": observed,
        "as_of_et": datetime.fromtimestamp(observed, ET).strftime("%Y-%m-%d %H:%M:%S ET") if observed is not None else None,
        "stale": bool(meta.get("stale") or observed is None),
        "source": origin if raw else "Current PR state unreported",
    }


def _program_coverage(value):
    value = _dict(value)
    return {k: copy.deepcopy(value.get(k)) for k in
            ("registered", "expected_receipts", "present_receipts", "valid_receipts",
             "verified", "pending", "expected", "present", "valid", "errors") if k in value}


def _count_value(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _program_platforms(checkpoint):
    """Keep signed hosted tests and unsigned Simulator compilation distinct."""
    out = []
    for lane in _rows(_dict(checkpoint.get("facts")).get("platforms")):
        platform_id = str(lane.get("id") or "")
        platform = "macos" if platform_id == "macos" else "ios" if platform_id == "ios_simulator" else None
        tests = _dict(lane.get("tests"))
        compile_only = lane.get("compile_only") is True
        signed = lane.get("signed") if isinstance(lane.get("signed"), bool) else None
        metrics = []
        if not compile_only:
            for field, label in (("passed", "Tests passed"), ("suites", "Test suites"),
                                 ("new", "New tests"), ("failed", "Tests failed"), ("skipped", "Tests skipped")):
                value = _count_value(tests.get(field))
                if value is not None:
                    metrics.append({"label": label, "value": value})
        for field, label in (("pinned_checkouts", "Pinned checkouts"), ("warnings", "Warnings")):
            value = _count_value(lane.get(field))
            if value is not None:
                metrics.append({"label": label, "value": value})
        architectures = [a for a in lane.get("architectures", []) if isinstance(a, str)] if isinstance(lane.get("architectures"), list) else []
        labels = []
        if lane.get("build"):
            labels.append("Build " + str(lane["build"]))
        labels.append("Signed" if signed is True else "Unsigned" if signed is False else "Signing unreported")
        if compile_only:
            labels.append("Compile/link only; runtime and device tests unverified")
        elif tests and _count_value(tests.get("passed")) is not None:
            labels.append("Hosted test results recorded")
        else:
            labels.append("Runtime/test outcome unreported")
        out.append({
            "id": platform_id, "platform": platform, "checkpoint_id": checkpoint.get("id"),
            "scope": checkpoint.get("scope"), "title": lane.get("title") or "Registered platform result",
            "machine": lane.get("machine"), "build": lane.get("build"), "signed": signed,
            "compile_only": compile_only, "architectures": architectures,
            "metrics": metrics, "tests": copy.deepcopy(tests) if tests and not compile_only else None,
            "occurred_at": lane.get("occurred_at"), "stale": checkpoint.get("stale", True),
            "summary": " · ".join(labels), "state": checkpoint.get("state"),
            "evidence": copy.deepcopy(checkpoint.get("evidence") or []),
        })
    return out


def _program_view(raw, now):
    raw = _dict(raw)
    registered = _count_value(_dict(raw.get("coverage")).get("registered"))
    source_at = _epoch(raw.get("observed_at"))
    source_stale = bool(raw.get("stale") or raw.get("error") or source_at is None or now - source_at > 720)
    checkpoints = []
    for original in _rows(raw.get("checkpoints")):
        cp = copy.deepcopy(original)
        lineage = _dict(cp.get("lineage"))
        coverage = _dict(cp.get("coverage"))
        expected = _count_value(coverage.get("expected"))
        valid = _count_value(coverage.get("valid"))
        present = _count_value(coverage.get("present"))
        complete = expected is not None and expected > 0 and valid == expected and present is not None and present >= expected
        accepted_at = _epoch(cp.get("accepted_at"))
        pending = cp.get("kind") == "pending_adapter" or cp.get("state") in ("pending", "pending_adapter")
        if pending:
            cp["state"] = "pending_adapter"
        verified = (cp.get("accepted") is True and cp.get("state") == "verified"
                    and not source_stale and not cp.get("stale") and not cp.get("error")
                    and lineage.get("verified") is True and complete)
        retained = (cp.get("accepted") is True and (cp.get("state") == "stale" or source_stale)
                    and lineage.get("verified") is True
                    and accepted_at is not None and bool(_dict(cp.get("facts"))))
        cp.update(
            objective=cp.get("objective") or cp.get("title") or "Registered product checkpoint",
            impact=cp.get("impact") or "Accepted evidence advances this registered product checkpoint within its stated scope.",
            next_step=cp.get("next_step") or "Confirm the independent follow-up and any remaining acceptance evidence.",
            release_boundary=cp.get("release_boundary") or "This checkpoint does not establish overall release approval.",
            accepted_verified=verified, historical_accepted=verified or retained,
            stale=bool(cp.get("stale") or (not verified and not pending)),
            status_label="Accepted evidence verified" if verified else "Previously accepted · recheck evidence" if retained else "Evidence connection pending" if pending else "Evidence incomplete",
        )
        cp["evidence"] = [dict(row, label=row.get("label") or row.get("kind") or "Registered receipt",
                               value=row.get("value") or row.get("id") or "Receipt identity unreported")
                          for row in _rows(cp.get("evidence"))]
        cp["platforms"] = _program_platforms(cp) if cp["historical_accepted"] else []
        cp["current"] = copy.deepcopy(_dict(cp.get("current")))
        current = cp["current"]
        if cp["stale"] or source_stale:
            if current.get("state") == "available":
                current["reported_state"] = "available"
                current["state"] = "stale"
            current["stale"] = True
        if source_stale and not pending:
            cp["state"] = "stale" if cp["historical_accepted"] else "unavailable"
            cp["error"] = cp.get("error") or raw.get("error") or "Program evidence source has not refreshed"
        checkpoints.append(cp)
    unconfigured = raw.get("state") == "unconfigured"
    source_known = bool(raw) and not unconfigured and _epoch(raw.get("observed_at")) is not None and registered is not None
    evidence_complete = source_known and registered == len(checkpoints) and all(c["accepted_verified"] for c in checkpoints)
    today = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0)
    week = today.fromordinal(today.toordinal() - today.weekday()).replace(tzinfo=ET)
    achievements = {}
    for key, start in (("today", today), ("week", week)):
        events = []
        timing_unknown = False
        for cp in checkpoints:
            if not cp["historical_accepted"]:
                continue
            accepted_at = _epoch(cp.get("accepted_at"))
            if accepted_at is None or accepted_at > now:
                timing_unknown = True
                continue
            if accepted_at < start.timestamp():
                continue
            events.append({
                "kind": "program_checkpoint", "evidence_id": "program:" + str(cp.get("id") or ""),
                "checkpoint_id": cp.get("id"), "title": cp.get("title") or cp["objective"],
                "goal": cp["objective"], "program": cp.get("program"), "scope": cp.get("scope"),
                "impact": cp["impact"], "release_boundary": cp["release_boundary"],
                "occurred_at": cp.get("occurred_at"), "accepted_at": accepted_at,
                "verified_at": cp.get("verified_at"), "current_observed_at": cp["current"].get("observed_at"),
                "at": datetime.fromtimestamp(accepted_at, ET).strftime("%Y-%m-%d %-I:%M %p ET"),
                "stale": cp["stale"], "verified": cp["accepted_verified"], "evidence": copy.deepcopy(cp.get("evidence") or []),
            })
        # Missing timing makes the window count unknown rather than a fresh zero.
        timing_unknown = timing_unknown or any(c["historical_accepted"] and _epoch(c.get("accepted_at")) is None for c in checkpoints)
        count = len(events) if evidence_complete and not timing_unknown else None
        achievements[key] = {
            "label": "Today" if key == "today" else "Week of " + week.strftime("%a %b %-d"),
            "count": count, "known_count": len(events),
            "verified_count": sum(e["verified"] for e in events),
            "retained_count": sum(not e["verified"] for e in events), "completed": events, "partial": count is None, "stale": any(e["stale"] for e in events),
            "summary": ("No registered product acceptances in this period." if count == 0 else
                        "%s registered product checkpoint%s accepted." % (count, "" if count == 1 else "s") if count is not None else
                        ("%s recorded acceptance%s; %s currently verified%s. Registered coverage is partial." %
                         (len(events), "" if len(events) == 1 else "s", sum(e["verified"] for e in events),
                          ("; %s retained for rechecking" % sum(not e["verified"] for e in events))
                          if any(not e["verified"] for e in events) else ""))
                        if any(e["verified"] for e in events) else
                        ("%s recorded acceptance%s retained; verification needs rechecking. Registered coverage is partial." %
                         (len(events), "" if len(events) == 1 else "s")) if events else
                        "Registered product acceptance coverage is incomplete; no complete window total can be confirmed."),
            "time_basis": "Original acceptance time; local verification and current follow-up are separate clocks.",
        }
    return {
        "state": "unconfigured" if unconfigured else "available" if evidence_complete else "partial" if checkpoints else "unavailable",
        "observed_at": raw.get("observed_at"), "attempted_at": raw.get("attempted_at"),
        "coverage": _program_coverage(raw.get("coverage")), "checkpoints": checkpoints,
        "achievements": achievements,
        "scope_note": "Registered product checkpoints only. These records are separate from workflow totals and do not qualify the whole release.",
        "partial": not evidence_complete, "error": raw.get("error"),
    }

def build_dashboard(state, now=None):
    """Project only supplied evidence. A fresh render never refreshes source clocks."""
    state = _dict(state)
    now = time.time() if now is None else _epoch(now)
    if now is None:
        raise ValueError("now must be a datetime or Unix timestamp")
    acc = _dict(_dict(state.get("overview")).get("accomplishments"))
    am = _meta(acc, now, 720)
    source_status = copy.deepcopy(_dict(acc.get("source_status")))
    achievements = {key: _window(acc.get(key), am, source_status, now) for key in ("today", "week")}
    programs = _program_view(state.get("programs"), now)
    platforms, unassigned = _platforms(state, now)
    for group in platforms:
        group["program_evidence"] = [copy.deepcopy(lane) for cp in programs["checkpoints"]
                                     for lane in cp["platforms"] if lane["platform"] == group["id"]]

    machines = _machines(state, now)
    for platform in platforms:
        if platform["id"] == "macos":
            platform["members"] = [dict(
                name=machine["name"], state=machine["state"],
                activity=machine["activity"], source=machine["source"],
                observed_at=machine["observed_at"],
                as_of_et=machine["as_of_et"], stale=machine["stale"],
            ) for machine in machines]
    blockers = _blockers(state, now)
    counts = _dict(_dict(acc.get("week")).get("counts"))
    def count(kind):
        value = counts.get(kind)
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    def measure(label, kind):
        value = count(kind)
        return _evidence(label, "Unknown" if value is None else str(value), am,
                         count=value, source="overview.accomplishments.week")
    goals = [
        {"id": "integration", "title": GOALS["integration"], "status": "attention" if blockers and not all(b["stale"] for b in blockers) else "unverified",
         "summary": "PR openings propose work; recorded merges show accepted integration, not release completion.",
         "why": "Review and integrate accepted changes before qualifying a candidate.",
         "next_step": "Resolve reported PR impediments and verify the accepted candidate head.",
         "evidence": [measure("Merged this calendar week", "merged"), measure("Opened this calendar week", "opened")]},
        {"id": "qualification", "title": GOALS["qualification"], "status": "unverified",
         "summary": "Recorded gates and platform results qualify their named receipts and heads. Overall candidate coverage is unverified.",
         "why": "Confirm the same candidate works on its intended Apple platforms.",
         "next_step": "Verify required platform gates and physical-device results for the exact candidate.",
         "evidence": [measure("Green gates this calendar week", "gates")]},
        {"id": "distribution", "title": GOALS["distribution"], "status": "recorded" if count("shipped") else "unverified",
         "summary": "Historical successful uploads are distribution handoffs. Current candidate processing and tester availability are unverified.",
         "why": "Make a qualified candidate available for external acceptance.",
         "next_step": "Confirm Apple processing and tester availability for the current qualified build.",
         "evidence": [measure("Uploads recorded this calendar week", "shipped")]},
    ]
    for goal in goals:
        goal.update(scope="workflow_evidence", readiness="unverified", stale=am["stale"],
                    milestones=[{"title": row["title"], "state": "recorded", "at": row["at"], "stale": row["stale"]}
                                for row in achievements["week"]["completed"] if row["goal_id"] == goal["id"]][:5])
    sources = {key: _meta(state.get(key), now, threshold) for key, threshold in
               (("github", 180), ("pipelines", 45), ("devices", 120))}
    stale = am["stale"]
    partial = bool(acc.get("partial") or stale or unassigned or any(m["stale"] for m in machines) or
                   any(m["stale"] for m in sources.values()) or
                   any(v.get("state") != "available" for v in source_status.values() if isinstance(v, dict)))
    return {
        "as_of_et": acc.get("as_of_et"), "generated_at": now,
        "stale": stale, "partial": partial, "readiness": "unverified",
        "headline": "Your release, at a glance.",
        "summary": "Track integrated changes, platform validation and TestFlight handoffs. Current release approval still needs confirmation.",
        "goals": goals, "achievements": achievements, "blockers": blockers[:12],
        "programs": programs,
        "delivery": copy.deepcopy(_dict(state.get("executive_owner"))),
        "brief": copy.deepcopy(_dict(state.get("executive_brief"))),
        "workflow_scope": {
            "label": "Recorded workflow events",
            "summary": "Workflow totals retain their recorded source scopes: branch integrations, repository openings, local gate receipts and TestFlight handoffs. Registered product checkpoints are reported separately; missing branch coverage remains unconfirmed.",
        },
        "platforms": platforms, "machines": machines,
        "coverage": {"source_status": source_status, "snapshot_sources": sources,
                     "achievement_sources": acc.get("sources"),
                     "achievements": copy.deepcopy(acc.get("coverage")),
                     "pr_observed": len(_rows(_dict(state.get("github")).get("prs"))),
                     "pr_scope": "Current dashboard PR snapshot; no total release denominator",
                     "blockers_total": len(blockers), "blockers_shown": min(12, len(blockers)),
                     "unassigned_platform_stages": unassigned,
                     "notes": ["Successful polling does not prove the underlying status file was recently updated.",
                               "Missing results and unavailable sources remain unknown, not zero.",
                               "Build/test records may describe historical heads. No total readiness percentage is inferred."]},
    }
