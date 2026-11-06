"""Zero-cost utilization readers: local files, local ps, and free gh metadata only.

Nothing here calls Codex, Grok, an LLM, or any endpoint that spends usage.
~/.codex/auth.json is never opened.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from zai_devdashd.probes import redact_text

ET = ZoneInfo("America/New_York")
STALE_AFTER_S = 24 * 3600
NA = "Not available"
from zai_devdashd import config as site
HOME = Path.home()
CODEX_SESSIONS = site.optional_path("codex_sessions_dir", section="telemetry")
USAGE_DIR = site.optional_path("usage_dir", section="telemetry")
LANES_ROOT = site.optional_path("lanes_root", section="telemetry")
STATUS_MD = site.optional_path("status")
GB_LOG_DIRS = [site.optional_path("run_logs", section="telemetry")]
CR_LEDGER = site.optional_path("ledger")
CR_FULL_PER_HOUR = 1
CR_CLI_PER_HOUR = 5  # default; [coderabbit] cli_per_hour in config.toml overrides. Source: https://docs.coderabbit.ai/management/rate-limits


def cr_limits() -> tuple:
    try:
        from .resources_v2 import settings
        c = settings()["coderabbit"]
        return int(c["full_per_hour"]), int(c["cli_per_hour"])
    except Exception:
        return CR_FULL_PER_HOUR, CR_CLI_PER_HOUR
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def et(epoch) -> str:
    if epoch is None:
        return NA
    try:
        return datetime.fromtimestamp(float(epoch), ET).strftime("%a %b %-d, %-I:%M %p ET")
    except (TypeError, ValueError, OSError):
        return NA


def iso_epoch(text) -> float | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def clean(text) -> str:
    return EMAIL_RE.sub("<redacted>", redact_text(str(text or "")))


def tail_lines(path: Path, max_bytes: int = 262144) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return lines


def first_line(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline()
    except OSError:
        return ""


# ---------------- Codex ----------------

def parse_codex_rollout(path: Path) -> dict:
    """Return cwd, start, last token_count totals and rate limits for one rollout file."""
    out: dict = {"path": str(path), "cwd": "", "started": None, "tokens": None, "rate": None, "rate_ts": None}
    try:
        meta = json.loads(first_line(path) or "{}")
    except ValueError:
        meta = {}
    if meta.get("type") == "session_meta":
        payload = meta.get("payload") or {}
        out["cwd"] = payload.get("cwd") or ""
        out["started"] = iso_epoch(payload.get("timestamp") or meta.get("timestamp"))
    for line in reversed(tail_lines(path)):
        if '"token_count"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        payload = row.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        if out["tokens"] is None and info.get("total_token_usage"):
            out["tokens"] = info["total_token_usage"].get("total_tokens")
        rl = payload.get("rate_limits")
        if rl and out["rate"] is None:
            out["rate"] = rl
            out["rate_ts"] = iso_epoch(row.get("timestamp"))
        if out["tokens"] is not None and out["rate"] is not None:
            break
    return out


class CodexReader:
    def __init__(self, root: Path = CODEX_SESSIONS) -> None:
        self.root = root
        self.cache: dict = {}

    def recent_files(self, days: int = 8) -> list[Path]:
        cutoff = time.time() - days * 86400
        files = []
        if not self.root.is_dir():
            return files
        for path in self.root.glob("*/*/*/rollout-*.jsonl"):
            try:
                if path.stat().st_mtime >= cutoff:
                    files.append(path)
            except OSError:
                pass
        return sorted(files, key=lambda p: p.stat().st_mtime)

    def sessions(self) -> list[dict]:
        rows = []
        for path in self.recent_files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            hit = self.cache.get(str(path))
            if not hit or hit[0] != mtime:
                hit = (mtime, parse_codex_rollout(path))
                self.cache[str(path)] = hit
            row = dict(hit[1])
            row["mtime"] = mtime
            rows.append(row)
        return rows


def codex_row(sessions: list[dict], now: float) -> dict:
    latest = None
    for row in sessions:
        if row.get("rate") and (latest is None or (row.get("rate_ts") or 0) >= (latest.get("rate_ts") or 0)):
            latest = row
    if not latest:
        return row_shell("Codex", "local", None, now, note="No token_count event in ~/.codex/sessions")
    rl = latest["rate"]
    primary = rl.get("primary") or {}
    credits = rl.get("credits") or {}
    window = primary.get("window_minutes")
    window_txt = "weekly" if window == 10080 else ("%s min window" % window if window else "")
    used = primary.get("used_percent")
    exhausted = bool(rl.get("rate_limit_reached_type")) or (used is not None and float(used) >= 100)
    cost = NA
    if credits:
        if credits.get("unlimited"):
            cost = "Credits: unlimited"
        elif credits.get("has_credits"):
            cost = "Credits balance %s" % credits.get("balance")
        else:
            cost = "No extra credits (balance %s)" % credits.get("balance", "0")
    row = row_shell("Codex", "local", latest.get("rate_ts"), now)
    row.update({
        "plan": clean(rl.get("plan_type") or NA),
        "usage": ("%s%% used (%s)" % (fmt_num(used), window_txt)) if used is not None else NA,
        "used_percent": used,
        "limit": window_txt or NA,
        "resets": et(primary.get("resets_at")),
        "cost": cost,
        "source": "~/.codex/sessions rollout token_count (last event)",
        "alert": "USAGE EXHAUSTED" if exhausted else "",
    })
    return row


def fmt_num(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f.is_integer() else "%.1f" % f


def latest_lane_dir(root: Path = LANES_ROOT) -> Path | None:
    dirs = [p for p in root.glob("codex-*") if (p / "queue.sh").is_file()]
    return max(dirs, key=lambda p: p.stat().st_mtime) if dirs else None


def parse_queue_sh(text: str) -> list[tuple[str, int]]:
    m = re.search(r"Q=\(([^)]*)\)", text)
    if not m:
        return []
    return [(a, int(b)) for a, b in re.findall(r'"([A-Z])\s+(\d+)"', m.group(1))]


def parse_status_md(text: str) -> dict:
    """Map PR -> {state, start, end, rc, log, wt} from Codex Lane START/END lines."""
    lanes: dict = {}
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) ET .*?Codex Lane (\w+): (START|END) PR #(\d+)(.*)$")
    for line in text.splitlines():
        m = pat.match(line.strip())
        if not m:
            continue
        when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=ET).timestamp()
        pr = int(m.group(4))
        rest = m.group(5)
        item = lanes.setdefault(pr, {"lane": m.group(2)})
        log = re.search(r"\blog (\S+)", rest)
        if log:
            item["log"] = log.group(1)
        if m.group(3) == "START":
            wt = re.search(r"\bwt (\S+)", rest)
            item.update({"start": when, "end": None, "rc": None, "wt": wt.group(1) if wt else ""})
        else:
            rc = re.search(r"rc=(-?\d+)", rest)
            item.update({"end": when, "rc": int(rc.group(1)) if rc else None})
    return lanes


def codex_lanes(sessions: list[dict], now: float, root: Path = LANES_ROOT, status_md: Path = STATUS_MD,
                running_cwds: list[str] | None = None) -> dict:
    lane_dir = latest_lane_dir(root)
    if not lane_dir:
        return {"lanes": [], "queue": [], "exhausted": False, "dir": ""}
    try:
        queue = parse_queue_sh((lane_dir / "queue.sh").read_text(errors="replace"))
    except OSError:
        queue = []
    try:
        status = parse_status_md(status_md.read_text(errors="replace"))
    except OSError:
        status = {}
    tokens_by_cwd: dict = {}
    for s in sessions:
        if s.get("cwd") and s.get("tokens") is not None:
            tokens_by_cwd[s["cwd"]] = tokens_by_cwd.get(s["cwd"], 0) + int(s["tokens"])
    running = running_cwds or []
    lanes, waiting = [], []
    for letter, pr in queue:
        st = status.get(pr)
        if not st or not st.get("start"):
            waiting.append({"lane": letter, "pr": pr})
            continue
        wt = st.get("wt") or ""
        if st.get("end"):
            state = "Done" if st.get("rc") == 0 else "Failed (rc=%s)" % st.get("rc")
            elapsed = st["end"] - st["start"]
        else:
            state = "Running" if (not running or wt in running) else "Stopped"
            elapsed = now - st["start"]
        last = ""
        if st.get("log"):
            for line in reversed(tail_lines(Path(st["log"]), 16384)):
                if line.strip():
                    last = clean(line.strip())[:200]
                    break
        lanes.append({
            "lane": letter, "pr": pr, "state": state, "elapsed": dur(elapsed),
            "started": et(st["start"]), "last_line": last,
            "tokens": tokens_by_cwd.get(wt),
        })
    return {"lanes": lanes, "queue": waiting, "exhausted": (lane_dir / "USAGE-EXHAUSTED").exists(),
            "dir": "~/" + str(lane_dir.relative_to(HOME)) if str(lane_dir).startswith(str(HOME)) else str(lane_dir)}


def dur(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    h, rem = divmod(seconds, 3600)
    return ("%dh %02dm" % (h, rem // 60)) if h else "%dm" % (rem // 60)


# ---------------- Grok Build ----------------

def parse_grok_costs(path: Path) -> list[float]:
    costs = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type":"end"' not in line and '"type": "end"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") == "end" and isinstance(row.get("total_cost_usd"), (int, float)):
                    costs.append(float(row["total_cost_usd"]))
    except OSError:
        pass
    return costs


class GrokReader:
    def __init__(self, dirs: list[Path] | None = None) -> None:
        self.dirs = dirs if dirs is not None else GB_LOG_DIRS
        self.cache: dict = {}

    def row(self, now: float) -> dict:
        total = day = 0.0
        runs = day_runs = 0
        newest = None
        today_s = day_start(now)
        sums = {"spend_today": 0.0, "spend_7d": 0.0, "spend_30d": 0.0}
        for d in self.dirs:
            for path in d.glob("*.jsonl") if d.is_dir() else []:
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                hit = self.cache.get(str(path))
                if not hit or hit[0] != mtime:
                    hit = (mtime, parse_grok_costs(path))
                    self.cache[str(path)] = hit
                costs = hit[1]
                if not costs:
                    continue
                total += sum(costs)
                runs += len(costs)
                newest = max(newest or 0, mtime)
                if mtime >= today_s:
                    sums["spend_today"] += sum(costs)
                if now - mtime < 7 * 86400:
                    sums["spend_7d"] += sum(costs)
                if now - mtime < 30 * 86400:
                    sums["spend_30d"] += sum(costs)
                if now - mtime < 86400:
                    day += sum(costs)
                    day_runs += len(costs)
        row = row_shell("Grok Build", "local", newest, now)
        if not runs:
            row["note"] = "No end events with total_cost_usd in lane logs"
            return row
        row.update({
            "cost": "$%.2f in logs touched last 24 h (%d runs) · $%.2f all logged runs (%d)" % (day, day_runs, total, runs),
            "spend_24h": round(day, 2), "spend_total": round(total, 2),
            **{k: round(v, 2) for k, v in sums.items()},
            "usage": "%d finished headless runs logged" % runs,
            "source": "total_cost_usd in end events from configured run-log directories",
            "note": "Limit and reset are not recorded locally",
        })
        return row


# ---------------- snapshots ----------------

SNAPSHOT_SCHEMA = {
    "grokbot.json": "{as_of: ISO-8601, plan, weekly_used_percent, resets_at: ISO-8601, on_demand_limit, top_consumers: [{name, percent}], note}",
    "cursor.json": "{as_of, status, plan, used_percent, resets_at, spend_usd, note}",
    "coderabbit.json": "{as_of, plan, seat_cost_usd, note}",
    "github.json": "{as_of, plan, actions_minutes_used, actions_minutes_included, storage_gb, spend_usd, note}",
}


def read_snapshot(name: str, usage_dir: Path = USAGE_DIR) -> dict | None:
    path = usage_dir / name
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def row_shell(name: str, kind: str, as_of, now: float, note: str = "") -> dict:
    stale = as_of is None or (now - float(as_of)) > STALE_AFTER_S
    return {
        "name": name, "kind": kind, "plan": NA, "usage": NA, "limit": NA, "resets": NA, "cost": NA,
        "as_of": et(as_of), "stale": bool(as_of is not None and stale), "manual": kind == "manual",
        "source": "", "note": note, "alert": "",
    }


def grokbot_row(now: float, usage_dir: Path = USAGE_DIR) -> dict:
    snap = read_snapshot("grokbot.json", usage_dir)
    if not snap:
        return row_shell("Grok Bot", "manual", None, now, note="No snapshot at configured usage directory: grokbot.json")
    row = row_shell("Grok Bot", "manual", iso_epoch(snap.get("as_of")), now)
    used = snap.get("weekly_used_percent")
    tops = snap.get("top_consumers") or []
    row.update({
        "plan": clean(snap.get("plan") or NA),
        "usage": ("%s%% of weekly limit" % fmt_num(used)) if used is not None else NA,
        "limit": "Weekly" + ("; on-demand: %s" % clean(snap["on_demand_limit"]) if snap.get("on_demand_limit") else ""),
        "resets": et(iso_epoch(snap.get("resets_at"))) if snap.get("resets_at") else NA,
        "cost": clean(snap.get("cost") or NA),
        "detail": ", ".join("%s %s%%" % (clean(t.get("name")), fmt_num(t.get("percent"))) for t in tops if isinstance(t, dict)),
        "source": "Manual snapshot configured usage directory: grokbot.json (written by Grok Bot)",
        "note": clean(snap.get("note") or ""),
    })
    return row


def cursor_row(now: float, usage_dir: Path = USAGE_DIR) -> dict:
    snap = read_snapshot("cursor.json", usage_dir)
    row = row_shell("Cursor", "manual", iso_epoch((snap or {}).get("as_of")), now)
    row["alert"] = ""
    snap_at = iso_epoch((snap or {}).get("as_of"))
    if snap and snap_at is not None and snap_at < ULTRA_SWITCH_EPOCH:
        # Written under the old Pro plan; its plan/status ("Parked") no longer applies.
        row.update(plan=ULTRA_PLAN, usage=NA, note="Manual snapshot from %s predates the configured plan cutoff; ignored" % et(snap_at),
                   source="CodexBar (live) when available; old manual cursor.json ignored")
        return row
    if snap:
        row.update({
            "plan": clean(snap.get("plan") or NA),
            "usage": ("%s%% used" % fmt_num(snap["used_percent"])) if snap.get("used_percent") is not None else clean(snap.get("status") or NA),
            "resets": et(iso_epoch(snap.get("resets_at"))) if snap.get("resets_at") else NA,
            "cost": ("$%.2f" % float(snap["spend_usd"])) if isinstance(snap.get("spend_usd"), (int, float)) else NA,
            "alert": clean(snap.get("status") or ""),
            "note": clean(snap.get("note") or ""),
            "source": "Manual snapshot configured usage directory: cursor.json (nothing readable locally without spending)",
        })
    else:
        row["source"] = "No snapshot at configured usage directory: cursor.json"
    return row


def parse_cr_ledger(text: str, now: float) -> dict:
    last_full = None
    cli_hour = 0
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 5:
            continue
        ts = iso_epoch(cols[0])
        if ts is None:
            continue
        kind, pr = cols[1], cols[2]
        if kind in ("full", "full-priority") and pr != "-" and "requested" in cols[4]:
            last_full = max(last_full or 0, ts)
        if kind == "cli" and now - ts < 3600:
            cli_hour += 1
    return {"last_full": last_full, "cli_hour": cli_hour}


def coderabbit_row(now: float, ledger: Path = CR_LEDGER, usage_dir: Path = USAGE_DIR) -> dict:
    try:
        mtime = ledger.stat().st_mtime
        info = parse_cr_ledger(ledger.read_text(errors="replace"), now)
    except OSError:
        return row_shell("CodeRabbit", "local", None, now, note="Ledger not found")
    row = row_shell("CodeRabbit", "local", mtime, now)
    full_h, cli_h = cr_limits()
    nxt = (info["last_full"] + 3600) if info["last_full"] else None
    row.update({
        "usage": "Last full review %s · CLI previews %d/%d this hour" % (et(info["last_full"]), info["cli_hour"], cli_h),
        "limit": "%d full review/hour + %d CLI previews/hour (docs.coderabbit.ai/management/rate-limits)" % (full_h, cli_h),
        "resets": ("Next full slot now" if not nxt or nxt <= now else "Next full slot " + et(nxt)),
        "source": "CodeRabbit routine ledger configured review ledger",
        "next_full_epoch": nxt,
    })
    snap = read_snapshot("coderabbit.json", usage_dir)
    if snap:
        row["plan"] = clean(snap.get("plan") or NA) + " (manual)"
        if isinstance(snap.get("seat_cost_usd"), (int, float)):
            row["cost"] = "$%.2f/seat (manual %s)" % (float(snap["seat_cost_usd"]), et(iso_epoch(snap.get("as_of"))))
    return row


def gh_json(args: list[str]) -> dict | None:
    try:
        out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def github_row(now: float, rate: dict | None, usage_dir: Path = USAGE_DIR) -> dict:
    res = (rate or {}).get("resources") or {}
    core, gql = res.get("core") or {}, res.get("graphql") or {}
    row = row_shell("GitHub", "local", now if rate else None, now)
    if rate:
        row.update({
            "usage": "API core %s/%s left · GraphQL %s/%s left" % (core.get("remaining"), core.get("limit"), gql.get("remaining"), gql.get("limit")),
            "limit": "%s core + %s GraphQL per hour" % (core.get("limit"), gql.get("limit")),
            "resets": "core %s · GraphQL %s" % (et(core.get("reset")), et(gql.get("reset"))),
            "source": "gh api rate_limit (free metadata call)",
            "core_limit": core.get("limit"), "core_remaining": core.get("remaining"), "core_reset": core.get("reset"),
            "note": "Actions minutes/storage need the gh 'user' scope, which is not granted; see manual snapshot",
        })
    else:
        row["note"] = "gh api rate_limit failed"
    snap = read_snapshot("github.json", usage_dir)
    if snap:
        row["plan"] = clean(snap.get("plan") or NA) + " (manual)"
        if snap.get("actions_minutes_used") is not None:
            row["detail"] = "Actions %s/%s min, storage %s GB (manual %s)" % (
                snap.get("actions_minutes_used"), snap.get("actions_minutes_included", NA),
                snap.get("storage_gb", NA), et(iso_epoch(snap.get("as_of"))))
        if isinstance(snap.get("spend_usd"), (int, float)):
            row["cost"] = "$%.2f (manual)" % float(snap["spend_usd"])
    return row


def sync_gauges(exec_view: dict, providers: list, now: float) -> None:
    """Each exec gauge shows the same primary number as its tool card: the card's first usage bar."""
    cards = {c.get("name"): c for c in exec_view.get("cards") or []}
    pv = {p.get("name"): p for p in providers}
    gb = cards.get("Grok Bot")
    if gb is not None and (pv.get("Grok Bot") or {}).get("bars"):
        gb["spend"] = "Grok Bot pool (reported via Cursor account)"
        gb.pop("plan", None)
        gb.pop("value", None)
    cur, cc = cards.get("Cursor"), (pv.get("Cursor") or {}).get("cost") or {}
    if cc.get("d30_tokens") is not None:
        toks = (exec_view.get("summary") or {}).get("tokens") or []
        row = next((r for r in toks if r.get("name") == "Cursor"), None)
        if row is None:
            row = {"name": "Cursor"}
            toks.append(row)
        row.update(today={"tokens": cc.get("today_tokens")} if cc.get("today_tokens") is not None else None,
                   d30={"tokens": cc["d30_tokens"]}, d7=None, window=None, source="Cursor dashboard via CodexBar")
        if cur is not None and cc.get("d30_cost") is not None:
            cur["sub"] = "30-day usage value %s, reported by Cursor (not in spend totals)" % money(cc["d30_cost"])
            cur.pop("value", None)
    for p in providers:
        c = cards.get(p.get("name"))
        bars = p.get("bars") or []
        if c is None or not bars:
            continue
        b = bars[0]
        pc = b.get("pace") or {}
        c.update(state="live", percent=b["used"], reset_epoch=b.get("resets"),
                 headline="%s: %s%% used, %s%% left" % (b["label"], fmt_num(b["used"]), fmt_num(b["left"])),
                 pace={"runs_out_before_reset": bool(pc.get("runs_out_before_reset")), "delta_percent": pc.get("delta"),
                       "expected_percent": pc.get("expected"), "runout_epoch": pc.get("runs_out")} if pc else None,
                 gauge_source="CodexBar (%s)" % (p.get("source") or "cli"))
        c["tone"] = tone(b["used"])
        if p.get("name") == "Cursor":
            c["headline"] = "Account usage: %s%% used, resets %s" % (fmt_num(round(b["used"], 1)), short_et(b.get("resets")) if b.get("resets") else "date not reported")
            c.pop("tag", None)
            if b["used"] >= 100:
                c.update(state="parked", headline="Parked: Reported account limit reached (live)")


