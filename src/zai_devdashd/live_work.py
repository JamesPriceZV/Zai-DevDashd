"""Live work for the In progress card. Read-only: ps, gate SUMMARY.md, autotune loop.log, gh PR snapshot."""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
from zai_devdashd import config as site
Z = str(site.optional_path("scratch"))
_cache = {"t": 0.0, "rows": [], "error": None}


class InspectionUnavailable(RuntimeError):
    pass


def _ps() -> list[dict]:
    try:
        result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,lstart=,command="], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InspectionUnavailable("Process inspection unavailable") from exc
    if result.returncode != 0:
        raise InspectionUnavailable("Process inspection unavailable")
    out = result.stdout
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\w+ \w+ +\d+ [\d:]+ \d+)\s+(.*)", line)
        if not m:
            continue
        try:
            start = datetime.strptime(re.sub(r" +", " ", m.group(3)), "%a %b %d %H:%M:%S %Y").replace(tzinfo=ET)
        except ValueError:
            continue
        rows.append({"pid": int(m.group(1)), "ppid": int(m.group(2)), "start": start, "cmd": m.group(4)})
    return rows


def _el(start: datetime, now: datetime) -> str:
    mins = int((now - start).total_seconds() // 60)
    span = "%dh %02dm" % (mins // 60, mins % 60) if mins >= 60 else "%dm" % mins
    return "started %s ET · %s" % (start.strftime("%-I:%M %p"), span)


def _children(procs, pid):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        for c in procs:
            if c["ppid"] == p:
                out.append(c)
                todo.append(c["pid"])
    return out


def _xc_step(cmd: str) -> str:
    for verb in ("build-for-testing", "test-without-building", "archive", "build", "test"):
        if " %s " % verb in " %s " % cmd:
            if verb == "build" and "generic/platform=iOS" in cmd:
                return "iOS generic build"
            return "xcodebuild " + verb
    return "xcodebuild"


def _gate_rows(procs, now):
    rows = []
    for p in procs:
        m = re.search(r"(/\S*gate-logs-[^/\s]+)/(gate[^/\s]*\.sh)", p["cmd"])
        if not m or not p["cmd"].startswith("/bin/bash"):
            continue
        logdir, script = m.group(1), m.group(2)
        pr, sha, step = "", "", "running"
        try:
            text = open(os.path.join(logdir, script)).read()
            calls = re.findall(r"^gate (\d+) ([0-9a-f]{8})", text, re.M)
        except OSError:
            calls = []
        summ = ""
        try:
            summ = open(os.path.join(logdir, "SUMMARY.md")).read()
        except OSError:
            pass
        kids = _children(procs, p["pid"])
        xc = [k for k in kids if "xcodebuild" in k["cmd"] and "build-slot.sh" not in k["cmd"]] or \
             [k for k in kids if "xcodebuild" in k["cmd"]]
        heads = re.findall(r"^## #(\d+) @ ([0-9a-f]{8})", summ, re.M)
        if xc:
            if heads:
                pr, sha = heads[-1]
            step = _xc_step(xc[0]["cmd"])
            wt = re.search(r"DerivedData/gate-(\d+)-([0-9a-f]{8})", xc[0]["cmd"])
            if wt:
                pr, sha = wt.group(1), wt.group(2)
            if not any("xcodebuild" in k["cmd"] and "build-slot.sh" not in k["cmd"] for k in kids):
                step += " (queued for build slot)"
        elif any(re.match(r"sleep\b", k["cmd"]) for k in kids) and "while ! grep" in (open(os.path.join(logdir, script)).read() if os.path.exists(os.path.join(logdir, script)) else ""):
            mine = [c for c in calls if (c[0], c[1]) not in heads]
            if mine:
                pr, sha = mine[0]
            step = "waiting for the earlier gate in %s to finish" % os.path.basename(logdir)
        else:
            if heads:
                pr, sha = heads[-1]
            last = [l for l in summ.splitlines() if l.startswith("- ")]
            step = last[-1][2:].split(" log=")[0][20:] if last else "starting"
        if not pr and calls:
            pr, sha = calls[0]
        rows.append({"kind": "Mini gate", "what": "Mini gate %s" % script, "pr": ("#" + pr) if pr else "",
                     "head": sha[:8], "step": step, "when": _el(p["start"], now), "pid": p["pid"]})
    return rows


def _lane_rows(procs, now):
    rows, seen = [], set()
    for p in procs:
        m = re.search(r"lane\.sh \w+ (L-[A-Z]+-\d+)", p["cmd"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        lane = m.group(1)
        sha = re.search(r"-([0-9a-f]{8})-\d{4}\.md", p["cmd"])
        kids = _children(procs, p["pid"])
        step = "agent working"
        for p2 in procs:
            if lane in p2["cmd"] and "xcodebuild" in p2["cmd"] and "build-slot.sh" in p2["cmd"]:
                step = "agent working · " + _xc_step(p2["cmd"]) + " running"
                break
        num = re.search(r"-(\d+)$", lane).group(1)
        rows.append({"kind": "Refresh lane", "what": "Refresh lane %s" % lane, "pr": "#" + num,
                     "head": sha.group(1) if sha else "", "step": step, "when": _el(p["start"], now), "pid": p["pid"]})
    return rows


def _autotune_rows(procs, now):
    rows = []
    for p in procs:
        match = re.match(r"^\S*(?:Python|python(?:[0-9.]+)?)\s+(\S*autotune\.py)(?:\s|$)", p["cmd"])
        if not match:
            continue
        script = match.group(1)
        d = os.path.dirname(script) if os.path.isabs(script) else ""
        step = "running" if d else "running; run directory not reported"
        if d:
            try:
                last = [l for l in open(os.path.join(d, "logs", "loop.log")).read().splitlines() if l.strip()][-1]
                step = re.sub(r"(PAUSED \([^)]*\)):.*", r"\1 · waiting for a build slot", last)[:160]
            except (OSError, IndexError):
                pass
        rows.append({"kind": "Autotune", "what": "zai-coder autotune loop" + ((" (%s)" % os.path.basename(d)) if d else ""), "pr": "",
                     "head": "", "step": step, "when": _el(p["start"], now), "pid": p["pid"]})
        break
    for p in procs:
        if "zai-coder review" in p["cmd"] and "build-slot.sh" not in p["cmd"]:
            u = re.search(r"--units (\S+)", p["cmd"])
            s = re.search(r"--suite (\S+)", p["cmd"])
            rows.append({"kind": "Autotune", "what": "zai-coder review", "pr": "", "head": "",
                         "step": "unit %s · suite %s" % (u.group(1) if u else "?", s.group(1) if s else "?"),
                         "when": _el(p["start"], now), "pid": p["pid"]})
    return rows


def _cr_rows(procs, now):
    rows = []
    for p in procs:
        if re.match(r"(\S*/)?coderabbit review", p["cmd"]):
            base = re.search(r"--base-commit ([0-9a-f]{8})", p["cmd"])
            cwd = ""
            try:
                out = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(p["pid"]), "-d", "cwd", "-Fn"],
                                     capture_output=True, text=True, timeout=3).stdout
                cwd = next((l[1:] for l in out.splitlines() if l.startswith("n")), "")
            except Exception:
                pass
            pr = re.search(r"(\d{3,5})", os.path.basename(cwd) or "")
            rows.append({"kind": "CodeRabbit", "what": "CodeRabbit CLI preview" + (" in " + os.path.basename(cwd) if cwd else ""),
                         "pr": ("#" + pr.group(1)) if pr else "", "head": "",
                         "step": "reviewing vs base %s" % (base.group(1) if base else "?"),
                         "when": _el(p["start"], now), "pid": p["pid"]})
    return rows


def _native_build_rows(procs, now, represented=()):
    """Project actual xcodebuild processes that older gate/lane wrappers miss."""
    rows = []
    by_pid = {p["pid"]: p for p in procs}
    covered = {r.get("pid") for r in represented if r.get("pid")}
    for p in procs:
        command = p.get("cmd") or ""
        executable = command.split(None, 1)[0] if command else ""
        if os.path.basename(executable) != "xcodebuild":
            continue
        # An existing gate/lane row represents its whole process subtree.
        parent, seen = p["pid"], set()
        while parent and parent not in seen and parent not in covered:
            seen.add(parent)
            parent = (by_pid.get(parent) or {}).get("ppid")
        if parent in covered:
            continue
        dd = re.search(r"(?:^|\s)-derivedDataPath\s+(\S+)", command)
        label = os.path.basename(dd.group(1))[:100] if dd else ""
        rows.append({"kind": "Native build", "what": "Native build" + ((" · " + label) if label else ""),
                     "pr": "", "head": "", "step": _xc_step(command),
                     "when": _el(p["start"], now), "pid": p["pid"],
                     "progress": None, "progress_note": "Progress percentage not reported by xcodebuild"})
    return rows


def _pr_rows(prs, now):
    rows = []
    for pr in prs or []:
        ts = pr.get("updated_at") if isinstance(pr, dict) else None
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
        except ValueError:
            continue
        if now - t <= timedelta(hours=2):
            rows.append({"kind": "PR activity", "what": "PR #%s %s" % (pr.get("number"), (pr.get("title") or "")[:60]),
                         "pr": "#%s" % pr.get("number"), "head": pr.get("sha") or "",
                         "step": "updated on GitHub %s ET" % t.strftime("%-I:%M %p"),
                         "when": "", "pid": None})
    return rows


def collect(prs=None) -> list[dict]:
    now = datetime.now(ET)
    if not site.adapter_enabled("local_system"):
        return [{"kind": "Status unavailable", "what": "Local activity adapter is disabled", "pr": "",
                 "head": "", "step": "Unconfigured", "when": "", "pid": None, "stale": True}]
    if time.time() - _cache["t"] < 5:
        procs_rows = _cache["rows"]
    else:
        try:
            procs = _ps()
        except InspectionUnavailable:
            procs_rows = _cache["rows"]
            _cache.update(t=time.time(), error="Process inspection unavailable")
        else:
            procs_rows = []
            for fn in ((_gate_rows, _lane_rows, _autotune_rows, _cr_rows) if site.adapter_enabled("git") else ()):
                try:
                    procs_rows += fn(procs, now)
                except Exception:
                    pass
            procs_rows += _native_build_rows(procs, now, procs_rows)
            _cache.update(t=time.time(), rows=procs_rows, error=None)
    if _cache.get("error"):
        procs_rows = [dict(row, stale=True) for row in procs_rows]
        procs_rows.append({"kind": "Status unavailable", "what": "Job status unavailable", "pr": "", "head": "",
                           "step": _cache["error"], "when": "", "pid": None, "stale": True})
    active = {r["pr"] for r in procs_rows if r["pr"]}
    return procs_rows + [r for r in _pr_rows(prs, now) if r["pr"] not in active]


def line(row: dict) -> str:
    bits = [row["what"]]
    if row.get("pr") and row["pr"] not in row["what"]:
        bits.append(row["pr"])
    if row.get("head"):
        bits.append(row["head"])
    bits.append(row["step"])
    if row.get("when"):
        bits.append(row["when"])
    return " · ".join(bits)
