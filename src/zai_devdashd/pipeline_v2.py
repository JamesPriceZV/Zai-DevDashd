"""v2 phase 2: pipeline board, lane board, review ladder. Existing local sources only, no new credentials.

Sources: `gh pr list` / `gh api .../reviews` (read-only), ~/DevDashInputs/mission/status.md (free text,
quoted as-is with its timestamp, never interpreted beyond simple patterns), ~/DevDashInputs/headless/state
lane files, and `os.statvfs` for BuildScratch. Anything not found stays "not reported".
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from zai_devdashd import config as site
STATUS_MD = site.optional_path("status")
LANE_STATE = site.optional_path("headless_state")
SCRATCH = site.optional_path("scratch")
SCRATCH_FLOOR_GB = 0
DEVICES = []
XCODE_CLOUD = "needs App Store Connect API key"
NR = "not reported"
PR_LIMIT = 40
BOTS = {
    "Codex": ("chatgpt-codex-connector", "chatgpt-codex-connector[bot]"),
    "CodeRabbit": ("coderabbitai", "coderabbitai[bot]"),
    "Bugbot": ("cursor", "cursor[bot]"),
    "DeepSource": ("deepsource-io", "deepsource-io[bot]"),
}  # GH Copilot is ignored by policy and not shown
FINISH_RE = re.compile(r"^(?P<ts>\S+ \S+ ET) (?P<lane>L-[\w-]+) FINISH #(?P<pr>\d+) (?P<sha>[0-9a-f]{7,40}) bft=(?P<bft>\S+) unit=(?P<unit>\S+)")
TS_RE = re.compile(r"^-?\s*(\d{4}-\d\d-\d\d \d\d:\d\d (?:ET|EDT|EST))")


def run_gh(args: list, timeout: int = 25):
    if not site.adapter_enabled("github"):
        return None
    try:
        p = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def status_lines(text: str) -> list:
    """[(timestamp or None, line)] keeping the last dated timestamp for undated continuation lines."""
    out, last = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = TS_RE.match(line)
        if m:
            last = m.group(1)
        out.append((last, line))
    return out


def clip(s: str, n: int = 220) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def mini_status(lines: list, pr: int, head: str) -> dict:
    """Latest structured FINISH for this PR at this head; else latest note mentioning #pr and the head prefix."""
    fin = None
    for _, line in lines:
        m = FINISH_RE.match(line)
        if m and int(m.group("pr")) == pr and head.startswith(m.group("sha")[:7]):
            fin = m
    if fin:
        return {"state": "reported", "build": "BFT %s" % fin.group("bft"), "unit": fin.group("unit"),
                "at": fin.group("ts"), "lane": fin.group("lane"), "source": "status.md FINISH line"}
    note = latest_note(lines, [r"#%d\b" % pr, re.escape(head[:8])])
    if note:
        return {"state": "note", "note": note["text"], "at": note["at"], "source": "status.md (quoted)"}
    return {"state": NR, "source": "no status.md line for this head"}


def latest_note(lines: list, patterns: list) -> dict | None:
    rx = [re.compile(p, re.I) for p in patterns]
    hit = None
    for ts, line in lines:
        if all(r.search(line) for r in rx):
            hit = {"at": ts, "text": clip(line)}
    return hit


def diesel_status(lines: list, pr: int, head: str) -> dict:
    n = latest_note(lines, [r"#%d\b" % pr, re.escape(head[:8]), r"(?:integration tests|diesel)"])
    if n:
        return {"state": "note", "note": n["text"], "at": n["at"], "source": "status.md (quoted)"}
    return {"state": NR, "source": "no Integration tests line for this head in status.md"}


def review_state(reviews: list, comments: list, head: str) -> dict:
    out = {}
    for name, logins in BOTS.items():
        rs = [r for r in reviews if ((r.get("author") or {}).get("login") or (r.get("user") or {}).get("login")) in logins]
        cs = [c for c in comments if ((c.get("author") or {}).get("login") or (c.get("user") or {}).get("login")) in logins]
        heads = []
        for r in rs:
            oid = (r.get("commit") or {}).get("oid") or r.get("commit_id")
            if oid and oid not in heads:
                heads.append(oid)
        on_head = [r for r in rs if ((r.get("commit") or {}).get("oid") or r.get("commit_id")) == head]
        last = on_head[-1] if on_head else None
        e = {"reviews": len(rs), "comments": len(cs), "unique_heads": len(heads),
             "on_head": (last.get("state") or "").upper() if last else None}
        if not rs and not cs:
            e["summary"] = "none yet"
        elif last:
            e["summary"] = "%s on head" % e["on_head"]
        else:
            e["summary"] = "older heads only" if rs else "comment only"
        out[name] = e
    out["CodeRabbit"]["summary"] += " · %d unique head%s reviewed" % (out["CodeRabbit"]["unique_heads"], "" if out["CodeRabbit"]["unique_heads"] == 1 else "s")
    return out


def scratch_free(path: Path = SCRATCH, floor_gb: int = SCRATCH_FLOOR_GB) -> dict:
    try:
        st = os.statvfs(path)
    except OSError:
        return {"state": NR, "floor_gb": floor_gb}
    free = st.f_bavail * st.f_frsize / 1e9
    total = st.f_blocks * st.f_frsize / 1e9
    return {"state": "ok" if free >= floor_gb else "below floor", "free_gb": round(free, 1), "total_gb": round(total, 1),
            "floor_gb": floor_gb, "headroom_gb": round(free - floor_gb, 1), "source": "statvfs %s" % path}


