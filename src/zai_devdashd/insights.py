"""Local telemetry and deterministic findings for the engineering monitor.

The monitor process owns one aggregator thread. This module uses the
standard-library sqlite3 only. It does not spawn probes, SSH, or gh.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SAMPLE_KEEP = 7 * 24 * 3600
ROW_KEEP = 60 * 24 * 3600

_BUCKETS = (
    ("pushing", re.compile(r"\b(git\s+push|\bpush\b|git\s+commit|\bcommit\b)\b", re.I)),
    ("testing", re.compile(r"\b(xcodebuild\s+test|unit tests?|pytest|xcresult|\btest\b)\b", re.I)),
    ("building", re.compile(r"\b(xcodebuild|swift\s+build|\bcompile\b|\bbuild\b)\b", re.I)),
    ("editing", re.compile(r"\b(search_replace|strreplace|apply_patch|\bapply\b|\bedit\b|\bwrite\b)\b", re.I)),
    ("waiting", re.compile(r"\b(wait|sleep|poll)\b", re.I)),
)
_UNIT_PREFIX = re.compile(r"^(.*?)(\d+)$")
_SCORE_RE = re.compile(r"scored\s+(\d+)\s*/\s*(\d+)", re.I)
_NET_RE = re.compile(r"\b(network|dns|timed out|timeout)\b", re.I)
_PR_RE = re.compile(r"#(\d{3,5})\b")
_TEST_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Tests|Test)\.[A-Za-z0-9_]+)\b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    machine TEXT NOT NULL,
    cpu REAL,
    gpu REAL,
    mem REAL,
    swap TEXT
);
CREATE INDEX IF NOT EXISTS samples_machine_ts ON samples(machine, ts);
CREATE TABLE IF NOT EXISTS pipeline_steps (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    run_name TEXT,
    pr INTEGER,
    head TEXT,
    step TEXT,
    state TEXT,
    exit_code INTEGER,
    duration_s REAL,
    run_elapsed REAL,
    note TEXT,
    row_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS lane_activity (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    lane TEXT,
    actions_per_min INTEGER,
    cost REAL,
    last_commit REAL,
    bucket TEXT,
    branch TEXT,
    pr INTEGER
);
CREATE INDEX IF NOT EXISTS lane_activity_lane_ts ON lane_activity(lane, ts);
CREATE TABLE IF NOT EXISTS coderabbit (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    pr INTEGER,
    post_ts REAL,
    verdict_ts REAL,
    turnaround_s REAL,
    sha TEXT,
    row_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS prs (
    number INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    title TEXT,
    head TEXT,
    state TEXT,
    updated_at TEXT,
    pending INTEGER,
    fail INTEGER
);
CREATE TABLE IF NOT EXISTS zai_runs (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    run_id TEXT,
    kind TEXT,
    score INTEGER,
    total INTEGER,
    line TEXT,
    row_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS insights (
    job_key TEXT PRIMARY KEY,
    input_hash TEXT,
    updated REAL,
    payload TEXT
);
"""


def action_bucket(titles: list) -> str:
    """Dominant category over the newest-first titles. Ties take the newest."""
    scores = {"pushing": 0, "testing": 0, "building": 0, "editing": 0, "waiting": 0}
    order = []
    for title in titles or []:
        text = str(title or "")
        found = "waiting"
        for name, pattern in _BUCKETS:
            if pattern.search(text):
                found = name
                break
        scores[found] = scores.get(found, 0) + 1
        order.append(found)
    if not order:
        return "waiting"
    best = max(scores.values())
    for name in order:
        if scores.get(name) == best:
            return name
    return "waiting"


def collapse_units(spec: str) -> str:
    items = [part.strip() for part in (spec or "").split(",") if part.strip()]
    parsed = []
    for item in items:
        match = _UNIT_PREFIX.match(item)
        if match:
            parsed.append((match.group(1), int(match.group(2)), item))
        else:
            parsed.append((None, None, item))
    groups: list[list] = []
    current: list = []

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for entry in parsed:
        if entry[0] is None:
            flush()
            groups.append([entry])
            continue
        if current and entry[0] == current[-1][0] and entry[1] == current[-1][1] + 1:
            current.append(entry)
            continue
        flush()
        current = [entry]
    flush()
    parts = []
    for group in groups:
        if len(group) >= 2 and group[0][0] is not None:
            parts.append("%s%d–%s%d" % (group[0][0], group[0][1], group[-1][0], group[-1][1]))
        else:
            parts.extend(item[2] for item in group)
    return ", ".join(parts)


