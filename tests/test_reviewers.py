import json
from zai_devdashd import reviewers


def test_missing_rollup_is_honest(tmp_path):
    out = reviewers.build(0, tmp_path / "none.json")
    assert out["state"] == "no data yet"


def test_cards_pass_through_without_invention(tmp_path):
    p = tmp_path / "rollup.json"
    p.write_text(json.dumps({"generated_at": "2026-11-02T20:00:00Z", "prs_scanned": 2, "findings_total": 3,
                             "reviewers": {"Bugbot": {"findings": 3, "precision_proxy": None}}, "recent": []}))
    out = reviewers.build(0, p)
    names = [c["name"] for c in out["cards"]]
    assert names == ["Bugbot", "CodeRabbit", "Codex", "DeepSource"] and "Copilot" not in json.dumps(out["cards"])
    bb = out["cards"][0]
    assert bb["findings"] == 3 and bb["precision_proxy"] is None and bb["thin"] is True
    assert out["cards"][1].get("findings") is None
