"""Open a Mac app by name. Opening is harmless; acting inside an app needs its own narrow tools."""
from __future__ import annotations

import re
import subprocess

from . import Registry, ToolSpec

APP_NAME = re.compile(r"^[A-Za-z0-9 .&+\-]{1,60}$")


def register(reg: Registry, policy) -> None:
    def app_open(name: str):
        if not APP_NAME.match(name):
            raise ValueError("app name contains unexpected characters")
        subprocess.run(["open", "-a", name], check=True, capture_output=True, timeout=20)
        return {"opened": name}

    reg.add(ToolSpec("app.open", app_open, "Open a Mac application by name, e.g. 'Microsoft Teams'.",
                     {"name": "application name"}, source="app", required=["name"], returns="{opened}"))
