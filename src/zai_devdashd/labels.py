"""Plain-language titles, device lines, and the settings form."""

from __future__ import annotations

from zai_devdashd.config import (
    DEFAULT_CONFIG,
    apply_settings,
    load_config,
    save_config,
)

import copy
import html
import ipaddress
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Toronto")

CANON_STEPS = ["MAC_BFT", "PHONE_BFT", "MAC_UNIT", "DIESEL", "REL"]
STEP_WORDS = {
    "MAC_BFT": "Mac build",
    "PHONE_BFT": "phone build",
    "MAC_UNIT": "Mac unit tests",
    "DIESEL": "Integration tests",
    "REL": "release build",
}

# Whole-title generics. A useful title that merely contains one of these words stays.
GENERIC_TITLE = re.compile(
    r"^(?:the\s+)?(?:"
    r"build lane|mac build|phone build|unit tests|integration tests|pipeline|release|worker run|device test"
    r")(?:\s+step)?\s+running\.?$"
    r"|^(?:the\s+)?build lane is running\.?$"
    r"|^(?:the\s+)?worker run\.?$",
    re.I,
)
SWIFT_FILE = re.compile(r"([A-Za-z0-9_+.-]+\.swift)\b")
FAILED_WORD = re.compile(r"\bfailed\b", re.I)


_LAN = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)


def generic_title(text: str | None) -> bool:
    return bool(GENERIC_TITLE.match(" ".join((text or "").split())))


def usable_title(candidate: str | None, fallback: str) -> str:
    text = " ".join((candidate or "").split())
    if text and not generic_title(text):
        return text[:180]
    return " ".join((fallback or "Job").split())[:180]


def usable_goal(candidate: str | None, fallback: str) -> str:
    text = " ".join((candidate or "").split())
    if text.lower().startswith("goal:"):
        text = text[5:].strip()
    if not text or generic_title(text) or generic_title("Goal: " + text):
        text = fallback or ""
        if text.lower().startswith("goal:"):
            return text[:220]
    if not text:
        return ""
    if text.lower().startswith("goal:"):
        return text[:220]
    return ("Goal: " + text)[:220]


