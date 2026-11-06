"""Probe parsers and the generic-title guard."""

from zai_devdashd import labels as board
from zai_devdashd.probes import (
    hardware_from_parts,
    parse_gpu,
    parse_mem_used,
    parse_worker,
    parse_swap_label,
    parse_top_cpu,
    spec_line,
)


def test_load_parsers():
    assert parse_top_cpu("CPU usage: 9.70% user, 8.35% sys, 81.94% idle") == 18
    assert parse_gpu('{"Device Utilization %"=4} {"Device Utilization %"=20}') == 20
    assert parse_mem_used("System-wide memory free percentage: 59%") == 41
    assert parse_swap_label("total = 8192.00M  used = 5632.00M  free = 2560.00M") == "5.5 GB"
    hw = hardware_from_parts(
        ["Mac17,16", "Apple M5 Pro", "18", "68719476736"],
        "Model Name: Mac mini\nChip: Apple M5 Pro\n"
        "Total Number of Cores: 18 (6 Super and 12 Performance)\nMemory: 64 GB\n",
    )
    assert hw["config"] == "Mac mini M5 Pro · 18-core CPU · 64 GB"
    assert spec_line(hw["config"], {"cpu": 18, "gpu": 20, "mem": 41, "swap": "5.5 GB"}).endswith("Swap 5.5 GB")


def test_worker_probe_text():
    probe = (
        "PS\t1\t0.1\t00:01\tpython zai\n"
        "LIVE_BEGIN\n"
        "CPU usage: 9.70% user, 8.35% sys, 81.94% idle\n"
        'Device Utilization %"=7\n'
        "System-wide memory free percentage: 80%\n"
        "used = 512.00M\n"
        "LIVE_END\n"
        "PROBE_END\n"
    )
    parsed = parse_worker(probe)
    assert parsed["load"]["cpu"] == 18
    assert parsed["load"]["gpu"] == 7
    assert parsed["load"]["mem"] == 20
    assert parsed["load"]["swap"] == "512 MB"


def test_generic_title_guard():
    assert board.generic_title("Mac build running")
    assert board.generic_title("The build lane is running.")
    assert not board.generic_title("#17 · Keep the settled receipt")
    line = board.work_line(
        "L-A · Fix #17: ensure all macOS unit tests pass",
        "Goal: ensure all macOS unit tests pass",
        17,
        "Reading the project",
        "L-A",
        "Fix the PDF export so turn 3 includes the Research more answer",
    )
    assert line.startswith("#17 · Fix the PDF export")
    assert "L-A" not in line
    assert not line[:1].islower()