def zai_title(command: str) -> dict:
    """Deterministic Worker title from a process command. No model name unless present."""
    text = command or ""
    kind_match = re.search(r"zai-coder\s+([A-Za-z0-9_-]+)", text, re.I)
    if kind_match:
        kind = kind_match.group(1)
    elif re.search(r"\breview\b", text, re.I):
        kind = "review"
    else:
        kind = "run"
    units_match = re.search(r"--units\s+(\S+)", text)
    units = collapse_units(units_match.group(1).strip(",")) if units_match else ""
    run_match = re.search(r"/dd-([A-Za-z0-9._-]+)/", text) or re.search(
        r"run-ds-([A-Za-z0-9._-]+)", text
    )
    run_id = run_match.group(1) if run_match else ""
    if run_id.endswith("-arev.sh"):
        run_id = run_id[: -len("-arev.sh")]
    if run_id.endswith("-arev"):
        run_id = run_id[: -len("-arev")]
    model = ""
    model_match = re.search(r"--model\s+(\S+)", text)
    if model_match:
        model = model_match.group(1)
    elif re.search(r"ds-r1", text, re.I):
        model = "DS-R1"
    bits = ["ZAI"]
    if model:
        bits.append(model)
    bits.append(kind)
    title = " ".join(bits)
    if units:
        title += " · units " + units
    if run_id:
        title += " · run " + run_id
    # A path such as .../zai/dd-... is the checkout, not the zai-coder tool.
    first = text.split()[:1]
    base = first[0].rsplit("/", 1)[-1] if first else ""
    is_tool = base.lower().startswith("zai") or "zai-coder" in text.lower()
    return {
        "title": title if is_tool else "",
        "run_id": run_id or "live",
        "units": units,
        "kind": kind,
        "model": model,
    }


def goal_line(text: str) -> str:
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() in ("## goal", "goal"):
            for nxt in lines[index + 1 : index + 6]:
                stripped = nxt.strip()
                if stripped and not stripped.startswith("#"):
                    return stripped[:180]
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()[:180]
    flat = " ".join(part.strip() for part in lines if part.strip())
    return flat[:180]


def pick_brief_file(files: list, lane: str, history: str, pr: int | None):
    """Choose a brief path. History tokens win, then a PR-numbered file."""
    usable = [path for path in files if path is not None]
    if not usable:
        return None
    blob = history or ""
    lowered = blob.lower()
    for path in usable:
        name = getattr(path, "name", str(path))
        stem = name[:-3] if name.endswith(".md") else name
        if name in blob or stem in blob:
            return path
    best = None
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}", blob)
    lane_lower = (lane or "").lower()
    for path in usable:
        name = getattr(path, "name", str(path))
        stem = (name[:-3] if name.endswith(".md") else name).lower()
        for token in tokens:
            token_lower = token.lower()
            if token_lower == lane_lower or token_lower in ("resume", "start", "log"):
                continue
            if token_lower in stem or token_lower in lowered and token_lower in stem:
                if best is None or len(token) > best[0]:
                    best = (len(token), path)
    if best:
        return best[1]
    if pr:
        numbered = []
        for path in usable:
            name = getattr(path, "name", str(path))
            if str(pr) in name and "resume" not in name.lower():
                numbered.append(path)
        if numbered:
            return sorted(numbered, key=lambda path: getattr(path, "name", str(path)))[0]
    ordered = sorted(
        usable,
        key=lambda path: path.stat().st_mtime if hasattr(path, "stat") else 0,
        reverse=True,
    )
    for path in ordered:
        name = getattr(path, "name", str(path))
        if "resume" not in name.lower():
            return path
    return ordered[0]


def lane_title(lane: str, pr: int | None, goal: str) -> str:
    parts = [lane or "lane"]
    if pr:
        parts.append("#%d" % pr)
    words = (goal or "").split()
    if words:
        parts.append(" ".join(words[:16]))
    return " · ".join(parts)


def _clock(epoch: float | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, ET).strftime("%H:%M")


