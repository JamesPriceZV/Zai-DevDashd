"""Invented public status stories; no private gate, PR or device history."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from zai_devdashd import pipeline_v2 as p


def synthetic_head(label):
    # SHA-shaped values are deterministically minted from public fixture labels.
    return sha256(("zai-devdashd public synthetic " + label).encode("ascii")).hexdigest()[:40]


HEAD = synthetic_head("board-main-head")
INTEGRATION_HEAD = synthetic_head("board-integration-head")
OTHER_HEAD = synthetic_head("board-unrelated-head")
MAIN_PR, INTEGRATION_PR = 4102, 4101  # Invented fixture IDs, not external PRs.
STATUS = f"""2025-11-02 00:12 ET L-Example START #{MAIN_PR} {HEAD}
2025-11-02 00:26 ET L-Example FINISH #{MAIN_PR} {HEAD[:7]} bft=0 unit=3
2025-11-02 00:37 ET Synthetic checks: #{INTEGRATION_PR} gate {INTEGRATION_HEAD[:8]} HandsetDemo Integration tests fixture pass (input only)
- 2025-11-02 00:45 EDT: HandsetDemo fixture rerun complete
"""


def test_mini_and_diesel_from_status():
    lines = p.status_lines(STATUS)
    m = p.mini_status(lines, MAIN_PR, HEAD)
    assert m["state"] == "reported" and m["build"] == "BFT 0" and m["unit"] == "3" and m["lane"] == "L-Example"
    assert p.mini_status(lines, MAIN_PR, OTHER_HEAD)["state"] == p.NR
    d = p.diesel_status(lines, INTEGRATION_PR, INTEGRATION_HEAD)
    assert d["state"] == "note" and "Integration tests" in d["note"] and d["at"].startswith("2025-11-02 00:37")
    assert p.diesel_status(lines, MAIN_PR, HEAD)["state"] == p.NR
    devs = {x["device"]: x for x in p.devices(lines, names=["HandsetDemo", "TabletDemo"])}
    assert devs["HandsetDemo"]["at"].startswith("2025-11-02 00:45") and devs["TabletDemo"]["note"] == p.NR


def test_review_ladder_unique_heads():
    previous = synthetic_head("review-previous-head")
    current = synthetic_head("review-current-head")
    rv = [{"author": {"login": "coderabbitai"}, "state": "CHANGES_REQUESTED", "commit": {"oid": previous}},
          {"author": {"login": "coderabbitai"}, "state": "COMMENTED", "commit": {"oid": previous}},
          {"author": {"login": "coderabbitai"}, "state": "APPROVED", "commit": {"oid": current}},
          {"author": {"login": "chatgpt-codex-connector"}, "state": "COMMENTED", "commit": {"oid": previous}}]
    r = p.review_state(rv, [], current)
    assert r["CodeRabbit"]["unique_heads"] == 2 and r["CodeRabbit"]["reviews"] == 3
    assert r["CodeRabbit"]["summary"].startswith("APPROVED on head") and "2 unique heads" in r["CodeRabbit"]["summary"]
    assert r["Codex"]["summary"] == "older heads only" and r["Bugbot"]["summary"] == "none yet" and "Copilot" not in r


def test_scratch_lanes_board(tmp_path):
    s = p.scratch_free(tmp_path, floor_gb=10 ** 9)
    assert s["state"] == "below floor" and s["headroom_gb"] < 0
    assert p.scratch_free(tmp_path / "nope")["state"] == p.NR
    (tmp_path / "L-A.branch").write_text("synthetic/example-one\n")
    (tmp_path / "L-B.branch").write_text("synthetic/example-two\n")
    (tmp_path / "L-B.PAUSED").write_text("")
    lanes = {r["lane"]: r for r in p.lanes(tmp_path)}
    assert lanes["L-B"]["state"] == "paused" and lanes["L-A"]["branch"] == "synthetic/example-one" and lanes["L-A"]["state"] == "recent activity"
    b = p.Board().collect(now=1.0, status_text=STATUS, prs=[{"number": MAIN_PR, "title": "Synthetic parser example", "headRefOid": HEAD, "reviews": [], "comments": []}])
    assert b["pipeline"][0]["xcode_cloud"] == "needs App Store Connect API key" and b["pipeline"][0]["mini"]["state"] == "reported"
    assert b["ladder"][0]["CodeRabbit"]["unique_heads"] == 0



def test_failed_pr_refresh_preserves_old_heads_and_success_timestamp(monkeypatch):
    b = p.Board(every_s=1000)
    row = {"number": MAIN_PR, "headRefOid": HEAD, "reviews": []}
    monkeypatch.setattr(p, "run_gh", lambda *a: [row])
    b._refresh(100)
    monkeypatch.setattr(p, "run_gh", lambda *a: None)
    b._refresh(200)
    snap = b.collect(now=210, status_text="")
    assert snap["gh_at"] == 100 and snap["gh_attempted_at"] == 200
    assert snap["gh_available"] is True and snap["gh_ok"] is False
    assert snap["gh_stale"] and snap["gh_error"]
    assert snap["pipeline"][0]["head"] == HEAD[:8]


def test_first_failed_pr_refresh_is_unknown_not_empty_success(monkeypatch):
    b = p.Board(every_s=1000)
    monkeypatch.setattr(p, "run_gh", lambda *a: None)
    b._refresh(100)
    snap = b.collect(now=110, status_text="")
    assert snap["gh_at"] is None and snap["gh_ok"] is False
    assert snap["gh_coverage"]["count"] is None
    assert snap["gh_error"] and snap["gh_stale"]


def test_old_success_is_stale_and_capped_coverage_is_explicit(monkeypatch):
    b = p.Board(every_s=1000)
    monkeypatch.setattr(p, "run_gh", lambda *a: [{"number": n, "headRefOid": HEAD} for n in range(p.PR_LIMIT)])
    b._refresh(100)
    snap = b.collect(now=2201, status_text="")
    assert snap["gh_at"] == 100 and snap["gh_stale"]
    assert snap["gh_coverage"] == {"count": 40, "limit": 40, "limited": True}


def test_empty_verified_pr_list_is_a_real_success(monkeypatch):
    b = p.Board(every_s=1000)
    monkeypatch.setattr(p, "run_gh", lambda *a: [])
    b._refresh(100)
    snap = b.collect(now=110, status_text="")
    assert snap["gh_ok"] is True and snap["gh_stale"] is False
    assert snap["gh_coverage"]["count"] == 0


import pytest
from zai_devdashd import config as _site

@pytest.fixture(autouse=True)
def _synthetic_adapter_configuration(monkeypatch):
    # The capacity decision consumes supplied numbers, never host disk readings.
    def synthetic_statvfs(path):
        if not Path(path).exists():
            raise FileNotFoundError(path)
        return SimpleNamespace(f_bavail=8000, f_frsize=1024, f_blocks=16000)
    monkeypatch.setattr(p.os, "statvfs", synthetic_statvfs)
    monkeypatch.setitem(_site.CURRENT, "adapters", {"git": True, "github": True})
    monkeypatch.setitem(_site.CURRENT, "github", {"repo": "example/project", "integration_branch": "main"})