def scrub_step(text: str) -> str:
    """A running step is never described as failed."""
    cleaned = FAILED_WORD.sub("", text or "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned).strip(" ,;·")
    if not cleaned or generic_title(cleaned):
        return "Running"
    return cleaned[:180]


def human_short(seconds: float | None) -> str:
    if seconds is None:
        return ""
    try:
        value = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return ""
    if value >= 3600:
        return "%dh %dm" % (value // 3600, (value % 3600) // 60)
    if value >= 60:
        return "%dm" % (value // 60)
    return "%ds" % value


def clock_hhmm(epoch: float | None) -> str:
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), ET).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def pipeline_purpose(pr: int | None, pr_title: str | None, subject: str | None) -> str:
    name = " ".join(((pr_title or subject or "pipeline work") or "").split()).rstrip(".")
    if not name:
        name = "pipeline work"
    if pr:
        return usable_title(None, "#%d · %s" % (pr, name))
    return usable_title(None, name)


def pipeline_goal(pr_title: str | None, subject: str | None, brief_goal: str | None, needs_goal: str | None) -> str:
    for source in (brief_goal, needs_goal):
        text = " ".join((source or "").split())
        if text and not generic_title(text):
            return usable_goal(text, "")
    name = " ".join(((pr_title or subject or "this change") or "").split()).rstrip(".") or "this change"
    return "Goal: %s passes Mac build, phone build, Mac unit tests, integration tests and release build" % name


def pipeline_step(run: dict) -> str:
    """Current step of this run only. Ignores older runs and failed steps."""
    if (run or {}).get("state") != "running":
        return ""
    by_name = {}
    for step in run.get("steps") or []:
        if isinstance(step, dict) and step.get("name"):
            by_name[step["name"]] = step
    ordered = list(by_name)
    total = run.get("progress_total") or max(len(CANON_STEPS), len(ordered))
    for index, name in enumerate(ordered, 1):
        step = by_name.get(name)
        if not step or step.get("state") != "run":
            continue
        word = step.get("word") or STEP_WORDS.get(name, name)
        if name == "DIESEL":
            word = "Integration tests"
        dur = human_short(step.get("duration_s"))
        text = "Step %d of %d: %s, running" % (index, total, word)
        if dur:
            text += " " + dur
        return scrub_step(text)
    return "Running"


def action_phrase(action: str | None) -> str:
    """Plain words for the newest lane action. A tool path is not repeated."""
    text = " ".join(str(action or "").split())
    if not text:
        return ""
    match = SWIFT_FILE.search(text)
    if match:
        return "Editing " + match.group(1)
    lowered = text.lower()
    first = lowered.split()[0]
    base = first.rsplit("/", 1)[-1].rstrip(":")
    if base in ("search_replace", "strreplace", "apply_patch", "write", "edit"):
        return "Editing"
    if base in ("read_file", "grep", "glob", "list_dir"):
        return "Reading the project"
    if base in ("get_command_or_subagent_output", "await"):
        return "Waiting on a command"
    if base == "run_terminal_command" or lowered.startswith("run_terminal_command"):
        if re.search(r"\b(xcodebuild|unit-tests|swift-test|xctest)\b", lowered) and re.search(r"\btests?\b", lowered):
            return "Running tests"
        if re.search(r"\b(xcodebuild|swift-build|swift build)\b", lowered):
            return "Building"
        return "Running a command"
    if base in ("swift-test", "xctest") or (base == "xcodebuild" and re.search(r"\btest\b", lowered)):
        return "Running tests"
    if base in ("xcodebuild", "swift-build", "swift"):
        return "Building"
    if base == "git" or lowered.startswith("git "):
        if re.search(r"\bpush\b", lowered):
            return "Pushing"
        if re.search(r"\bcommit\b", lowered):
            return "Committing"
        return "Updating git"
    if re.search(r"\b(pytest|xcresult)\b", lowered):
        return "Running tests"
    if "/" in first:
        return "Running " + base
    return ""


def lane_step(actions: list | None, bucket: str | None, epoch: float | None) -> str:
    action = str(actions[0] or "") if actions else ""
    text = action_phrase(action)
    if not text:
        text = {
            "editing": "Editing",
            "testing": "Running tests",
            "building": "Building",
            "pushing": "Pushing",
            "waiting": "Waiting for the next action",
        }.get(bucket or "", "Working")
    clock = clock_hhmm(epoch)
    if clock:
        text += " · last commit " + clock
    return scrub_step(text)


def scan_lane_jsonl(text: str) -> dict:
    """Newest assistant sentence and tool call in one lane jsonl.

    Text events are streamed deltas. A chunk is kept only when it is long
    enough to be a sentence, so a trailing fragment does not replace it.
    """
    assistant = ""
    tool = ""
    newest = ""
    chunk = ""

    def flush_text() -> None:
        nonlocal assistant, newest, chunk
        body = " ".join(chunk.split())
        chunk = ""
        if len(body) >= 12:
            assistant = body
            newest = "text"

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = str(obj.get("type") or "")
        if kind == "text":
            data = obj.get("data")
            if isinstance(data, str):
                chunk += data
            continue
        flush_text()
        if kind != "tool_call":
            continue
        title = str(obj.get("title") or obj.get("toolName") or "").strip()
        raw_input = obj.get("rawInput")
        command = ""
        if isinstance(raw_input, dict):
            command = str(raw_input.get("command") or raw_input.get("description") or "").strip()
        label = title if not command else "%s: %s" % (title, command)
        label = " ".join(label.split())
        if label:
            tool = label[:160]
            newest = "tool"
    flush_text()
    return {"assistant": assistant, "tool": tool, "newest": newest}


def _first_sentence(text: str) -> str:
    body = " ".join(str(text or "").split())
    if len(body) < 12:
        return ""
    match = re.match(r"^(.+?[.!?])(?:\s|$)", body)
    sentence = match.group(1).strip() if match else body
    return sentence


def _activity_phrase(assistant: str, tool: str, newest: str, actions: list | None, bucket: str | None) -> str:
    if newest == "text":
        sentence = _first_sentence(assistant)
        if len(sentence) >= 12:
            return sentence
    tool_text = " ".join(str(tool or "").split())
    if not tool_text and actions:
        tool_text = " ".join(str(actions[0] or "").split())
    lowered = tool_text.lower()
    if "diesel" in lowered:
        return "Running integration tests"
    mac_unit = "unit-tests" in lowered or "unit tests" in lowered
    if (
        not mac_unit
        and re.search(r"\bxcodebuild\b", lowered)
        and re.search(r"\btests?\b", lowered)
        and not re.search(r"\b(iphone|ipad|phone)\b", lowered)
        and not re.search(r"\bplatform\s*=\s*(ios|ipados|visionos|tvos|watchos)\b", lowered)
        and not re.search(r"\b(iphoneos|iphonesimulator)\b", lowered)
    ):
        mac_unit = True
    if mac_unit:
        return "Running Mac unit tests"
    phrase = action_phrase(tool_text)
    if phrase:
        return phrase
    return lane_step(actions, bucket, None)


def lane_activity(
    assistant: str | None,
    tool: str | None,
    newest: str | None,
    busy: bool,
    epoch: float | None,
    now: float | None = None,
    actions: list | None = None,
    bucket: str | None = None,
) -> str:
    """Working line from the newest jsonl event. The clock is that file's mtime."""
    try:
        when = float(epoch) if epoch else 0.0
    except (TypeError, ValueError):
        when = 0.0
    try:
        current = float(now) if now is not None else time.time()
    except (TypeError, ValueError):
        current = time.time()
    rel = human_short(max(0.0, current - when)) if when else ""
    clock = clock_hhmm(when) if when else ""
    if not busy:
        # A fresh idle is "Just finished". After a minute, name the clock only.
        age = max(0.0, current - when) if when else None
        if age is not None and age < 60:
            return "Just finished"
        if clock:
            return scrub_step("Idle since %s ET" % clock)
        return "Idle"
    phrase = _activity_phrase(assistant or "", tool or "", newest or "", actions, bucket)
    if rel and clock:
        suffix = " · last activity %s (%s ET)" % (rel, clock)
    elif clock:
        suffix = " · last activity %s ET" % clock
    elif rel:
        suffix = " · last activity %s" % rel
    else:
        suffix = ""
    room = 180 - len(suffix)
    if len(phrase) > room:
        phrase = phrase[: max(1, room - 1)].rstrip() + "…"
    return scrub_step(phrase + suffix)


def zai_goal(units: str, run_id: str, kind: str) -> str:
    if units and run_id:
        return "Goal: finish units %s in run %s" % (units, run_id)
    if units:
        return "Goal: finish units %s" % units
    if run_id:
        return "Goal: finish run %s" % run_id
    return "Goal: finish the %s on the worker" % (kind or "worker run")


def zai_step(units: str) -> str:
    if units:
        return scrub_step("Running units " + units)
    return "Running"


def device_who(name: str | None) -> str:
    """A spoken device name. A bare phone token is never the whole title."""
    who = " ".join(str(name or "Device").split()) or "Device"
    key = who.lower().replace(" ", "")
    if key == "phone":
        return "Phone"
    return who


def device_lines(name: str, raw: str) -> tuple[str, str, str]:
    """What a device session is for, from the current log line only.

    The step names the test and the device. It never repeats a tool path
    and never says a running session failed.
    """
    text = " ".join((raw or "").split())
    who = device_who(name)
    tool = ""
    if text:
        token = text.split()[0]
        if "/" in token:
            tool = token.rsplit("/", 1)[-1]
    pr_match = re.search(r"\beng-(\d+)\b", text) or re.search(r"#(\d{3,})\b", text)
    pr = pr_match.group(1) if pr_match else ""
    lowered = text.lower()
    if "diesel" in lowered:
        phase = "integration tests"
    elif "dsymutil" in lowered or "-rel" in lowered or "/release" in lowered:
        phase = "release build"
    elif "xcodebuild" in lowered and "test" in lowered:
        phase = "device test"
    elif tool:
        phase = tool
    else:
        phase = ""
    phase_title = (phase[:1].upper() + phase[1:]) if phase else ""
    if pr and phase:
        title = "#%s · %s on %s" % (pr, phase, who)
        goal = "Goal: #%s %s finishes on %s" % (pr, phase, who)
    elif phase:
        title = "%s on %s" % (phase_title, who)
        goal = "Goal: %s finishes on %s" % (phase, who)
    else:
        title = "Work on %s" % who
        goal = "Goal: the current session on %s finishes" % who
    step = ("Running %s on %s" % (phase, who)) if phase else ("Running on %s" % who)
    return usable_title(title, "Work on %s" % who), goal, scrub_step(step)


_LOWER_DEVICE = re.compile(r"^(?:phone|ipad|iphone|device)\b", re.I)
_GOALISH = re.compile(r"^(?:goal:|all )\b", re.I)
_BUILD_FAIL = re.compile(r"UNIT-FAIL|(?:^|[\s|/])FAIL(?:[\s|]|$)|\bbft=65\b", re.I)
_MERGE_HOLD = re.compile(r"UNIT-BLOCKED|conflicts with #", re.I)
_AHEAD = re.compile(r"conflicts with #(\d+)", re.I)


def _pr_number(value) -> str:
    text = str(value or "").strip().lstrip("#")
    return text if text.isdigit() else ""


def work_title(name: str | None, goal: str | None, pr, where: str | None, subject: str | None = "") -> str:
    """A plain working title. A goal sentence and a bare device word are not titles."""
    number = _pr_number(pr)
    subject_text = " ".join(str(subject or "").split()).rstrip(".")
    where_text = str(where or "")
    code_work = where_text in ("Pipeline", "Lane", "Worker") or where_text.startswith("L-")
    if code_work and number and subject_text and not _GOALISH.match(subject_text) and not _LOWER_DEVICE.match(subject_text):
        return ("#%s · %s" % (number, subject_text))[:180]
    text = " ".join(str(name or "").split())
    if (
        not text
        or _GOALISH.match(text)
        or "/" in text
        or "\\" in text
        or text.lower().startswith("pipeline")
        or _LOWER_DEVICE.match(text)
        or text.lower() in ("phone", "running")
        or re.match(r"^(?:L-[A-Z]|MON)\b", text)
        or (not text.startswith("#") and " · " not in text and len(text) > 90)
    ):
        text = ""
    if not text:
        subject_text = " ".join(str(subject or "").split()).rstrip(".")
        if not subject_text:
            subject_text = re.sub(r"^Goal:\s*", "", " ".join(str(goal or "").split()), flags=re.I)
            subject_text = re.sub(r"\s+passes Mac build.*$", "", subject_text).rstrip(".")
        if _GOALISH.match(subject_text) or _LOWER_DEVICE.match(subject_text) or len(subject_text) > 140:
            subject_text = ""
        device = ""
        if where and where not in ("Pipeline", "Lane", "Worker"):
            device = device_who(where)
        if number and subject_text:
            text = "#%s · %s" % (number, subject_text)
        elif subject_text and device:
            text = "%s on %s" % (subject_text[:1].upper() + subject_text[1:], device)
        elif device:
            text = "Work on %s" % device
        elif number:
            text = "#%s" % number
        else:
            text = "Work in progress"
    elif number and not re.match(r"^#\d+\b", text):
        text = "#%s · %s" % (number, text)
    if _LOWER_DEVICE.match(text):
        text = "Work on " + device_who(text)
    return text[:180]


def work_line(name: str | None, goal: str | None, pr, step: str | None, where: str | None, subject: str | None = "") -> str:
    title = work_title(name, goal, pr, where, subject)
    step_text = " ".join(str(step or "").split())
    step_text = re.sub(r"^current step:\s*", "", step_text, flags=re.I)
    step_text = re.sub(r"\bDiesel(?: test)?\b", "integration tests", step_text, flags=re.I)
    if step_text.lower() in ("running", ""):
        return title
    return "%s — %s" % (title, step_text)


def fail_reason(pr: dict, fail: int, hold: str) -> str:
    """Name the failing check and the head it ran on, instead of a bare 'A check failed'."""
    sha = str(pr.get("sha") or "")[:8]
    names = list(dict.fromkeys(str(n) for n in (pr.get("failed_names") or []) if n))
    at = (" on head %s" % sha) if sha else ""
    if fail > 0 and names:
        shown = ", ".join(names[:3]) + (" and %d more" % (len(names) - 3) if len(names) > 3 else "")
        return "GitHub check failed%s: %s." % (at, shown)
    if fail > 0:
        return "%d GitHub check%s failed%s." % (fail, "" if fail == 1 else "s", at)
    m = _BUILD_FAIL.search(hold or "")
    return "Local gate note says %s (from the merge ledger, may predate head %s)." % (
        (m.group(0).strip() if m else "a failure"), sha or "the current head")


def pr_wait(pr: dict, running: set | None = None) -> dict | None:
    """One waiting line for a pull request, or nothing when it is not waiting.

    A failed check outranks a merge hold, which outranks code review, which
    outranks checks that are still running. The spoken person comes from that
    reason, not from the raw status line.
    """
    if not isinstance(pr, dict) or pr.get("number") is None:
        return None
    number = pr.get("number")
    checks = pr.get("checks") or {}
    review = str(pr.get("coderabbit") or "")
    hold = str(pr.get("hold") or "")
    title = " ".join(str(pr.get("title") or "").split())
    label = "#%s" % number
    if title:
        label = "%s · %s" % (label, title[:120])
    try:
        fail = int(checks.get("fail") or 0)
        pending = int(checks.get("pending") or 0)
    except (TypeError, ValueError):
        fail, pending = 0, 0
    ahead_match = _AHEAD.search(hold)
    ahead = ahead_match.group(1) if ahead_match else ""
    merge_hold = bool(_MERGE_HOLD.search(hold)) and str(number) != ahead
    build_fail = fail > 0 or bool(_BUILD_FAIL.search(hold))
    review_wait = review in ("CHANGES_REQUESTED", "PENDING", "REVIEW_REQUIRED")
    moving = number in (running or set())
    if build_fail and pending == 0:
        return {"tone": "stop", "pr": number, "text": "%s — Waiting on the pipeline. %s" % (label, fail_reason(pr, fail, hold))}
    if merge_hold:
        if ahead and str(number) != ahead:
            return {
                "tone": "wait",
                "pr": number,
                "text": "%s — Waiting on the merge order, behind #%s." % (label, ahead),
            }
        return {"tone": "wait", "pr": number, "text": "%s — Waiting on the recorded merge hold." % label}
    if review_wait:
        return {
            "tone": "wait",
            "pr": number,
            "text": "%s — Waiting on CodeRabbit for code review." % label,
        }
    if pending:
        if moving:
            return {"tone": "go", "pr": number, "text": "%s — The pipeline is running checks." % label}
        return {
            "tone": "wait",
            "pr": number,
            "text": "%s — Waiting on the pipeline for checks." % label,
        }
    if build_fail:
        return {
            "tone": "stop",
            "pr": number,
            "text": "%s — Waiting on the pipeline. %s" % (label, fail_reason(pr, fail, hold)),
        }
    return None


def _job_pr(job: dict):
    for row in job.get("technical") or []:
        if isinstance(row, dict) and row.get("label") == "Pull request":
            number = _pr_number(row.get("value"))
            if number:
                return int(number)
    match = re.search(r"#(\d+)", str(job.get("name") or ""))
    return int(match.group(1)) if match else None


def recorded_person(source: str | None) -> str:
    text = " ".join(str(source or "").split())
    if not text or "/" in text or "\\" in text or re.search(r"\.[A-Za-z0-9]{1,8}$", text):
        return ""
    return text


def overview_lists(jobs: list | None, runs: list | None, prs: list | None, decisions: list | None, titles: dict | None = None) -> dict:
    """Working, done, and one waiting line per pull request, in merge-queue order."""
    titles = titles or {}
    working = []
    job_waits = []
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        number = _job_pr(job)
        line = work_line(
            job.get("name"),
            job.get("goal"),
            number,
            job.get("step"),
            job.get("where"),
            titles.get(number) or "",
        )
        if re.search(r"waiting", str(job.get("step") or ""), re.I):
            job_waits.append({"tone": "wait", "text": line})
        else:
            working.append({"tone": "go", "text": line})
    done = []
    running = set()
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        if run.get("state") == "running" and run.get("pr"):
            running.add(run.get("pr"))
        if run.get("today") and run.get("state") == "passed":
            label = run.get("purpose") or run.get("fm_label") or run.get("label") or "a recorded pipeline"
            done.append({"tone": "go", "text": "Finished: %s" % label})
    waiting = list(job_waits)
    for pr in prs or []:
        row = pr_wait(pr, running)
        if row:
            waiting.append({"tone": row["tone"], "text": row["text"], "pr": row["pr"]})
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        question = item.get("question") or item.get("raw") or "An open question is recorded."
        person = recorded_person(item.get("source"))
        whom = ("Waiting on %s." % person) if person else "Waiting on an answer."
        waiting.append({"tone": "wait", "text": "%s %s" % (whom, question)})
    return {
        "working": working,
        "done": done,
        "waiting": waiting[:6],
        "waiting_more": waiting[6:],
    }


def remote_tool_lines(commands: list | None) -> tuple[str, str, str]:
    """Name an Worker build or test from the live commands, not from a path that contains zai."""
    commands = [str(command) for command in (commands or []) if command]
    blob = "\n".join(commands)
    product = ""
    match = re.search(r"/([A-Za-z0-9_+.-]+)\.xctest\b", blob)
    if match:
        product = match.group(1)
    tool = ""
    for command in commands:
        base = command.split()[0].rsplit("/", 1)[-1]
        if base in ("swift-test", "xctest", "xcodebuild", "swift-build"):
            tool = base
            break
    if tool in ("swift-test", "xctest") or (tool == "xcodebuild" and re.search(r"\btest\b", blob)):
        what = product or "Swift tests"
        title = "Worker · %s" % what
        goal = "Goal: %s passes on Worker" % what
        step = "Running " + tool
    elif tool:
        title = "Worker · %s" % tool
        goal = "Goal: %s finishes on Worker" % tool
        step = "Running " + tool
    else:
        title = "Worker job"
        goal = "Goal: the current Worker job finishes"
        step = "Running"
    return usable_title(title, "Worker job"), goal, scrub_step(step)


def device_state(conn: dict | None) -> str:
    """Match `devicectl list devices`: connected, available (paired), unavailable."""
    conn = conn or {}
    tunnel = conn.get("tunnelState") or ""
    if tunnel == "connected":
        return "connected"
    if conn.get("pairingState") == "paired" and conn.get("transportType"):
        return "available (paired)"
    if tunnel in ("disconnected", "unavailable", ""):
        return "unavailable"
    return str(tunnel)


def match_rfb(names: list | None, host: str | None) -> str:
    """Match the configured hostname exactly; never guess a different fleet member."""
    host_text = (host or "").split(":", 1)[0].strip().lower().rstrip(".")
    stem = host_text.split(".")[0]
    found = [str(name).strip() for name in (names or []) if str(name).strip()]
    for name in found:
        if name.lower() == stem or name.lower() == host_text:
            return name
    return ""


def vnc_href(instance: str | None, fallback_ip: str | None) -> str:
    name = (instance or "").strip()
    if name:
        return "vnc://%s._rfb._tcp.local?quality=adaptive&numVirtualDisplays=0" % quote(name, safe="")
    ip = (fallback_ip or "").strip()
    if ip and re.match(r"^[0-9.]+$", ip):
        return "vnc://%s?quality=adaptive&numVirtualDisplays=0" % ip
    return ""


def vncloc_plist(href: str) -> bytes:
    safe = html.escape(href or "", quote=True)
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        "<plist version=\"1.0\"><dict><key>URL</key><string>%s</string></dict></plist>\n" % safe
    )
    return text.encode("utf-8")


