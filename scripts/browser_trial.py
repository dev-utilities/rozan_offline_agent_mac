"""Try the browser extension end to end without touching the owner's browsers.

Starts: a local copy of tests/fixtures (fake webmail), an agent server with its own home
(~/.personal_agent/trial), and Playwright's Chromium with a throwaway profile and the extension loaded.
Keeps running until Ctrl-C (or --seconds). Prints the agent URL + token for the UI.

  .venv/bin/python scripts/browser_trial.py [--seconds 900] [--headless]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
TRIAL = Path("~/.personal_agent/trial").expanduser()     # short path: the bridge socket has a 104-char limit
PY = str(ROOT / ".venv" / "bin" / "python")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=900)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--agent-port", type=int, default=8767)
    ap.add_argument("--mail-port", type=int, default=8090)
    args = ap.parse_args()

    TRIAL.mkdir(parents=True, exist_ok=True)
    os.chmod(TRIAL, 0o700)
    env = dict(os.environ, AGENT_HOME=str(TRIAL), AGENT_AUDIT=str(TRIAL / "audit.jsonl"))
    procs = []
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))   # clean up children
    try:
        procs.append(subprocess.Popen([PY, "-m", "http.server", str(args.mail_port), "--bind", "127.0.0.1",
                                       "-d", str(ROOT / "tests" / "fixtures")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs.append(subprocess.Popen([PY, "-m", "agent.server", "--port", str(args.agent_port)], cwd=ROOT, env=env,
                                      stdout=open(TRIAL / "server.log", "w"), stderr=subprocess.STDOUT))
        for _ in range(60):
            try:
                httpx.get(f"http://127.0.0.1:{args.agent_port}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.5)

        # throwaway profile; the native host manifest goes in the profile, pointing at our launcher
        profile = TRIAL / "chromium-profile"
        shutil.rmtree(profile, ignore_errors=True)
        hosts = profile / "NativeMessagingHosts"
        hosts.mkdir(parents=True)
        ext_id = json.loads((ROOT / "extension" / ".identity.json").read_text())["id"]
        launcher = TRIAL / "run_host.sh"
        launcher.write_text(f'#!/bin/sh\nAGENT_HOME="{TRIAL}" exec /usr/bin/python3 -I "{ROOT / "bridge" / "native_host.py"}"\n')
        launcher.chmod(0o700)
        manifest = json.dumps({"name": "com.personal_agent.bridge", "description": "trial", "path": str(launcher),
                               "type": "stdio", "allowed_origins": [f"chrome-extension://{ext_id}/"]})
        (hosts / "com.personal_agent.bridge.json").write_text(manifest)
        # Chromium also looks in its per-user folder; keep a copy there for the trial only
        user_hosts = Path("~/Library/Application Support/Chromium/NativeMessagingHosts").expanduser()
        user_hosts.mkdir(parents=True, exist_ok=True)
        (user_hosts / "com.personal_agent.bridge.json").write_text(manifest)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(profile), headless=args.headless, channel="chromium",
                args=[f"--disable-extensions-except={ROOT / 'extension'}", f"--load-extension={ROOT / 'extension'}"],
                env=env, viewport={"width": 1100, "height": 760})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("about:blank")
            token = (TRIAL / "session.token").read_text().strip()
            print(json.dumps({"agent": f"http://127.0.0.1:{args.agent_port}/#token={token}",
                              "mail": f"http://127.0.0.1:{args.mail_port}/webmail.html",
                              "token_file": str(TRIAL / "session.token")}), flush=True)
            deadline = time.time() + args.seconds
            while time.time() < deadline:
                time.sleep(1)
            ctx.close()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