def commit_summary(bucket: str, epoch: float | None) -> str:
    verb = {
        "testing": "Re-running tests",
        "building": "Building",
        "editing": "Editing",
        "waiting": "Waiting",
        "pushing": "Pushing",
    }.get(bucket or "", "Working")
    clock = _clock(epoch)
    if clock:
        return "%s; last commit %s ET" % (verb, clock)
    return verb


def apply_commit_clock(summary: str, epoch: float | None) -> str:
    """Keep the displayed clock equal to git log. Call it a commit."""
    text = " ".join((summary or "").split())
    clock = _clock(epoch)
    if not clock:
        return text
    if clock in text:
        text = re.sub(r"last push\s+" + re.escape(clock), "last commit " + clock, text, flags=re.I)
        return text
    replaced = re.sub(r"\b\d{1,2}:\d{2}\b", clock, text, count=1)
    if clock in replaced:
        replaced = re.sub(r"last push", "last commit", replaced, count=1, flags=re.I)
        return replaced
    extra = "last commit %s ET" % clock
    if not text:
        return extra[0].upper() + extra[1:]
    return text.rstrip(".") + "; " + extra


def coderabbit_pairs(lines: list) -> list:
    """Pair a ledger 'requested' row with the next findings row for that PR."""
    pending = {}
    found = []
    for line in lines or []:
        parts = str(line).split("\t")
        if len(parts) < 5:
            continue
        try:
            parsed = datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            continue
        epoch = parsed.timestamp()
        note = parts[4]
        blob = parts[2] + " " + note
        match = _PR_RE.search(blob)
        if not match:
            continue
        number = int(match.group(1))
        lowered = note.lower()
        if "requested" in lowered:
            pending[number] = (epoch, parts[3])
        elif "findings" in lowered and number in pending and epoch >= pending[number][0]:
            post, sha = pending.pop(number)
            found.append({
                "pr": number,
                "post_ts": post,
                "verdict_ts": epoch,
                "turnaround_s": round(epoch - post, 1),
                "sha": sha,
            })
    return found


def _pr_from_bits(*parts: str) -> int | None:
    for part in parts:
        if not part:
            continue
        match = re.search(r"eng-(\d{3,5})-", part)
        if match:
            return int(match.group(1))
        match = _PR_RE.search(part)
        if match:
            return int(match.group(1))
    return None


