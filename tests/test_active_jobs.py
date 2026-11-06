#!/usr/bin/env python3
"""Active-job titles name the work. A running step is never described as failed."""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.request
from pathlib import Path

from zai_devdashd import labels as board

GENERIC = re.compile(
    r"^(Grok Build lane|Mac build|phone build|Mac unit tests|Integration tests|Pipeline)( step)? running$",
    re.I,
)
SIMILAR = re.compile(
    r"^(the\s+)?(grok build lane|mac build|tablet101 build|unit tests|diesel|pipeline|release|zai run)"
    r"( is)?( step)? running\.?$",
    re.I,
)


def assert_title(text: str, where: str) -> None:
    title = " ".join((text or "").split())
    if GENERIC.match(title) or SIMILAR.match(title) or board.generic_title(title):
        raise AssertionError("%s uses a generic title: %s" % (where, title))


def assert_step(text: str, where: str) -> None:
    if text and re.search(r"\bfailed\b", text, re.I):
        raise AssertionError("%s running step says failed: %s" % (where, text))


def synthetic() -> None:
    title = board.pipeline_purpose(17, "Keep the settled receipt", "subject")
    goal = board.pipeline_goal("Keep the settled receipt", None, None, None)
    step = board.pipeline_step({
        "state": "running",
        "steps": [
            {"name": "MAC_BFT", "word": "Mac build", "state": "fail", "duration_s": 10},
            {"name": "PHONE_BFT", "word": "phone build", "state": "run", "duration_s": 240},
        ],
    })
    assert_title(title, "synthetic pipeline")
    assert title.startswith("#17 · ")
    assert goal.startswith("Goal: ")
    assert_step(step, "synthetic pipeline")
    assert step.startswith("Step 2 of 5:")
    assert board.usable_title("Mac build running", title) == title
    assert board.usable_title("Pipeline running", title) == title
    assert board.usable_title("The build lane is running", "L-B · #18 · queue prep") == "L-B · #18 · queue prep"
    lane = board.lane_step(["search_replace ResponseExportService.swift"], "editing", 1_762_000_000)
    assert lane.startswith("Editing ResponseExportService.swift")
    assert_step(lane, "synthetic lane")
    assert board.lane_step(["search_replace"], "waiting", None) == "Editing"
    assert board.lane_step(["grep"], "waiting", None) == "Reading the project"
    raw = board.lane_step(["/usr/bin/xcodebuild test -scheme ExampleApp"], "waiting", None)
    assert raw == "Running tests"
    assert "/" not in raw
    assert board.lane_step([], "waiting", None) == "Waiting for the next action"
    assert board.lane_step(["get_command_or_subagent_output"], "waiting", None) == "Waiting on a command"
    command = board.lane_step(
        ["run_terminal_command: ~/DevDashInputs/headless/bin/unit-tests.sh -only-testing:ExampleApp"],
        "waiting",
        None,
    )
    assert command == "Running tests"
    assert "/" not in command
    plain = board.lane_step(["run_terminal_command: ls -lt ~/DevDashInputs/headless/logs"], "waiting", None)
    assert plain == "Running a command"
    assert "/" not in plain
    assert_step(board.lane_step(["the tests failed"], "testing", None), "scrubbed lane")
    assert board.zai_step("A-R47–A-R50") == "Running units A-R47–A-R50"
    raw = (
        "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain"
        "/usr/bin/dsymutil /Volumes/BuildScratch/DevDash/DerivedData/"
        "eng-17-d72-rel/Build/Products/Rel TEST FAILED"
    )
    title, goal, step = board.device_lines("Tablet101", raw)
    assert_title(title, "synthetic device")
    assert title.startswith("#17 · ")
    assert "Tablet101" in title
    assert goal.startswith("Goal: ")
    assert_step(step, "synthetic device")
    assert step == "Running release build on Tablet101"
    assert "Tablet101" in title
    assert not title.lower().startswith("phone")
    assert "/" not in step
    swift = [
        "/Volumes/BuildDisk/DevDash/zai/dd-test-zf35/swift-test --scratch-path /tmp/x",
        "/usr/bin/xctest /Volumes/BuildDisk/DevDash/zai/dd-test-zf35/out/Products/Debug/ZAICoderTests.xctest",
    ]
    title, goal, step = board.remote_tool_lines(swift)
    assert_title(title, "synthetic worker tool")
    assert title == "Worker · ZAICoderTests"
    assert goal == "Goal: ZAICoderTests passes on Worker"
    assert step == "Running swift-test"
    assert "ZAI run" not in title
    assert board.vnc_href("Build-Mac", "192.0.2.246") == (
        "vnc://Build-Mac._rfb._tcp.local?quality=adaptive&numVirtualDisplays=0"
    )
    mini_name = "Example Mini"
    mini_href = board.vnc_href(mini_name, "192.0.2.243")
    assert mini_href.startswith("vnc://") and " " not in mini_href and "%20" in mini_href
    assert board.vnc_href("", "192.0.2.246") == "vnc://192.0.2.246?quality=adaptive&numVirtualDisplays=0"
    assert board.match_rfb(["Build-Mac", mini_name], "Build-Mac.local") == "Build-Mac"
    assert board.match_rfb(["Build-Mac", mini_name], "example-mini.local") == ""
    assert board.match_rfb(["Example-Mini"], "example-mini.local") == "Example-Mini"
    plist = board.vncloc_plist(board.vnc_href("Build-Mac", ""))
    assert b"<key>URL</key>" in plist and b"vnc://Build-Mac._rfb._tcp.local" in plist


def check_snapshot(snap: dict) -> None:
    for job in (snap.get("active") or {}).get("jobs") or []:
        where = "active %s" % (job.get("where") or job.get("anchor") or "")
        assert_title(job.get("name") or "", where)
        assert_step(job.get("step") or "", where)
        if job.get("goal") and not str(job.get("goal")).startswith("Goal:"):
            raise AssertionError("%s goal is missing the Goal prefix: %s" % (where, job.get("goal")))
    for tile in (snap.get("overview") or {}).get("tiles") or []:
        where = "tile %s" % (tile.get("name") or "")
        assert_title(tile.get("job") or "", where)
        if tile.get("status") == "active":
            assert_step(tile.get("step") or "", where)


def from_server(url: str) -> None:
    ctx = None
    if url.startswith("https://"):
        ctx = ssl.create_default_context()
        ca = os.environ.get("ZAI_DEVDASHD_CA") or ""
        if ca:
            ctx.load_verify_locations(cafile=ca)
    with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=20, context=ctx) as response:
        if response.status != 200:
            raise AssertionError("api/state status %s" % response.status)
        snap = json.loads(response.read().decode("utf-8"))
    check_snapshot(snap)


def from_build() -> None:
    os.environ.setdefault("ZAI_DEVDASHD_PORT", "8766")
    from zai_devdashd import aggregator as monitor
    monitor.ENGINE.poll_local()
    check_snapshot(monitor.ENGINE.build())


def main() -> None:
    synthetic()
    url = os.environ.get("ZAI_DEVDASHD_TEST_URL") or ""
    if url:
        from_server(url)
    elif "--local" in sys.argv[1:]:
        from_build()  # Explicit opt-in observation, never part of pytest.
    print("synthetic active jobs checks ok")


def test_active_jobs_snapshot() -> None:
    synthetic()


if __name__ == "__main__":
    main()
