"""xAI Management API collector (read-only) for the Resource Utilization block.

Allow-listed calls only: four documented GETs and the documented read-only POST usage query
(https://docs.x.ai/docs/management-api/billing). No write/create/delete/rotate/update/export endpoint is reachable
from this module. The key is read from a 0600 file named in [xai] key_file and is never logged or cached. API key
values, redacted values, hashes and user ids are dropped before anything is cached; only key names and status stay.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .usage import ET, USAGE_DIR
from zai_devdashd import config as site

BASE = "https://management-api.x.ai"
DOCS = "https://docs.x.ai/docs/management-api/billing"
CACHE = USAGE_DIR / "xai-mgmt.json"
EVERY_S = 900
KEY_DROP = {"apiKey", "redactedApiKey", "apiKeyHash", "userId", "createUser", "modifyUser", "aclStrings", "team"}
TEAM_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _get_paths(team: str) -> dict:
    return {
        "balance": "/v1/billing/teams/%s/prepaid/balance" % team,
        "limits": "/v1/billing/teams/%s/postpaid/spending-limits" % team,
        "preview": "/v1/billing/teams/%s/postpaid/invoice/preview" % team,
        "invoices": "/v1/billing/teams/%s/invoices" % team,
        "keys": "/auth/teams/%s/api-keys" % team,
    }


def usage_path(team: str) -> str:
    return "/v1/billing/teams/%s/usage" % team


GET_SUFFIXES = ("/prepaid/balance", "/postpaid/spending-limits", "/postpaid/invoice/preview", "/invoices", "/api-keys")


def is_allowed(path: str, post: bool) -> bool:
    """Read-only allow-list: the documented GETs above, and POST only for the usage analytics query."""
    m = re.match(r"^/(v1/billing|auth)/teams/[0-9a-fA-F-]{36}(/.*)$", path or "")
    if not m or ".." in path:
        return False
    tail = m.group(2)
    if post:
        return m.group(1) == "v1/billing" and tail == "/usage"
    return tail in GET_SUFFIXES and (tail == "/api-keys") == (m.group(1) == "auth")


def read_key(path) -> str | None:
    try:
        p = Path(os.path.expanduser(str(path or "")))
        if not path or not p.is_file():
            return None
        v = p.read_text().strip()
        return v or None
    except OSError:
        return None


def _request(key: str, path: str, body: dict | None = None, timeout: int = 20):
    if not is_allowed(path, body is not None):
        raise ValueError("path not allow-listed")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if body is not None else "GET",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def usage_query(start: str, end: str, group: str, unit: str = "TIME_UNIT_NONE") -> dict:
    return {"analyticsRequest": {"timeRange": {"startTime": start, "endTime": end, "timezone": "America/New_York"},
                                 "timeUnit": unit, "values": [{"name": "usd", "aggregation": "AGGREGATION_SUM"}],
                                 "groupBy": [group], "filters": []}}


def month_window(now: float):
    d = datetime.fromtimestamp(now, ET)
    start = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt


def clean_keys(raw: dict | None) -> list:
    out = []
    for k in (raw or {}).get("apiKeys") or []:
        if isinstance(k, dict):
            out.append({"id": k.get("apiKeyId"), "name": str(k.get("name") or "unnamed"), "disabled": bool(k.get("disabled")),
                        "created": k.get("createTime"), "expires": k.get("expireTime")})
    return out


def _strip_label(s: str) -> str:
    return re.sub(r"\s*\(xai-[^)]*\)\s*$", "", str(s or "")).strip()


def series_totals(raw: dict | None) -> list:
    rows = []
    for ts in (raw or {}).get("timeSeries") or []:
        if not isinstance(ts, dict):
            continue
        total = 0.0
        for dp in ts.get("dataPoints") or []:
            for v in (dp or {}).get("values") or []:
                if isinstance(v, (int, float)):
                    total += float(v)
        lbl = (ts.get("groupLabels") or ts.get("group") or ["?"])[0]
        rows.append({"label": _strip_label(lbl), "id": (ts.get("group") or [None])[0], "usd": round(total, 6)})
    rows.sort(key=lambda r: -r["usd"])
    return rows


def collect(key: str, team: str, now: float, req=_request) -> dict:
    """One read-only sweep. Each endpoint fails independently; errors carry only the HTTP status / class name."""
    paths = _get_paths(team)
    out = {"at": now, "ok": {}, "errors": {}}
    for name, path in paths.items():
        try:
            out[name] = req(key, path)
            out["ok"][name] = True
        except urllib.error.HTTPError as e:
            out["errors"][name] = "HTTP %s" % e.code
        except Exception as e:  # noqa: BLE001
            out["errors"][name] = e.__class__.__name__
    if "keys" in out:
        out["keys"] = clean_keys(out["keys"])
    if "invoices" in out:
        inv = []
        for i in (out["invoices"] or {}).get("invoices") or []:
            cyc = ((i.get("monthly") or {}).get("billingCycle") or {})
            inv.append({"number": i.get("invoiceNumber") or i.get("invoice_number"), "status": i.get("invoiceStatus"),
                        "created": i.get("createTime"), "total_cents": i.get("total"), "cycle": cyc,
                        "prepaid_used_cents": ((i.get("monthly") or {}).get("prepaidTokensToSpend") or {}).get("val")})
        out["invoices"] = inv[:6]
    if "balance" in out:
        b = out["balance"] or {}
        out["balance"] = {"total_cents": ((b.get("total") or {}).get("val")),
                          "purchases": sum(1 for c in b.get("changes") or [] if c.get("changeOrigin") in ("PURCHASE", "AUTO_PURCHASE"))}
    ms, me = month_window(now)
    fmt = "%Y-%m-%d %H:%M:%S"
    d30 = datetime.fromtimestamp(now, ET) - timedelta(days=30)
    end = (datetime.fromtimestamp(now, ET) + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    for name, start, group in (("month_by_model", ms.strftime(fmt), "description"),
                               ("d30_by_model", d30.strftime("%Y-%m-%d 00:00:00"), "description"),
                               ("d30_by_key", d30.strftime("%Y-%m-%d 00:00:00"), "api_key_id"),
                               ("all_by_model", "2024-01-01 00:00:00", "description")):
        try:
            out[name] = series_totals(req(key, usage_path(team), usage_query(start, end, group)))
            out["ok"][name] = True
        except urllib.error.HTTPError as e:
            out["errors"][name] = "HTTP %s" % e.code
        except Exception as e:  # noqa: BLE001
            out["errors"][name] = e.__class__.__name__
    return out


def read_cache(path: Path = CACHE) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


class Poller:
    """Background refresh every EVERY_S into CACHE (no secrets in the cache)."""

    def __init__(self, key_file: str, team: str, path: Path = CACHE, every: int = EVERY_S):
        self.key_file, self.team, self.path, self.every = key_file, team, path, max(300, int(every or EVERY_S))
        self.started = False
        self.lock = threading.Lock()

    def start(self):
        if (not site.adapter_enabled("usage") or not self.key_file or not self.team
                or os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ZAI_DEVDASHD_NO_XAI_MGMT")):
            return
        with self.lock:
            if self.started:
                return
            self.started = True
        threading.Thread(target=self.loop, daemon=True, name="xai-mgmt").start()

    def loop(self):
        while True:
            try:
                self.refresh(time.time())
            except Exception:
                pass
            time.sleep(60)

    def refresh(self, now: float, force: bool = False):
        cur = read_cache(self.path)
        if not force and now - float(cur.get("at") or 0) < self.every:
            return cur
        key = read_key(self.key_file)
        if not key or not TEAM_RE.match(self.team or ""):
            data = {"at": now, "ok": {}, "errors": {"config": "key file or team id missing"}}
        else:
            data = collect(key, self.team, now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        return data


_POLLER: Poller | None = None


def ensure_poller(s: dict) -> None:
    global _POLLER
    if _POLLER is None and s.get("key_file") and s.get("team_id"):
        _POLLER = Poller(s["key_file"], s["team_id"], every=s.get("poll_s") or EVERY_S)
        _POLLER.start()


def _cents(v) -> float | None:
    try:
        return int(str(v)) / 100.0
    except (TypeError, ValueError):
        return None


def _usd(v) -> str:
    return "unavailable" if v is None else "$%.2f" % v


def card(x: dict | None, s: dict, now: float) -> dict:
    """Grok API (xAI) card. Missing endpoints show 'unavailable'; nothing is estimated."""
    x = x or {}
    configured = bool(s.get("key_file") and s.get("team_id"))
    ok = x.get("ok") or {}
    base = {"name": "xAI / Grok API", "tone": "grok", "role": "Direct xAI API usage (inference key) billed to the team",
            "period": "monthly", "source": "xAI Management API (read-only) · " + DOCS}
    if not configured:
        return dict(base, status="pending credential", used=None, plan="", rows=[["Status", "pending credential (xAI Management key + team ID)"]])
    if not ok:
        err = ", ".join("%s: %s" % kv for kv in (x.get("errors") or {}).items()) or "not collected yet"
        return dict(base, status="unavailable", used=None, plan="", rows=[["Status", "unavailable (%s)" % err]], as_of=x.get("at"))
    ms, me = month_window(now)
    month = x.get("month_by_model") if ok.get("month_by_model") else None
    spend = round(sum(r["usd"] for r in month), 4) if month is not None else None
    lim = ((x.get("limits") or {}).get("spendingLimits") or {}) if ok.get("limits") else None
    soft = _cents(((lim or {}).get("effectiveSl") or {}).get("val")) if lim is not None else None
    hard = _cents(((lim or {}).get("effectiveHardSl") or {}).get("val")) if lim is not None else None
    bal_c = _cents((x.get("balance") or {}).get("total_cents")) if ok.get("balance") else None
    credit = None if bal_c is None else max(0.0, -bal_c)
    pv = (x.get("preview") or {}) if ok.get("preview") else None
    due = _cents(((pv or {}).get("coreInvoice") or {}).get("amountAfterVat")) if pv is not None else None
    cap = (credit or 0) + (soft or 0) if (credit is not None and soft is not None) else None
    used = round(min(100.0, spend / cap * 100), 1) if (spend is not None and cap) else (0.0 if spend == 0 else None)
    keys = x.get("keys") if ok.get("keys") else None
    names = {k["id"]: k["name"] for k in keys or []}
    rows = [["Spend this month (%s)" % ms.strftime("%b %Y"), _usd(spend)],
            ["Prepaid credit balance", _usd(credit)],
            ["Postpaid due this cycle", _usd(due)],
            ["Monthly spending limit", "unavailable" if lim is None else "soft %s · hard %s" % (_usd(soft), _usd(hard))]]
    if keys is not None:
        rows.append(["API keys", ", ".join("%s (%s)" % (k["name"], "disabled" if k["disabled"] else "active") for k in keys) or "none"])
    if ok.get("d30_by_key"):
        rows.append(["Last 30 days by key", ", ".join("%s %s" % (names.get(r["id"], r["label"]), _usd(r["usd"])) for r in x["d30_by_key"]) or "$0.00"])
    inv = x.get("invoices") if ok.get("invoices") else None
    if inv:
        i = inv[0]
        c = i.get("cycle") or {}
        rows.append(["Last invoice", "%s-%02d · %s · %s" % (c.get("year", "?"), int(c.get("month") or 0), i.get("status") or "?", _usd(_cents(i.get("total_cents"))))])
    if credit == 0 and soft == 0:
        rows.append(["Warning", "No prepaid credit and a $0 soft limit: per xAI docs the API stops serving requests until credit is added or the limit is raised"])
    errs = x.get("errors") or {}
    if errs:
        rows.append(["Unavailable", ", ".join("%s (%s)" % kv for kv in errs.items())])
    models = [{"model": r["label"], "tokens": None, "cost": r["usd"]} for r in (x.get("d30_by_model") or []) if ok.get("d30_by_model")]
    lifetime = [{"model": "%s (all time)" % r["label"], "tokens": None, "cost": r["usd"]} for r in (x.get("all_by_model") or [])[:8]]
    return dict(base, status="live" if not errs else "partial", used=used, resets=me.timestamp(),
                plan="Prepaid %s + postpaid soft limit %s" % (_usd(credit), _usd(soft)),
                rows=rows, models=models + lifetime, as_of=x.get("at"),
                xai={"spend_month_usd": spend, "credit_usd": credit, "postpaid_due_usd": due, "soft_limit_usd": soft,
                     "hard_limit_usd": hard, "keys": keys, "by_model_30d": x.get("d30_by_model"),
                     "by_key_30d": x.get("d30_by_key"), "by_model_all": x.get("all_by_model"), "errors": errs,
                     "updated": x.get("at")})