def _test_name(note: str, step: str) -> str:
    match = _TEST_RE.search(note or "")
    if match:
        return match.group(1)
    return step or "step"


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def ingest(self, packet: dict) -> None:
        now = float(packet.get("now") or time.time())
        with self.lock:
            self._sample(now, "mini", packet.get("mini_load") or {})
            self._sample(now, "worker", packet.get("worker_load") or {})
            for run in packet.get("runs") or []:
                self._steps(now, run)
            for lane in packet.get("lanes") or []:
                if not lane.get("busy"):
                    continue
                self.db.execute(
                    "INSERT INTO lane_activity (ts, lane, actions_per_min, cost, last_commit, bucket, branch, pr) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        lane.get("lane") or "",
                        int(lane.get("actions_per_min") or 0),
                        lane.get("cost"),
                        lane.get("commit_epoch"),
                        lane.get("bucket") or "",
                        lane.get("branch") or "",
                        lane.get("pr"),
                    ),
                )
            for pair in coderabbit_pairs(packet.get("ledger_lines") or []):
                key = "cr:%s:%s:%s" % (pair["pr"], int(pair["post_ts"]), int(pair["verdict_ts"]))
                self.db.execute(
                    "INSERT OR IGNORE INTO coderabbit (ts, pr, post_ts, verdict_ts, turnaround_s, sha, row_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (now, pair["pr"], pair["post_ts"], pair["verdict_ts"], pair["turnaround_s"], pair["sha"], key),
                )
            for pr in packet.get("prs") or []:
                number = pr.get("number")
                if not isinstance(number, int):
                    continue
                self.db.execute(
                    "INSERT INTO prs (number, ts, title, head, state, updated_at, pending, fail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(number) DO UPDATE SET ts=excluded.ts, title=excluded.title, head=excluded.head, "
                    "state=excluded.state, updated_at=excluded.updated_at, pending=excluded.pending, fail=excluded.fail",
                    (
                        number,
                        now,
                        pr.get("title") or "",
                        pr.get("head") or "",
                        pr.get("state") or "",
                        pr.get("updated_at") or "",
                        int(pr.get("pending") or 0),
                        int(pr.get("fail") or 0),
                    ),
                )
            zai = packet.get("zai") or {}
            run_id = zai.get("run_id") or ""
            for line in zai.get("tails") or []:
                self._zai_line(now, run_id, str(line))
            self.db.commit()

    def _sample(self, now: float, machine: str, load: dict) -> None:
        if not isinstance(load, dict):
            return
        cpu = load.get("cpu")
        gpu = load.get("gpu")
        mem = load.get("mem")
        swap = load.get("swap")
        if cpu is None and gpu is None and mem is None and not swap:
            return
        self.db.execute(
            "INSERT INTO samples (ts, machine, cpu, gpu, mem, swap) VALUES (?, ?, ?, ?, ?, ?)",
            (now, machine, cpu, gpu, mem, None if swap is None else str(swap)),
        )

    def _steps(self, now: float, run: dict) -> None:
        name = run.get("name") or ""
        pr = run.get("pr")
        head = run.get("head") or ""
        note = " ".join(run.get("extras") or [])[:240]
        elapsed = run.get("elapsed_s") if run.get("state") in ("passed", "failed", "stopped") else None
        for step in run.get("steps") or []:
            state = step.get("state") or ""
            if state == "pending":
                continue
            exit_code = step.get("exit")
            duration = step.get("duration_s")
            row_key = "|".join([
                name,
                step.get("name") or "",
                state,
                "" if exit_code is None else str(exit_code),
                "" if duration is None else str(duration),
                head[:12],
            ])
            self.db.execute(
                "INSERT OR IGNORE INTO pipeline_steps "
                "(ts, run_name, pr, head, step, state, exit_code, duration_s, run_elapsed, note, row_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, name, pr, head, step.get("name") or "", state, exit_code, duration, elapsed, note, row_key),
            )

    def _zai_line(self, now: float, run_id: str, line: str) -> None:
        score_match = _SCORE_RE.search(line)
        kind = ""
        score = None
        total = None
        if score_match:
            kind = "score"
            score = int(score_match.group(1))
            total = int(score_match.group(2))
        elif re.search(r"\bFAILED\b", line):
            kind = "failed"
        elif re.search(r"timeout", line, re.I):
            kind = "timeout"
        else:
            return
        digest = hashlib.sha1((run_id + "|" + kind + "|" + line).encode("utf-8", "replace")).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO zai_runs (ts, run_id, kind, score, total, line, row_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, run_id, kind, score, total, line[:180], digest),
        )

    def prune(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        with self.lock:
            self.db.execute("DELETE FROM samples WHERE ts < ?", (current - SAMPLE_KEEP,))
            for table in ("pipeline_steps", "lane_activity", "coderabbit", "zai_runs"):
                self.db.execute("DELETE FROM %s WHERE ts < ?" % table, (current - ROW_KEEP,))
            self.db.execute("DELETE FROM prs WHERE ts < ?", (current - ROW_KEEP,))
            self.db.execute("DELETE FROM insights WHERE updated < ?", (current - ROW_KEEP,))
            self.db.commit()

    def save_insight(self, job_key: str, input_hash: str, payload: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO insights (job_key, input_hash, updated, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(job_key) DO UPDATE SET input_hash=excluded.input_hash, "
                "updated=excluded.updated, payload=excluded.payload",
                (job_key, input_hash, time.time(), json.dumps(payload, separators=(",", ":"), default=str)),
            )
            self.db.commit()

    def insight_row(self, job_key: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT input_hash, updated, payload FROM insights WHERE job_key = ?",
                (job_key,),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        return {"input_hash": row["input_hash"], "updated": row["updated"], "payload": payload}

    def public_block(self, keys: list) -> dict:
        wanted = [key for key in keys or [] if key]
        with self.lock:
            if wanted:
                marks = ",".join("?" for _ in wanted)
                rows = self.db.execute(
                    "SELECT job_key, payload FROM insights WHERE job_key IN (%s) ORDER BY job_key" % marks,
                    wanted,
                ).fetchall()
            else:
                rows = []
        jobs = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                jobs.append(payload)
        return {"jobs": jobs}

    def repeated_failures(self, pr: int | None) -> list:
        if not pr:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT run_name, step, exit_code, note FROM pipeline_steps "
                "WHERE pr = ? AND state = 'fail' ORDER BY ts",
                (pr,),
            ).fetchall()
        groups: dict[str, list] = {}
        for row in rows:
            name = _test_name(row["note"] or "", row["step"] or "")
            groups.setdefault(name, [])
            run_name = row["run_name"] or ""
            if not any(item["run"] == run_name for item in groups[name]):
                groups[name].append({
                    "run": run_name,
                    "exit": row["exit_code"],
                    "step": row["step"],
                })
        found = []
        for name, runs in groups.items():
            if len(runs) < 2:
                continue
            exits = [item["exit"] for item in runs]
            found.append({
                "kind": "repeated_failure",
                "detail": "%s failed in %d runs of #%s (exits %s)." % (
                    name, len(runs), pr, ", ".join(str(item) for item in exits),
                ),
                "numbers": {"pr": pr, "runs": len(runs), "name": name, "exits": exits},
                "source": runs[:4],
            })
        return found

    def flaky(self, pr: int | None) -> list:
        if not pr:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT run_name, head, step, state, note FROM pipeline_steps "
                "WHERE pr = ? AND state IN ('pass', 'fail') ORDER BY ts",
                (pr,),
            ).fetchall()
        by_head: dict[tuple, set] = {}
        sources: dict[tuple, list] = {}
        network = []
        for row in rows:
            head = (row["head"] or "")[:12]
            step = row["step"] or ""
            key = (head, step)
            by_head.setdefault(key, set()).add(row["state"])
            bucket = sources.setdefault(key, [])
            if len(bucket) < 4:
                bucket.append({"run": row["run_name"], "state": row["state"], "head": head})
            note = row["note"] or ""
            match = _NET_RE.search(note)
            if match and row["state"] == "fail":
                network.append({
                    "kind": "flaky",
                    "detail": "%s on #%s contains %r in %s." % (step, pr, match.group(1), row["run_name"]),
                    "numbers": {"pr": pr, "word": match.group(1), "run": row["run_name"], "step": step},
                    "source": [{"run": row["run_name"], "note": note[:160]}],
                })
        found = []
        for key, states in by_head.items():
            if states >= {"pass", "fail"} and key[0]:
                found.append({
                    "kind": "flaky",
                    "detail": "%s passed and failed on #%s at the same head %s." % (key[1], pr, key[0]),
                    "numbers": {"pr": pr, "step": key[1], "head": key[0], "states": sorted(states)},
                    "source": sources.get(key) or [],
                })
        found.extend(network[:2])
        return found

    def rising_durations(self, pr: int | None) -> list:
        if not pr:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT run_name, MIN(ts) AS ts, MAX(run_elapsed) AS duration_s FROM pipeline_steps "
                "WHERE pr = ? AND run_elapsed IS NOT NULL AND state IN ('pass', 'fail') "
                "GROUP BY run_name ORDER BY ts DESC LIMIT 3",
                (pr,),
            ).fetchall()
        ordered = list(reversed(rows))
        if len(ordered) < 3:
            return []
        durations = [float(row["duration_s"]) for row in ordered]
        if not (durations[0] < durations[1] < durations[2]):
            return []
        return [{
            "kind": "rising_duration",
            "detail": "Last 3 #%s run durations rose %s, %s, %s seconds." % (
                pr, durations[0], durations[1], durations[2],
            ),
            "numbers": {"pr": pr, "durations_s": durations},
            "source": [
                {"run": row["run_name"], "duration_s": float(row["duration_s"])}
                for row in ordered
            ],
        }]

    def lane_test_loop(self, lane: str, now: float) -> list:
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, bucket, last_commit FROM lane_activity WHERE lane = ? AND ts >= ? ORDER BY ts",
                (lane, now - 50 * 60),
            ).fetchall()
        if len(rows) < 2:
            return []
        span = rows[-1]["ts"] - rows[0]["ts"]
        if span < 45 * 60:
            return []
        if any((row["bucket"] or "") != "testing" for row in rows):
            return []
        commits = {row["last_commit"] for row in rows}
        if len(commits) != 1:
            return []
        commit = next(iter(commits))
        if commit and now - float(commit) <= 45 * 60:
            return []
        minutes = round(span / 60.0, 1)
        return [{
            "kind": "test_loop",
            "detail": "%s stayed on tests for %s minutes with no newer commit." % (lane, minutes),
            "numbers": {
                "lane": lane,
                "minutes": minutes,
                "samples": len(rows),
                "last_commit": commit,
            },
            "source": [
                {"ts": rows[0]["ts"], "bucket": rows[0]["bucket"]},
                {"ts": rows[-1]["ts"], "bucket": rows[-1]["bucket"]},
            ],
        }]

    def cpu_idle_while_waiting(self, now: float, queue_waiting: bool, wait_source: list) -> list:
        if not queue_waiting:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, cpu FROM samples WHERE machine = 'mini' AND ts >= ? AND cpu IS NOT NULL ORDER BY ts",
                (now - 10 * 60,),
            ).fetchall()
        if len(rows) < 8:
            return []
        span = rows[-1]["ts"] - rows[0]["ts"]
        cpus = [float(row["cpu"]) for row in rows]
        if span < 540 or max(cpus) >= 15:
            return []
        return [{
            "kind": "cpu_idle_queue",
            "detail": "Mini CPU stayed under 15%% for %s minutes (max %.1f%%, %d samples) while work is waiting." % (
                round(span / 60.0, 1), max(cpus), len(rows),
            ),
            "numbers": {
                "samples": len(rows),
                "max_cpu": round(max(cpus), 1),
                "span_s": round(span, 1),
            },
            "source": wait_source[:4] + [
                {"ts": rows[0]["ts"], "cpu": cpus[0]},
                {"ts": rows[-1]["ts"], "cpu": cpus[-1]},
            ],
        }]

    def zai_trend(self, run_id: str) -> list:
        if not run_id:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, score, total, line FROM zai_runs WHERE run_id = ? AND kind = 'score' "
                "ORDER BY ts DESC LIMIT 3",
                (run_id,),
            ).fetchall()
        ordered = list(reversed(rows))
        if len(ordered) < 2:
            return []
        ratios = []
        for row in ordered:
            total = row["total"] or 0
            ratios.append(None if not total else round(float(row["score"]) / float(total), 3))
        direction = "flat"
        if all(value is not None for value in ratios) and len(ratios) >= 2:
            if all(ratios[index] < ratios[index + 1] for index in range(len(ratios) - 1)):
                direction = "rising"
            elif all(ratios[index] > ratios[index + 1] for index in range(len(ratios) - 1)):
                direction = "falling"
        scores = ["%s/%s" % (row["score"], row["total"]) for row in ordered]
        return [{
            "kind": "zai_score_trend",
            "detail": "ZAI %s scores %s (%s)." % (run_id, ", ".join(scores), direction),
            "numbers": {"run_id": run_id, "scores": scores, "direction": direction},
            "source": [{"line": row["line"], "ts": row["ts"]} for row in ordered],
        }]

    def turnaround(self, pr: int | None) -> dict | None:
        if not pr:
            return None
        with self.lock:
            row = self.db.execute(
                "SELECT turnaround_s, post_ts, verdict_ts FROM coderabbit WHERE pr = ? "
                "ORDER BY verdict_ts DESC LIMIT 1",
                (pr,),
            ).fetchone()
        if not row:
            return None
        return {
            "turnaround_min": round(float(row["turnaround_s"]) / 60.0, 1),
            "post_ts": row["post_ts"],
            "verdict_ts": row["verdict_ts"],
        }


