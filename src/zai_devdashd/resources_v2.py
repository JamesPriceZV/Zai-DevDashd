"""zai-devdashd v2 phase 1: Resource Utilization block for the revised (configured plan) workflow.

Read-only. Inputs are values CodexBar 0.83 has already fetched and cached through its documented CLI
(`codexbar usage|cost --json-only`, cached by cbcli.Poller), the CodeRabbit routine ledger, and the dashboard
TOML. No credentials are read. Identity fields are already dropped by cbcli.redact; project paths are shortened
to their last path component so no user-specific path leaves this process.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from .usage import ET, CR_LEDGER, iso_epoch

CR_RATE_SOURCE = "https://docs.coderabbit.ai/management/rate-limits"
XCODE_CLOUD_SOURCE = "https://developer.apple.com/xcode-cloud/get-started/"
COPILOT_BILLING_SOURCE = "https://github.blog/changelog/2026-06-01-updates-to-github-copilot-billing-and-plans/"

DEFAULTS = {
    "coderabbit": {"cli_per_hour": 5, "full_per_hour": 1, "weekly_full_target": 30, "weekly_full_slowdown": 60,
                   "cli_resets": ""},
    "workflow": {"diesel_windows": [], "cloud_agent_cap": 0, "ultra_resets": "",
                 "bugbot_pilot_start": "", "bugbot_pilot_days": 7},
    "xcode_cloud": {"included_hours": 25, "hours_used_manual": -1},
    "copilot": {"overage_usd": 0},
    "xai": {"key_file": "", "team_id": "", "poll_s": 900},
}


def settings(cfg: dict | None = None) -> dict:
    if cfg is None:
        try:
            from . import config as site
            cfg = site.CURRENT
        except Exception:
            cfg = {}
    out = {}
    for sec, vals in DEFAULTS.items():
        raw = (cfg or {}).get(sec) if isinstance((cfg or {}).get(sec), dict) else {}
        out[sec] = {k: (raw.get(k) if raw.get(k) is not None and type(raw.get(k)) is type(v) or
                        (isinstance(v, int) and isinstance(raw.get(k), (int, float)) and not isinstance(raw.get(k), bool)) else v)
                    for k, v in vals.items()}
    return out


# ---------------- CPU label (Activity Monitor style) ----------------

def cpu_label(perflevels: list | None, total: int | None = None) -> str:
    """'18-core: 12 Performance + 6 Super' from sysctl hw.perflevelN.{name,physicalcpu}; any chip, larger cluster first."""
    lv = [(str(x.get("name") or "").strip(), int(x.get("cores") or 0)) for x in (perflevels or []) if isinstance(x, dict)]
    lv = [(n, c) for n, c in lv if n and c > 0]
    if not lv:
        return ""
    order = sorted(range(len(lv)), key=lambda i: (-lv[i][1], i))
    n = sum(c for _, c in lv)
    return "%d-core: %s" % (total or n, " + ".join("%d %s" % (lv[i][1], lv[i][0]) for i in order))


# ---------------- CodexBar parsers ----------------

def _f(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def htok(n) -> str:
    if not isinstance(n, (int, float)):
        return "not reported"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return "%.1f%s" % (n / div, suf)
    return str(int(n))


def short_project(path) -> str:
    p = str(path or "").rstrip("/")
    return Path(p).name or "unknown"


def model_daily(cost: dict | None, days: int = 7) -> list:
    """Per-day per-model tokens and cost from `codexbar cost` daily[].modelBreakdowns (newest last)."""
    out = []
    for d in ((cost or {}).get("daily") or [])[-days:]:
        if not isinstance(d, dict):
            continue
        models = [{"model": str(m.get("modelName") or "?"), "tokens": int(m.get("totalTokens") or 0), "cost": _f(m.get("cost"))}
                  for m in d.get("modelBreakdowns") or [] if isinstance(m, dict)]
        models.sort(key=lambda m: -(m["cost"] or 0))
        out.append({"date": d.get("date"), "tokens": int(d.get("totalTokens") or 0), "cost": _f(d.get("totalCost")),
                    "cache_read": d.get("cacheReadTokens"), "cache_write": d.get("cacheCreationTokens"), "models": models})
    return out


def model_totals(cost: dict | None) -> list:
    acc: dict = {}
    for d in (cost or {}).get("daily") or []:
        for m in (d or {}).get("modelBreakdowns") or []:
            k = str(m.get("modelName") or "?")
            a = acc.setdefault(k, {"model": k, "tokens": 0, "cost": 0.0})
            a["tokens"] += int(m.get("totalTokens") or 0)
            a["cost"] += _f(m.get("cost")) or 0.0
    return sorted(acc.values(), key=lambda a: -a["cost"])


def project_costs(cost: dict | None, top: int = 6) -> list:
    rows = [{"project": short_project(p.get("path")), "tokens": int(p.get("totalTokens") or 0), "cost": _f(p.get("totalCost"))}
            for p in (cost or {}).get("projects") or [] if isinstance(p, dict)]
    rows.sort(key=lambda r: -(r["cost"] or 0))
    return rows[:top]


def reset_credits(usage: dict | None) -> dict | None:
    """Codex reset credits: available count and the soonest expiry (CodexBar 0.83 `resetCredits`)."""
    u = usage or {}
    rc = u.get("resetCredits") if isinstance(u.get("resetCredits"), dict) else None
    inner = ((u.get("usage") or {}).get("codexResetCredits") or {})
    creds = [c for c in inner.get("credits") or [] if isinstance(c, dict) and c.get("status", "available") == "available"]
    exp = [iso_epoch(c.get("expires_at")) for c in creds if c.get("expires_at")]
    nxt = iso_epoch((rc or {}).get("nextExpiresAt")) or (min(exp) if exp else None)
    avail = (rc or {}).get("available")
    if avail is None:
        avail = inner.get("availableCount", len(creds) if creds else None)
    if avail is None and nxt is None:
        return None
    return {"available": avail, "next_expires": nxt}


def cb_pace(usage: dict | None, key: str) -> dict | None:
    p = ((usage or {}).get("pace") or {}).get(key)
    if not isinstance(p, dict):
        return None
    return {"summary": p.get("summary"), "expected": p.get("expectedUsedPercent"), "delta": p.get("deltaPercent"),
            "eta_s": p.get("etaSeconds"), "will_last": bool(p.get("willLastToReset")), "stage": p.get("stage")}


def window(usage: dict | None, key: str) -> dict | None:
    w = ((usage or {}).get("usage") or {}).get(key)
    if not isinstance(w, dict) or w.get("usedPercent") is None:
        return None
    return {"used": round(float(w["usedPercent"]), 1), "resets": iso_epoch(w.get("resetsAt")), "window_min": w.get("windowMinutes")}


def provider_cost(usage: dict | None) -> dict | None:
    pc = ((usage or {}).get("usage") or {}).get("providerCost")
    if not isinstance(pc, dict):
        return None
    return {"used": _f(pc.get("used")), "limit": _f(pc.get("limit")), "balance": _f(pc.get("balance")),
            "currency": pc.get("currencyCode") or "", "period": pc.get("period") or ""}


def detail_rows(usage: dict | None) -> list:
    out = []
    for blk in ((usage or {}).get("usage") or {}).get("details") or []:
        for r in (blk or {}).get("rows") or []:
            if isinstance(r, dict) and r.get("label"):
                out.append({"label": str(r["label"]), "value": str(r.get("value") or ""), "extra": r.get("secondaryValue")})
    return out


def month_pace(used: float | None, resets: float | None, now: float) -> dict | None:
    """Linear monthly pace (CodexBar 0.83 Copilot monthly pace): window = previous month start .. resets."""
    if used is None or not resets or resets <= now:
        return None
    r = datetime.fromtimestamp(resets, ET)
    start = (r.replace(day=1) - timedelta(days=1)).replace(day=1)
    if r.day != 1:
        start = r.replace(day=1)
    dur = resets - start.timestamp()
    if dur <= 0:
        return None
    elapsed = max(0.0, now - start.timestamp())
    expected = max(0.0, min(100.0, elapsed / dur * 100))
    eta = None
    last = used <= 0
    if used > 0 and elapsed > 0:
        cand = (100 - used) / (used / elapsed)
        last = cand >= resets - now
        eta = None if last else cand
    return {"expected": round(expected, 1), "delta": round(used - expected, 1), "eta_s": eta, "will_last": last,
            "summary": "%s%% %s | Expected %d%% used" % (abs(round(used - expected)), "in deficit" if used > expected else "in reserve", round(expected))}


# ---------------- CodeRabbit band ----------------

def cr_counts(text: str, now: float) -> dict:
    full7 = cli_hour = 0
    for line in (text or "").splitlines():
        cols = line.split("\t")
        if len(cols) < 5:
            continue
        ts = iso_epoch(cols[0])
        if ts is None:
            continue
        if cols[1] in ("full", "full-priority") and cols[2] != "-" and "requested" in cols[4] and now - ts < 7 * 86400:
            full7 += 1
        if cols[1] == "cli" and now - ts < 3600:
            cli_hour += 1
    return {"full_7d": full7, "cli_hour": cli_hour}


def cr_band(full_7d: int, s: dict) -> dict:
    tgt, slow = int(s["weekly_full_target"]), int(s["weekly_full_slowdown"])
    if full_7d >= slow:
        state, note = "bad", "At or over %d reviews in 7 days: CodeRabbit may slow the seat to 1 review/hour" % slow
    elif full_7d >= tgt:
        state, note = "warn", "Over the %d/week target; requests should wait for green heads" % tgt
    else:
        state, note = "good", "Under the %d/week target" % tgt
    return {"state": state, "note": note, "target": tgt, "slowdown": slow, "full_7d": full_7d}


# ---------------- Integration tests windows ----------------

def diesel_windows(times: list, now: float) -> dict:
    today = datetime.fromtimestamp(now, ET)
    slots = []
    for t in times or []:
        try:
            hh, mm = [int(x) for x in str(t).split(":")[:2]]
        except ValueError:
            continue
        slots.append((hh, mm))
    nxt = None
    for add in (0, 1):
        d = today + timedelta(days=add)
        for hh, mm in sorted(slots):
            cand = d.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
            if cand > now and nxt is None:
                nxt = cand
    lbl = ["%d %s" % ((hh % 12) or 12, "AM" if hh < 12 else "PM") if not mm else "%d:%02d %s" % ((hh % 12) or 12, mm, "AM" if hh < 12 else "PM")
           for hh, mm in sorted(slots)]
    return {"label": " and ".join(lbl) + " ET" if lbl else "not set", "next": nxt}


# ---------------- assemble ----------------

def _clock(name, used, resets, period, pace=None, note=""):
    return {"name": name, "used": used, "resets": resets, "period": period, "pace": pace, "note": note}


def _day_epoch(s: str, hour: int = 0):
    try:
        y, m, d = [int(x) for x in str(s).split("-")]
        return datetime(y, m, d, hour, tzinfo=ET).timestamp()
    except (ValueError, TypeError):
        return None


def build(cache: dict | None, now: float | None = None, cfg: dict | None = None, ledger_text: str | None = None) -> dict:
    now = now or time.time()
    s = settings(cfg)
    cache = cache or {}
    u, c = cache.get("usage") or {}, cache.get("cost") or {}
    cx, cu, cp, gk, cr = (u.get(k) or {} for k in ("codex", "cursor", "copilot", "grok", "coderabbit"))
    cards, clocks = [], []

    # Cursor / Grok Configured shared usage (one weekly pool for Cursor, Grok, Grok Bot, Bugbot)
    uw = window(cu, "primary") or window(gk, "primary")
    cc = c.get("cursor") or {}
    tot = cc.get("totals") or {}
    gb_models = [m for m in model_totals(cc) if m["model"].startswith("grok-bot")]
    gb_cost = sum(m["cost"] for m in gb_models)
    ultra_reset = (uw or {}).get("resets") or _day_epoch(s["workflow"]["ultra_resets"], 22)
    cards.append({
        "name": "Cursor / Grok Configured shared usage", "tone": "cursor", "role": "Cloud agents (cap %d concurrent), Grok Build, Grok Bot, Bugbot pilot" % s["workflow"]["cloud_agent_cap"],
        "plan": "Configured account plan", "used": (uw or {}).get("used"), "resets": ultra_reset, "period": "weekly",
        "rows": [
            ["Grok Bot share (30 d)", "$%.0f API value across %s" % (gb_cost, ", ".join(m["model"] for m in gb_models)) if gb_models else "not reported"],
            ["Cache read / write tokens (30 d)", "%s / %s" % (htok(tot.get("cacheReadTokens")), htok(tot.get("cacheCreationTokens"))) if tot else "not reported"],
            ["Metered vs total (30 d)", "$%.0f metered of $%.0f (%s)" % (_f(cc.get("meteredCostUSD")) or 0, _f(cc.get("last30DaysCostUSD")) or 0, cc.get("provenance") or "?") if cc else "not reported"],
            ["Grok credits (balance / limit / used)", "%s" % (lambda pc: "%s %g / %g / %g" % (pc["currency"], pc["balance"] or 0, pc["limit"] or 0, pc["used"] or 0) if pc else "not reported")(provider_cost(gk))],
            ["Watch", "Front-load Sun–Wed; keep a 20% reserve"],
        ],
        "models": model_totals(cc)[:5], "daily": model_daily(cc, 7), "status": "live" if uw else "no data",
        "source": "CodexBar 0.83 cursor/grok usage + cursor cost"})
    clocks.append(_clock("Configured shared usage", (uw or {}).get("used"), ultra_reset, "weekly"))

    # Codex
    cw = window(cx, "secondary") or window(cx, "primary")
    cpace = cb_pace(cx, "secondary") or cb_pace(cx, "primary")
    rc = reset_credits(cx)
    xc = c.get("codex") or {}
    cov = xc.get("coverage") or {}
    cards.append({
        "name": "Codex", "tone": "codex", "role": "Configured coding and review usage",
        "plan": "ChatGPT (%s)" % (((cx.get("usage") or {}).get("loginMethod")) or "plan unknown"), "used": (cw or {}).get("used"),
        "resets": (cw or {}).get("resets"), "period": "weekly", "pace": cpace,
        "rows": [
            ["Reset credits", ("%s available · next expires %s" % (rc["available"], _et(rc["next_expires"]))) if rc else "not reported"],
            ["Price coverage (30 d)", "%s priced / %s estimated / %s unpriced" % (cov.get("priced", 0), cov.get("estimated", 0), cov.get("unpriced", 0)) if cov else "not reported"],
            ["Reasoning / cache-read tokens (30 d)", "%s / %s" % (htok((xc.get("totals") or {}).get("reasoningTokens")), htok((xc.get("totals") or {}).get("cacheReadTokens"))) if xc else "not reported"],
        ],
        "models": model_totals(xc)[:5], "daily": model_daily(xc, 7), "projects": project_costs(xc),
        "status": "live" if cw else "no data", "source": "CodexBar 0.83 codex usage (pace, resetCredits) + cost"})
    clocks.append(_clock("Codex", (cw or {}).get("used"), (cw or {}).get("resets"), "weekly", cpace))

    # Xcode Cloud (pilot)
    xs = s["xcode_cloud"]
    manual = xs["hours_used_manual"] if isinstance(xs["hours_used_manual"], (int, float)) and xs["hours_used_manual"] >= 0 else None
    inc = xs["included_hours"]
    inc = inc if isinstance(inc, (int, float)) and inc > 0 else None
    nm = datetime.fromtimestamp(now, ET).replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)
    month_reset = nm.replace(day=1).timestamp()
    cards.append({
        "name": "Xcode Cloud", "tone": "xcode", "role": "Configured cloud build integration; device acceptance remains a separate gate",
        "plan": ("Configured %g compute hours/month" % inc) if inc else "Compute allowance not configured", "used": round(manual / inc * 100, 1) if manual is not None and inc else None,
        "resets": month_reset, "period": "monthly", "status": "pending credential",
        "rows": [["Hours used", ("%g of %g h (manual entry)" % (manual, inc)) if manual is not None and inc else "not reported; configure allowance and source"],
                 ["Workflow A “PR Ready”", "Configured by the operator"],
                 ["Workflow B (TestFlight)", "Pending the App Store Connect app record"],
                 ["Budget", "Not reported; no run-time or throughput estimate is inferred"],
                 ["Physical devices", "Not supported: explicit device acceptance remains a separate gate"]],
        "pending": ["App Store Connect API key (Developer role) for ciBuildRuns / actions / test results"],
        "source": XCODE_CLOUD_SOURCE})
    clocks.append(_clock("Xcode Cloud", round(manual / inc * 100, 1) if manual is not None and inc else None, month_reset, "monthly", note="pending credential" if manual is None else "manual"))

    # CodeRabbit
    cs = s["coderabbit"]
    if ledger_text is None:
        try:
            ledger_text = CR_LEDGER.read_text(errors="replace")
        except OSError:
            ledger_text = ""
    cnt = cr_counts(ledger_text, now)
    band = cr_band(cnt["full_7d"], cs)
    crd = {r["label"]: r["value"] for r in detail_rows(cr)}
    cli_reset = _day_epoch(crd.get("Period resets") or cs["cli_resets"])
    cards.append({
        "name": "CodeRabbit", "tone": "coderabbit", "role": "Required approver: one full review per green head; CLI previews before push",
        "plan": "Current seat (unchanged)", "used": round(min(100.0, cnt["full_7d"] / max(1, band["target"]) * 100), 1),
        "resets": cli_reset, "period": "7-day rolling (PR) · CLI billing period", "band": band,
        "rows": [["Full-review requests logged, last 7 days", "%d vs %d target (slowdown at %d)" % (cnt["full_7d"], band["target"], band["slowdown"])],
                 ["CLI previews this hour", "%d of %d (configurable: [coderabbit] cli_per_hour)" % (cnt["cli_hour"], cs["cli_per_hour"])],
                 ["CLI billing", "%s reviews · %s · resets %s" % (crd.get("Reviews", "?"), crd.get("Usage billing", "?"), crd.get("Period resets", cs["cli_resets"]))],
                 ["Limits source", CR_RATE_SOURCE]],
        "status": "live", "source": "cr-queue ledger + CodexBar 0.83 coderabbit usage"})
    clocks.append(_clock("CodeRabbit (7-day)", round(min(100.0, cnt["full_7d"] / max(1, band["target"]) * 100), 1), cli_reset, "rolling", note=band["note"]))

    # Bugbot pilot
    ws = s["workflow"]
    bstart = _day_epoch(ws["bugbot_pilot_start"])
    bend = (datetime.fromtimestamp(bstart, ET) + timedelta(days=int(ws["bugbot_pilot_days"]))).timestamp() if bstart else None
    cards.append({
        "name": "Bugbot", "tone": "bugbot", "role": "One-week pilot: keep only if it finds what Codex, CodeRabbit and DeepSource miss",
        "plan": "Configured shared usage (no separate meter in CodexBar)", "used": None, "no_meter": True,
        "headline": pilot_line(bstart, int(ws["bugbot_pilot_days"]), now),
        "note": "No Bugbot usage meter available.",
        "resets": None, "period": "pilot", "status": "pilot",
        "rows": [["Pilot window", "%s → %s" % (_et(bstart), _et(bend))], ["Cost", "Counts against the Configured shared usage"]],
        "source": "Workflow decision 7 (Nov 1)"})
    # xAI / Grok API (Management API, read-only)
    from . import xai_mgmt
    xs2 = s["xai"]
    xai_on = bool(xs2["key_file"] and xs2["team_id"])
    if xai_on:
        xai_mgmt.ensure_poller(xs2)
    xc = xai_mgmt.card(xai_mgmt.read_cache(xai_mgmt.CACHE) if xai_on else {}, xs2, now)
    cards.append(xc)
    clocks.append(_clock("xAI API (monthly)", xc.get("used"), xc.get("resets"), "monthly",
                         note="spend vs prepaid credit + soft limit" if xc.get("used") is not None else xc.get("status", "")))
    pending = [{"panel": "Xcode Cloud runs and hours", "credential": "App Store Connect API key (Developer role)"},
               ]
    if not xai_on:
        pending.append({"panel": "xAI API spend by model (optional)", "credential": "xAI Management key + team ID"})
    return {"cards": cards, "clocks": clocks, "diesel": diesel_windows(ws["diesel_windows"], now),
            "pending": pending,
            "as_of": now, "codexbar_at": max((cache.get("at") or {}).values() or [0]) or None}


def pilot_line(start: float | None, days: int, now: float) -> str:
    """'Pilot day N of 7' by ET calendar day; elapsed time is not usage, so no percent."""
    if not start:
        return "Pilot dates not set"
    d0 = datetime.fromtimestamp(start, ET)
    end = d0 + timedelta(days=days)
    if now < start:
        return "Pilot starts %s" % d0.strftime("%b %-d")
    if now >= end.timestamp():
        return "Pilot ended %s, decision due" % end.strftime("%b %-d")
    return "Pilot day %d of %d" % ((datetime.fromtimestamp(now, ET).date() - d0.date()).days + 1, days)


def _et(epoch) -> str:
    if not epoch:
        return "unknown"
    return datetime.fromtimestamp(epoch, ET).strftime("%b %-d, %-I:%M %p ET")
