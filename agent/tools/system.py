"""Read-only system information with plain-language summaries (no shell needed)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from pathlib import Path

from .. import memory as mem
from . import Registry, ToolSpec


def _gb(n: float) -> str:
    return f"{n:.1f} GB"


HELPER = Path(__file__).resolve().parents[2] / "bin" / "input_sources"   # built by scripts/build_mac_app.sh


def _keyboards(*args: str):
    """Run the input-source helper; returns parsed JSON."""
    import json
    if not HELPER.exists():
        raise RuntimeError("the keyboard helper is not built yet (run scripts/build_mac_app.sh)")
    r = subprocess.run([str(HELPER), *args], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"keyboard helper failed ({r.returncode})")
    return json.loads(r.stdout)


def _pick(found: list, language: str):
    """Best match for "Urdu": the plain layout named exactly that, else one whose main language matches,
    preferring non-transliteration layouts."""
    q = language.strip().lower()
    exact = [s for s in found if s["name"].lower() == q]
    if exact:
        return exact[0]
    plain = [s for s in found if "translit" not in s["name"].lower() and "phonetic" not in s["name"].lower()]
    return (plain or found or [None])[0]


def register(reg: Registry, policy) -> None:
    def system_memory():
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30
        used_pct = mem.used_percent()
        free = total * (100 - used_pct) / 100
        swap_used, swap_free = mem.swap_used_gb(), mem.swap_free_gb()
        return {"total_gb": round(total, 1), "free_gb": round(free, 1), "used_percent": round(used_pct),
                "swap_used_gb": round(swap_used, 1), "swap_free_gb": round(swap_free, 1),
                "summary": f"{_gb(free)} of {_gb(total)} memory is free ({used_pct:.0f}% in use). "
                           f"Swap: {_gb(swap_used)} used, {_gb(swap_free)} left."}

    def system_disk():
        home = shutil.disk_usage(os.path.expanduser("~"))
        return {"total_gb": round(home.total / 2**30, 1), "free_gb": round(home.free / 2**30, 1),
                "summary": f"{_gb(home.free / 2**30)} free of {_gb(home.total / 2**30)} on your disk."}

    def system_battery():
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"(\d+)%;\s*([^;]+);\s*([^\n]*)", out)
        if not m:
            return {"summary": "No battery found (desktop Mac or no reading)."}
        pct, status, rest = m.group(1), m.group(2).strip(), m.group(3).strip()
        source = "on power adapter" if "AC Power" in out else "on battery"
        return {"percent": int(pct), "status": status, "summary": f"Battery {pct}%, {status}, {source}. {rest}".strip()}

    def system_top_apps(limit: int = 8):
        top = mem.top_apps(max(1, min(int(limit), 20)))
        return {"apps": top, "summary": "Using the most memory: " + ", ".join(f"{a['name']} {_gb(a['memory_gb'])}" for a in top) + "."}

    def system_keyboards():
        on = _keyboards("list")
        names = [k["name"] for k in on]
        return {"keyboards": names, "summary": "Keyboards turned on: " + (", ".join(names) or "none") + "."}

    def system_add_keyboard(language: str):
        found = _keyboards("find", language)
        if not found:
            return {"summary": f"macOS has no keyboard for {language!r}."}
        best = _pick(found, language)
        if best["enabled"]:
            return {"keyboard": best["name"], "summary": f"The {best['name']} keyboard is already on. "
                                                        "Switch with Control-Space or the menu bar."}
        _keyboards("enable", best["id"])
        others = [s["name"] for s in found if s["id"] != best["id"]][:4]
        more = f" Other {language} keyboards you could add: {', '.join(others)}." if others else ""
        return {"keyboard": best["name"], "id": best["id"],
                "summary": f"Added the {best['name']} keyboard. Switch to it with Control-Space or from the "
                           f"input menu in the menu bar.{more}"}

    def system_remove_keyboard(language: str):
        on = _keyboards("list")
        q = language.strip().lower()
        hit = [k for k in on if q in k["name"].lower()]
        if not hit:
            return {"summary": f"No keyboard matching {language!r} is turned on."}
        if len(on) - len(hit) < 1:
            return {"summary": "Not removed: that would leave no keyboard turned on."}
        for k in hit:
            _keyboards("disable", k["id"])
        return {"summary": "Removed: " + ", ".join(k["name"] for k in hit) + "."}

    R = "{summary, ...numbers}"
    reg.add(ToolSpec("system.keyboards", system_keyboards, "Keyboards (input sources) that are turned on.", {},
                     source="system", returns=R))
    reg.add(ToolSpec("system.add_keyboard", system_add_keyboard,
                     "Turn on a keyboard / input source for a language, e.g. Urdu, Arabic, French.",
                     {"language": "language name, e.g. Urdu"}, source="system", returns=R))
    reg.add(ToolSpec("system.remove_keyboard", system_remove_keyboard,
                     "Turn off a keyboard / input source (never the last one).",
                     {"language": "language or keyboard name"}, source="system", returns=R))
    reg.add(ToolSpec("system.memory", system_memory, "Free/used memory and swap, with a plain summary.", {},
                     source="system", returns=R))
    reg.add(ToolSpec("system.disk", system_disk, "Free disk space, with a plain summary.", {}, source="system", returns=R))
    reg.add(ToolSpec("system.battery", system_battery, "Battery level and charging state.", {}, source="system", returns=R))
    reg.add(ToolSpec("system.top_apps", system_top_apps, "Apps using the most memory.", {"limit": "how many (default 8)"},
                     source="system", returns="{apps: [{name, memory_gb}], summary}"))
