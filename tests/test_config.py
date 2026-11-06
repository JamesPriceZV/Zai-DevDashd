"""The TOML reader round-trips the example file. No third-party parser."""

from pathlib import Path

from zai_devdashd import config as site


def test_example_toml_round_trip():
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    parsed = site.parse_toml(example.read_text(encoding="utf-8"))
    assert parsed["worker"]["user"] == ""
    assert parsed["worker"]["ip"] == ""
    assert parsed["server"] == {"host": "127.0.0.1", "port": 8780}
    assert parsed["auth_group"] == "zai-devdashd"
    assert parsed["service"]["allow_restart"] is False
    assert parsed["devices"]["numbers"] == []
    assert parsed["executive_ai"]["enabled"] is False
    assert parsed["adapters"]["local_system"] is True
    assert all(value is False for key, value in parsed["adapters"].items() if key != "local_system")
    assert site.parse_toml(site.dump_toml(parsed)) == parsed


def test_settings_keep_auth_group():
    kept = site.apply_settings({
        "worker": dict(site.DEFAULT_CONFIG["worker"]),
        "devices": dict(site.DEFAULT_CONFIG["devices"]),
        "mini": dict(site.DEFAULT_CONFIG["mini"]),
        "auth_group": "zai-devdashd",
        "allow_localhost_unauth": True,
        "session_ttl_s": 43200,
    }, {"worker_host": "Build-Mac.local"})
    assert kept["auth_group"] == "zai-devdashd"
    assert kept["worker"]["host"] == "Build-Mac.local"
    assert kept["allow_localhost_unauth"] is True


def test_example_session_input_reaches_both_readers(monkeypatch, tmp_path):
    from zai_devdashd import usage
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    parsed = site.parse_toml(example.read_text(encoding="utf-8"))
    assert "codex_sessions_dir" in parsed["telemetry"]
    configured = tmp_path / "synthetic-session-input"
    parsed["telemetry"]["codex_sessions_dir"] = str(configured)
    monkeypatch.setattr(site, "CURRENT", parsed)
    collector = usage.UsageCollector()
    assert collector.codex.root == configured
    assert collector.cost.root == configured


def test_remote_requires_explicit_opt_in_and_safe_ssh_alias():
    import copy
    cfg = site._sanitize(copy.deepcopy(site.DEFAULT_CONFIG))
    assert cfg["remote"]["enabled"] is False
    cfg["remote"] = {"enabled": True, "host": "-oProxyCommand=bad"}
    assert site._sanitize(cfg)["remote"]["enabled"] is False
    cfg["remote"] = {"enabled": True, "host": "remote"}
    got = site._sanitize(cfg)["remote"]
    assert got["enabled"] is True and got["host"] == "remote"

def test_settings_preserve_remote_enablement():
    import copy
    cfg = copy.deepcopy(site.DEFAULT_CONFIG)
    cfg["remote"].update(enabled=True, host="remote")
    kept = site.apply_settings(cfg, {"worker_host": "Build-Mac.local"})
    assert kept["remote"]["enabled"] is True
    assert kept["remote"]["host"] == "remote"


def test_program_registry_private_path_survives_settings_and_round_trip(monkeypatch, tmp_path):
    import copy
    cfg = copy.deepcopy(site.DEFAULT_CONFIG)
    cfg["state"]["dir"] = str(tmp_path)
    cfg["paths"]["programs_registry"] = str(tmp_path / "private-checkpoints.json")
    kept = site.apply_settings(cfg, {"worker_host": "Build-Mac.local"})
    again = site.parse_toml(site.dump_toml(kept))
    monkeypatch.setattr(site, "CURRENT", again)
    assert site.programs_registry() == tmp_path / "private-checkpoints.json"
    again["paths"]["programs_registry"] = ""
    assert site.programs_registry() == tmp_path / "programs.json"