def device_reachable(state: str | None, placeholder: bool = False) -> bool:
    if placeholder:
        return False
    return (state or "").lower() in ("connected", "available (paired)")


_DEVICE_TOKEN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def console_device(items: list | None, token: str) -> dict | None:
    """A physical device from the current list, matched by udid or CoreDevice id."""
    text = str(token or "").strip()
    if not text or ".." in text or not _DEVICE_TOKEN.match(text):
        return None
    for item in items or []:
        if not isinstance(item, dict) or item.get("placeholder"):
            continue
        if str(item.get("reality") or "").lower() == "simulated":
            continue
        udid = str(item.get("udid") or "")
        identifier = str(item.get("identifier") or "")
        if (udid and text == udid) or (identifier and text == identifier):
            return item
    return None


def device_console_label(device: dict) -> str:
    text = " ".join(str(device.get("tile_name") or device.get("name") or "the device").split())
    return (text[:80] or "the device")


def open_device_console(items: list | None, token: str, launch) -> tuple[str, int]:
    """Launch Device Hub only for an id on the current physical list.

    `launch` is called with the fixed argv. The spoken label comes from that
    list. A client-supplied name is not an argument.
    """
    device = console_device(items, token)
    if not device:
        return "unknown device", 400
    label = device_console_label(device)
    code, _out, err = launch(["/usr/bin/open", "-b", "com.apple.dt.Devices"])
    if code != 0:
        if isinstance(err, bytes):
            detail = err.decode("utf-8", "replace").strip()
        else:
            detail = str(err or "").strip()
        detail = " ".join(detail.split())[:160] or "open failed"
        return "Could not open Device Hub on the Mini (%s)." % detail, 200
    return "Opens on the Mini; Device Hub can't preselect %s, so pick it in the sidebar." % label, 200




















