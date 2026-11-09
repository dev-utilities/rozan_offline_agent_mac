"""How much memory macOS can hand out right now, without swapping."""
from __future__ import annotations

import os
import shutil
import subprocess


def free_gb() -> float:
    """macOS's own estimate: `memory_pressure` free percentage x physical RAM (counts reclaimable cache)."""
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
    pct = float(out.split("System-wide memory free percentage:")[1].split("%")[0])
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=10).stdout)
    return pct / 100 * total / 2**30


def swap_used_gb() -> float:
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=10).stdout
    return float(out.split("used = ")[1].split("M")[0]) / 1024


def used_percent() -> float:
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
    return 100.0 - float(out.split("System-wide memory free percentage:")[1].split("%")[0])


def _swap_file_free_gb() -> float:
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=10).stdout
    return float(out.split("free = ")[1].split("M")[0]) / 1024


DISK_RESERVE_GB = 10.0   # disk kept free for everything else; swap may only grow into the rest


def swap_free_gb() -> float:
    """Room left to swap into. macOS grows swap with new files on demand, so the free space inside the
    current swap files alone is misleading (it is often under 1 GB on a healthy Mac). Room = that free
    space + startup-disk space above a reserve."""
    disk = shutil.disk_usage("/System/Volumes/VM" if os.path.exists("/System/Volumes/VM") else "/").free / 2**30
    return _swap_file_free_gb() + max(0.0, disk - DISK_RESERVE_GB)


def pressure_level() -> int:
    """macOS's own memory pressure verdict: 1 normal, 2 warning, 4 critical."""
    out = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                         capture_output=True, text=True, timeout=10).stdout
    try:
        return int(out.strip())
    except ValueError:
        return 1


def snapshot() -> dict:
    return {"used_percent": round(used_percent(), 1), "swap_free_gb": round(swap_free_gb(), 2),
            "pressure": pressure_level()}
