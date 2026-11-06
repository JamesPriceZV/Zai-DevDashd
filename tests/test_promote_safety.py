"""Public service boundaries with injected commands; no installer or host probes."""
from unittest.mock import patch
import pytest
from zai_devdashd import config, server


def test_busy_port_refuses_without_signalling():
    with patch.object(server, "socket_bind_ok", return_value=False), patch.object(
            server.os, "kill", side_effect=AssertionError("foreign signal forbidden")):
        with pytest.raises(SystemExit):
            server.ensure_port_free()


def test_default_service_restart_is_disabled(monkeypatch):
    monkeypatch.setitem(config.CURRENT, "service", {"label": "org.zai.devdashd", "allow_restart": False})
    with patch.object(server.subprocess, "Popen", side_effect=AssertionError("restart forbidden")):
        assert server.schedule_live_restart() is False


def test_service_cannot_restart_an_unrelated_label(monkeypatch):
    monkeypatch.setitem(config.CURRENT, "service", {"label": "com.example.other", "allow_restart": True})
    with patch.object(server.subprocess, "Popen", side_effect=AssertionError("foreign restart forbidden")):
        assert server.schedule_live_restart() is False


def test_explicit_service_restart_uses_fixed_argv(monkeypatch):
    monkeypatch.setitem(config.CURRENT, "service", {"label": "org.zai.devdashd.test", "allow_restart": True})
    with patch.object(server.os, "getuid", return_value=501), patch.object(server.subprocess, "Popen") as start:
        assert server.schedule_live_restart() is True
    assert start.call_args.args[0] == ["/bin/launchctl", "kickstart", "-k", "gui/501/org.zai.devdashd.test"]
