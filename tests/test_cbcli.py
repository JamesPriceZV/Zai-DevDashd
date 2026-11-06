"""Wholly synthetic provider projections; no CLI, observed account data or paid defaults."""
import json
from pathlib import Path
from zai_devdashd import cbcli, config as site
from zai_devdashd.usage import iso_epoch

FIX = Path(__file__).parent / "fixtures" / "codexbar-cli.json"
NOW = iso_epoch("2001-06-06T12:00:00Z")


def view(**kw):
    return {c["name"]: c for c in cbcli.providers_view(json.loads(FIX.read_text()), NOW, **kw)}


def test_order_and_roles():
    cards = cbcli.providers_view({}, NOW)
    assert [c["name"] for c in cards] == ["Codex", "Grok Build", "Grok Bot", "Cursor", "CodeRabbit"]
    assert all(c["price"] is None and c["value"] is None for c in cards)
    assert all("No CodexBar reading" in " ".join(c["missing"]) or c["name"] == "Grok Bot" for c in cards)


def test_synthetic_windows_keep_separate_account_boundaries():
    v = view()
    assert v["Cursor"]["bars"][0]["used"] == 30
    assert v["Cursor"]["bars"][0]["left"] == 70
    assert v["Grok Bot"]["bars"] == []
    assert v["Grok Bot"]["cost"] is None
    assert v["Grok Build"]["cost"] is None


def test_cost_project_redaction_and_unknown_price():
    card = view()["Codex"]
    assert card["cost"]["d30_tokens"] == 1200
    assert card["cost"]["d30_cost"] == 2
    assert card["consumers"][0][0] == "synthetic/ExampleProject"
    assert card["price"] is None and card["value"] is None
    assert "bill" in card["cost"]["note"]


def test_price_only_comes_from_explicit_manual_plan():
    card = view(plans={"Codex": {"plan": "Synthetic paid plan", "price_usd": 8, "source": "manual"}})["Codex"]
    assert card["price"] == 8 and card["price_source"] == "manual"
    assert card["plan"] == "Synthetic paid plan" and card["value"]["api_30d"] == 2


def test_redact_drops_identity():
    assert cbcli.redact({"accountEmail": "person@example.invalid", "x": {"accountID": "fiction", "note": "person@example.invalid"}}) == {"x": {"note": "[email]"}}


def test_cr_by_pr_and_gb_by_lane(tmp_path):
    led = "2001-06-06T10:00:00Z\tcli\tsynthetic (#17)\tabc\tok\n"
    assert cbcli.cr_by_pr(led, NOW) == [["#17", 1]]
    p = tmp_path / "L-A-20010606-113300.jsonl"
    p.write_text('{"type":"end","usage":{"total_tokens":10}}\n')
    import os
    os.utime(p, (NOW, NOW))
    assert cbcli.gb_by_lane([tmp_path], NOW, {}) == [["L-A", 10, 1]]