def client_allowed(ip: str | None) -> bool:
    raw = (ip or "").split("%", 1)[0].strip()
    if raw.startswith("::ffff:"):
        raw = raw[7:]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    return any(addr in network for network in _LAN)


def same_origin(request_host: str | None, origin: str | None, referer: str | None) -> bool:
    host = (request_host or "").strip().lower()
    if not host:
        return False
    for value in (origin, referer):
        text = (value or "").strip()
        if not text or text.lower() == "null":
            continue
        netloc = (urlparse(text).netloc or "").lower()
        if netloc and netloc == host:
            return True
    return False


def backoff_seconds(fails: int) -> int:
    if fails <= 0:
        return 10
    return min(120, 10 * (2 ** (fails - 1)))


def recovering_text(active: bool, next_try: float | None, now: float) -> str:
    if not active or not next_try:
        return ""
    remaining = max(0, int(round(next_try - now)))
    return "recovering, next try in %ds" % remaining


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def settings_page(cfg: dict, token: str, notice: str, resolved_ip: str, worker_note: str, devices_note: str) -> bytes:
    worker = cfg["worker"]
    mini = cfg["mini"]
    devices = cfg["devices"]
    checked = " checked" if worker.get("resolve_mdns") else ""
    notice_html = "<p>%s</p>" % _esc(notice) if notice else ""
    resolved = resolved_ip or worker.get("ip") or ""
    body = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>Zai-DevDashd – Developer Dashboard</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<style>
  :root {
    color-scheme: light dark;
    --text: #102033;
    --muted: #3d4f63;
    --bg: #f4f7f5;
    --card: #ffffff;
    --line: #d5e0dc;
    --link: #0b3f86;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --text: #f4f7f2;
      --muted: #d5ddd6;
      --bg: #071422;
      --card: #102033;
      --line: #2c4660;
      --link: #8ec1ff;
    }
  }
  body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; }
  main { max-width: 40rem; margin: 0 auto; padding: 16px; }
  h1 { font-size: clamp(1.25rem, 1rem + 1.2vw, 1.7rem); margin: 0 0 4px; }
  .mast { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 12px; }
  .mast img { width: 48px; height: 48px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px; margin: 0 0 16px; }
  p { margin: 0 0 8px; }
  a { color: var(--link); }
  .muted { color: var(--muted); }
  form { margin: 0 0 16px; }
  label { display: block; font-weight: 700; margin-top: 10px; }
  input[type="text"], input[type="number"] { display: block; width: 100%%; max-width: 28rem; margin-top: 4px; font: inherit; padding: 6px 8px; }
  button { font: inherit; font-weight: 700; margin-top: 12px; padding: 6px 12px; }
  .check { font-weight: 400; }
  fieldset { border: 1px solid var(--line); border-radius: 8px; margin: 12px 0; }
