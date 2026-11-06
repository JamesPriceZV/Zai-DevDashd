"""Hardware cards for the Computers and devices panel.

Macs: live readings from hw_probe.py (sysctl, system_profiler, ioreg, host_processor_info; no sudo).
iOS/iPadOS: published Apple specs looked up by model identifier (SPECS below), plus live connection/storage
from `devicectl list devices`. Spec values are labeled as specs, never as live readings.
"""
from __future__ import annotations

import copy
import json
import time
import re
from pathlib import Path

from .resources_v2 import cpu_label

PROBE = Path(__file__).with_name("hw_probe.py")
ANE_NOTE = "Not measurable without admin rights (needs powermetrics/sudo, which is not set up)"
IOS_CORE_NOTE = "Per-core use is not available from iPhone/iPad unless a running test exports it"

# Published Apple specs (apple.com/iphone/compare, apple.com/ipad/compare, support.apple.com tech specs).
# Neural Accelerators (in each GPU core) exist only on A19/M5-family chips; none of these devices have them.
SPECS = {
    "iPhone15,4": {"chip": "A16 Bionic", "cpu_p": 2, "cpu_e": 4, "gpu": 5, "ne": 16, "ram_gb": 6, "neural_accel": None},
    "iPhone16,1": {"chip": "A17 Pro", "cpu_p": 2, "cpu_e": 4, "gpu": 6, "ne": 16, "ram_gb": 8, "neural_accel": None},
    "iPhone17,1": {"chip": "A18 Pro", "cpu_p": 2, "cpu_e": 4, "gpu": 6, "ne": 16, "ram_gb": 8, "neural_accel": None},
    "iPhone17,2": {"chip": "A18 Pro", "cpu_p": 2, "cpu_e": 4, "gpu": 6, "ne": 16, "ram_gb": 8, "neural_accel": None},
    # iPad Pro M1/M2: 8 GB RAM, 16 GB on 1 TB and 2 TB models.
    "iPad13,4": {"chip": "M1", "cpu_p": 4, "cpu_e": 4, "gpu": 8, "ne": 16, "ram_gb": 8, "ram_gb_1tb": 16, "neural_accel": None},
    "iPad14,6": {"chip": "M2", "cpu_p": 4, "cpu_e": 4, "gpu": 10, "ne": 16, "ram_gb": 8, "ram_gb_1tb": 16, "neural_accel": None},
}
# Mac chip specs that the machine does not report itself (Neural Engine core count, Neural Accelerators).
MAC_CHIP_SPECS = {
    "Apple M5 Pro": {"ne": 16, "neural_accel": "One per GPU core (M5 family)"},
    "Apple M2": {"ne": 16, "neural_accel": None},
}
CLUSTER_FALLBACK = {"E": "Efficiency", "P": "Performance", "M": "Performance", "S": "Super"}


def cpu_text(topo: dict) -> str:
    """Activity Monitor style, e.g. '18-core: 12 Performance + 6 Super', from sysctl perflevels (any chip)."""
    lbl = cpu_label(topo.get("perflevels"), topo.get("cpu_cores"))
    if lbl:
        return lbl
    if topo.get("cpu_cores"):
        return "%s-core%s" % (topo["cpu_cores"], (" (%s)" % topo["cpu_breakdown"]) if topo.get("cpu_breakdown") else "")
    return "Not available"


def parse_probe(text: str) -> dict | None:
    for line in (text or "").splitlines():
        if line.startswith("HWJSON "):
            try:
                data = json.loads(line[7:])
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def cluster_names(topology: dict) -> dict:
    """Map ioreg cluster letters to sysctl perflevel names by matching core counts."""
    letters = topology.get("cluster_by_cpu") or []
    counts: dict = {}
    for letter in letters:
        counts[letter] = counts.get(letter, 0) + 1
    names = {}
    levels = list(topology.get("perflevels") or [])
    for letter, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        match = next((lv for lv in levels if lv.get("cores") == n), None)
        if match:
            names[letter] = match["name"]
            levels.remove(match)
        else:
            names[letter] = CLUSTER_FALLBACK.get(letter, "Cluster " + letter)
    return names