def lanes(state_dir: Path = LANE_STATE, now: float | None = None) -> list:
    now = now or time.time()
    out = []
    if not state_dir.is_dir():
        return out
    for b in sorted(state_dir.glob("*.branch")):
        name = b.stem
        try:
            branch = b.read_text(errors="replace").strip().splitlines()[0][:120]
        except (OSError, IndexError):
            branch = NR
        hist = state_dir / (name + ".history")
        try:
            last = max(p.stat().st_mtime for p in [b, hist] if p.exists())
        except ValueError:
            last = None
        paused = (state_dir / (name + ".PAUSED")).exists()
        out.append({"lane": name, "branch": branch, "paused": paused, "last_activity": last,
                    "idle_h": round((now - last) / 3600, 1) if last else None,
                    "state": "paused" if paused else ("recent activity" if last and now - last < 3 * 3600 else "idle")})
    out.sort(key=lambda r: -(r["last_activity"] or 0))
    return out


def devices(lines: list, names=None) -> list:
    out = []
    configured = [value.get("label") or key for key, value in (site.CURRENT.get("devices") or {}).items() if isinstance(value, dict)]
    for d in (names if names is not None else configured):
        n = latest_note(lines, [r"\b%s\b" % d])
        out.append({"device": d, "at": n["at"] if n else None, "note": n["text"] if n else NR,
                    "source": "latest status.md mention (quoted)" if n else "not in status.md"})
    return out


class Board:
    def __init__(self, every_s: int = 120):
        self.every_s = every_s
        self.at = 0.0  # last attempt, used only for retry cadence
        self.gh_at = 0.0  # last successfully observed GitHub snapshot
        self.error = None
        self.prs: list | None = None
        self.reviews: dict = {}
        self._t = None
        self._refreshing = False
        self._lock = threading.Lock()

    def _refresh(self, now: float):
        cfg = site.CURRENT.get("github") or {}
        repository, base = cfg.get("repo", ""), cfg.get("integration_branch", "")
        if not site.adapter_enabled("github") or not repository or not base:
            with self._lock:
                self.at, self.error, self._refreshing = now, "GitHub adapter is not configured", False
            return
        try:
            prs = run_gh(["pr", "list", "-R", repository, "--base", base, "--state", "open", "--limit", str(PR_LIMIT),
                          "--json", "number,title,headRefOid,headRefName,isDraft,reviews,url"])
        except Exception:
            prs = None
        valid = isinstance(prs, list) and all(
            isinstance(item, dict) and type(item.get("number")) is int
            and isinstance(item.get("headRefOid", ""), str) for item in (prs or []))
        with self._lock:
            self.at = now
            if valid:
                self.prs = prs
                self.gh_at = now
                self.error = None
            else:
                self.error = "GitHub PR list lookup unavailable"
            self._refreshing = False

    def collect(self, now: float | None = None, status_text: str | None = None, prs: list | None = None) -> dict:
        now = time.time() if now is None else now
        if prs is None and not site.adapter_enabled("github"):
            gh_at, attempted, error, refreshing = None, None, "GitHub adapter is disabled", False
        elif prs is None:
            launch = None
            with self._lock:
                if now - self.at > self.every_s and not self._refreshing:
                    self.at = now
                    self._refreshing = True
                    launch = self._t = threading.Thread(target=self._refresh, args=(now,), daemon=True)
                prs = copy.deepcopy(self.prs)
                gh_at, attempted, error = self.gh_at or None, self.at or None, self.error
                refreshing = self._refreshing
            if launch:
                try:
                    launch.start()
                except (RuntimeError, OSError):
                    with self._lock:
                        self._refreshing = False
                        self.error = "GitHub PR refresh could not start"
                    error, refreshing = self.error, False
        else:
            # Explicitly supplied snapshots are used by deterministic callers.
            gh_at, attempted, error, refreshing = now, now, None, False
        available = prs is not None
        age = max(0.0, now - gh_at) if gh_at is not None else None
        stale = not available or bool(error) or age is None or age > max(60, self.every_s * 2)
        if status_text is None:
            try:
                status_text = site.optional_path("status").read_text(errors="replace") if site.adapter_enabled("git") else ""
            except OSError:
                status_text = ""
        lines = status_lines(status_text)
        pipeline, ladder = [], []
        for p in prs or []:
            n, head = int(p["number"]), p.get("headRefOid") or ""
            pipeline.append({"pr": n, "title": clip(p.get("title") or "", 80), "url": p.get("url"), "draft": bool(p.get("isDraft")),
                             "head": head[:8], "mini": mini_status(lines, n, head), "diesel": diesel_status(lines, n, head),
                             "xcode_cloud": XCODE_CLOUD})
            ladder.append({"pr": n, "head": head[:8], **review_state(p.get("reviews") or [], p.get("comments") or [], head)})
        return {"as_of": now, "gh_at": gh_at, "gh_attempted_at": attempted,
                "gh_ok": available and not stale, "gh_available": available, "gh_stale": stale,
                "gh_age_s": age, "gh_error": error, "gh_refreshing": refreshing,
                "gh_coverage": {"count": len(prs) if available else None, "limit": PR_LIMIT,
                                "limited": bool(available and len(prs) >= PR_LIMIT)},
                "pipeline": pipeline, "ladder": ladder,
                "lanes": {"mini": lanes(site.optional_path("headless_state")) if site.adapter_enabled("git") else [],
                          "devices": devices(lines), "scratch": scratch_free(site.optional_path("scratch")) if site.adapter_enabled("local_system") else {"state": "unconfigured"}},
                "sources": "gh pr list (read-only), status.md, headless/state, statvfs"}
