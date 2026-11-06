"""Conservative read-only resource guard for one owned Apple CLI client."""
from __future__ import annotations

import subprocess


def snapshot():
    result = subprocess.run(["/bin/ps", "-axo", "pid=,rss=,lstart=,comm="],
                            capture_output=True, text=True, timeout=2)
    if result.returncode:
        raise RuntimeError("Model resource observation unavailable")
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 7)
        if len(fields) != 8:
            raise RuntimeError("Model resource observation incomplete")
        rows[(int(fields[0]), " ".join(fields[2:7]))] = int(fields[1]) * 1024
    if not rows:
        raise RuntimeError("Model resource observation empty")
    pressure = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                              capture_output=True, text=True, timeout=2)
    if pressure.returncode or pressure.stdout.strip() != "1":
        raise RuntimeError("Model memory pressure is not normal or cannot be observed")
    return rows


class Guard:
    def __init__(self, observe=snapshot):
        self.observe = observe
        self.before = observe()
        self.peak_growth = None
        self.peak_client = None
        self.samples = 0
        self.client_samples = 0

    def check(self, pid):
        rows = self.observe()
        # Identity includes process birth; unrelated frees never offset growth.
        growth = sum(max(0, rss - self.before.get(identity, 0)) for identity, rss in rows.items())
        clients = [rss for (number, _), rss in rows.items() if number == pid]
        self.samples += 1
        self.peak_growth = max(self.peak_growth or 0, growth)
        if clients:
            self.client_samples += 1
            self.peak_client = max([self.peak_client or 0] + clients)
        if growth > 4 * 1024**3 or any(value > 256 * 1024**2 for value in clients):
            raise RuntimeError("Owned Apple client resource bound exceeded")
        if not clients:
            raise ProcessLookupError("Owned Apple client no longer observable")

    def receipt(self):
        return {"client_peak_rss_bytes": self.peak_client,
                "conservative_positive_host_rss_growth_bytes": self.peak_growth,
                "resource_observations": self.samples, "client_observations": self.client_samples,
                "client_limit_bytes": 256 * 1024**2, "host_growth_limit_bytes": 4 * 1024**3}
