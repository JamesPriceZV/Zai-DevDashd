"""Hardware card builders, with made-up probe output only."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from zai_devdashd import hwinfo

TOPO = {"chip": "Apple M5 Pro", "model_id": "MacX,1", "cpu_cores": 6, "cpu_breakdown": "2 Super and 4 Performance",
        "perflevels": [{"name": "Super", "cores": 2}, {"name": "Performance", "cores": 4}], "gpu_cores": 20,
        "memory_gb": 64, "os": "27.2", "build": "1A1", "ane_present": True,
        "cluster_by_cpu": ["M", "M", "M", "M", "P", "P"]}


def test_parse_probe_line():
    assert hwinfo.parse_probe("noise\nHWJSON " + json.dumps({"gpu": 5}) + "\n") == {"gpu": 5}
    assert hwinfo.parse_probe("HWJSON {bad") is None
    assert hwinfo.parse_probe("") is None


def test_cluster_names_follow_sysctl_counts_not_letters():
    names = hwinfo.cluster_names(TOPO)
    assert names == {"M": "Performance", "P": "Super"}
    m2 = {"perflevels": [{"name": "Performance", "cores": 4}, {"name": "Efficiency", "cores": 4}],
          "cluster_by_cpu": ["E"] * 4 + ["P"] * 4}
    # equal counts: first unmatched level wins by count, fall back stays sane
    assert set(hwinfo.cluster_names(m2).values()) == {"Performance", "Efficiency"}


def test_cluster_gauges_average_per_cluster():
    g = hwinfo.cluster_gauges(TOPO, [10, 20, 30, 40, 100, 90])
    assert g[0] == {"label": "Super cores (2)", "short": "Super", "sub": "2", "pct": 95, "kind": "cpu"}
    assert g[1]["label"] == "Performance cores (4)" and g[1]["pct"] == 25


def test_mac_card_live_and_honest_ane():
    probe = {"core_busy": [0, 0, 0, 0, 50, 50], "gpu": 40, "mem_free_pct": 70, "swap_used_gb": 1.0,
             "swap_total_gb": 2.0, "wired_limit_gb": 52.0, "thermal": "Nominal"}
    c = hwinfo.mac_card(probe, TOPO, "Building")
    labels = [g["label"] for g in c["gauges"]]
    assert labels == ["Super cores (2)", "Performance cores (4)", "GPU (20 cores)", "Memory pressure", "ANE power (mW, not busy %)"]
    shorts = [(x["short"], x["sub"]) for x in c["gauges"]]
    assert shorts == [("Super", "2"), ("P-cores", "4"), ("GPU", "20 cores"), ("Memory", "pressure"), ("ANE", "power")]
    assert all(len(a) <= 8 and len(b) <= 9 for a, b in shorts)
    assert c["gauges"][3]["pct"] == 30
    facts = {f["k"]: f["v"] for f in c["facts"]}
    assert facts["GPU wired memory limit"] == "52.0 GB" and facts["Neural Engine"] == "16-core (spec)"
    assert facts["Neural Engine power"].startswith("Not reported")
    off = hwinfo.mac_card(None, TOPO)
    assert all(g["pct"] is None for g in off["gauges"]) and off["live"] is False


def test_device_card_specs_labeled():
    c = hwinfo.device_card("iPad14,6", 1024 * 10 ** 9, "connected", "iPadOS 27.2", "")
    facts = {f["k"]: f["v"] for f in c["facts"]}
    assert facts["Chip (spec)"] == "M2" and facts["RAM (spec)"] == "16 GB" and facts["GPU (spec)"] == "10-core"
    assert "Neural Accelerators (spec)" not in facts
    assert facts["Storage"] == "1024 GB" and c["gauges"] == []
    small = {f["k"]: f["v"] for f in hwinfo.device_card("iPad14,6", 256 * 10 ** 9, "", "", "")["facts"]}
    assert small["RAM (spec)"] == "8 GB"
    unknown = {f["k"]: f["v"] for f in hwinfo.device_card("iPhone99,9", None, "", "", "")["facts"]}
    assert "Not in the spec table" in unknown["Specs"] and unknown["Storage"] == "Not available"


def test_core_strip_uses_macos_labels_one_based():
    hist = [[10, 20, 30, 40, 50, 60], [11, 21, 31, 41, 51, 61]]
    c = hwinfo.mac_card({"core_busy": [1, 2, 3, 4, 5, 6]}, TOPO, "", hist)
    cores = c["core_strip"]["cores"]
    assert cores[0]["label"] == "Core 1" and cores[0]["cluster"] == "Performance" and cores[0]["samples"] == [10, 11]
    assert cores[5]["label"] == "Core 6" and cores[5]["cluster"] == "Super" and cores[5]["now"] == 6
    assert hwinfo.mac_card(None, TOPO, "", hist)["core_strip"] is None


def synthetic_watch_head(label):
    return hashlib.sha256(("zai-devdashd synthetic worker watch v1:" + label).encode()).hexdigest()[:40]


def test_worker_watch_loop_idle_and_stale():
    now = datetime(2025, 2, 18, 10, 40, tzinfo=ZoneInfo("America/Toronto")).timestamp()
    head = synthetic_watch_head("completed-failure")
    procs = [{"command": "/bin/zsh /example/loop.sh"}, {"command": "caffeinate -dims /example/loop.sh"}]
    files = [{"label": "eng-fixture-watch", "mtime": str(now - 700), "path": "/example/eng-fixture-watch/loop.out",
              "tail": [f"Tue Feb 18 10:14:10 EST 2025 run {head}",
                       f"Tue Feb 18 10:19:45 EST 2025 done {head} BUILD_EXIT:64 UNIT_EXIT:3"]},
             {"label": "unrelated-fixture", "mtime": str(now - 99999), "path": "/example/unrelated/old.log", "tail": []}]
    w = hwinfo.worker_watch(procs, files, now)
    assert w["loop_alive"] and not w["building"] and w["job"] == "Watching recorded for new commits"
    assert w["last"] == f"Last run {head[:8]} at 10:19: failed (BUILD, UNIT)"
    assert w["stale"] is True and w["status_as_of"] == now - 700
    busy = hwinfo.worker_watch(procs + [{"command": "xcodebuild build-for-testing"}], files, now)
    assert busy["building"] and busy["stale"] is False
    assert hwinfo.worker_watch([], [], now)["job"] == ""


def test_short_job_name():
    from zai_devdashd.aggregator import board_short_job
    assert board_short_job("zsh /example/synthetic-session/observe-fixture.sh") == "observe-fixture.sh"


def test_worker_watch_orphaned_marker_is_not_active():
    now = datetime(2025, 2, 18, 10, 40, tzinfo=ZoneInfo("America/Toronto")).timestamp()
    head = synthetic_watch_head("orphaned-incomplete")
    files = [{"label": "eng-fixture-watch", "mtime": str(now - 3 * 86400), "path": "/example/eng-fixture-watch/loop.out",
              "tail": [f"Sat Feb 15 08:25:30 EST 2025 run {head}"]}]
    other = [{"command": "/example/toolchain/xcodebuild test -project Fixture.xcodeproj -scheme Fixture Unit Tests"}]
    w = hwinfo.worker_watch(other, files, now)
    assert w["job"] == "" and w["last"] == "" and w["status_as_of"] is None and w["orphaned_marker"]
    fresh = [dict(files[0], mtime=str(now - 60))]
    assert hwinfo.worker_watch(other, fresh, now)["job"] == "Running recorded Mac gate (building)"


# Disk readings are physical capacity pools, not per-volume allocated blocks.
from types import SimpleNamespace
from zai_devdashd import hw_probe


def _disk_fixture():
    physical = {"AllDisksAndPartitions": [{"DeviceIdentifier": "disk0", "Partitions": []}]}
    inventory = {"AllDisksAndPartitions": [{"DeviceIdentifier": "disk3", "APFSPhysicalStores": [{"DeviceIdentifier": "disk0s2"}],
        "APFSVolumes": [{"DeviceIdentifier": "disk3s1s1", "VolumeName": "macOS", "MountPoint": "/"},
                        {"DeviceIdentifier": "disk3s2", "VolumeName": "macOS - Data", "MountPoint": "/System/Volumes/Data"}]}]}
    apfs = {"Containers": [{"ContainerReference": "disk3", "CapacityCeiling": 1000, "CapacityFree": 250,
                            "APFSContainerUUID": "DO_NOT_EXPORT_UUID", "Volumes": [{}, {}]}]}
    mounts = {"/": "disk3s1s1", "/System/Volumes/Data": "disk3s2"}
    return inventory, apfs, physical, mounts


def _rows(fixture, **kwargs):
    return hw_probe.disk_rows(*fixture, {"disk0": True}, 1234.5, canonical=lambda path: True,
                              statvfs=kwargs.pop("statvfs", lambda path: SimpleNamespace(f_blocks=1000, f_bavail=250, f_frsize=1)), **kwargs)


def test_apfs_startup_data_share_one_pool_and_preserve_byte_arithmetic():
    rows = _rows(_disk_fixture())
    assert len(rows) == 1
    disk = rows[0]
    assert disk["name"] == "Startup disk" and disk["mount"] == "/System/Volumes/Data"
    assert disk["container_id"] == "disk3" and disk["shared_capacity"] is True and disk["internal"] is True
    assert (disk["total_bytes"], disk["used_bytes"], disk["free_bytes"], disk["used_percent"]) == (1000, 750, 250, 75.0)
    assert len(disk["members"]) == 2 and disk["accounting"] == "APFS container"
    assert disk["as_of"] == 1234.5 and "sibling volumes and snapshots" in disk["note"]
    assert "DO_NOT_EXPORT_UUID" not in json.dumps(rows)


def test_missing_mount_never_reads_the_parent_filesystem():
    fixture = _disk_fixture()
    fixture[3].clear()
    def forbidden(path):
        raise AssertionError("must not read an unverified mount")
    row = _rows(fixture, statvfs=forbidden)[0]
    assert row["total_bytes"] is None and row["free_bytes"] is None and row["used_percent"] is None
    assert row["error"] == "Local disk mount not verified"


def test_actual_mount_device_must_match_inventory_even_if_path_exists():
    fixture = _disk_fixture()
    fixture[3].update({"/": "disk99s1", "/System/Volumes/Data": "disk99s1"})
    assert _rows(fixture)[0]["used_bytes"] is None


def test_inconsistent_apfs_capacity_is_unknown_not_clamped_or_replaced():
    for free in (-1, 1001, None):
        fixture = _disk_fixture()
        fixture[1]["Containers"][0]["CapacityFree"] = free
        row = _rows(fixture)[0]
        assert row["total_bytes"] is None and row["used_bytes"] is None
        assert "inconsistent" in row["error"]


def test_physical_local_pools_exclude_network_and_disk_images():
    fixture = _disk_fixture()
    fixture[0]["AllDisksAndPartitions"].extend([
        {"DeviceIdentifier": "disk5", "APFSPhysicalStores": [{"DeviceIdentifier": "disk99"}],
         "APFSVolumes": [{"DeviceIdentifier": "disk5s1", "VolumeName": "Mounted image", "MountPoint": "/Volumes/Image"}]},
        {"DeviceIdentifier": "disk7", "APFSPhysicalStores": [{"DeviceIdentifier": "disk0s3"}],
         "APFSVolumes": [{"DeviceIdentifier": "disk7s1", "VolumeName": "Model helper", "MountPoint": "/System/Library/AssetsV2/model"}]}])
    fixture[3].update({"/Volumes/Image": "disk5s1", "/Volumes/NAS": "server/share", "/System/Library/AssetsV2/model": "disk7s1"})
    assert len(_rows(fixture)) == 1


def test_internal_scratch_is_primary_shared_pool_member():
    fixture = _disk_fixture()
    volumes = fixture[0]["AllDisksAndPartitions"][0]["APFSVolumes"]
    volumes[:] = [{"DeviceIdentifier": "disk3s1", "VolumeName": "Macintosh HD", "MountPoint": "/Volumes/Macintosh HD"},
                  {"DeviceIdentifier": "disk3s7", "VolumeName": "BuildScratch", "MountPoint": "/Volumes/BuildScratch"}]
    fixture[3].update({v["MountPoint"]: v["DeviceIdentifier"] for v in volumes})
    row = _rows(fixture)[0]
    assert row["mount"] == "/Volumes/BuildScratch" and row["name"] == "BuildScratch"
    assert row["shared_capacity"] and row["total_bytes"] == row["used_bytes"] + row["free_bytes"]


def test_filesystem_capacity_for_non_apfs_physical_volume():
    fixture = _disk_fixture()
    fixture[0]["AllDisksAndPartitions"] = []
    fixture[2]["AllDisksAndPartitions"][0]["Partitions"] = [{"DeviceIdentifier": "disk0s3", "VolumeName": "Local USB", "Content": "Microsoft Basic Data", "MountPoint": "/Volumes/Local USB"}]
    fixture[3]["/Volumes/Local USB"] = "disk0s3"
    row = _rows(fixture)[0]
    assert row["accounting"] == "filesystem" and row["container_id"] is None
    assert row["total_bytes"] == 1000 and row["free_bytes"] == 250 and row["used_percent"] == 75.0


def test_df_local_mount_parser_preserves_spaces_and_filters_network():
    text = "Filesystem 512-blocks Used Available Capacity Mounted on\n/dev/disk3s7 1000 750 250 75% /Volumes/Internal Scratch\nserver/share 1000 0 1000 0% /Volumes/NAS\n"
    assert hw_probe._disk_mounts(text) == {"/Volumes/Internal Scratch": "disk3s7"}


def test_disk_metadata_budget_exhaustion_does_not_spawn_a_command(monkeypatch):
    monkeypatch.setattr(hw_probe.time, "monotonic", lambda: 5.0)
    def forbidden(*args, **kwargs):
        raise AssertionError("deadline exhausted")
    monkeypatch.setattr(hw_probe.subprocess, "run", forbidden)
    assert hw_probe._disk_plist(["list", "-plist"], 4.0) is None


def test_mac_card_forwards_disks_with_original_observation_and_offline_unknown(monkeypatch):
    monkeypatch.setattr(hwinfo.time, "time", lambda: 1235.0)
    disks = _rows(_disk_fixture())
    probe = {"as_of": 1234.5, "disks": disks, "disks_status": {"as_of": 1234.5, "partial": False, "error": None}}
    card = hwinfo.mac_card(probe, TOPO)
    assert card["disks"] == disks and card["disks_status"]["as_of"] == 1234.5
    assert hwinfo.mac_card(None, TOPO)["disks"] == []


def test_verified_mount_read_failure_and_missing_apfs_metadata_stay_unknown():
    def unavailable(path):
        raise OSError("SENSITIVE_DISK_ERROR")
    failed = _rows(_disk_fixture(), statvfs=unavailable)[0]
    assert failed["total_bytes"] is None and failed["used_percent"] is None
    assert "SENSITIVE_DISK_ERROR" not in json.dumps(failed)
    fixture = _disk_fixture()
    fixture[1].clear()
    absent = _rows(fixture)[0]
    assert absent["container_id"] == "disk3" and absent["free_bytes"] is None
    assert absent["error"]


def test_internal_location_is_unknown_without_actual_physical_info():
    row = hw_probe.disk_rows(*_disk_fixture(), {}, 1234.5, canonical=lambda path: True,
                              statvfs=lambda path: None)[0]
    assert row["internal"] is None and row["total_bytes"] == 1000


def test_disk_metadata_timeout_respects_remaining_budget(monkeypatch):
    import subprocess
    monkeypatch.setattr(hw_probe.time, "monotonic", lambda: 5.0)
    seen = []
    def timeout(args, **kwargs):
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setattr(hw_probe.subprocess, "run", timeout)
    assert hw_probe._disk_plist(["list", "-plist"], 5.25) is None
    assert seen == [0.25]


def test_malformed_diskutil_xml_is_storage_unavailable_instead_of_probe_crash(monkeypatch):
    import subprocess
    import time
    monkeypatch.setattr(hw_probe.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 0, b'<?xml version="1.0"?><plist><bad', b""))
    assert hw_probe._disk_plist(["list", "-plist"], time.monotonic() + 3) is None