class UsageCollector:
    def __init__(self) -> None:
        self.codex = CodexReader(site.optional_path("codex_sessions_dir", section="telemetry"))
        self.cost = CodexCost(site.optional_path("codex_sessions_dir", section="telemetry"), site.optional_path("pricing", section="telemetry"))
        self.grok_usage_cache: dict = {}
        self.grok = GrokReader([site.optional_path("run_logs", section="telemetry")])
        self.gh_rate = None
        self.gh_at = 0.0
        self.cbview = None

    def collect(self, running_cwds: list[str] | None = None) -> dict:
        now = time.time()
        if not site.adapter_enabled("usage"):
            return {"state": "unconfigured", "rows": [], "lanes": {"lanes": [], "queue": []},
                    "executive": {"cards": [], "summary": {}, "state": "unconfigured"},
                    "error": "Usage adapter is disabled", "observed_at": None}
        if site.adapter_enabled("github") and now - self.gh_at > 120:
            self.gh_rate = gh_json(["api", "rate_limit"])
            self.gh_at = now
        usage_dir = site.optional_path("usage_dir", section="telemetry")
        run_dirs = [site.optional_path("run_logs", section="telemetry")]
        plans_path = getattr(self, "plans_path", None) or usage_dir / "plans.json"
        sessions = self.codex.sessions()
        rows = []
        for fn in (lambda: codex_row(sessions, now), lambda: self.grok.row(now), lambda: grokbot_row(now, usage_dir),
                   lambda: cursor_row(now, usage_dir), lambda: coderabbit_row(now, site.optional_path("ledger"), usage_dir), lambda: github_row(now, self.gh_rate, usage_dir)):
            try:
                rows.append(fn())
            except Exception as exc:  # one bad source must not hide the panel
                rows.append({"name": "?", "note": "%s: %s" % (exc.__class__.__name__, exc)})
        try:
            lanes = codex_lanes(sessions, now, root=site.optional_path("lanes_root", section="telemetry"), status_md=site.optional_path("status"), running_cwds=running_cwds)
        except Exception as exc:
            lanes = {"lanes": [], "queue": [], "exhausted": False, "error": "%s: %s" % (exc.__class__.__name__, exc)}
        latest = None
        for srow in sessions:
            if srow.get("rate") and (latest is None or (srow.get("rate_ts") or 0) >= (latest.get("rate_ts") or 0)):
                latest = srow
        codex_rate = latest["rate"] if latest else None
        spend = None
        try:
            events, rates = self.cost.scan(now)
            prim = (codex_rate or {}).get("primary") or {}
            wstart = (float(prim["resets_at"]) - float(prim["window_minutes"]) * 60) if prim.get("resets_at") and prim.get("window_minutes") else None
            spend = codex_spend(events, rates, now, wstart)
            if rows and rows[0].get("name") == "Codex":
                parts = []
                for label, key in (("today", "today"), ("this window", "window"), ("7 d", "d7"), ("30 d", "d30")):
                    b = spend.get(key) or {}
                    if b:
                        parts.append("%s %s tokens / %s" % (label, human_tokens(b.get("tokens")), money(b.get("cost"))))
                rows[0]["detail"] = "Estimate, not a bill: " + "; ".join(parts) + (
                    " (each call priced at its model's published OpenAI API rate, e.g. %s $%g in / $%g cached / $%g out per 1M, higher above %dK context; rates from CodexBar's models.dev pricing cache)" % (
                        rates["model"], rates["input"], rates["cache_read"], rates["output"], rates["tier_tokens"] // 1000) if rates else " (rates not found: cost unavailable)")
        except Exception as exc:
            rows[0]["note"] = "estimate failed: %s" % exc.__class__.__name__
        try:
            grok_row = next((r for r in rows if r.get("name") == "Grok Build"), None)
            exec_view = executive(rows, codex_rate, spend, grok_row, read_snapshot("grokbot.json", usage_dir), now)
        except Exception as exc:
            exec_view = {"cards": [], "summary": {}, "error": "%s: %s" % (exc.__class__.__name__, exc)}
        try:
            cb = read_codexbar(getattr(self, 'codexbar_path', None) or site.optional_path('codexbar_snapshot', section='telemetry'))
            gw = cb_window(cb.get("grok"), "primary")
            gstart = (gw["resets"] - gw["window_s"]) if gw and gw.get("resets") and gw.get("window_s") else None
            gtok = grok_tokens(run_dirs, now, gstart, self.grok_usage_cache)
            exec_view = enrich(exec_view, rows, cb, spend, gtok, now, plans=read_plans(plans_path))
        except Exception as exc:
            exec_view.setdefault("summary", {})["error"] = "codexbar: %s" % exc.__class__.__name__
        try:
            from .cbcli import CodexBarView
            if self.cbview is None:
                self.cbview = CodexBarView()
            exec_view["providers"] = self.cbview.collect(now, read_plans(plans_path),
                                                         start_poller=not getattr(self, 'no_cli_poller', False))
            sync_gauges(exec_view, exec_view["providers"], now)
        except Exception as exc:
            exec_view["providers_error"] = "%s: %s" % (exc.__class__.__name__, exc)
        try:
            from .cbcli import read_cache as cb_cache
            from .resources_v2 import build as v2_build
            exec_view["v2"] = v2_build(cb_cache(), now)
        except Exception as exc:
            exec_view["v2_error"] = "%s: %s" % (exc.__class__.__name__, exc)
        try:
            from .reviewers import build as rv_build
            exec_view["reviewers"] = rv_build(now)
        except Exception as exc:
            exec_view["reviewers"] = {"state": "error", "note": "Reviewer panel error: %s: %s" % (exc.__class__.__name__, exc)}
        if site.adapter_enabled("github") and not getattr(self, 'no_cli_poller', False):
            try:
                from .pipeline_v2 import Board
                if getattr(self, "board", None) is None:
                    self.board = Board()
                exec_view["phase2"] = self.board.collect(now)
            except Exception as exc:
                exec_view["phase2_error"] = "%s: %s" % (exc.__class__.__name__, exc)
        reading = rows[0] if rows else {}
        lanes["marker"] = bool(lanes.get("exhausted"))
        lanes["reading_exhausted"] = reading.get("alert") == "USAGE EXHAUSTED"
        lanes["reading"] = reading.get("usage") or NA
        if lanes["reading_exhausted"]:
            lanes["exhausted"] = True
        return {"rows": rows, "codex": lanes, "as_of": now, "schema": SNAPSHOT_SCHEMA, "executive": exec_view}


# ---------------- executive view: pace, Codex token cost estimate, summary ----------------

PRICING_CACHE = site.optional_path("pricing", section="telemetry")
TIER_TOKENS = 272000


def pace(used_percent, resets_at, window_s, now: float) -> dict | None:
    """Deficit (+) or surplus (-) vs an even pace through the window, and projected run-out."""
    try:
        used = float(used_percent)
        reset = float(resets_at)
        window = float(window_s)
    except (TypeError, ValueError):
        return None
    if window <= 0:
        return None
    left_s = max(0.0, reset - now)
    elapsed = min(window, max(0.0, window - left_s))
    expected = 100.0 * elapsed / window
    out = {"expected_percent": round(expected, 1), "delta_percent": round(used - expected, 1),
           "left_s": left_s, "runout_epoch": None, "runs_out_before_reset": False}
    if used >= 100:
        out.update(runout_epoch=now, runs_out_before_reset=True)
    elif elapsed > 0 and used > 0:
        rate = used / elapsed
        runout = now + (100.0 - used) / rate
        out["runout_epoch"] = runout
        out["runs_out_before_reset"] = runout < reset
    return out


def load_rates(path: Path = PRICING_CACHE, model: str = "gpt-6.1-sol") -> dict | None:
    """Published OpenAI per-1M-token rates for the model from CodexBar's public models.dev pricing cache."""
    try:
        data = json.loads(path.read_text())
        cost = data["catalog"]["providers"]["openai"]["models"][model]["cost"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    over = cost.get("context_over_200k") or {}
    tiers = cost.get("tiers") or []
    size = (tiers[0].get("tier") or {}).get("size") if tiers else None
    return {
        "model": model, "input": float(cost["input"]), "cache_read": float(cost.get("cache_read", cost["input"])),
        "output": float(cost["output"]),
        "over_input": float(over.get("input", cost["input"])), "over_cache_read": float(over.get("cache_read", cost.get("cache_read", cost["input"]))),
        "over_output": float(over.get("output", cost["output"])), "tier_tokens": int(size or TIER_TOKENS),
    }


def call_cost(last: dict, rates: dict) -> float:
    inp = int(last.get("input_tokens") or 0)
    cached = min(inp, int(last.get("cached_input_tokens") or 0))
    out = int(last.get("output_tokens") or 0)
    big = inp > rates["tier_tokens"]
    ri, rc, ro = (rates["over_input"], rates["over_cache_read"], rates["over_output"]) if big else (rates["input"], rates["cache_read"], rates["output"])
    return ((inp - cached) * ri + cached * rc + out * ro) / 1e6


MODEL_RE = re.compile(r'"model":"([^"]+)"')


def parse_token_events(lines, rates, state: dict | None = None) -> list:
    """(epoch, tokens, est_cost or None) per model call; repeated token_count events with unchanged totals are skipped.

    rates: a rates dict, or a callable model -> rates|None (the model comes from the latest turn_context/session line).
    """
    out = []
    state = state if state is not None else {}
    prev_total = state.get("prev_total")
    for line in lines:
        if '"token_count"' not in line:
            if '"model":"' in line and ('"turn_context"' in line or '"session_meta"' in line):
                m = MODEL_RE.search(line)
                if m:
                    state["model"] = m.group(1)
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        payload = row.get("payload") or {}
        info = payload.get("info") or {}
        if payload.get("type") != "token_count" or not info:
            continue
        total = (info.get("total_token_usage") or {}).get("total_tokens")
        if total is not None and total == prev_total:
            continue
        prev_total = total
        last = info.get("last_token_usage") or {}
        ts = iso_epoch(row.get("timestamp"))
        if ts is None or not last:
            continue
        r = rates(state.get("model") or "") if callable(rates) else rates
        inp = int(last.get("input_tokens") or 0)
        cached = min(inp, int(last.get("cached_input_tokens") or 0))
        out.append((ts, int(last.get("total_tokens") or 0), call_cost(last, r) if r else None,
                    inp - cached, cached, int(last.get("output_tokens") or 0)))
    state["prev_total"] = prev_total
    return out


class CodexCost:
    """Incremental token/cost totals from rollout token_count events (files changed in the last 31 days)."""

    def __init__(self, root: Path = CODEX_SESSIONS, pricing: Path = PRICING_CACHE) -> None:
        self.root = root
        self.pricing = pricing
        self.files: dict = {}  # path -> {"offset", "events", "prev_total"}

    def scan(self, now: float) -> list:
        cache: dict = {}

        def rates_for(model: str):
            if model not in cache:
                cache[model] = load_rates(self.pricing, model) if model else None
            return cache[model]
        rates = rates_for("gpt-6.1-sol")
        events = []
        cutoff = now - 31 * 86400
        for path in self.root.glob("*/*/*/rollout-*.jsonl") if self.root.is_dir() else []:
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            item = self.files.get(str(path))
            if item is None or st.st_size < item["offset"] or item.get("priced") != bool(rates):
                item = {"offset": 0, "events": [], "priced": bool(rates), "state": {}}
                self.files[str(path)] = item
            if st.st_size > item["offset"]:
                try:
                    with open(path, "rb") as fh:
                        fh.seek(item["offset"])
                        chunk = fh.read()
                except OSError:
                    chunk = b""
                end = chunk.rfind(b"\n") + 1
                if end:
                    text = chunk[:end].decode("utf-8", "replace")
                    item["events"].extend(parse_token_events(text.splitlines(), rates_for, item["state"]))
                    item["offset"] += end
            events.extend(item["events"])
        return events, rates


def bucket(events: list, start: float) -> dict:
    tokens = sum(e[1] for e in events if e[0] >= start)
    costs = [e[2] for e in events if e[0] >= start]
    known = [c for c in costs if c is not None]
    sel = [e for e in events if e[0] >= start]
    split = {}
    if sel and len(sel[0]) >= 6:
        split = {"input": sum(e[3] for e in sel), "cached": sum(e[4] for e in sel), "output": sum(e[5] for e in sel)}
    return {"tokens": tokens, "cost": round(sum(known), 2) if known or not costs else None,
            "partial": len(known) < len(costs), **split}


def day_start(now: float) -> float:
    d = datetime.fromtimestamp(now, ET)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def codex_spend(events: list, rates, now: float, window_start: float | None) -> dict:
    out = {
        "today": bucket(events, day_start(now)), "d7": bucket(events, now - 7 * 86400),
        "d30": bucket(events, now - 30 * 86400),
        "window": bucket(events, window_start) if window_start else None,
        "rates": rates, "unpriced_calls": sum(1 for e in events if e[2] is None and e[0] >= now - 30 * 86400), "source": "Estimated from Codex session token counts at published OpenAI API rates (not a bill)",
    }
    return out


def human_tokens(n) -> str:
    if n is None:
        return NA
    n = float(n)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return "%.1f%s" % (n / size, unit) if n < 10 * size else "%d%s" % (round(n / size), unit)
    return str(int(n))


def money(v) -> str:
    return NA if v is None else "$%s" % format(v, ",.2f")


def short_et(epoch) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch), ET).strftime("%a %-I:%M %p")


def tone(percent) -> str:
    if percent is None:
        return "unknown"
    p = float(percent)
    return "red" if p >= 85 else ("amber" if p >= 60 else "green")


def executive(rows: list, codex_rate: dict | None, spend: dict | None, grok: dict | None,
              grokbot: dict | None, now: float) -> dict:
    """Plain-language cards for the executive view. Unknown stays unknown."""
    cards = []
    byname = {r.get("name"): r for r in rows}
    # Codex
    c = {"name": "Codex", "state": "unknown", "percent": None, "headline": "Usage not available", "reset_epoch": None,
         "spend": "", "pace": None, "sub": ""}
    if codex_rate:
        prim = codex_rate.get("primary") or {}
        used = prim.get("used_percent")
        window = (prim.get("window_minutes") or 0) * 60
        reset = prim.get("resets_at")
        if used is not None:
            c.update(state="live", percent=float(used), reset_epoch=reset,
                     headline="%s%% of weekly used, resets %s" % (fmt_num(used), short_et(reset)) if reset else "%s%% of weekly used" % fmt_num(used),
                     pace=pace(used, reset, window, now) if reset and window else None)
    if spend:
        w = spend.get("window") or {}
        c["spend"] = "Est. %s today · %s this week" % (money(spend["today"]["cost"]), money(w.get("cost"))) if spend["today"]["cost"] is not None \
            else "%s tokens today (cost unavailable)" % human_tokens(spend["today"]["tokens"])
        c["sub"] = "%s tokens today · %s this window" % (human_tokens(spend["today"]["tokens"]), human_tokens(w.get("tokens")))
    if byname.get("Codex", {}).get("alert"):
        c["state"] = "alert"
    cards.append(c)
    # Grok Build
    g = {"name": "Grok Build", "state": "unknown", "percent": None, "headline": "No limit is published for this plan",
         "reset_epoch": None, "spend": "", "pace": None, "sub": ""}
    if grok and grok.get("spend_total") is not None:
        g.update(state="spend", spend="%s today · %s last 7 days" % (money(grok.get("spend_today")), money(grok.get("spend_7d"))),
                 headline="%s spent in the last 7 days" % money(grok.get("spend_7d")), sub="Spend reported by each finished run")
    cards.append(g)
    # Grok Bot
    b = {"name": "Grok Bot", "state": "unknown", "percent": None, "headline": "No recent reading", "reset_epoch": None,
         "spend": "Included in plan", "pace": None, "sub": ""}
    if grokbot and grokbot.get("weekly_used_percent") is not None:
        reset = iso_epoch(grokbot.get("resets_at"))
        used = float(grokbot["weekly_used_percent"])
        b.update(state="live", percent=used, reset_epoch=reset,
                 headline="%s%% of weekly used%s" % (fmt_num(used), ", resets %s" % short_et(reset) if reset else ""),
                 pace=pace(used, reset, 7 * 86400, now) if reset else None,
                 sub="Reading from %s" % short_et(iso_epoch(grokbot.get("as_of"))))
        if byname.get("Grok Bot", {}).get("stale"):
            b["state"] = "stale"
    cards.append(b)
    # Cursor
    ur = ultra_reset_epoch()
    cards.append({"name": "Cursor", "state": "unknown", "percent": None,
                  "headline": "Account usage: not reported yet" + (", resets %s" % datetime.fromtimestamp(ur, ET).strftime("%b %-d") if ur else ""),
                  "reset_epoch": ur, "spend": "", "pace": None, "sub": ""})
    # CodeRabbit
    cr = byname.get("CodeRabbit") or {}
    r = {"name": "CodeRabbit", "state": "unknown", "percent": None, "headline": "Review schedule not available",
         "reset_epoch": None, "spend": "", "pace": None, "sub": ""}
    m = re.search(r"CLI previews (\d+)/(\d+)", cr.get("usage") or "")
    nxt = cr.get("next_full_epoch")
    if m:
        used, cap = int(m.group(1)), int(m.group(2))
        r.update(state="live", percent=100.0 * used / cap, reset_epoch=nxt if nxt and nxt > now else None,
                 headline=("Next full review %s" % short_et(nxt)) if nxt and nxt > now else "Full review available now",
                 sub="%d of %d quick reviews used this hour" % (used, cap))
    cards.append(r)
    # GitHub
    gh = byname.get("GitHub") or {}
    h = {"name": "GitHub", "state": "unknown", "percent": None, "headline": "Not available", "reset_epoch": None,
         "spend": "", "pace": None, "sub": ""}
    if gh.get("core_limit"):
        used = 100.0 * (gh["core_limit"] - gh["core_remaining"]) / gh["core_limit"]
        h.update(state="live", percent=round(used, 1), reset_epoch=gh.get("core_reset"),
                 headline="%s%% of hourly API used" % fmt_num(round(used, 1)), sub="Plenty left" if used < 60 else "Running low")
    cards.append(h)
    for card in cards:
        card["tone"] = tone(card["percent"]) if card["state"] in ("live", "alert", "stale") else card["state"]
    # summary strip
    def total(key_codex, key_grok):
        parts = []
        cx = (spend or {}).get(key_codex) or {}
        if cx.get("cost") is not None:
            parts.append(cx["cost"])
        if grok and grok.get(key_grok) is not None:
            parts.append(grok[key_grok])
        return round(sum(parts), 2) if parts else None
    risks = [cd["name"] for cd in cards if (cd.get("pace") or {}).get("runs_out_before_reset")]
    summary = {
        "today": total("today", "spend_today"), "d7": total("d7", "spend_7d"), "d30": total("d30", "spend_30d"),
        "note": "Estimated: Codex from token counts at published API rates, Grok Build from run reports. Not a bill.",
        "runs_out": risks,
    }
    return {"cards": cards, "summary": summary}


# ---------------- CodexBar cached usage (read-only widget snapshot; no cookies, tokens or auth) ----------------

CODEXBAR_SNAPSHOT = site.optional_path("codexbar_snapshot", section="telemetry")
CODEXBAR_LABEL = "CodexBar's cached usage snapshot (widget-snapshot.json)"


def read_codexbar(path: Path = CODEXBAR_SNAPSHOT) -> dict:
    """provider -> entry. Only numeric usage, windows, dates and labels are used; emails are stripped."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for e in data.get("entries") or []:
        if isinstance(e, dict) and e.get("provider"):
            out[str(e["provider"])] = e
    return out


def cb_window(entry: dict, key: str) -> dict | None:
    w = (entry or {}).get(key)
    if not isinstance(w, dict) or w.get("usedPercent") is None:
        return None
    return {"used": float(w["usedPercent"]), "resets": iso_epoch(w.get("resetsAt")),
            "window_s": float(w["windowMinutes"]) * 60 if w.get("windowMinutes") else None}


def cb_tokens(entry: dict, now: float, window_start: float | None) -> dict | None:
    """today / window / 7d / 30d token and cost buckets from CodexBar dailyUsage (ET day keys)."""
    days = (entry or {}).get("dailyUsage") or []
    if not days:
        return None
    today = datetime.fromtimestamp(now, ET).date()

    def total(start_date):
        toks = [d.get("totalTokens") for d in days if d.get("dayKey") and d["dayKey"] >= start_date.isoformat()]
        costs = [d.get("costUSD") for d in days if d.get("dayKey") and d["dayKey"] >= start_date.isoformat()]
        t = sum(int(x) for x in toks if x is not None)
        c = round(sum(float(x) for x in costs if x is not None), 2) if any(x is not None for x in costs) else None
        return {"tokens": t, "cost": c}
    from datetime import timedelta
    out = {"today": total(today), "d7": total(today - timedelta(days=6)), "d30": total(today - timedelta(days=29))}
    out["window"] = total(datetime.fromtimestamp(window_start, ET).date()) if window_start else None
    return out


def cb_detail_rows(entry: dict) -> list:
    rows = []
    for det in (entry or {}).get("details") or []:
        for r in det.get("rows") or []:
            if isinstance(r, dict) and r.get("label"):
                rows.append((clean(r.get("label")), clean(r.get("value"))))
    return rows


# ---------------- Grok Build token usage from run end events ----------------

def parse_grok_usage(path: Path) -> list:
    """[{input, cached, output, total}] from each run's end event usage block."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type":"end"' not in line and '"type": "end"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                u = row.get("usage") if row.get("type") == "end" else None
                if isinstance(u, dict):
                    out.append({"input": int(u.get("input_tokens") or 0), "cached": int(u.get("cache_read_input_tokens") or 0),
                                "output": int(u.get("output_tokens") or 0),
                                "total": int(u.get("total_tokens") or 0),
                                "cost": float(row["total_cost_usd"]) if isinstance(row.get("total_cost_usd"), (int, float)) else None})
    except OSError:
        pass
    return out


def grok_tokens(dirs: list, now: float, window_start: float | None, cache: dict) -> dict | None:
    buckets = {k: {"input": 0, "cached": 0, "output": 0, "total": 0, "runs": 0} for k in ("today", "window", "d7", "d30")}
    starts = {"today": day_start(now), "d7": now - 7 * 86400, "d30": now - 30 * 86400, "window": window_start}
    found = False
    for d in dirs:
        for path in d.glob("*.jsonl") if d.is_dir() else []:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            hit = cache.get(str(path))
            if not hit or hit[0] != mtime:
                hit = (mtime, parse_grok_usage(path))
                cache[str(path)] = hit
            for u in hit[1]:
                found = True
                for k, s in starts.items():
                    if s is not None and mtime >= s:
                        b = buckets[k]
                        for f in ("input", "cached", "output", "total"):
                            b[f] += u[f]
                        b["runs"] += 1
                        if u.get("cost") is not None:
                            b["cost"] = b.get("cost", 0.0) + u["cost"]
                            b["cost_runs"] = b.get("cost_runs", 0) + 1
    if not found:
        return None
    if window_start is None:
        buckets["window"] = None
    return buckets


def ptxt(w: dict | None) -> str:
    return "Not available" if not w else "%s%% used, resets %s" % (fmt_num(round(w["used"], 1)), et(w["resets"]))


def enrich(view: dict, rows: list, cb: dict, codex_spend_: dict | None, grok_tok: dict | None, now: float, plans: dict | None = None) -> dict:
    """Add CodexBar-cached Cursor, Copilot, Grok and Grok Bot readings, and the token table. Unknown stays unknown."""
    cards = {c["name"]: c for c in view.get("cards") or []}
    byrow = {r.get("name"): r for r in rows}
    cb_asof = {k: iso_epoch(v.get("updatedAt")) for k, v in cb.items()}
    src = "CodexBar cache"

    def live(card, w, headline):
        card.update(state="live", percent=w["used"], reset_epoch=w["resets"], headline=headline,
                    pace=pace(w["used"], w["resets"], w["window_s"], now) if w.get("window_s") and w.get("resets") else None)
        card["tone"] = tone(w["used"])

    # Grok Build: weekly limit from CodexBar's Grok tab
    g = cards.get("Grok Build")
    gw = cb_window(cb.get("grok"), "primary")
    if g is not None and gw:
        spend = g.get("spend")
        live(g, gw, "%s%% of weekly used, resets %s" % (fmt_num(gw["used"]), short_et(gw["resets"])))
        g["spend"] = spend
        g["sub"] = "Limit from %s, %s" % (src, short_et(cb_asof.get("grok")))
        r = byrow.get("Grok Build")
        if r:
            r.update(limit="Weekly (CodexBar)", resets=et(gw["resets"]), usage=r.get("usage", NA) + " · %s%% of weekly used (CodexBar)" % fmt_num(gw["used"]))
    # Grok Bot: live from CodexBar's Cursor tab row, if newer than the manual snapshot
    gb = cards.get("Grok Bot")
    for row in (cb.get("cursor") or {}).get("usageRows") or []:
        if row.get("id") == "cursor-grok-bot" and isinstance(row.get("window"), dict) and gb is not None:
            w = cb_window({"w": row["window"]}, "w")
            if w:
                live(gb, w, "%s%% of weekly used, resets %s" % (fmt_num(round(w["used"], 1)), short_et(w["resets"])))
                gb["sub"] = "Live from %s, %s" % (src, short_et(cb_asof.get("cursor")))
                r = byrow.get("Grok Bot")
                if r:
                    r.update(usage="%s%% of weekly limit (CodexBar, %s); manual snapshot: %s" % (fmt_num(round(w["used"], 1)), et(cb_asof.get("cursor")), r.get("usage")),
                             resets=et(w["resets"]), manual=False, stale=False, as_of=et(cb_asof.get("cursor")))
    # Cursor: real usage (state derived from the live window; Parked only when the limit is hit)
    cu = cards.get("Cursor")
    cw = cb_window(cb.get("cursor"), "primary")
    cut = None
    if cu is not None and cw:
        ws = (cw["resets"] - cw["window_s"]) if cw.get("resets") and cw.get("window_s") else None
        cut = cb_tokens(cb.get("cursor"), now, ws)
        live(cu, cw, "Account usage: %s%% used, resets %s" % (fmt_num(round(cw["used"], 1)), short_et(cw["resets"])))
        if cw["used"] >= 100:
            cu.update(state="parked", headline="Parked: Reported account limit reached (live), resets %s" % short_et(cw["resets"]))
        tu = (cb.get("cursor") or {}).get("tokenUsage") or {}
        cu["spend"] = "Usage value %s today · %s in 30 days" % (money(tu.get("sessionCostUSD")), money(tu.get("last30DaysCostUSD"))) if tu.get("sessionCostUSD") is not None else ""
        sw, tw = cb_window(cb.get("cursor"), "secondary"), cb_window(cb.get("cursor"), "tertiary")
        cu["sub"] = "Cursor models %s · Third-party %s" % (fmt_num(round(sw["used"])) + "%" if sw else "?", fmt_num(round(tw["used"])) + "%" if tw else "?")
        r = byrow.get("Cursor")
        if r:
            r.update(usage="Total %s%% · Cursor models %s · Third-party %s" % (fmt_num(round(cw["used"], 1)), ptxt(sw), ptxt(tw)),
                     limit="Reported included usage", resets=et(cw["resets"]),
                     cost="Usage value %s today, %s 30 d (CodexBar)" % (money(tu.get("sessionCostUSD")), money(tu.get("last30DaysCostUSD"))),
                     as_of=et(cb_asof.get("cursor")), manual=False, stale=False,
                     source=CODEXBAR_LABEL + "; manual cursor.json kept as fallback")
    # GH Copilot is ignored by policy (Nov 2): no card, usage row or token row.
    order = ["Codex", "Grok Build", "Grok Bot", "Cursor", "CodeRabbit", "GitHub"]  # GH Copilot ignored: not shown
    # CodeRabbit billing details from CodexBar
    crd = cb_detail_rows(cb.get("coderabbit"))
    if crd and byrow.get("CodeRabbit"):
        byrow["CodeRabbit"]["detail"] = "CodexBar: " + "; ".join("%s %s" % kv for kv in crd)
        if cards.get("CodeRabbit"):
            reviews = next((v for k, v in crd if k.lower() == "reviews"), None)
            resets = next((v for k, v in crd if "reset" in k.lower()), None)
            if reviews:
                cards["CodeRabbit"]["sub"] += " · %s reviews this billing period%s" % (reviews, " (resets %s)" % resets if resets else "")
    view["cards"] = [cards[n] for n in order if n in cards]
    # token table
    def t(b):
        return None if not b else {k: b.get(k) for k in ("tokens", "total", "input", "cached", "output", "cost") if b.get(k) is not None}
    table = []
    cs = codex_spend_ or {}
    table.append({"name": "Codex", "source": "Codex session logs", **{k: t(cs.get(k)) for k in ("today", "window", "d7", "d30")}})
    gt = grok_tok or {}
    table.append({"name": "Grok Build", "source": "Run reports (by log file date)", **{k: t(gt.get(k)) for k in ("today", "window", "d7", "d30")}})
    table.append({"name": "Cursor", "source": "CodexBar cache" if cut else "Not available", **{k: t((cut or {}).get(k)) for k in ("today", "window", "d7", "d30")}})
    for n in ("Grok Bot", "CodeRabbit"):
        table.append({"name": n, "source": "Not reported", "today": None, "window": None, "d7": None, "d30": None})
    view.setdefault("summary", {})["tokens"] = table
    add_value(view, rows, cs, gt, cb, plans or {}, now)
    risks = [cd["name"] for cd in view["cards"] if (cd.get("pace") or {}).get("runs_out_before_reset")]
    view["summary"]["runs_out"] = risks
    return view


# ---- plans and subscription value (estimates) ----
PLANS_FILE = site.optional_path("usage_dir", section="telemetry") / "plans.json"
MONTH_DAYS = 365.25 / 12
# Subscription identity, price and reset are unknown until configured.
ULTRA_PLAN = "Configured shared plan"
ULTRA_SWITCH_EPOCH = 0
ULTRA_RESETS = ""


def ultra_reset_epoch():
    return None


CONFIGURED_PLANS = {}
VALUE_SOURCES = {
    "Codex": "Codex session tokens x OpenAI API rates (CodexBar's models.dev pricing cache)",
    "Grok Build": "total_cost_usd reported in each Grok Build run's end event (xAI API rates)",
    "Cursor": "CodexBar's cached Cursor usage value (API-rate cost of tokens used)",
}
NOT_COMPUTABLE = {
    "Grok Bot": "Token counts are not reported locally",
    "GitHub Copilot": "Token counts are not reported (only premium-request counts)",
    "CodeRabbit": "Token counts and API rates are not reported (only review counts)",
    "GitHub": "Not a token service",
}


def read_plans(path: Path = PLANS_FILE) -> dict:
    """Manual plans.json: {"<card name>": {"plan": str|null, "price_usd": number|null}}. Labeled manual."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in (data.get("plans") or {}).items() if isinstance(data, dict) else []:
        if isinstance(v, dict):
            price = v.get("price_usd")
            out[k] = {"plan": v.get("plan") if isinstance(v.get("plan"), str) else None,
                      "price_usd": price if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0 else None,
                      "source": "manual"}
    return out


def plan_for(name: str, plans: dict) -> dict:
    p = dict(CONFIGURED_PLANS.get(name) or {})
    m = plans.get(name) or {}
    if not p:
        p = {"plan": m.get("plan"), "price_usd": m.get("price_usd"), "source": "manual" if (m.get("plan") or m.get("price_usd")) else None}
    return p


def value_30d(name: str, cs: dict, gt: dict, cb: dict, now: float) -> tuple:
    if name == "Codex":
        b = (cs or {}).get("d30") or {}
        return (b.get("cost"), None) if b.get("cost") is not None else (None, "Codex token costs unavailable")
    if name == "Grok Build":
        b = (gt or {}).get("d30") or {}
        if b.get("cost") is None:
            return None, "Run reports carry no API-rate cost"
        partial = b.get("cost_runs", 0) < b.get("runs", 0)
        return b["cost"], ("%d of %d runs reported a cost" % (b["cost_runs"], b["runs"])) if partial else None
    if name == "Cursor":
        cu = (cb or {}).get("cursor") or {}
        v = ((cu.get("tokenUsage") or {}).get("last30DaysCostUSD"))
        return (float(v), None) if isinstance(v, (int, float)) else (None, "CodexBar has no Cursor usage value")
    return None, NOT_COMPUTABLE.get(name, "Not computable")


def add_value(view: dict, rows: list, cs: dict, gt: dict, cb: dict, plans: dict, now: float) -> None:
    byrow = {r.get("name"): r for r in rows}
    shared_total = {}
    vals = {}
    for cd in view.get("cards") or []:
        v, why = value_30d(cd["name"], cs, gt, cb, now)
        vals[cd["name"]] = (v, why)
        sh = (CONFIGURED_PLANS.get(cd["name"]) or {}).get("shared")
        if sh and v is not None:
            shared_total[sh] = shared_total.get(sh, 0.0) + v
    out = []
    for cd in view.get("cards") or []:
        name = cd["name"]
        if name == "GitHub":
            continue
        p = plan_for(name, plans)
        v, why = vals[name]
        price = p.get("price_usd")
        e = {"name": name, "plan": p.get("plan"), "price_usd": price, "plan_source": p.get("source"),
             "local_label": p.get("local_label"), "value_30d": v, "reason": why if v is None else None,
             "note": why if v is not None else None, "value_source": VALUE_SOURCES.get(name)}
        if v is not None:
            e["value_month"] = v / 30 * MONTH_DAYS
            if price:
                e["multiple_30d"] = v / price
                e["multiple_month"] = e["value_month"] / price
        sh = (CONFIGURED_PLANS.get(name) or {}).get("shared")
        if sh and shared_total.get(sh) is not None and price:
            e["shared"] = "One %s subscription covers Grok Build and Grok Bot: counted value %s on %s = %.1fx (Grok Bot not computable)" % (
                p.get("plan"), money(shared_total[sh]), money(price), shared_total[sh] / price)
        cd["plan"] = plan_line(e)
        cd["value"] = value_line(e)
        r = byrow.get(name)
        if r is not None:
            r["plan"] = cd["plan"] + ((" · Local label: " + e["local_label"]) if e.get("local_label") else "")
            r["value"] = cd["value"] + ((" · Source: " + e["value_source"]) if e.get("value_source") else "")
        out.append(e)
    view.setdefault("summary", {})["value"] = out
    view["summary"]["value_note"] = ("Estimates. Subscription value = API-rate value of the tokens used, compared with the monthly plan price. "
                                     "30 days is the trailing 30 days; per month scales that to an average month (30.44 days).")


def plan_line(e: dict) -> str:
    if not e.get("plan") and not e.get("price_usd"):
        return ""
    parts = [e.get("plan") or "name not set"]
    if e.get("price_usd"):
        parts.append("%s/mo" % money(e["price_usd"]))
    return "Plan: %s (%s)" % (" · ".join(parts), e.get("plan_source") or "manual")


def value_line(e: dict) -> str:
    if e.get("value_30d") is None:
        return "Subscription value: not computable (%s)" % e.get("reason")
    v = e["value_30d"]
    if not e.get("price_usd"):
        return "Est. %s of API usage in 30 days" % money(v)
    return "Est. %s of API usage in 30 days on a %s plan = %.1fx · per month %s = %.1fx" % (
        money(v), money(e["price_usd"]), e["multiple_30d"], money(e["value_month"]), e["multiple_month"])