def cluster_gauges(topology: dict, busy: list | None) -> list:
    letters = topology.get("cluster_by_cpu") or []
    names = cluster_names(topology)
    groups: dict = {}
    for i, letter in enumerate(letters):
        groups.setdefault(letter, []).append(i)
    out = []
    order = sorted(groups, key=lambda l: ("SPMEU?".find(l) if l in "SPMEU" else 9))
    for letter in order:
        idx = groups[letter]
        vals = [busy[i] for i in idx if busy and i < len(busy)]
        nm = names.get(letter, letter)
        out.append({"label": "%s cores (%d)" % (nm, len(idx)), "short": SHORT_CLUSTER.get(nm, nm), "sub": str(len(idx)),
                    "pct": round(sum(vals) / len(vals)) if vals else None, "kind": "cpu"})
    return out


SHORT_CLUSTER = {"Performance": "P-cores", "Efficiency": "E-cores", "Super": "Super"}


def core_strip(topology: dict, history: list | None) -> dict | None:
    """Per-core history like Activity Monitor's CPU History: core labels (1-based, Apple's perflevel names) and samples."""
    letters = (topology or {}).get("cluster_by_cpu") or []
    if not letters or not history:
        return None
    names = cluster_names(topology)
    cores = []
    for i, letter in enumerate(letters):
        cores.append({"label": "Core %d" % (i + 1), "cluster": names.get(letter, letter),
                      "samples": [round(h[i]) for h in history if h and i < len(h)]})
    return {"cores": cores, "interval_s": 15}


ANE_STALE_S = 60
ANE_HISTORY: dict = {}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def ane_gauge(ane: dict | None, live: bool = True) -> dict:
    """Neural Engine power from the powermetrics sampler file. Power in mW, not a busy percentage."""
    g = {"label": "ANE power (mW, not busy %)", "short": "ANE", "sub": "power", "pct": None, "kind": "ane", "value_text": None,
         "stale": False, "note": "not reported"}
    if not live or not isinstance(ane, dict):
        return g
    mw, peak = _num(ane.get("ane_mw")), _num(ane.get("ane_peak_mw_seen"))
    if mw is None:
        return g
    ref = _num(ane.get("probe_time")) or 0
    as_of = _num(ane.get("as_of"))
    if as_of is None and isinstance(ane.get("as_of"), str):
        from .usage import iso_epoch
        as_of = iso_epoch(ane["as_of"])
    as_of = as_of or _num(ane.get("file_mtime"))
    age = (ref - as_of) if (ref and as_of) else None
    g["stale"] = age is None or age > ANE_STALE_S
    if g["stale"]:
        g["note"] = "not reported (sampler file is stale%s)" % ((": last sample %ds ago" % age) if age is not None else "")
        g["sub"] = "power · stale"
        return g
    hist = ANE_HISTORY.setdefault(str(ane.get("host") or "local"), [])
    if not hist or hist[-1][0] != as_of:
        hist.append((as_of, mw))
    del hist[:-200]
    g["spark"] = [round(v) for t, v in hist if t >= ref - 600]
    if not peak:
        g["pct"] = 0.0
        g["value_text"] = "idle"
        g["note"] = "idle, no ANE activity seen yet (0 mW; power draw, not a busy percentage)"
        return g
    g["pct"] = round(min(100.0, mw / peak * 100), 1)
    g["value_text"] = "%d mW" % round(mw)
    share = ("%s%% of highest seen (%d mW)" % (g["pct"], round(peak))) if g["pct"] is not None else "highest seen not reported"
    g["note"] = "%d mW, %s. This is power draw, not a busy percentage." % (round(mw), share)
    return g


