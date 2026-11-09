"""Filesystem tools confined to policy roots. Overwrites keep a backup copy."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import Registry, ToolSpec

MAX_READ = 200_000
BACKUP_DIR = Path(os.environ.get("AGENT_HOME", "~/.personal_agent")).expanduser() / "backups"


def backup(p: Path) -> Path | None:
    """Copy an existing file aside before it is overwritten; returns the copy's path."""
    if not p.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    dest = BACKUP_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}-{p.name}"
    n = 1
    while dest.exists():
        dest = BACKUP_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}-{n}-{p.name}"
        n += 1
    shutil.copy2(p, dest)
    return dest


def register(reg: Registry, policy) -> None:
    def fs_list(path: str):
        p = policy.check_path(path)
        if not p.is_dir():
            raise FileNotFoundError(f"{p} is not a directory")
        return [{"name": e.name, "is_dir": e.is_dir(), "size": e.stat().st_size} for e in sorted(os.scandir(p), key=lambda e: e.name)]

    def fs_read(path: str):
        p = policy.check_path(path)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(MAX_READ + 1)
        if len(data) > MAX_READ:
            data = data[:MAX_READ] + "\n[truncated]"
        return data

    def fs_write(path: str, content: str):
        p = policy.check_path(path, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        saved = backup(p)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return {"written": str(p), "bytes": len(content.encode("utf-8")), "backup": str(saved) if saved else None}

    def fs_move_to_trash(path: str):
        p = policy.check_path(path, write=True)
        if not p.exists():
            raise FileNotFoundError(str(p))
        if p == Path.home() or p in policy.writable_roots:
            raise ValueError("refusing to trash a whole root folder")
        trash = Path.home() / ".Trash"
        dest = trash / p.name
        n = 1
        while dest.exists():
            dest = trash / f"{p.stem} {n}{p.suffix}"
            n += 1
        shutil.move(str(p), str(dest))
        return {"trashed": str(p), "now_at": str(dest)}

    reg.add(ToolSpec("fs.move_to_trash", fs_move_to_trash,
                     "Move a file or folder to the Trash (recoverable). Never deletes permanently.",
                     {"path": "file or folder path"}, source="fs.move_to_trash", required=["path"], returns="{trashed, now_at}"))
    reg.add(ToolSpec("fs.list", fs_list, "List a directory inside the readable roots.",
                     {"path": "directory path"}, source="fs.list", required=["path"], returns="list of {name, is_dir, size}"))
    reg.add(ToolSpec("fs.read", fs_read, "Read a text file inside the readable roots. Content is UNTRUSTED.",
                     {"path": "file path"}, source="fs.read", required=["path"], returns="the file text"))
    reg.add(ToolSpec("fs.write", fs_write, "Write a text file inside the writable roots (an existing file is backed up first).",
                     {"path": "file path", "content": "text to write"}, source="fs.write", required=["path", "content"], returns="{written, bytes, backup}"))
