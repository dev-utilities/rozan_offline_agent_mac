"""Secrets broker. Tools fetch credentials by handle at call time; the model never sees them.

Secrets live outside the project in ~/.personal_agent/secrets.json (mode 600), e.g.
{
  "gmail": {"user": "you@gmail.com", "app_password": "xxxx xxxx xxxx xxxx"}
}
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

SECRETS_PATH = Path(os.environ.get("AGENT_SECRETS", "~/.personal_agent/secrets.json")).expanduser()


class BrokerError(Exception):
    pass


def get(handle: str) -> dict:
    if not SECRETS_PATH.exists():
        raise BrokerError(f"no secrets file at {SECRETS_PATH}")
    mode = stat.S_IMODE(SECRETS_PATH.stat().st_mode)
    if mode & 0o077:
        raise BrokerError(f"{SECRETS_PATH} is readable by others (mode {oct(mode)}); chmod 600 it")
    with open(SECRETS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if handle not in data:
        raise BrokerError(f"no secret for handle {handle!r}")
    return data[handle]
