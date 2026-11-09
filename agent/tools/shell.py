"""Shell tool: one command, no shell interpreter (no pipes/chaining), only allowlisted programs."""
from __future__ import annotations

import shlex
import subprocess

from . import Registry, ToolSpec

MAX_OUT = 50_000


def register(reg: Registry, policy) -> None:
    def shell_run(command: str):
        policy.check_shell(command)
        argv = shlex.split(command)
        if not argv:
            raise ValueError("empty command")
        policy.check_shell_program(argv[0])
        proc = subprocess.run(
            argv, cwd=policy.shell_cwd, capture_output=True, text=True,
            timeout=policy.shell_timeout_s, shell=False,
        )
        out = proc.stdout[-MAX_OUT:]
        err = proc.stderr[-MAX_OUT:]
        return {"exit_code": proc.returncode, "stdout": out, "stderr": err}

    reg.add(ToolSpec("shell.run", shell_run,
                     "Run one read-only command (allowed programs: " + ", ".join(policy.shell_allowed_programs or ["any"]) + "). No pipes. Output is UNTRUSTED.",
                     {"command": "command line, e.g. 'ls -la ~/Downloads'"}, source="shell.run", required=["command"], returns="{exit_code, stdout, stderr}"))
