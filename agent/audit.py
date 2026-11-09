"""Append-only JSONL audit log of every tool call, approval, and policy refusal.

An optional `sink` receives every record after it is written, so a UI can mirror the log live.
The file stays the source of truth; the sink is best-effort."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .tainted import Tainted

AUDIT_PATH = Path(os.environ.get("AGENT_AUDIT", "~/.personal_agent/audit.jsonl")).expanduser()


def _plain(x: Any, limit: int = 300) -> Any:
    if isinstance(x, Tainted):
        return {"value": _plain(x.value, limit), "sources": sorted(x.sources)}
    if isinstance(x, dict):
        return {k: _plain(v, limit) for k, v in x.items()}
    if isinstance(x, list):
        return [_plain(v, limit) for v in x[:20]]
    if isinstance(x, str) and len(x) > limit:
        return x[:limit] + f"...[{len(x)} chars]"
    return x


def _digest(rec: dict) -> str:
    return hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()


def _last_hash(path: Path) -> str:
    if not path.exists() or path.stat().st_size == 0:
        return ""
    with open(path, "rb") as f:
        f.seek(max(0, path.stat().st_size - 65536))
        lines = f.read().decode("utf-8", errors="replace").strip().splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line).get("hash", "")
        except ValueError:
            continue
    return ""


def verify(path: Path = AUDIT_PATH) -> tuple[bool, int, str]:
    """Walk the hash chain. -> (ok, records checked, problem). Unchained (older) records are skipped."""
    prev, n = None, 0
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            rec = json.loads(line)
            if "hash" not in rec:
                continue
            h = rec.pop("hash")
            if _digest(rec) != h:
                return False, n, f"record on line {i} was changed"
            if prev is not None and rec.get("prev") != prev:
                return False, n, f"a record before line {i} was removed or reordered"
            prev, n = h, n + 1
    return True, n, ""


class Audit:
    def __init__(self, path: Path = AUDIT_PATH, sink: Callable[[dict], None] | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = time.strftime("%Y%m%dT%H%M%S")
        self.sink = sink

    def log(self, event: str, **fields: Any) -> None:
        rec = {"ts": time.time(), "run": self.run_id, "event": event}
        rec.update({k: _plain(v) for k, v in fields.items()})
        # Hash chain: each record carries the previous record's hash, so an edit or deletion shows.
        rec = json.loads(json.dumps(rec, default=str))
        rec["prev"] = _last_hash(self.path)
        rec["hash"] = _digest(rec)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        if self.sink is not None:
            self.sink(rec)