</style>
</head>
<body>
<main>
  <header class="mast">
    <img src="/static/logo.svg" alt="Zai-DevDashd" width="48" height="48">
    <div>
      <h1>Zai-DevDashd – Developer Dashboard</h1>
      <p class="muted">Settings</p>
    </div>
  </header>
  <nav><a href="/">Back to the board</a></nav>
  %(notice)s
  <p>Optional collectors stay disabled until configured. These actions apply to this dashboard service.</p>
  <p>Configured worker: %(user)s@%(resolved)s (%(host)s). %(worker_note)s</p>
  <p>Devices: %(devices_note)s</p>
  <form method="POST" action="/action/settings">
    <input type="hidden" name="csrf_token" value="%(token)s">
    <fieldset>
      <legend>%(worker_name)s</legend>
      <label for="worker-host">Host</label>
      <input id="worker-host" name="worker_host" type="text" value="%(host)s" autocomplete="off">
      <label for="worker-ip">Fallback IP</label>
      <input id="worker-ip" name="worker_ip" type="text" value="%(ip)s" autocomplete="off">
      <label for="worker-user">SSH user</label>
      <input id="worker-user" name="worker_user" type="text" value="%(user)s" autocomplete="off">
      <label for="worker-interval">Probe interval (seconds)</label>
      <input id="worker-interval" name="worker_interval" type="number" min="5" max="600" inputmode="numeric" value="%(worker_interval)s" required>
      <label class="check" for="resolve-mdns"><input id="resolve-mdns" name="resolve_mdns" type="checkbox" value="1"%(checked)s> Auto-resolve .local via mDNS</label>
    </fieldset>
    <fieldset>
      <legend>%(mini_name)s</legend>
      <label for="mini-host">Host</label>
      <input id="mini-host" name="mini_host" type="text" value="%(mini_host)s" autocomplete="off">
      <label for="mini-ip">IP</label>
      <input id="mini-ip" name="mini_ip" type="text" value="%(mini_ip)s" autocomplete="off">
    </fieldset>
    <fieldset>
      <legend>Devices</legend>
      <label for="devices-interval">devicectl interval (seconds)</label>
      <input id="devices-interval" name="devices_interval" type="number" min="5" max="600" inputmode="numeric" value="%(devices_interval)s" required>
    </fieldset>
    <button type="submit">Save settings</button>
  </form>
  <h2>Actions</h2>
  <form method="POST" action="/action/reconnect-worker">
    <input type="hidden" name="csrf_token" value="%(token)s">
    <button type="submit">Reconnect Worker</button>
  </form>
  <form method="POST" action="/action/refresh-devices">
    <input type="hidden" name="csrf_token" value="%(token)s">
    <button type="submit">Refresh devices</button>
  </form>
  <form method="POST" action="/action/restart">
    <input type="hidden" name="csrf_token" value="%(token)s">
    <button type="submit">Restart monitor</button>
  </form>
  <p>Restart is available only for the explicitly installed Zai-DevDashd service. A standalone development process is not restarted.</p>
</main>
<script>
(function () {
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("button[type=submit]");
      if (button) button.disabled = true;
    });
  });
})();
</script>
</body>
</html>
""" % {
        "notice": notice_html,
        "token": _esc(token),
        "host": _esc(worker.get("host")),
        "ip": _esc(worker.get("ip")),
        "user": _esc(worker.get("user")),
        "resolved": _esc(resolved),
        "checked": checked,
        "worker_interval": _esc(worker.get("probe_interval_s")),
        "devices_interval": _esc(devices.get("probe_interval_s")),
        "mini_host": _esc(mini.get("host")),
        "mini_ip": _esc(mini.get("ip")),
        "mini_name": _esc(mini.get("name") or "Local host"),
        "worker_name": _esc(worker.get("name") or "Worker"),
        "worker_note": _esc(worker_note or ""),
        "devices_note": _esc(devices_note or ""),
    }
    return body.encode("utf-8")
