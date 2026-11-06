"""Independent synthetic pipeline scenarios; no recorded build history."""
from datetime import datetime
from hashlib import sha256
from zai_devdashd.aggregator import build_runs, pipeline_outcome, ET

# Invented identities, not source revisions: sha256 of this public fixture label.
HEAD = sha256(b"zai-devdashd public synthetic pipeline-result-head").hexdigest()[:40]
RUN_NAME = "synthetic-sample-pipeline"
NOW = datetime(2025, 4, 14, 15, 20, tzinfo=ET).timestamp()


def item(exits, ended=True):
    return {"name": RUN_NAME, "head": HEAD,
            "start": datetime.fromtimestamp(NOW - 600, ET),
            "end": datetime.fromtimestamp(NOW - 30, ET) if ended else None,
            "mtime": NOW - 30, "exits": exits,
            "extras": ["DIESEL_PENDING: synthetic integration stage awaits fixture authorization"]}


def test_end_with_pending_diesel_is_incomplete_not_passed():
    value = item([("MAC_BFT", 0), ("HandsetDemo_BFT", 0), ("MAC_UNIT", 0), ("REL", 0)])
    runs, _, med = build_runs([value], [], NOW)
    r = runs[0]
    assert r["state"] == "incomplete" and pipeline_outcome(r) != "Pipeline passed"
    assert (r["progress_done"], r["progress_total"]) == (4, 5)
    assert "Integration tests" in r["verification_note"] and med is None
    assert "PHONE_BFT" not in [s["name"] for s in r["steps"]]


def test_observed_phone_alias_and_all_expected_results_can_pass():
    exits = [(name, 0) for name in ("MAC_BFT", "HandsetDemo_BFT", "MAC_UNIT", "DIESEL", "REL")]
    runs, _, med = build_runs([item(exits)], [], NOW)
    assert runs[0]["state"] == "passed" and runs[0]["progress_done"] == 5
    assert runs[0]["today"] is True and med == 570


def test_recent_log_is_unconfirmed_without_pipeline_process():
    value = item([], ended=False)
    runs, _, _ = build_runs([value], ["/bin/cat /synthetic/" + RUN_NAME + ".sh"], NOW)
    assert runs[0]["state"] == "unconfirmed" and runs[0]["eta"] is None
    runs, _, _ = build_runs([value], ["/bin/bash /synthetic/" + RUN_NAME + ".sh"], NOW)
    assert runs[0]["state"] == "running" and runs[0]["alive_via"] == "process"


def test_failed_recorded_step_stays_failed_even_with_end():
    runs, _, _ = build_runs([item([("MAC_BFT", 65)])], [], NOW)
    assert runs[0]["state"] == "failed"


def test_pending_diesel_is_not_inferred_running_from_missing_exit():
    value = item([("MAC_BFT", 0), ("HandsetDemo_BFT", 0), ("MAC_UNIT", 0)], ended=False)
    runs, _, _ = build_runs([value], ["/bin/bash /synthetic/" + RUN_NAME + ".sh"], NOW)
    run = runs[0]
    assert run["state"] == "running"
    assert not any(s["state"] == "run" for s in run["steps"])
    assert pipeline_outcome(run) == "Pipeline running"
    assert "current stage not reported" in run["verification_note"]
