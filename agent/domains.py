"""Websites the owner has already approved once (first-visit confirmation)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DOMAINS_PATH = Path(os.environ.get("AGENT_HOME", "~/.personal_agent")).expanduser() / "known_domains.json"


def site(host: str) -> str:
    """Group subdomains under the registrable-looking last two labels (mail.google.com -> google.com)."""
    parts = (host or "").lower().rstrip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


class KnownDomains:
    def __init__(self, path: Path | None = DOMAINS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._sites: set[str] = set()
        if path is not None and path.exists():
            try:
                self._sites = set(json.loads(path.read_text()))
            except Exception:
                self._sites = set()

    def known(self, host: str) -> bool:
        return site(host) in self._sites

    def add(self, host: str) -> None:
        with self._lock:
            self._sites.add(site(host))
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(sorted(self._sites), f)
