from xml.parsers.expat import ExpatError
# Read-only hardware probe. Runs as a script on the Mini and (over ssh stdin) on Worker with the system python3.
# Prints one JSON object. No sudo, no powermetrics, no serials or UUIDs.
import ctypes, json, os, plistlib, re, subprocess, sys, time

def sh(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""

def sysctl(name):
    return sh(["/usr/sbin/sysctl", "-n", name]).strip()

def core_ticks():
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    lib.mach_host_self.restype = ctypes.c_uint
    count = ctypes.c_uint(0)
    info = ctypes.POINTER(ctypes.c_int)()
    n = ctypes.c_uint(0)
    lib.host_processor_info.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                                        ctypes.POINTER(ctypes.POINTER(ctypes.c_int)), ctypes.POINTER(ctypes.c_uint)]
    if lib.host_processor_info(lib.mach_host_self(), 2, ctypes.byref(count), ctypes.byref(info), ctypes.byref(n)) != 0:
        return None
    return [tuple(info[i * 4 + k] & 0xFFFFFFFF for k in range(4)) for i in range(count.value)]

def read_ane():
    """Read the powermetrics sampler file ~/DevDashInputs/usage/ane-<hostname>.json (written by a separate job)."""
    import os, socket
    names = {socket.gethostname(), socket.gethostname().split(".")[0], sh(["/usr/sbin/scutil", "--get", "LocalHostName"]).strip()}
    paths = []  # No unconfigured external sampler input.
    # Hostname can change (e.g. after a repair); prefer the newest sampler file, not set order.
    paths.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    for path in paths:
        try:
            with open(path) as fh:
                d = json.load(fh)
            mtime = os.path.getmtime(path)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            keep = {k: d.get(k) for k in ("as_of", "ane_mw", "cpu_mw", "gpu_mw", "ane_peak_mw_seen")}
            keep["file_mtime"] = mtime
            keep["probe_time"] = time.time()
            return keep
    return None


DISK_BUDGET_S = 3.0