def mac_card(probe: dict | None, topology: dict | None, job: str = "", history: list | None = None) -> dict:
    topo = topology or {}
    p = probe or {}
    gauges = cluster_gauges(topo, p.get("core_busy")) if topo else [{"label": "CPU", "short": "CPU", "sub": "", "pct": None, "kind": "cpu"}]
    gauges.append({"label": "GPU (%s cores)" % (topo.get("gpu_cores") or "?"), "short": "GPU", "sub": "%s cores" % (topo.get("gpu_cores") or "?"), "pct": p.get("gpu"), "kind": "gpu"})
    mem_used = None if p.get("mem_free_pct") is None else 100 - int(p["mem_free_pct"])
    gauges.append({"label": "Memory pressure", "short": "Memory", "sub": "pressure", "pct": mem_used, "kind": "mem"})
    ane = p.get("ane")
    if isinstance(ane, dict):
        ane = dict(ane, host=topo.get("model_id", "") + ":" + str(topo.get("chip", "")))
    gauges.append(ane_gauge(ane, probe is not None))
    chip = topo.get("chip") or ""
    cspec = MAC_CHIP_SPECS.get(chip, {})
    facts = [
        ("Chip", chip or "Not available"),
        ("Model", topo.get("model_id") or "Not available"),
        ("CPU cores", cpu_text(topo)),
        ("GPU cores", str(topo.get("gpu_cores") or "Not available")),
        ("Neural Engine", ("%d-core (spec)" % cspec["ne"]) if cspec.get("ne") else ("Present" if topo.get("ane_present") else "Not available")),
        ("Neural Accelerators", (cspec.get("neural_accel") + " (spec)") if cspec.get("neural_accel") else "None on this chip" if cspec else "Not available"),
        ("Unified memory", "%s GB" % topo["memory_gb"] if topo.get("memory_gb") else "Not available"),
        ("GPU wired memory limit", "%s GB" % p["wired_limit_gb"] if p.get("wired_limit_gb") else "System default"),
        ("Swap", "%s of %s GB used" % (p.get("swap_used_gb"), p.get("swap_total_gb")) if p.get("swap_total_gb") is not None else "Not available"),
        ("Thermal", p.get("thermal") or "Not available"),
        ("Neural Engine power", gauges[-1]["note"] if gauges[-1]["note"] != "not reported" else "Not reported (no sampler file ane-<hostname>.json)"),
        ("macOS", "%s (%s)" % (topo.get("os"), topo.get("build")) if topo.get("os") else "Not available"),
        ("Current job", job or "Idle"),
    ]
    headline = " · ".join(x for x in (chip, "%s-core CPU" % topo["cpu_cores"] if topo.get("cpu_cores") else "",
                                      "%s-core GPU" % topo["gpu_cores"] if topo.get("gpu_cores") else "",
                                      "%s GB" % topo["memory_gb"] if topo.get("memory_gb") else "") if x)
    strip = core_strip(topo, history) if probe else None
    if strip and p.get("core_busy"):
        for core, now_pct in zip(strip["cores"], p["core_busy"]):
            core["now"] = round(now_pct)
    source_at = _num(p.get("as_of"))
    stale = bool(p.get("stale") or p.get("error") or source_at is None or time.time() - source_at > 45)
    disks = copy.deepcopy(list(p.get("disks") or []))
    disk_status = copy.deepcopy(p.get("disks_status") or {})
    if stale:
        disk_status.update(partial=True, stale=True,
                           error=p.get("error") or "Hardware readings have not refreshed")
        for disk in disks:
            disk["stale"] = True
    return {"kind": "mac", "headline": headline, "core_strip": strip, "gauges": gauges, "facts": [{"k": k, "v": v} for k, v in facts],
            "live": bool(probe), "as_of": source_at, "attempted_at": p.get("attempted_at"),
            "stale": stale, "error": p.get("error"),
            "disks": disks, "disks_status": disk_status}


