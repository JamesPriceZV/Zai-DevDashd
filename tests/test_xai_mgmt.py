import json
import urllib.error

from zai_devdashd import xai_mgmt as x
from zai_devdashd import resources_v2 as r

TEAM = "00000000-0000-4000-8000-000000000000"
NOW = 1793509200.0  # 2026-11-01 01:00 ET
FAKE_KEY = "test-fixture-not-a-key"
S = {"key_file": "/nonexistent", "team_id": TEAM, "poll_s": 900}


def fake_req(fail=()):
    calls = []

    def req(key, path, body=None):
        assert key == FAKE_KEY
        assert x.is_allowed(path, body is not None)
        calls.append((path, body))
        for f in fail:
            if path.endswith(f):
                raise urllib.error.HTTPError(path, 403, "Forbidden", {}, None)
        if path.endswith("/prepaid/balance"):
            return {"changes": [{"changeOrigin": "PURCHASE", "amount": {"val": "-500"}}, {"changeOrigin": "SPEND", "amount": {"val": "120"}}],
                    "total": {"val": "-380"}}
        if path.endswith("/spending-limits"):
            return {"spendingLimits": {"effectiveSl": {"val": "1000"}, "effectiveHardSl": {"val": "2500"}}}
        if path.endswith("/invoice/preview"):
            return {"coreInvoice": {"amountAfterVat": "0"}, "billingCycle": {"year": 2026, "month": 11}}
        if path.endswith("/invoices"):
            return {"invoices": [{"invoice_number": "AAAA", "invoiceStatus": "PAID", "total": "0",
                                  "monthly": {"billingCycle": {"year": 2026, "month": 9}, "prepaidTokensToSpend": {"val": "-16"}}}]}
        if path.endswith("/api-keys"):
            return {"apiKeys": [{"apiKey": "SECRETVALUE", "redactedApiKey": "xai-...abcd", "apiKeyHash": "deadbeef", "userId": "u1",
                                 "name": "Main", "apiKeyId": "k1", "disabled": False, "aclStrings": ["api-key:model:*"]}]}
        if path.endswith("/usage"):
            g = body["analyticsRequest"]["groupBy"][0]
            lbl = "Main (xai-...abcd)" if g == "api_key_id" else "API grok-4.7"
            return {"timeSeries": [{"group": ["k1" if g == "api_key_id" else lbl], "groupLabels": [lbl],
                                    "dataPoints": [{"values": [0.5]}, {"values": [0.7]}]}]}
        raise AssertionError(path)
    req.calls = calls
    return req


def test_allow_list_is_read_only():
    assert x.is_allowed("/v1/billing/teams/%s/prepaid/balance" % TEAM, False)
    assert x.is_allowed("/auth/teams/%s/api-keys" % TEAM, False)
    assert x.is_allowed("/v1/billing/teams/%s/usage" % TEAM, True)
    for bad, post in (("/v1/billing/teams/%s/prepaid/top-up" % TEAM, True), ("/v1/billing/teams/%s/postpaid/spending-limits" % TEAM, True),
                      ("/auth/teams/%s/api-keys" % TEAM, True), ("/v1/billing/teams/%s/billing-info" % TEAM, False),
                      ("/v1/billing/teams/%s/usage" % TEAM, False), ("/auth/teams/%s/api-keys/../x" % TEAM, False),
                      ("/v1/billing/teams/%s/api-keys" % TEAM, False)):
        assert not x.is_allowed(bad, post), bad
    try:
        x._request(FAKE_KEY, "/v1/billing/teams/%s/prepaid/top-up" % TEAM, {"amount": {"val": "1"}})
        raise AssertionError("write path accepted")
    except ValueError:
        pass


def test_collect_drops_key_material_and_card_numbers():
    req = fake_req()
    data = x.collect(FAKE_KEY, TEAM, NOW, req=req)
    blob = json.dumps(data)
    for secret in ("SECRETVALUE", "xai-...abcd", "deadbeef", FAKE_KEY, "u1"):
        assert secret not in blob
    assert data["keys"] == [{"id": "k1", "name": "Main", "disabled": False, "created": None, "expires": None}]
    assert data["d30_by_key"][0]["label"] == "Main"
    assert all(b is None or "groupBy" in b["analyticsRequest"] for _, b in req.calls)
    c = x.card(data, S, NOW)
    assert c["status"] == "live"
    assert c["xai"]["spend_month_usd"] == 1.2
    assert c["xai"]["credit_usd"] == 3.8
    assert c["xai"]["soft_limit_usd"] == 10.0 and c["xai"]["hard_limit_usd"] == 25.0
    assert c["used"] == round(1.2 / 13.8 * 100, 1)
    rows = dict(c["rows"])
    assert rows["Prepaid credit balance"] == "$3.80"
    assert rows["API keys"] == "Main (active)"
    assert "Main $1.20" in rows["Last 30 days by key"]
    assert c["models"][0] == {"model": "API grok-4.7", "tokens": None, "cost": 1.2}


def test_partial_failure_is_honest():
    data = x.collect(FAKE_KEY, TEAM, NOW, req=fake_req(fail=("/spending-limits", "/usage")))
    c = x.card(data, S, NOW)
    assert c["status"] == "partial"
    rows = dict(c["rows"])
    assert rows["Monthly spending limit"] == "unavailable"
    assert rows["Spend this month (Nov 2026)"] == "unavailable"
    assert "limits (HTTP 403)" in rows["Unavailable"]
    assert c["used"] is None


def test_unconfigured_and_empty_states():
    assert x.card({}, {"key_file": "", "team_id": ""}, NOW)["status"] == "pending credential"
    c = x.card({"at": NOW, "ok": {}, "errors": {"config": "key file or team id missing"}}, S, NOW)
    assert c["status"] == "unavailable" and c["used"] is None


def test_read_key_and_poller(tmp_path):
    kf = tmp_path / "k"
    kf.write_text(FAKE_KEY + "\n")
    assert x.read_key(str(kf)) == FAKE_KEY
    assert x.read_key(str(tmp_path / "missing")) is None
    p = x.Poller(str(tmp_path / "missing"), TEAM, path=tmp_path / "c.json")
    d = p.refresh(NOW, force=True)
    assert d["errors"] == {"config": "key file or team id missing"}
    assert FAKE_KEY not in (tmp_path / "c.json").read_text()


def test_build_includes_xai_card(monkeypatch, tmp_path):
    cache = tmp_path / "x.json"
    cache.write_text(json.dumps(x.collect(FAKE_KEY, TEAM, NOW, req=fake_req())))
    monkeypatch.setattr(x, "CACHE", cache)
    out = r.build({}, NOW, cfg={"xai": dict(S)}, ledger_text="")
    names = [c["name"] for c in out["cards"]]
    assert "xAI / Grok API" in names
    assert not any("xAI" in p["panel"] for p in out["pending"])
    out2 = r.build({}, NOW, cfg={}, ledger_text="")
    assert any("xAI" in p["panel"] for p in out2["pending"])


def test_zero_credit_zero_limit_warns():
    data = x.collect(FAKE_KEY, TEAM, NOW, req=fake_req())
    data["balance"]["total_cents"] = "0"
    data["limits"]["spendingLimits"]["effectiveSl"]["val"] = "0"
    assert "Warning" in dict(x.card(data, S, NOW)["rows"])