def _disk_plist(args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        result = subprocess.run(["/usr/sbin/diskutil", *args], capture_output=True,
                                timeout=min(1.0, remaining))
        data = plistlib.loads(result.stdout) if result.returncode == 0 else None
        return data if isinstance(data, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException, ExpatError):
        return None


def _disk_id(value):
    value = str(value or "").removeprefix("/dev/") if sys.version_info >= (3, 9) else str(value or "").replace("/dev/", "", 1)
    return value if re.fullmatch(r"disk[0-9]+(?:s[0-9]+)*", value) else ""


def _disk_mounts(text):
    mounts = {}
    for line in (text or "").splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6 and _disk_id(parts[0]):
            mounts[parts[5]] = _disk_id(parts[0])
    return mounts


def _user_mount(mount):
    return mount in ("/", "/System/Volumes/Data") or (os.path.dirname(mount) == "/Volumes" and not os.path.basename(mount).startswith("tmp-mount-"))


def _mount_rank(member):
    mount = member["mount"]
    return (0 if mount == "/System/Volumes/Data" else
            3 if mount == "/" else 2, mount)


def _capacity(total, free):
    if type(total) is not int or type(free) is not int or total <= 0 or not 0 <= free <= total:
        return {"total_bytes": None, "used_bytes": None, "free_bytes": None, "used_percent": None}
    return {"total_bytes": total, "used_bytes": total - free, "free_bytes": free,
            "used_percent": round(100.0 * (total - free) / total, 1)}


def disk_rows(inventory, apfs, physical, mounts, internal, as_of, canonical=None, statvfs=None):
    """One row per mounted physical capacity pool; no directory scans or UUID export."""
    canonical = (lambda path: os.path.realpath(path) == path and not os.path.islink(path)) if canonical is None else canonical
    statvfs = os.statvfs if statvfs is None else statvfs
    physical_ids = {_disk_id(d.get("DeviceIdentifier")) for d in physical.get("AllDisksAndPartitions", [])}
    physical_ids.discard("")
    containers = {c.get("ContainerReference"): c for c in (apfs or {}).get("Containers", [])}
    rows = []
    for disk in inventory.get("AllDisksAndPartitions", []):
        cid = _disk_id(disk.get("DeviceIdentifier"))
        stores = [re.match(r"disk[0-9]+", _disk_id(s.get("DeviceIdentifier"))) for s in disk.get("APFSPhysicalStores", [])]
        roots = {m.group(0) for m in stores if m}
        if not roots or not roots.issubset(physical_ids):
            continue
        members = []
        for volume in disk.get("APFSVolumes", []):
            mount = volume.get("MountPoint") or ""
            if not _user_mount(mount):
                continue
            member = {"name": str(volume.get("VolumeName") or mount), "mount": mount}
            member["verified"] = mounts.get(mount) == _disk_id(volume.get("DeviceIdentifier")) and canonical(mount)
            members.append(member)
        if not members:
            continue
        members.sort(key=_mount_rank)
        verified = [m for m in members if m["verified"]]
        primary = verified[0] if verified else members[0]
        c = containers.get(cid) or {}
        row = {"name": "Startup disk" if any(m["mount"] == "/System/Volumes/Data" for m in members) else primary["name"],
               "mount": primary["mount"], "container_id": cid, "shared_capacity": len(c.get("Volumes") or disk.get("APFSVolumes") or []) > 1,
               "internal": all(internal.get(root) is True for root in roots) if all(root in internal for root in roots) else None,
               "members": members, "accounting": "APFS container", "as_of": as_of,
               "source": "diskutil APFS container capacity; df and mount verified", "error": None}
        values = _capacity(c.get("CapacityCeiling"), c.get("CapacityFree"))
        if not verified:
            row["error"] = "Local disk mount not verified"
        else:
            try:
                statvfs(primary["mount"])
            except OSError:
                row["error"] = "Local disk capacity inspection unavailable"
            if values["total_bytes"] is None:
                row["error"] = row["error"] or "APFS container capacity unavailable or inconsistent"
        row.update(_capacity(None, None) if row["error"] else values)
        if row["shared_capacity"]:
            row["note"] = "Shared APFS pool occupancy, including sibling volumes and snapshots; not this volume's allocation"
        else:
            row["note"] = "APFS container occupancy, including snapshots and filesystem metadata"
        rows.append(row)
    # Mounted non-APFS physical partitions use filesystem capacity, never network or images.
    for disk in physical.get("AllDisksAndPartitions", []):
        for volume in [disk, *(disk.get("Partitions") or [])]:
            mount = volume.get("MountPoint") or ""
            if not _user_mount(mount) or volume.get("Content") == "Apple_APFS":
                continue
            device = _disk_id(volume.get("DeviceIdentifier"))
            if any(m["mount"] == mount for r in rows for m in r["members"]):
                continue
            row = {"name": str(volume.get("VolumeName") or mount), "mount": mount, "container_id": None,
                   "shared_capacity": False, "internal": internal.get(disk.get("DeviceIdentifier")),
                   "accounting": "filesystem", "as_of": as_of, "members": [], "source": "statvfs on verified local disk mount", "error": None}
            values = _capacity(None, None)
            if mounts.get(mount) != device or not canonical(mount):
                row["error"] = "Local disk mount not verified"
            else:
                try:
                    st = statvfs(mount)
                    values = _capacity(st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize)
                    if values["total_bytes"] is None:
                        row["error"] = "Filesystem capacity unavailable or inconsistent"
                except OSError:
                    row["error"] = "Local disk capacity inspection unavailable"
            row.update(values)
            rows.append(row)
    rows.sort(key=lambda r: (r["name"] != "Startup disk", r["mount"]))
    return rows


def read_disks(budget_s=DISK_BUDGET_S):
    deadline = time.monotonic() + budget_s
    as_of = time.time()
    physical = _disk_plist(["list", "-plist", "physical"], deadline)
    inventory = _disk_plist(["list", "-plist"], deadline)
    apfs = _disk_plist(["apfs", "list", "-plist"], deadline)
    mounts, internal = {}, {}
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            result = subprocess.run(["/bin/df", "-Pl"], capture_output=True, text=True, timeout=min(0.5, remaining))
            mounts = _disk_mounts(result.stdout) if result.returncode == 0 else {}
        except (OSError, subprocess.SubprocessError):
            pass
    for disk in (physical or {}).get("AllDisksAndPartitions", []):
        info = _disk_plist(["info", "-plist", disk.get("DeviceIdentifier") or ""], deadline)
        if info and type(info.get("Internal")) is bool:
            internal[disk["DeviceIdentifier"]] = info["Internal"]
    rows = disk_rows(inventory or {}, apfs, physical or {}, mounts, internal, as_of)
    error = "Local disk inventory unavailable" if physical is None or inventory is None else None
    if not mounts:
        error = error or "Local disk mount inspection unavailable"
    if not rows and not error:
        error = "No mounted local data disks reported"
    return rows, {"as_of": as_of, "partial": bool(error or any(r["error"] for r in rows)), "error": error,
                  "coverage": "Mounted local physical data pools; network and disk images excluded", "budget_s": budget_s}


def main():
    topo = len(sys.argv) > 1 and sys.argv[1] == "topo" or "TOPO=1" in sys.argv
    out = {"ok": True}
    a = core_ticks()
    if topo:
        levels = []
        for i in range(int(sysctl("hw.nperflevels") or 0)):
            levels.append({"name": sysctl("hw.perflevel%d.name" % i), "cores": int(sysctl("hw.perflevel%d.physicalcpu" % i) or 0)})
        prof = sh(["/usr/sbin/system_profiler", "SPHardwareDataType", "SPDisplaysDataType"], 30)
        cores = re.findall(r"Total Number of Cores:\s*(\d+)(?:\s*\(([^)]*)\))?", prof)
        chip = re.search(r"Chip(?:set Model)?:\s*(.+)", prof)
        out["topology"] = {
            "chip": (chip.group(1).strip() if chip else sysctl("machdep.cpu.brand_string")),
            "model_id": sysctl("hw.model"),
            "cpu_cores": int(sysctl("hw.ncpu") or 0),
            "cpu_breakdown": cores[0][1] if cores else "",
            "perflevels": levels,
            "gpu_cores": int(cores[1][0]) if len(cores) > 1 else None,
            "memory_gb": int(int(sysctl("hw.memsize") or 0) / 2 ** 30),
            "os": sysctl("kern.osproductversion"), "build": sysctl("kern.osversion"),
            "ane_present": bool(re.search(r'"ane"|H11ANE|ANEDevice', sh(["/usr/sbin/ioreg", "-l", "-w0", "-p", "IODeviceTree", "-n", "ane"]) or "")) or "ane" in sh(["/usr/sbin/ioreg", "-c", "H11ANEIn", "-d", "1"]).lower(),
        }
        clusters = {}
        tree = sh(["/usr/sbin/ioreg", "-l", "-w0", "-p", "IODeviceTree"], 20)
        cur_type = cur_id = None
        for line in tree.splitlines():
            m = re.search(r'"cluster-type" = <"(\w)">', line)
            if m:
                cur_type = m.group(1)
            m = re.search(r'"logical-cpu-id" = (\d+)', line)
            if m:
                cur_id = int(m.group(1))
            if cur_type is not None and cur_id is not None:
                clusters[cur_id] = cur_type
                cur_type = cur_id = None
            if "+-o cpu" in line:
                cur_type = cur_id = None
        out["topology"]["cluster_by_cpu"] = [clusters.get(i, "?") for i in range(out["topology"]["cpu_cores"])]
    time.sleep(1.0)
    b = core_ticks()
    if a and b and len(a) == len(b):
        per = []
        for x, y in zip(a, b):
            d = [y[k] - x[k] for k in range(4)]
            tot = sum(d)
            per.append(round(100.0 * (tot - d[2]) / tot, 1) if tot > 0 else 0.0)
        out["core_busy"] = per
    gpu = re.findall(r'Device Utilization %"\s*=\s*(\d+)', sh(["/usr/sbin/ioreg", "-r", "-d", "1", "-c", "IOAccelerator"]))
    out["gpu"] = max(int(g) for g in gpu) if gpu else None
    mp = re.search(r"free percentage:\s*(\d+)", sh(["/usr/bin/memory_pressure", "-Q"]))
    out["mem_free_pct"] = int(mp.group(1)) if mp else None
    sw = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", sysctl("vm.swapusage"))
    out["swap_used_gb"] = round(float(sw.group(2)) / 1024, 1) if sw else None
    out["swap_total_gb"] = round(float(sw.group(1)) / 1024, 1) if sw else None
    wl = sysctl("iogpu.wired_limit_mb")
    out["wired_limit_gb"] = round(int(wl) / 1024, 1) if wl.isdigit() and int(wl) > 0 else None
    therm = sh(["/usr/bin/pmset", "-g", "therm"])
    lim = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", therm)
    if lim:
        out["thermal"] = "Nominal" if int(lim.group(1)) >= 100 else "Throttled to %s%%" % lim.group(1)
    elif "No thermal warning level has been recorded" in therm:
        out["thermal"] = "Nominal (no thermal warning)"
    else:
        out["thermal"] = None
    out["ane"] = read_ane()
    out["disks"], out["disks_status"] = read_disks()
    print("HWJSON " + json.dumps(out))

if __name__ == "__main__":
    main()