def _queue_waiting(packet: dict) -> tuple[bool, list]:
    source = []
    for line in packet.get("ledger_lines") or []:
        parts = str(line).split("\t")
        if len(parts) >= 5 and "requested" in parts[4].lower():
            source.append({"ledger": parts[2][:80], "note": parts[4][:80]})
            break
    for pr in packet.get("prs") or []:
        if int(pr.get("pending") or 0) > 0:
            source.append({"pr": pr.get("number"), "pending_checks": pr.get("pending")})
    return bool(source), source[:4]


def jobs_from_packet(packet: dict, store: Store) -> list:
    """One record per active job. Findings cite stored rows."""
    now = float(packet.get("now") or time.time())
    waiting, wait_source = _queue_waiting(packet)
    idle = store.cpu_idle_while_waiting(now, waiting, wait_source)
    jobs = []
    for lane in packet.get("lanes") or []:
        if not lane.get("busy"):
            continue
        pr = lane.get("pr")
        findings = []
        findings.extend(store.repeated_failures(pr))
        findings.extend(store.flaky(pr))
        findings.extend(store.rising_durations(pr))
        findings.extend(store.lane_test_loop(lane.get("lane") or "", now))
        findings.extend(idle)
        turn = store.turnaround(pr)
        stats = {
            "actions_per_min": lane.get("actions_per_min"),
            "bucket": lane.get("bucket"),
            "cost_usd": lane.get("cost"),
            "last_commit": lane.get("commit_epoch"),
            "branch": lane.get("branch"),
            "pr": pr,
        }
        if turn:
            stats["coderabbit_turnaround_min"] = turn["turnaround_min"]
        jobs.append(_job(
            "lane:" + str(lane.get("lane") or ""),
            lane.get("title") or lane.get("lane") or "lane",
            "lane",
            findings,
            stats,
            lane,
        ))
    for run in packet.get("runs") or []:
        if run.get("state") != "running":
            continue
        pr = run.get("pr")
        pr_title = ""
        for row in packet.get("prs") or []:
            if row.get("number") == pr:
                pr_title = row.get("title") or ""
                break
        findings = []
        findings.extend(store.repeated_failures(pr))
        findings.extend(store.flaky(pr))
        findings.extend(store.rising_durations(pr))
        findings.extend(idle)
        stats = {
            "pr": pr,
            "state": run.get("state"),
            "elapsed_s": run.get("elapsed_s"),
            "head": (run.get("head") or "")[:12],
            "steps": [
                {"name": step.get("name"), "state": step.get("state"), "exit": step.get("exit")}
                for step in (run.get("steps") or [])
            ],
        }
        turn = store.turnaround(pr)
        if turn:
            stats["coderabbit_turnaround_min"] = turn["turnaround_min"]
        jobs.append(_job(
            "pipeline:" + str(run.get("name") or ""),
            run.get("label") or run.get("name") or "pipeline",
            "pipeline",
            findings,
            stats,
            {"pr": pr, "branch": "", "brief_goal": "", "needs": "", "pr_title": pr_title},
        ))
    zai = packet.get("zai") or {}
    if zai.get("active") and zai.get("title"):
        findings = store.zai_trend(zai.get("run_id") or "")
        findings.extend(idle)
        jobs.append(_job(
            "zai:" + str(zai.get("run_id") or "live"),
            zai.get("title") or "ZAI",
            "zai",
            findings,
            {
                "run_id": zai.get("run_id"),
                "units": zai.get("units"),
                "kind": zai.get("kind"),
                "model": zai.get("model") or "",
            },
            {"pr": None, "branch": "", "brief_goal": "", "needs": "", "pr_title": zai.get("title") or ""},
        ))
    return jobs


def _job(key: str, title: str, kind: str, findings: list, stats: dict, lane: dict) -> dict:
    context = {
        "job": key,
        "kind": kind,
        "pr": lane.get("pr"),
        "branch": lane.get("branch") or "",
        "goal_text": (lane.get("brief_goal") or "")[:800],
        "brief_excerpt": (lane.get("brief_excerpt") or "")[:900],
        "needs_excerpt": (lane.get("needs") or "")[:700],
        "pr_title": lane.get("pr_title") or "",
        "pr_body": lane.get("pr_body") or "",
        "stats": stats,
        "findings": findings[:8],
    }
    raw = json.dumps(context, separators=(",", ":"), default=str)
    if len(raw) > 8000:
        context["brief_excerpt"] = (context.get("brief_excerpt") or "")[:300]
        context["needs_excerpt"] = (context.get("needs_excerpt") or "")[:200]
        raw = json.dumps(context, separators=(",", ":"), default=str)[:8000]
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()
    return {
        "job_key": key,
        "title": title,
        "kind": kind,
        "input_hash": digest,
        "context": raw,
        "findings": findings[:8],
        "stats": stats,
        "brief_goal": lane.get("brief_goal") or "",
    }