def device_card(product_type: str, storage_bytes, state: str, os_label: str, job: str = "") -> dict:
    spec = SPECS.get(product_type or "")
    storage_gb = None
    try:
        storage_gb = round(int(storage_bytes) / 1e9) if storage_bytes else None
    except (TypeError, ValueError):
        storage_gb = None
    facts = []
    if spec:
        ram = spec["ram_gb"]
        if spec.get("ram_gb_1tb") and storage_gb and storage_gb >= 900:
            ram = spec["ram_gb_1tb"]
        facts += [
            ("Chip (spec)", spec["chip"]),
            ("CPU (spec)", cpu_label([{"name": "Performance", "cores": spec["cpu_p"]}, {"name": "Efficiency", "cores": spec["cpu_e"]}])),
            ("GPU (spec)", "%d-core" % spec["gpu"]),
            ("Neural Engine (spec)", "%d-core" % spec["ne"]),
            ("RAM (spec)", "%d GB" % ram),
        ]
        if spec.get("neural_accel"):
            facts.append(("Neural Accelerators (spec)", spec["neural_accel"]))
        headline = "%s · %d-core CPU · %d-core GPU" % (spec["chip"], spec["cpu_p"] + spec["cpu_e"], spec["gpu"])
    else:
        facts.append(("Specs", "Not in the spec table for model %s" % (product_type or "unknown")))
        headline = product_type or "Unknown model"
    facts += [
        ("Model identifier", product_type or "Not available"),
        ("Storage", "%d GB" % storage_gb if storage_gb else "Not available"),
        ("OS", os_label or "Not available"),
        ("Connection (live)", state or "Not available"),
        ("Battery / thermal", "Not reported by the device list"),
        ("Per-core use", IOS_CORE_NOTE),
        ("Current job", job or "Idle"),
    ]
    return {"kind": "device", "headline": headline, "gauges": [], "facts": [{"k": k, "v": v} for k, v in facts],
            "live": False}


STATUS_STALE_S = 600
ACTIVE_MARKER_S = 1800
LOOP_LINE_RE = re.compile(r"^(\w{3} \w{3} +\d+ [\d:]+) \w+ \d{4} (run|done) ([0-9a-f]{7,40})(.*)$")


def worker_watch(processes: list, files: list, now: float) -> dict:
    """What the configured Worker loop is really doing, from live processes and loop.out (read-only)."""
    cmds = [str(p.get("command") or "") for p in processes or []]
    loop = any("loop.sh" in c for c in cmds)
    gate = any("mac-gate" in c for c in cmds)
    building = any("xcodebuild" in c for c in cmds)
    eng = [f for f in files or [] if str(f.get("label") or "").startswith("eng-")]
    newest = max([float(f.get("mtime") or 0) for f in (eng or files or [])] or [0]) or None
    last = ""
    for f in files or []:
        if str(f.get("path") or "").endswith("loop.out"):
            for line in reversed(f.get("tail") or []):
                m = LOOP_LINE_RE.match(line.strip())
                if m:
                    sha = m.group(3)[:8]
                    if m.group(2) == "run":
                        last = "Testing %s since %s" % (sha, m.group(1).split()[-1][:5])
                    else:
                        bad = re.findall(r"(\w+)_EXIT:(\d+)", m.group(4))
                        failed = [k for k, v in bad if v != "0"]
                        last = "Last run %s at %s: %s" % (sha, m.group(1).split()[-1][:5],
                                                        "failed (" + ", ".join(failed) + ")" if failed else "passed")
                    break
    # The recorded loop markers count only while alive: a loop/gate process is running, or its
    # newest eng- file changed in the last ACTIVE_MARKER_S. Any other xcodebuild is not recorded.
    marker_fresh = bool(eng and newest and now - newest < ACTIVE_MARKER_S)
    ours = loop or gate or (building and marker_fresh)
    if not ours:
        last = ""  # loop stopped or orphaned: do not show its frozen loop.out line
        return {"loop_alive": False, "gate_running": False, "building": building, "job": "", "last": "",
                "status_as_of": None, "stale": False, "orphaned_marker": bool(eng)}
    if gate or building:
        job = "Running recorded Mac gate" + (" (building)" if building else "")
    elif loop:
        job = "Watching recorded for new commits"
    else:
        job = ""
    return {"loop_alive": loop, "gate_running": gate, "building": building, "job": job, "last": last,
            "status_as_of": newest, "stale": bool(loop and newest and now - newest > STATUS_STALE_S and not (gate or building))}
