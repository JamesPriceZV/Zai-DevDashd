import json
import re
from pathlib import Path

from zai_devdashd import resources_v2 as r
from zai_devdashd.hwinfo import cpu_text

NOW = 1793506800.0  # 2026-11-01 00:20 ET


def cache():
    return {
        "usage": {
            "codex": {"usage": {"secondary": {"usedPercent": 60, "resetsAt": "2026-11-04T12:26:15Z", "windowMinutes": 10080},
                                "loginMethod": "prolite",
                                "codexResetCredits": {"availableCount": 1, "credits": [{"status": "available", "expires_at": "2026-11-30T20:29:00Z"}]}},
                      "pace": {"secondary": {"expectedUsedPercent": 53, "deltaPercent": 7, "etaSeconds": 241387, "willLastToReset": False}},
                      "resetCredits": {"nextExpiresAt": "2026-11-30T20:29:00Z", "available": 1}},
            "cursor": {"usage": {"primary": {"usedPercent": 0.6, "resetsAt": "2026-11-08T03:40:26Z", "windowMinutes": 10080}}},
            "copilot": {"usage": {"loginMethod": "Individual", "secondary": {"usedPercent": 10, "resetsAt": "2026-12-01T00:00:00Z"},
                                  "details": [{"rows": [{"label": "Credits used", "value": "0", "secondaryValue": "Nov 30"}]}]}},
            "grok": {"usage": {"providerCost": {"balance": 3, "limit": 50, "used": 1, "currencyCode": "USD"}}},
            "coderabbit": {"usage": {"details": [{"rows": [{"label": "Reviews", "value": "700"}, {"label": "Period resets", "value": "2026-11-12"}]}]}},
        },
        "cost": {
            "codex": {"daily": [{"date": "2026-11-01", "totalTokens": 10, "totalCost": 2.0,
                                 "modelBreakdowns": [{"modelName": "gpt-6.1-sol", "totalTokens": 10, "cost": 2.0}]}],
                      "projects": [{"path": "/Users/x/secret/ExampleProject", "totalTokens": 5, "totalCost": 1.5}],
                      "coverage": {"priced": 14}},
            "cursor": {"daily": [{"date": "2026-10-31", "totalTokens": 9, "totalCost": 3.0, "cacheCreationTokens": 4,
                                  "modelBreakdowns": [{"modelName": "grok-bot-default", "totalTokens": 9, "cost": 3.0}]}],
                       "totals": {"cacheReadTokens": 100, "cacheCreationTokens": 4}},
        },
        "at": {"usage:codex": NOW - 60},
    }


def test_cpu_label_activity_monitor_style():
    assert r.cpu_label([{"name": "Super", "cores": 6}, {"name": "Performance", "cores": 12}]) == "18-core: 12 Performance + 6 Super"
    assert r.cpu_label([{"name": "Performance", "cores": 4}, {"name": "Efficiency", "cores": 4}]) == "8-core: 4 Performance + 4 Efficiency"
    assert r.cpu_label([]) == ""
    assert cpu_text({"cpu_cores": 18, "perflevels": [{"name": "Super", "cores": 6}, {"name": "Performance", "cores": 12}]}) == "18-core: 12 Performance + 6 Super"
    assert cpu_text({"cpu_cores": 8, "cpu_breakdown": "4 P and 4 E"}) == "8-core (4 P and 4 E)"


def test_reset_credits_and_projects_are_redacted():
    rc = r.reset_credits(cache()["usage"]["codex"])
    assert rc["available"] == 1 and rc["next_expires"] > NOW
    pr = r.project_costs(cache()["cost"]["codex"])
    assert pr == [{"project": "ExampleProject", "tokens": 5, "cost": 1.5}]


def test_model_daily_and_totals():
    d = r.model_daily(cache()["cost"]["cursor"])
    assert d[0]["cache_write"] == 4 and d[0]["models"][0]["model"] == "grok-bot-default"
    assert r.model_totals(cache()["cost"]["codex"])[0]["cost"] == 2.0


def test_month_pace_and_cr_band():
    p = r.month_pace(10, 1796101200.0, NOW)  # Dec 1 00:00 ET
    assert p and p["expected"] < 5 and p["delta"] > 0
    s = r.settings({})["coderabbit"]
    assert s["cli_per_hour"] == 5
    assert r.cr_band(10, s)["state"] == "good" and r.cr_band(31, s)["state"] == "warn" and r.cr_band(60, s)["state"] == "bad"
    assert r.settings({"coderabbit": {"cli_per_hour": 7}})["coderabbit"]["cli_per_hour"] == 7


def test_cr_counts():
    t = "2026-11-01T03:00:00Z\tfull\t#1\tabc\trequested\n2026-11-01T04:10:00Z\tcli\t-\t-\tok\n2026-10-01T00:00:00Z\tfull\t#2\tx\trequested\n"
    c = r.cr_counts(t, NOW)
    assert c == {"full_7d": 1, "cli_hour": 1}


def test_diesel_windows():
    d = r.diesel_windows(["09:00", "18:00"], NOW)
    assert d["label"] == "9 AM and 6 PM ET" and d["next"] - NOW < 10 * 3600  # Nov 1 is the DST fall-back day


def test_build_cards():
    out = r.build(cache(), NOW, {}, ledger_text="")
    names = [c["name"] for c in out["cards"]]
    assert names == ["Cursor / Grok Configured shared usage", "Codex", "Xcode Cloud", "CodeRabbit", "Bugbot", "xAI / Grok API"]
    xc = out["cards"][2]
    assert xc["status"] == "pending credential" and xc["used"] is None
    assert len(out["pending"]) == 2  # Copilot billing panel dropped (ignored)
    blob = json.dumps(out)
    assert "/Users/" not in blob
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", blob)


def test_bugbot_pilot_day_no_meter():
    from zai_devdashd import resources_v2 as r
    from zai_devdashd.usage import iso_epoch
    s = iso_epoch("2026-11-01T04:00:00Z")
    assert r.pilot_line(s, 7, iso_epoch("2026-11-01T05:56:00Z")) == "Pilot day 1 of 7"
    assert r.pilot_line(s, 7, iso_epoch("2026-11-08T04:59:00Z")) == "Pilot day 7 of 7"
    assert r.pilot_line(s, 7, iso_epoch("2026-11-08T05:00:00Z")) == "Pilot ended Nov 8, decision due"
    v = r.build({}, now=iso_epoch("2026-11-03T15:00:00Z"), cfg={"workflow": {"bugbot_pilot_start": "2026-11-01", "bugbot_pilot_days": 7}})
    b = next(c for c in v["cards"] if c["name"] == "Bugbot")
    assert b["used"] is None and b["no_meter"] and b["headline"] == "Pilot day 3 of 7"
    assert b["note"] == "No Bugbot usage meter available." and b["status"] == "pilot"
    assert [x[0] for x in b["rows"]] == ["Pilot window", "Cost"] and b["source"]
    x = next(c for c in v["cards"] if c["name"] == "Xcode Cloud")
    rows = dict(x["rows"])
    assert rows["Workflow A \u201cPR Ready\u201d"] == "Configured by the operator" and "app record" in rows["Workflow B (TestFlight)"]
    assert "simulator tests," not in x["role"]
