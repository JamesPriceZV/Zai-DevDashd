from zai_devdashd import hwinfo


def test_missing_is_not_reported():
    g = hwinfo.ane_gauge(None)
    assert g["short"] == "ANE" and g["sub"] == "power" and g["note"] == "not reported" and g["pct"] is None


def test_fresh_value_share_of_peak():
    g = hwinfo.ane_gauge({"as_of": 1000.0, "ane_mw": 250, "ane_peak_mw_seen": 1000, "probe_time": 1030.0})
    assert g["value_text"] == "250 mW" and g["pct"] == 25.0 and not g["stale"]
    assert "not a busy percentage" in g["note"]


def test_stale_after_60s_iso_as_of():
    g = hwinfo.ane_gauge({"as_of": "1970-01-01T00:16:40Z", "ane_mw": 0, "ane_peak_mw_seen": 0, "probe_time": 1100.0})
    assert g["stale"] and g["sub"] == "power · stale" and g["pct"] is None and g["note"].startswith("not reported")


def test_zero_peak_is_idle_with_spark():
    g = hwinfo.ane_gauge({"as_of": 1000.0, "ane_mw": 0, "ane_peak_mw_seen": 0, "probe_time": 1010.0, "host": "t"})
    assert g["value_text"] == "idle" and g["note"].startswith("idle, no ANE activity seen yet") and g["spark"] == [0]


def test_mac_card_has_ane_gauge_last():
    card = hwinfo.mac_card({"ane": None}, {"chip": "Apple M4 Pro", "gpu_cores": 20})
    assert card["gauges"][-1]["kind"] == "ane"
