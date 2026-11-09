"""Encrypted memory: the owner's preferences, readable only after the owner unlocks it.

The file ~/.personal_agent/memory.enc holds AES-256-GCM ciphertext. The key is derived from the owner's
passphrase with scrypt and is kept only in this process's memory while unlocked; it auto-locks after
`idle_s` without use. Nothing here is ever written by a model: entries are added only through the
owner's own action in the UI, so nothing untrusted can enter memory. Malware already running as the
owner while the vault is unlocked could read process memory; at rest the file is unreadable.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VAULT_PATH = Path(os.environ.get("AGENT_HOME", "~/.personal_agent")).expanduser() / "memory.enc"
MAX_PREFS = 200
MAX_PREF_CHARS = 300


class VaultError(Exception):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 2 ** 20, dklen=32)


class Vault:
    def __init__(self, path: Optional[Path] = VAULT_PATH, idle_s: float = 15 * 60):
        self.path = path
        self.idle_s = idle_s
        self._key: Optional[bytes] = None
        self._salt: Optional[bytes] = None
        self._data: dict = {}
        self._used = 0.0
        self._lock = threading.Lock()

    # ---- state ----------------------------------------------------------------------------------
    @property
    def exists(self) -> bool:
        return self.path is not None and self.path.exists()

    @property
    def unlocked(self) -> bool:
        self.expire()
        return self._key is not None

    def status(self) -> dict:
        return {"set_up": self.exists, "unlocked": self.unlocked, "auto_lock_minutes": round(self.idle_s / 60)}

    def expire(self) -> None:
        if self._key is not None and time.time() - self._used > self.idle_s:
            self.lock()

    def lock(self) -> None:
        with self._lock:
            self._key, self._data = None, {}

    # ---- setup / unlock ---------------------------------------------------------------------------
    def setup(self, passphrase: str) -> None:
        if self.exists:
            raise VaultError("memory is already set up; unlock it instead")
        if len(passphrase) < 8:
            raise VaultError("use a passphrase of at least 8 characters")
        with self._lock:
            self._salt = secrets.token_bytes(16)
            self._key = _key(passphrase, self._salt)
            self._data = {"preferences": []}
            self._used = time.time()
            self._write()

    def unlock(self, passphrase: str) -> None:
        if not self.exists:
            raise VaultError("memory is not set up yet")
        blob = json.loads(self.path.read_text())
        salt = base64.b64decode(blob["salt"])
        key = _key(passphrase, salt)
        try:
            plain = AESGCM(key).decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["ct"]), b"pa-memory-v1")
        except InvalidTag:
            raise VaultError("wrong passphrase")
        with self._lock:
            self._key, self._salt, self._data, self._used = key, salt, json.loads(plain), time.time()

    def _write(self) -> None:
        nonce = secrets.token_bytes(12)
        ct = AESGCM(self._key).encrypt(nonce, json.dumps(self._data).encode("utf-8"), b"pa-memory-v1")
        blob = {"v": 1, "kdf": "scrypt-n15-r8-p1", "salt": base64.b64encode(self._salt).decode(),
                "nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(ct).decode()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(blob, f)
        os.replace(tmp, self.path)

    def _need(self) -> None:
        if not self.unlocked:
            raise VaultError("memory is locked")
        self._used = time.time()

    # ---- preferences ----------------------------------------------------------------------------
    def preferences(self) -> List[dict]:
        self._need()
        return list(self._data.get("preferences", []))

    def add_preference(self, text: str) -> dict:
        self._need()
        text = " ".join(text.split())[:MAX_PREF_CHARS]
        if not text:
            raise VaultError("empty preference")
        prefs = self._data.setdefault("preferences", [])
        if len(prefs) >= MAX_PREFS:
            raise VaultError(f"at most {MAX_PREFS} preferences")
        item = {"id": uuid.uuid4().hex[:10], "text": text, "created": time.time()}
        with self._lock:
            prefs.append(item)
            self._write()
        return item

    def remove_preference(self, pref_id: str) -> bool:
        self._need()
        prefs = self._data.get("preferences", [])
        keep = [p for p in prefs if p["id"] != pref_id]
        if len(keep) == len(prefs):
            return False
        with self._lock:
            self._data["preferences"] = keep
            self._write()
        return True
