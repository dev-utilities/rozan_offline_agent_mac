"""Install (or remove) the Chrome native-messaging host for the Rozan Bridge extension.

  .venv/bin/python scripts/install_chrome_bridge.py            install
  .venv/bin/python scripts/install_chrome_bridge.py --remove   uninstall

Then, once, in Chrome: chrome://extensions → turn on Developer mode → Load unpacked → choose the
`extension` folder of this project. The extension ID is fixed by the key in extension/manifest.json.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "com.personal_agent.bridge"
SUPPORT = Path("~/Library/Application Support").expanduser()
# Chromium browsers that support native messaging, by their per-user data folder.
BROWSERS = {
    "Google Chrome": SUPPORT / "Google/Chrome",
    "Google Chrome Beta": SUPPORT / "Google/Chrome Beta",
    "Chromium": SUPPORT / "Chromium",
    "Brave": SUPPORT / "BraveSoftware/Brave-Browser",
    "Microsoft Edge": SUPPORT / "Microsoft Edge",
    "Vivaldi": SUPPORT / "Vivaldi",
    "Arc": SUPPORT / "Arc/User Data",
}
def _launcher() -> Path:
    """Where Chrome's launcher script goes: --launcher-dir DIR (the downloaded app uses Application Support,
    since it must not write inside its own bundle), else bridge/ in this project."""
    if "--launcher-dir" in sys.argv:
        d = Path(sys.argv[sys.argv.index("--launcher-dir") + 1]).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d / "run_host.sh"
    return ROOT / "bridge" / "run_host.sh"


LAUNCHER = _launcher()


def main() -> int:
    installed = {name: d for name, d in BROWSERS.items() if d.exists()}
    if "--remove" in sys.argv:
        for d in BROWSERS.values():
            t = d / "NativeMessagingHosts" / f"{NAME}.json"
            if t.exists():
                t.unlink()
                print(f"removed {t}")
        if LAUNCHER.exists():
            LAUNCHER.unlink()
        return 0
    if not installed:
        print("no supported browser found (Chrome, Brave, Edge, Chromium, Vivaldi, Arc)")
        return 1
    ext_id = json.loads((ROOT / "extension" / ".identity.json").read_text())["id"]
    LAUNCHER.write_text("#!/bin/sh\n# Started by Chrome for the Rozan Bridge extension.\n"
                        f'exec "{sys.executable}" -I "{ROOT / "bridge" / "native_host.py"}"\n')
    LAUNCHER.chmod(LAUNCHER.stat().st_mode | stat.S_IXUSR)
    manifest = json.dumps({
        "name": NAME,
        "description": "Rozan Bridge (relays to the agent on this Mac)",
        "path": str(LAUNCHER),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }, indent=2)
    for name, d in installed.items():
        hosts = d / "NativeMessagingHosts"
        hosts.mkdir(parents=True, exist_ok=True)
        target = hosts / f"{NAME}.json"
        target.write_text(manifest)
        os.chmod(target, 0o644)
        print(f"{name}: installed {target}")
    print(f"\nextension id: {ext_id}\n")
    print("Now, once in each browser (and each profile) you want the agent to use:")
    print("  open its extensions page (chrome://extensions, brave://extensions, edge://extensions)")
    print("  → Developer mode on → Load unpacked →")
    print(f"  {ROOT / 'extension'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
