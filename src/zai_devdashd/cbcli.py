"""Read values CodexBar has already fetched, via its own CLI (codexbar usage/cost --json-only).

We never touch cookies, tokens, auth files or the keychain: CodexBar does its own fetching.
Identity fields (emails, account ids, org ids) are dropped before anything is cached or shown.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .usage import ET, HOME, USAGE_DIR, CR_LEDGER, GB_LOG_DIRS, MONTH_DAYS, iso_epoch, parse_grok_usage, plan_for

from zai_devdashd import config as site
CLI = "codexbar"
CACHE = USAGE_DIR / "codexbar-cli.json"
USAGE_PROVIDERS = ("codex", "cursor", "copilot", "grok", "coderabbit")
COST_PROVIDERS = ("codex", "cursor")  # `codexbar cost` does not support grok (CLI says so)
USAGE_EVERY_S = 300
COST_EVERY_S = 1800
DROP_KEYS = {"accountEmail", "accountID", "accountOrganization", "account", "email", "userId", "user_id"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Name on the dashboard, CodexBar provider id, role in our workflow.
PROVIDERS = [
    ("Codex", "codex", "Configured coding and review usage"),
    ("Grok Build", "grok", "Configured CLI usage"),
    ("Grok Bot", "cursor:grok-bot", "Configured agent usage"),
    ("Cursor", "cursor", "Cloud agents and IDE usage"),
    ("CodeRabbit", "coderabbit", "Configured review telemetry"),
]


def redact(o):
    if isinstance(o, dict):
        return {k: redact(v) for k, v in o.items() if k not in DROP_KEYS}
    if isinstance(o, list):
        return [redact(x) for x in o]
    if isinstance(o, str):
        return EMAIL_RE.sub("[email]", o)
    return o


def _cli_executable():
    """Discover the opted-in CLI through PATH or a configured absolute path."""
    value = (site.CURRENT.get("telemetry") or {}).get("codexbar_cli") or CLI
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x1f\x7f]", value):
        return None
    try:
        candidate = Path(value).expanduser()
        if "/" in value and not candidate.is_absolute():
            return None
        found = shutil.which(str(candidate))
        if not found:
            return None
        resolved = Path(found).resolve(strict=True)
        return str(resolved) if resolved.is_file() and os.access(resolved, os.X_OK) else None
    except (OSError, ValueError, RuntimeError):
        return None


def _read_only_cli_args(args):
    if not isinstance(args, list) or not args or any(not isinstance(x, str) for x in args):
        return False
    command = args[0]
    providers = USAGE_PROVIDERS if command == "usage" else COST_PROVIDERS if command == "cost" else ()
    if not providers:
        return False
    if len(args) == 1:
        return True
    if len(args) not in (3, 5) or args[1] != "--provider" or args[2] not in providers:
        return False
    return len(args) == 3 or (command == "cost" and args[3] == "--days" and
                              re.fullmatch(r"[0-9]{1,2}", args[4]) is not None and 1 <= int(args[4]) <= 31)


def run_cli(args: list, timeout: int = 300):
    if not site.adapter_enabled("usage"):
        return None
    if not _read_only_cli_args(args) or type(timeout) is not int or not 1 <= timeout <= 300:
        return None
    executable = _cli_executable()
    if not executable:
        return None
    try:
        p = subprocess.run(["/usr/bin/nice", "-n", "15", executable] + args + ["--json-only"],
                           capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return None
        data = json.loads(p.stdout) if p.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        d = data[0]
        if d.get("error"):
            return None
        if d.get("projects"):  # keep per-project totals only, not per-day per-project detail
            d["projects"] = [{"path": s.get("path"), "totalTokens": s.get("totalTokens"), "totalCost": s.get("totalCost")}
                             for p_ in d["projects"] for s in (p_.get("sources") or [p_])]
        return redact(d)
    return None


def read_cache(path: Path = CACHE) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


class Poller:
    """Background refresh of CodexBar CLI readings into CACHE. Light: niced, one call at a time."""

    def __init__(self, path: Path = CACHE):
        self.path = path
        self.started = False
        self.lock = threading.Lock()

    def start(self):
        if not site.adapter_enabled("usage") or os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ZAI_DEVDASHD_NO_CODEXBAR_CLI"):
            return
        with self.lock:
            if self.started:
                return
            self.started = True
        threading.Thread(target=self.loop, daemon=True, name="codexbar-cli").start()

    def loop(self):
        while True:
            try:
                self.refresh(time.time())
            except Exception:
                pass
            time.sleep(60)

    def refresh(self, now: float):
        if not site.adapter_enabled("usage"):
            return
        data = read_cache(self.path)
        data.setdefault("usage", {}); data.setdefault("cost", {}); data.setdefault("at", {})
        changed = False
        for p in USAGE_PROVIDERS:
            if now - data["at"].get("usage:" + p, 0) >= USAGE_EVERY_S:
                d = run_cli(["usage", "--provider", p], 90)
                data["at"]["usage:" + p] = now
                if d:
                    data["usage"][p] = d
                changed = True
        for p in COST_PROVIDERS:
            if now - data["at"].get("cost:" + p, 0) >= COST_EVERY_S:
                d = run_cli(["cost", "--provider", p, "--days", "30"], 300)
                data["at"]["cost:" + p] = now
                if d:
                    data["cost"][p] = d
                changed = True
        if changed:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.path)


# ---------------- view ----------------

def _next_day(t: float) -> float:
    d = datetime.fromtimestamp(t, ET).date() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, tzinfo=ET).timestamp()


def _is_workday(t: float, work_days: int) -> bool:
    return datetime.fromtimestamp(t, ET).isoweekday() <= work_days


def linear_pace(used: float, resets: float, duration: float, now: float, work_days: int | None = None) -> dict | None:
    """Port of CodexBarCore/UsagePace.swift weekly(), including the workday option for 7-day windows."""
    left_s = resets - now
    if duration <= 0 or left_s <= 0 or left_s > duration:
        return None
    elapsed = duration - left_s
    wd = None
    if work_days and 2 <= work_days < 7 and abs(duration - 10080 * 60) < 1:
        tot = el = rem = 0.0
        cur = resets - duration
        while cur < resets:
            end = min(_next_day(cur), resets)
            if _is_workday(cur, work_days):
                tot += end - cur
                if now > cur:
                    el += min(now, end) - cur
                if now < end:
                    rem += end - max(now, cur)
            cur = end
        if tot > 0:
            wd = (tot, el, rem)
    expected = max(0.0, min(100.0, (wd[1] / wd[0] if wd else elapsed / duration) * 100))
    pel = wd[1] if wd else elapsed
    prem = wd[2] if wd else left_s
    eta, last = None, False
    if used >= 100:
        eta = 0.0
    elif pel > 0 and used > 0:
        cand = (100 - used) / (used / pel)
        if cand >= prem:
            last = True
        elif wd:
            need, cur = cand, now
            while cur < resets:
                end = min(_next_day(cur), resets)
                if _is_workday(cur, work_days):
                    if need <= end - cur:
                        eta = cur + need - now
                        break
                    need -= end - cur
                cur = end
        else:
            eta = cand
    elif pel > 0:
        last = True
    return {"expectedUsedPercent": expected, "deltaPercent": used - expected, "etaSeconds": eta, "willLastToReset": last}


def work_days_setting() -> int | None:
    """Optional configured calendar preference; no implicit host preference query."""
    value = (site.CURRENT.get("telemetry") or {}).get("work_days")
    return value if type(value) is int and 1 <= value <= 7 else None


WORK_DAYS = [None]


def bar(label: str, w: dict | None, pace: dict | None, now: float) -> dict | None:
    if not isinstance(w, dict) or w.get("usedPercent") is None:
        return None
    used = max(0.0, min(100.0, float(w["usedPercent"])))
    resets = iso_epoch(w.get("resetsAt"))
    out = {"label": label, "used": round(used, 1), "left": round(100 - used, 1), "resets": resets,
           "window_min": w.get("windowMinutes"), "pace": None}
    if not isinstance(pace, dict) and resets and w.get("windowMinutes"):
        pace = linear_pace(used, resets, float(w["windowMinutes"]) * 60, now, WORK_DAYS[0])
        out["pace_source"] = "computed here with CodexBar's linear formula (UsagePace.weekly)"
    if isinstance(pace, dict) and pace.get("expectedUsedPercent") is not None:
        eta = pace.get("etaSeconds")
        out["pace"] = {"expected": round(float(pace["expectedUsedPercent"]), 1), "delta": round(float(pace.get("deltaPercent") or 0), 1),
                       "stage": pace.get("stage"), "will_last": bool(pace.get("willLastToReset")),
                       "runs_out": (now + float(eta)) if eta is not None else None,
                       "runs_out_before_reset": eta is not None and resets is not None and now + float(eta) < resets}
    return out


def bars_for(entry: dict, now: float) -> list:
    u = (entry or {}).get("usage") or {}
    labels = (entry or {}).get("rateWindowLabels") or {}
    pace = (entry or {}).get("pace") or {}
    out = []
    for k in ("primary", "secondary", "tertiary"):
        b = bar(labels.get(k) or k.title(), u.get(k), pace.get(k), now)
        if b:
            out.append(b)
    for x in u.get("extraRateWindows") or []:
        b = bar(x.get("title") or "Extra", x.get("window"), (pace.get("extra") or {}).get(x.get("id")) if isinstance(pace.get("extra"), dict) else None, now)
        if b:
            b["extra_id"] = x.get("id")
            out.append(b)
    return out


def detail_rows(entry: dict) -> list:
    rows = []
    for det in ((entry or {}).get("usage") or {}).get("details") or []:
        for r in det.get("rows") or []:
            if r.get("label"):
                v = str(r.get("value") or "")
                if r.get("secondaryValue"):
                    v += " (resets %s)" % r["secondaryValue"]
                rows.append([str(r["label"]), v])
    return rows


def cost_block(c: dict | None) -> dict | None:
    if not c or c.get("error") or c.get("last30DaysTokens") is None:
        return None
    return {"today_tokens": c.get("sessionTokens"), "today_cost": c.get("sessionCostUSD"),
            "d30_tokens": c.get("last30DaysTokens"), "d30_cost": c.get("last30DaysCostUSD"),
            "provenance": c.get("provenance"), "source": c.get("source"), "updated": iso_epoch(c.get("updatedAt")),
            "note": "Estimated from token usage at API list prices, not a subscription bill"}


def cr_by_pr(text: str, now: float, days: int = 30) -> list:
    counts = defaultdict(int)
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < 3 or f[1] not in ("cli", "full", "pr"):
            continue
        t = iso_epoch(f[0])
        if t is None or now - t > days * 86400:
            continue
        if f[2].strip() in ("", "-"):
            continue
        m = re.search(r"\(#(\d+)\)", f[2])
        counts["#" + m.group(1) if m else f[2]] += 1
    return sorted(([k, v] for k, v in counts.items()), key=lambda x: -x[1])[:8]


LANE_RE = re.compile(r"[-_]?\d{8}([-_]\d{4,6})?$")


def gb_by_lane(dirs: list, now: float, cache: dict, days: int = 30, today_out: dict | None = None) -> list:
    tot = defaultdict(lambda: [0, 0])
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    for d in dirs:
        for path in (d.glob("*.jsonl") if d.is_dir() else []):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime > days * 86400:
                continue
            hit = cache.get(str(path))
            if not hit or hit[0] != mtime:
                hit = (mtime, parse_grok_usage(path))
                cache[str(path)] = hit
            lane = LANE_RE.sub("", path.stem) or path.stem
            for u in hit[1]:
                tot[lane][0] += u["total"]
                tot[lane][1] += 1
                if today_out is not None:
                    today_out["d30"] = today_out.get("d30", 0) + u["total"]
                    if u.get("cost") is not None:
                        today_out["d30_cost"] = today_out.get("d30_cost", 0.0) + u["cost"]
                    if mtime >= day0:
                        today_out["today"] = today_out.get("today", 0) + u["total"]
                        if u.get("cost") is not None:
                            today_out["today_cost"] = today_out.get("today_cost", 0.0) + u["cost"]
    return sorted(([k, v[0], v[1]] for k, v in tot.items() if v[0]), key=lambda x: -x[1])[:8]


def short_path(p: str) -> str:
    p = (p or "").replace(str(HOME), "~")
    parts = p.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else p


def providers_view(cache: dict, now: float, ledger_text: str = "", gb_dirs=None, gb_cache=None, plans: dict | None = None) -> list:
    usage = cache.get("usage") or {}
    cost = cache.get("cost") or {}
    plans = plans or {}
    out = []
    for name, pid, role in PROVIDERS:
        base = pid.split(":")[0]
        e = usage.get(base) or {}
        card = {"name": name, "role": role, "provider": base, "plan": None, "source": None, "version": None,
                "updated": None, "bars": [], "details": [], "cost": None, "consumers": [], "consumers_label": None,
                "missing": []}
        if pid == "cursor:grok-bot":
            bs = [b for b in bars_for(e, now) if b.get("extra_id") == "cursor-grok-bot"]
            card["bars"] = bs
            card["plan"] = "Grok Bot pool (reported via Cursor account)" if bs else None
            card["source"] = "Cursor dashboard (get-sand-usage-status)" if bs else None
            card["updated"] = iso_epoch((e.get("usage") or {}).get("updatedAt"))
            card["missing"].append("Per-agent token split: Cursor only reports the Grok Bot pool as a percent")
        elif e:
            u = e.get("usage") or {}
            card.update(plan=plan_for(name, plans).get("plan") or u.get("loginMethod"), cb_plan=u.get("loginMethod"), source=e.get("source"), version=e.get("version"),
                        updated=iso_epoch(u.get("updatedAt")), details=detail_rows(e))
            card["bars"] = [b for b in bars_for(e, now) if b.get("extra_id") is None] if base == "cursor" else bars_for(e, now)
            if base == "codex":
                cr = e.get("credits") or {}
                card["details"].append(["Credits left", str(cr.get("remaining", "0"))])
            card["cost"] = cost_block(cost.get(base))
        else:
            card["missing"].append("No CodexBar reading yet")
        if base == "codex":
            projs = (cost.get("codex") or {}).get("projects") or []
            card["consumers"] = [[short_path(p.get("path")), p.get("totalTokens"), p.get("totalCost")] for p in projs][:8]
            card["consumers_label"] = "30-day tokens by project (CodexBar cost scan of ~/.codex/sessions)"
        elif name == "CodeRabbit":
            card["consumers"] = [[k, v, None] for k, v in cr_by_pr(ledger_text, now)]
            card["consumers_label"] = "Reviews by PR, last 30 days (cr-queue/ledger.tsv)"
        elif name == "Grok Build" and gb_dirs is not None:
            tt: dict = {}
            card["consumers"] = [[k, t, None, runs] for k, t, runs in gb_by_lane(gb_dirs, now, gb_cache if gb_cache is not None else {}, today_out=tt)]
            if card.get("cost") is None and tt:
                card["cost"] = {"today_tokens": tt.get("today", 0), "today_cost": round(tt["today_cost"], 2) if "today_cost" in tt else None,
                                "d30_tokens": tt.get("d30"), "d30_cost": round(tt["d30_cost"], 2) if "d30_cost" in tt else None,
                                "provenance": "Grok Build run logs (end-event usage, total_cost_usd)", "source": "local",
                                "note": "Headless lane runs only (interactive grok sessions are counted by CodexBar's app, not its CLI)"}
            card["consumers_label"] = "30-day tokens by lane (Grok Build run logs)"
        elif name == "GitHub Copilot":
            card["missing"].append("Per-PR review split: GitHub's quota API reports totals only")
        pl = plan_for(name, plans)
        price = pl.get("price_usd")
        d30 = (card.get("cost") or {}).get("d30_cost")
        if name == "Grok Bot":
            d30 = None
        card["price"] = price
        card["price_source"] = pl.get("source")
        if price and d30 is not None:
            card["value"] = {"api_30d": round(d30, 2), "price_30d": round(price * 30 / MONTH_DAYS, 2),
                             "ratio": round(d30 / (price * 30 / MONTH_DAYS), 1)}
        else:
            card["value"] = None
            card["value_why"] = ("Plan price unknown" if not price else "30-day API-rate cost not reported")
        out.append(card)
    return out


class CodexBarView:
    def __init__(self):
        self.poller = Poller(site.optional_path("usage_dir", section="telemetry") / "codexbar-cli.json")
        self.gb_cache: dict = {}

    def collect(self, now: float, plans: dict | None = None, start_poller: bool = True) -> list:
        if not site.adapter_enabled("usage"):
            return []
        if start_poller:
            self.poller.start()
            if WORK_DAYS[0] is None:
                WORK_DAYS[0] = work_days_setting()
        try:
            ledger = site.optional_path("ledger").read_text(errors="replace")
        except OSError:
            ledger = ""
        return providers_view(read_cache(self.poller.path), now, ledger, [site.optional_path("run_logs", section="telemetry")], self.gb_cache, plans)
