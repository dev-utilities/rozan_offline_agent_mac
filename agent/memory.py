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


def app_name(path: str) -> str:
    """The app a process belongs to: the first `.app` in its path ("…/Google Chrome.app/…/Helper (Renderer).app/…"
    → "Google Chrome"); otherwise the program name."""
    for part in path.split("/"):
        if part.endswith(".app"):
            return part[:-4]
    return os.path.basename(path) or path


def top_apps(limit: int = 5, own_pid: int | None = None) -> list[dict]:
    """Apps using the most memory right now, all processes of an app added together. Rozan's own core and the
    Ollama model it runs are named as such, so the owner can see when Rozan itself is the cause."""
    own_pid = own_pid or os.getpid()
    out = subprocess.run(["ps", "-axo", "pid=,rss=,comm="], capture_output=True, text=True, timeout=10).stdout
    totals: dict[str, float] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, rss, path = int(parts[0]), int(parts[1]), parts[2]
        name = app_name(path)
        if pid == own_pid:
            name = "Rozan (its core)"
        elif name.lower() == "ollama":
            name = "Ollama (Rozan's AI model)"
        elif name in ("PersonalAgent", "Rozan"):
            name = "Rozan"
        totals[name] = totals.get(name, 0.0) + rss / 2**20
    top = sorted(totals.items(), key=lambda kv: -kv[1])[:limit]
    return [{"name": n, "memory_gb": round(g, 1)} for n, g in top]


def snapshot() -> dict:
    return {"used_percent": round(used_percent(), 1), "swap_free_gb": round(swap_free_gb(), 2),
            "pressure": pressure_level()}
