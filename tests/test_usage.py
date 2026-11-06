"""Parsers for the zero-cost utilization panel, against fake fixtures only."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from zai_devdashd import usage
from zai_devdashd.aggregator import codex_cwds, mini_zai

FIX = Path(__file__).parent / "fixtures" / "usage"
NOW = usage.iso_epoch("2026-01-02T03:30:00Z")


def test_codex_rollout_takes_last_token_count():
    out = usage.parse_codex_rollout(FIX / "rollout-fake.jsonl")
    assert out["cwd"] == "/synthetic/worktrees/codex-pr9001-x"
    assert out["tokens"] == 2500
    assert out["rate"]["primary"]["used_percent"] == 12.5


def test_codex_row_fields():
    s = usage.parse_codex_rollout(FIX / "rollout-fake.jsonl")
    row = usage.codex_row([s], NOW)
    assert row["usage"] == "12.5% used (weekly)"
    assert row["plan"] == "testplan"
    assert row["resets"].endswith("ET")
    assert row["alert"] == "" and row["stale"] is False


def test_codex_row_exhausted_and_missing():
    s = usage.parse_codex_rollout(FIX / "rollout-fake.jsonl")
    s["rate"] = dict(s["rate"], rate_limit_reached_type="primary")
    assert usage.codex_row([s], NOW)["alert"] == "USAGE EXHAUSTED"
    assert usage.codex_row([], NOW)["usage"] == usage.NA


def test_codex_lanes_queue_tokens(tmp_path):
    lanes = tmp_path / "codex-1"
    lanes.mkdir()
    (lanes / "queue.sh").write_text('Q=("A 9001" "B 9002" "C 9003")\n')
    status = (FIX / "status.md").read_text().replace("LOGA", str(tmp_path / "a.log")).replace("LOGB", str(tmp_path / "b.log"))
    (tmp_path / "status.md").write_text(status)
    (tmp_path / "a.log").write_text("first\nlast line sk-abcdefghijklmnopqrstuvwxyz0123 me@example.com\n\n")
    s = usage.parse_codex_rollout(FIX / "rollout-fake.jsonl")
    out = usage.codex_lanes([s], usage.iso_epoch("2026-01-02T03:35:00Z"), root=tmp_path, status_md=tmp_path / "status.md")
    by = {l["pr"]: l for l in out["lanes"]}
    assert by[9001]["state"] == "Done" and by[9001]["tokens"] == 2500 and by[9001]["elapsed"] == "30m"
    assert "example.com" not in by[9001]["last_line"]
    assert by[9002]["state"] == "Running" and by[9002]["tokens"] is None
    assert out["queue"] == [{"lane": "C", "pr": 9003}]
    assert out["exhausted"] is False
    (lanes / "USAGE-EXHAUSTED").touch()
    assert usage.codex_lanes([], NOW, root=tmp_path, status_md=tmp_path / "status.md")["exhausted"] is True


def test_grok_costs(tmp_path):
    shutil.copy(FIX / "grok-lane.jsonl", tmp_path / "x.jsonl")
    assert usage.parse_grok_costs(tmp_path / "x.jsonl") == [1.25, 0.5]
    row = usage.GrokReader([tmp_path]).row(usage.time.time())
    assert row["spend_total"] == 1.75 and row["limit"] == usage.NA


def test_cr_ledger(monkeypatch):
    monkeypatch.setitem(usage.site.CURRENT, "coderabbit", {"cli_per_hour": 5, "full_per_hour": 1})
    info = usage.parse_cr_ledger((FIX / "ledger.tsv").read_text(), NOW)
    assert info["last_full"] == usage.iso_epoch("2026-01-02T03:00:00Z")
    assert info["cli_hour"] == 1
    row = usage.coderabbit_row(NOW, ledger=FIX / "ledger.tsv", usage_dir=FIX / "none")
    assert "1/5" in row["usage"]  # CodeRabbit docs: 5 CLI reviews/hour and row["resets"].startswith("Next full slot")


def test_snapshots_manual_and_stale(tmp_path):
    (tmp_path / "grokbot.json").write_text(json.dumps({
        "as_of": "2025-12-30T00:00:00Z", "plan": "P", "weekly_used_percent": 25.5,
        "resets_at": "2026-01-05T00:00:00Z", "top_consumers": [{"name": "r", "percent": 15}]}))
    row = usage.grokbot_row(NOW, tmp_path)
    assert row["manual"] and row["stale"] and row["usage"].startswith("25.5%") and "r 15%" in row["detail"]
    assert usage.grokbot_row(NOW, tmp_path / "missing")["usage"] == usage.NA
    assert usage.cursor_row(NOW, tmp_path)["alert"] == ""


def test_cursor_old_pro_snapshot_ignored(tmp_path):
    (tmp_path / "cursor.json").write_text(json.dumps({"as_of": "2026-10-29T21:20:00-04:00", "status": "Parked: usage exhausted", "plan": "Cursor Pro"}))
    row = usage.cursor_row(usage.iso_epoch("2026-11-01T06:00:00Z"), tmp_path)
    assert row["plan"] == "Cursor Pro" and row["manual"] and row["stale"]
    (tmp_path / "cursor.json").write_text(json.dumps({"as_of": "2026-11-01T01:00:00-04:00", "status": "Limit hit"}))
    assert usage.cursor_row(usage.iso_epoch("2026-11-01T06:00:00Z"), tmp_path)["alert"] == "Limit hit"


def test_plan_line_no_nag():
    assert usage.plan_line({"plan": "Ultra (shared with Grok and X)", "plan_source": "manual"}) == "Plan: Ultra (shared with Grok and X) (manual)"
    assert usage.plan_line({}) == ""
    assert "fill plans.json" not in usage.plan_line({"plan": "X"}) and "price not known" not in usage.plan_line({"plan": "X"})
    assert usage.plan_line({"plan": "X", "price_usd": 20, "plan_source": "manual"}) == "Plan: X · $20.00/mo (manual)"


def test_github_row():
    rate = {"resources": {"core": {"limit": 5000, "remaining": 4900, "reset": NOW}, "graphql": {"limit": 5000, "remaining": 10, "reset": NOW}}}
    row = usage.github_row(NOW, rate, Path("/nonexistent"))
    assert "4900/5000" in row["usage"] and row["cost"] == usage.NA
    assert usage.github_row(NOW, None, Path("/nonexistent"))["usage"] == usage.NA


def test_mini_zai_and_codex_cwds():
    cmds = ["/opt/homebrew/bin/codex exec --skip-git-repo-check -C /tmp/wt/a # zai mention",
            "python run_zai.py --model x"]
    assert codex_cwds(cmds) == ["/tmp/wt/a"]
    assert mini_zai(cmds[:1]) is None
    assert mini_zai(cmds) is not None


def test_marker_vs_reading_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "gh_json", lambda args: None)
    monkeypatch.setitem(usage.site.CURRENT, "adapters", {"usage": True})
    c = usage.UsageCollector()
    c.no_cli_poller = True
    c.codex.root = tmp_path / "none"
    c.codexbar_path = tmp_path / "no-codexbar.json"
    out = c.collect()
    assert out["codex"]["reading_exhausted"] is False
    assert [r["name"] for r in out["rows"]] == ["Codex", "Grok Build", "Grok Bot", "Cursor", "CodeRabbit", "GitHub"]


def test_pace_deficit_and_runout():
    # 2 of 7 days elapsed, 50% used: 21.4% expected -> deficit; runs out before reset.
    now = 1000000.0
    p = usage.pace(50, now + 5 * 86400, 7 * 86400, now)
    assert p["expected_percent"] == 28.6 and p["delta_percent"] == 21.4
    assert p["runs_out_before_reset"] and abs(p["runout_epoch"] - (now + 2 * 86400)) < 1
    q = usage.pace(10, now + 5 * 86400, 7 * 86400, now)
    assert q["delta_percent"] < 0 and q["runs_out_before_reset"] is False
    assert usage.pace(None, now, 1, now) is None


def test_rates_and_call_cost():
    r = usage.load_rates(FIX / "pricing.json", "m-cheap")
    assert r["input"] == 2 and r["over_output"] == 15 and r["tier_tokens"] == 1000
    assert usage.load_rates(FIX / "pricing.json", "nope") is None
    # 100 uncached*2 + 400 cached*0.1 + 100 out*10 per 1M
    assert abs(usage.call_cost({"input_tokens": 500, "cached_input_tokens": 400, "output_tokens": 100}, r) - (200 + 40 + 1000) / 1e6) < 1e-12
    # above the tier: 1000*4 + 1000*0.2 + 100*15
    assert abs(usage.call_cost({"input_tokens": 2000, "cached_input_tokens": 1000, "output_tokens": 100}, r) - (4000 + 200 + 1500) / 1e6) < 1e-12


def test_token_events_dedupe_and_per_model(tmp_path):
    root = tmp_path / "sessions" / "2026" / "01" / "02"
    root.mkdir(parents=True)
    shutil.copy(FIX / "rollout-cost.jsonl", root / "rollout-x.jsonl")
    cc = usage.CodexCost(tmp_path / "sessions", FIX / "pricing.json")
    events, rates = cc.scan(NOW)
    assert rates is None  # default model gpt-6.1-sol is not in the fixture catalog
    assert [e[1] for e in events] == [600, 2100, 1000]
    assert abs(events[2][2] - 1000 * 10 / 1e6) < 1e-12  # priced at m-dear
    again, _ = cc.scan(NOW)  # incremental: nothing re-read
    assert len(again) == 3
    sp = usage.codex_spend(events, rates, NOW, usage.iso_epoch("2026-01-02T03:01:30Z"))
    assert sp["d30"]["tokens"] == 3700 and sp["window"]["tokens"] == 3100


def test_bucket_unknown_cost_is_not_invented():
    b = usage.bucket([(10.0, 5, None)], 0)
    assert b["tokens"] == 5 and b["cost"] is None


def test_executive_cards_plain_and_unknown():
    now = 1000000.0
    rows = [{"name": "Codex"}, {"name": "CodeRabbit", "usage": "Last full review x · CLI previews 2/8 this hour", "next_full_epoch": now + 600},
            {"name": "GitHub", "core_limit": 5000, "core_remaining": 4000, "core_reset": now + 60}]
    rate = {"primary": {"used_percent": 50, "window_minutes": 10080, "resets_at": now + 5 * 86400}}
    spend = {"today": {"tokens": 10, "cost": 1.5}, "d7": {"tokens": 1, "cost": 3.0}, "d30": {"tokens": 1, "cost": 4.0}, "window": {"tokens": 5, "cost": 2.0}}
    ex = usage.executive(rows, rate, spend, {"spend_total": 9, "spend_today": 0.5, "spend_7d": 1.0, "spend_30d": 2.0},
                         {"weekly_used_percent": 25.5, "resets_at": "1970-01-17T00:00:00Z", "as_of": "1970-01-12T00:00:00Z"}, now)
    cards = {c["name"]: c for c in ex["cards"]}
    assert list(cards) == ["Codex", "Grok Build", "Grok Bot", "Cursor", "CodeRabbit", "GitHub"]
    assert cards["Codex"]["percent"] == 50 and cards["Codex"]["pace"]["runs_out_before_reset"]
    assert cards["Cursor"]["state"] == "unknown" and cards["Cursor"]["percent"] is None
    assert cards["Cursor"]["headline"] == "Account usage: not reported yet"
    assert cards["CodeRabbit"]["percent"] == 25.0 and cards["GitHub"]["percent"] == 20.0
    assert ex["summary"]["today"] == 2.0 and ex["summary"]["d30"] == 6.0
    assert "Codex" in ex["summary"]["runs_out"]
    none = usage.executive([], None, None, None, None, now)
    assert {c["name"]: c["tone"] for c in none["cards"]}["Codex"] == "unknown"
    assert none["summary"]["today"] is None
    for c in none["cards"]:
        assert "/" not in c["headline"]


def test_codexbar_snapshot_and_enrich(tmp_path):
    now = usage.iso_epoch("2026-10-30T02:30:00Z")
    snap = {"entries": [
        {"provider": "cursor", "updatedAt": "2026-10-30T02:27:13Z",
         "primary": {"usedPercent": 97.0, "windowMinutes": 44640, "resetsAt": "2026-11-25T18:14:08Z"},
         "secondary": {"usedPercent": 100, "windowMinutes": 44640, "resetsAt": "2026-11-25T18:14:08Z"},
         "tokenUsage": {"sessionCostUSD": 1.5, "last30DaysCostUSD": 9.0},
         "dailyUsage": [{"dayKey": "2026-10-29", "totalTokens": 100, "costUSD": 1.5}, {"dayKey": "2026-10-01", "totalTokens": 50, "costUSD": 7.5}],
         "usageRows": [{"id": "cursor-grok-bot", "window": {"usedPercent": 28.0, "windowMinutes": 10080, "resetsAt": "2026-11-04T23:20:21Z"}}]},
        {"provider": "copilot", "updatedAt": "2026-10-30T02:27:13Z", "secondary": {"usedPercent": 100, "resetsAt": "2026-11-01T00:00:00Z"},
         "details": [{"rows": [{"label": "Credits used", "value": "200"}]}]},
        {"provider": "grok", "updatedAt": "2026-10-30T02:27:13Z", "primary": {"usedPercent": 64, "windowMinutes": 10080, "resetsAt": "2026-11-03T13:08:28Z"}},
        {"provider": "coderabbit", "details": [{"rows": [{"label": "Reviews", "value": "607"}, {"label": "Period resets", "value": "2026-11-12"}, {"label": "Owner", "value": "a@b.com"}]}]},
    ]}
    p = tmp_path / "w.json"
    p.write_text(json.dumps(snap))
    cb = usage.read_codexbar(p)
    assert set(cb) == {"cursor", "copilot", "grok", "coderabbit"}
    assert usage.read_codexbar(tmp_path / "none.json") == {}
    toks = usage.cb_tokens(cb["cursor"], now, None)
    assert toks["today"] == {"tokens": 100, "cost": 1.5} and toks["d30"]["tokens"] == 150 and toks["d7"]["tokens"] == 100
    assert ("Owner", "<redacted>") in usage.cb_detail_rows(cb["coderabbit"])
    rows = [{"name": "Grok Build", "usage": "x"}, {"name": "Grok Bot", "usage": "y"}, {"name": "Cursor"}, {"name": "CodeRabbit"}]
    view = usage.executive(rows, None, None, None, None, now)
    view = usage.enrich(view, rows, cb, None, None, now)
    cards = {c["name"]: c for c in view["cards"]}
    assert [c["name"] for c in view["cards"]] == ["Codex", "Grok Build", "Grok Bot", "Cursor", "CodeRabbit", "GitHub"]
    assert cards["Cursor"]["percent"] == 97.0 and "tag" not in cards["Cursor"] and cards["Cursor"]["state"] == "live"
    assert cards["Cursor"]["headline"].startswith("Account usage: 97% used")
    assert "GitHub Copilot" not in cards
    assert cards["Grok Build"]["percent"] == 64 and cards["Grok Bot"]["percent"] == 28.0
    assert "607 reviews" in cards["CodeRabbit"]["sub"]
    tok = {r["name"]: r for r in view["summary"]["tokens"]}
    assert tok["Grok Bot"]["today"] is None and tok["CodeRabbit"]["d30"] is None and tok["Codex"]["today"] is None
    assert tok["Cursor"]["today"]["tokens"] == 100


def test_grok_usage_split(tmp_path):
    (tmp_path / "x.jsonl").write_text('{"type":"end","usage":{"input_tokens":10,"cache_read_input_tokens":90,"output_tokens":5,"total_tokens":105},"total_cost_usd":0.1}\n')
    b = usage.grok_tokens([tmp_path], usage.time.time(), None, {})
    assert b["today"] == {"input": 10, "cached": 90, "output": 5, "total": 105, "runs": 1, "cost": 0.1, "cost_runs": 1} and b["window"] is None
    assert usage.grok_tokens([tmp_path / "none"], 0, None, {}) is None


def test_plans_and_subscription_value(tmp_path):
    pf = tmp_path / "plans.json"
    pf.write_text(json.dumps({"plans": {"Cursor": {"plan": "Pro", "price_usd": None}, "CodeRabbit": {"plan": "X", "price_usd": "20"}}}))
    plans = usage.read_plans(pf)
    assert plans["Cursor"] == {"plan": "Pro", "price_usd": None, "source": "manual"} and plans["CodeRabbit"]["price_usd"] is None
    assert usage.read_plans(tmp_path / "none") == {}
    view = {"cards": [{"name": n} for n in ("Codex", "Grok Build", "Grok Bot", "Cursor", "CodeRabbit", "GitHub")]}
    rows = [{"name": "Codex"}]
    cs = {"d30": {"cost": 877.0}}
    gt = {"d30": {"cost": 30.0, "runs": 3, "cost_runs": 2}}
    cb = {"cursor": {"tokenUsage": {"last30DaysCostUSD": 50.0}}}
    usage.add_value(view, rows, cs, gt, cb, plans, 0)
    v = {e["name"]: e for e in view["summary"]["value"]}
    assert v["Codex"]["price_usd"] is None and v["Codex"]["plan_source"] is None and "multiple_30d" not in v["Codex"]
    assert "Est." in view["cards"][0]["value"] and rows[0]["plan"] == ""
    assert v["Grok Build"]["note"] == "2 of 3 runs reported a cost"
    assert v["Grok Bot"]["value_30d"] is None and "not reported" in v["Grok Bot"]["reason"]
    assert v["Cursor"]["value_30d"] == 50.0 and "multiple_30d" not in v["Cursor"] and v["Cursor"]["plan"] == "Pro" and v["Cursor"]["price_usd"] is None
    assert "fill plans.json" not in json.dumps(view) and "price not known" not in json.dumps(view)
    assert v["CodeRabbit"]["value_30d"] is None and "GitHub" not in v


def test_grokbot_old_plan_label_replaced(tmp_path):
    (tmp_path / "grokbot.json").write_text(json.dumps({"as_of": "2026-10-29T22:26:16-04:00", "plan": "Cursor Pro with included usage from SuperGrok Heavy", "weekly_used_percent": 28.1}))
    row = usage.grokbot_row(usage.iso_epoch("2026-11-01T06:00:00Z"), tmp_path)
    assert row["plan"] == "Cursor Pro with included usage from SuperGrok Heavy"
