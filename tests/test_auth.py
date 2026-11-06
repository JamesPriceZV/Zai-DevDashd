#!/usr/bin/env python3
"""Login rules that do not call dscl and do not use a real password."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from zai_devdashd import auth
from zai_devdashd import labels as board


def main() -> None:
    assert auth.valid_user("devdash")
    assert auth.valid_user("_spotlight")
    assert not auth.valid_user("Bad Name")
    assert not auth.valid_user("Auser")
    assert not auth.valid_user("")
    assert auth.membership_ok("user is a member of the group")
    assert not auth.membership_ok("user is not a member of the group")
    assert not auth.membership_ok("The group zai-devdashd cannot be found")
    assert auth.localhost_unauth("127.0.0.1", "", "", True)
    assert auth.localhost_unauth("::1", None, None, True)
    assert not auth.localhost_unauth("127.0.0.1", "203.0.113.5", "", True)
    assert not auth.localhost_unauth("127.0.0.1", "", 'for=203.0.113.5', True)
    assert not auth.localhost_unauth("192.0.2.20", "", "", True)  # RFC5737 synthetic peer.
    assert not auth.localhost_unauth("127.0.0.1", "", "", False)
    gate = auth.Gate()
    now = 1_000_000.0
    for _ in range(4):
        assert gate.record_failure("203.0.113.9", now) is False
    assert gate.record_failure("203.0.113.9", now) is True
    assert gate.locked_seconds("203.0.113.9", now + 1) > 14 * 60
    assert gate.record_failure("203.0.113.9", now + 1) is False or gate.locked_seconds("203.0.113.9", now + 1)
    later = now + auth.LOCK_BASE + 5
    for _ in range(4):
        gate.record_failure("203.0.113.9", later)
    assert gate.record_failure("203.0.113.9", later) is True
    assert gate.locked_seconds("203.0.113.9", later + 1) > 29 * 60
    gate.record_success("203.0.113.9")
    assert gate.locked_seconds("203.0.113.9", later + 1) == 0
    token, csrf = gate.create_session("smoke", 3600, now)
    assert gate.session(token, now)["csrf"] == csrf
    assert gate.session(token, now + 3601) is None
    cookie = auth.session_cookie(token, 3600)
    assert cookie.startswith("zai_devdashd_session=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "Domain=" not in cookie
    page = auth.login_page(csrf, "zai-devdashd", "", True)
    assert b"autocomplete=\"current-password\"" in page
    assert b"method=\"POST\"" in page
    assert b"does not exist" in page
    assert auth.FAKE_PASSWORD.encode() not in page
    os.environ[auth.FAKE_ENV] = "1"
    try:
        assert auth.group_exists("zai-devdashd") is True
        assert auth.authenticate(auth.FAKE_USER, auth.FAKE_PASSWORD, "zai-devdashd") == "ok"
        assert auth.authenticate(auth.FAKE_OUTSIDER, auth.FAKE_PASSWORD, "zai-devdashd") == "not-member"
        assert auth.authenticate(auth.FAKE_USER, "not-the-fixture", "zai-devdashd") == "bad-password"
        assert auth.authenticate("Bad Name", auth.FAKE_PASSWORD, "zai-devdashd") == "bad-user"
    finally:
        os.environ.pop(auth.FAKE_ENV, None)
    kept = board.apply_settings({
        "worker": dict(board.DEFAULT_CONFIG["worker"]),
        "devices": dict(board.DEFAULT_CONFIG["devices"]),
        "mini": dict(board.DEFAULT_CONFIG["mini"]),
        "auth_group": "zai-devdashd",
        "allow_localhost_unauth": True,
        "session_ttl_s": 43200,
    }, {"worker_host": "Build-Mac.local"})
    assert kept["auth_group"] == "zai-devdashd"
    assert kept["allow_localhost_unauth"] is True
    assert kept["session_ttl_s"] == 43200
    print("auth ok")


def test_auth_rules() -> None:
    main()


if __name__ == "__main__":
    main()