def test_disabled_adapter_never_starts_cli_or_reads_preferences(monkeypatch):
    monkeypatch.setitem(site.CURRENT, "adapters", {"usage": False})
    monkeypatch.setattr(cbcli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected process")))
    poller = cbcli.Poller(Path("/nonexistent/synthetic-cache"))
    poller.start(); poller.refresh(NOW)
    assert not poller.started and cbcli.run_cli(["usage"]) is None
    assert cbcli.CodexBarView().collect(NOW) == []
    assert cbcli.work_days_setting() is None


def test_linear_pace_plain_week():
    p = cbcli.linear_pace(50, NOW + 3.5 * 86400, 7 * 86400, NOW)
    assert round(p["expectedUsedPercent"]) == 50 and p["willLastToReset"] is True
    assert cbcli.linear_pace(None, NOW, 0, NOW) is None


def test_synthetic_grok_run_cost_does_not_become_other_account_cost(tmp_path):
    import os
    p = tmp_path / "L-B-20010606-120000.jsonl"
    p.write_text('{"type":"end","usage":{"total_tokens":40},"total_cost_usd":0.5}\n')
    os.utime(p, (NOW, NOW))
    cards = {c["name"]: c for c in cbcli.providers_view({}, NOW, "", [tmp_path], {})}
    assert cards["Grok Build"]["cost"]["today_tokens"] == 40
    assert cards["Grok Build"]["cost"]["today_cost"] == 0.5
    assert cards["Grok Bot"]["cost"] is None



def _synthetic_cli(tmp_path):
    executable = tmp_path / "codexbar"
    executable.write_text("SYNTHETIC TEST FILE; NEVER EXECUTED\n")
    executable.chmod(0o700)
    return executable


def test_enabled_usage_finds_cli_on_path_and_uses_bounded_argv(tmp_path, monkeypatch):
    import subprocess
    executable = _synthetic_cli(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setitem(site.CURRENT, "adapters", {"usage": True})
    monkeypatch.setitem(site.CURRENT, "telemetry", {})
    calls = []
    def supplied(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '[{"source":"synthetic","usage":{"usedPercent":12},"accountEmail":"person@example.invalid"}]', "")
    monkeypatch.setattr(cbcli.subprocess, "run", supplied)
    result = cbcli.run_cli(["usage", "--provider", "codex"], timeout=90)
    assert result == {"source": "synthetic", "usage": {"usedPercent": 12}}
    command, kwargs = calls[0]
    assert command == ["/usr/bin/nice", "-n", "15", str(executable.resolve()), "usage", "--provider", "codex", "--json-only"]
    assert kwargs == {"capture_output": True, "text": True, "timeout": 90}


def test_enabled_missing_cli_is_unknown_without_any_process(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setitem(site.CURRENT, "adapters", {"usage": True})
    monkeypatch.setitem(site.CURRENT, "telemetry", {})
    monkeypatch.setattr(cbcli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("missing CLI was invoked")))
    assert cbcli.run_cli(["usage", "--provider", "codex"]) is None


def test_disabled_usage_returns_before_discovery(monkeypatch):
    monkeypatch.setitem(site.CURRENT, "adapters", {"usage": False})
    monkeypatch.setattr(cbcli.shutil, "which", lambda *a, **k: (_ for _ in ()).throw(AssertionError("disabled adapter performed discovery")))
    monkeypatch.setattr(cbcli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("disabled adapter invoked CLI")))
    assert cbcli.run_cli(["usage", "--provider", "codex"]) is None


def test_explicit_absolute_cli_path_is_canonical_and_argv_only(tmp_path, monkeypatch):
    import subprocess
    executable = _synthetic_cli(tmp_path)
    alias = tmp_path / "alias with spaces"
    alias.symlink_to(executable)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setitem(site.CURRENT, "adapters", {"usage": True})
    monkeypatch.setitem(site.CURRENT, "telemetry", {"codexbar_cli": str(alias)})
    calls = []
    def supplied(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '[{"source":"synthetic"}]', "")
    monkeypatch.setattr(cbcli.subprocess, "run", supplied)
    assert cbcli.run_cli(["cost", "--provider", "cursor", "--days", "30"]) == {"source": "synthetic"}
    assert calls[0][0][3] == str(executable.resolve())
    assert "shell" not in calls[0][1] and calls[0][1]["timeout"] == 300
    monkeypatch.setitem(site.CURRENT, "telemetry", {"codexbar_cli": "relative/tool"})
    assert cbcli.run_cli(["usage"]) is None
    assert len(calls) == 1
    with monkeypatch.context() as scoped:
        scoped.setattr(cbcli.Path, "expanduser", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic missing home alias")))
        assert cbcli.run_cli(["usage"]) is None
    assert len(calls) == 1


def test_non_readonly_or_unbounded_cli_requests_are_refused_before_discovery(monkeypatch):
    monkeypatch.setitem(site.CURRENT, "adapters", {"usage": True})
    monkeypatch.setattr(cbcli.shutil, "which", lambda *a, **k: (_ for _ in ()).throw(AssertionError("invalid request performed discovery")))
    for args in ([], "usage", ["auth", "login"], ["usage", "--provider", "unknown"],
                 ["usage", "--provider", "codex", "--days", "30"],
                 ["cost", "--provider", "codex", "--days", "0"],
                 ["cost", "--provider", "codex", "--days", "32"],
                 ["cost", "--provider", "codex", "--days", "1000000"],
                 ["usage", "--provider", "codex", "--output", "/unowned"]):
        assert cbcli.run_cli(args) is None
    for timeout in (None, True, 0, 301, "90"):
        assert cbcli.run_cli(["usage"], timeout=timeout) is None


def test_failed_cli_or_non_json_output_stays_unknown(tmp_path, monkeypatch):
    import subprocess
    _synthetic_cli(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setitem(site.CURRENT, "adapters", {"usage": True})
    monkeypatch.setitem(site.CURRENT, "telemetry", {})
    for code, output in ((1, '[{"source":"partial"}]'), (0, 'not JSON'), (0, '[{"error":"unavailable"}]')):
        monkeypatch.setattr(cbcli.subprocess, "run", lambda *a, _code=code, _output=output, **k: subprocess.CompletedProcess(a, _code, _output, ""))
        assert cbcli.run_cli(["usage", "--provider", "codex"]) is None
