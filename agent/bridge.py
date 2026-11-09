"""Agent side of the browser bridge: a Unix socket the extension's native host connects to.

Every browser profile that runs the Rozan Bridge extension starts its own native host and
therefore its own connection. Each one introduces itself with a stable instance id and its browser
name; the owner can give each a label ("work Chrome"). Tools pick a browser with `browser=` (id, id
prefix, label, or browser name); if several are connected and none is named, the call is refused.

The socket lives in ~/.personal_agent (directory mode 700, socket mode 600), so only the owner's
processes can connect. Only `agent/tools/browser.py` may call `Bridge.call` (enforced by a test).
"""
from __future__ import annotations

import itertools
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME = Path(os.environ.get("AGENT_HOME", "~/.personal_agent")).expanduser()
SOCKET_PATH = HOME / "bridge.sock"
LABELS_PATH = HOME / "browsers.json"


class BridgeError(Exception):
    pass


@dataclass
class Instance:
    id: str
    browser: str
    version: str
    incognito: bool
    conn: socket.socket = field(repr=False)
    connected_at: float = field(default_factory=time.time)
    wlock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class Bridge:
    def __init__(self, path: Path = SOCKET_PATH, labels_path: Optional[Path] = LABELS_PATH):
        self.path = Path(path)
        self.labels_path = labels_path
        self._server: Optional[socket.socket] = None
        self._instances: Dict[str, Instance] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: Dict[int, dict] = {}
        self._events: Dict[int, threading.Event] = {}
        self._labels: Dict[str, str] = {}
        if labels_path is not None and labels_path.exists():
            try:
                self._labels = dict(json.loads(labels_path.read_text()))
            except Exception:
                self._labels = {}

    # ---- lifecycle ----------------------------------------------------------------------------
    def start(self) -> "Bridge":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.path))
        os.chmod(self.path, 0o600)
        srv.listen(8)
        self._server = srv
        threading.Thread(target=self._accept_loop, daemon=True, name="bridge-accept").start()
        return self

    def stop(self) -> None:
        with self._lock:
            conns = [i.conn for i in self._instances.values()]
            self._instances.clear()
        for s in conns + [self._server]:
            try:
                s and s.close()
            except OSError:
                pass
        self._server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def connected(self) -> bool:
        return bool(self._instances)

    @property
    def extension_version(self) -> Optional[str]:
        versions = sorted({i.version for i in self._instances.values()})
        return ", ".join(versions) or None

    def _accept_loop(self) -> None:
        while self._server is not None:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._read_loop, args=(conn,), daemon=True, name="bridge-read").start()

    def _read_loop(self, conn: socket.socket) -> None:
        inst: Optional[Instance] = None
        try:
            for line in conn.makefile("rb"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "hello" in msg:
                    iid = str(msg.get("instance") or f"legacy-{id(conn)}")[:64]
                    inst = Instance(iid, str(msg.get("browser") or "browser")[:40], str(msg["hello"])[:20],
                                    bool(msg.get("incognito")), conn)
                    with self._lock:
                        old = self._instances.get(iid)
                        self._instances[iid] = inst
                    if old is not None and old.conn is not conn:
                        try:
                            old.conn.close()
                        except OSError:
                            pass
                    continue
                mid = msg.get("id")
                if mid in self._events:
                    self._pending[mid] = msg
                    self._events[mid].set()
        finally:
            if inst is not None:
                with self._lock:
                    if self._instances.get(inst.id) is inst:
                        del self._instances[inst.id]
            for ev in list(self._events.values()):
                ev.set()                      # wake callers; the ones for this browser see no reply

    # ---- browsers -------------------------------------------------------------------------------
    def browsers(self) -> List[dict]:
        with self._lock:
            insts = list(self._instances.values())
        return [{"id": i.id, "browser": i.browser, "label": self._labels.get(i.id, ""), "version": i.version,
                 "incognito_allowed": i.incognito, "connected_at": i.connected_at} for i in insts]

    def set_label(self, instance_id: str, label: str) -> None:
        label = label.strip()[:40]
        if label:
            self._labels[instance_id] = label
        else:
            self._labels.pop(instance_id, None)
        if self.labels_path is not None:
            self.labels_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.labels_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._labels, f)

    def pick(self, browser: Optional[str] = None) -> Instance:
        with self._lock:
            insts = list(self._instances.values())
        if not insts:
            raise BridgeError("no browser is connected (is the Rozan Bridge extension installed and the browser open?)")
        def describe() -> str:
            return "; ".join(f"{self._labels.get(i.id) or i.browser} (id {i.id[:8]})" for i in insts)
        if not browser:
            if len(insts) == 1:
                return insts[0]
            raise BridgeError(f"several browsers are connected: {describe()}. Say which one.")
        want = browser.strip().lower()
        for test in (lambda i: i.id.lower() == want,
                     lambda i: len(want) >= 6 and i.id.lower().startswith(want),
                     lambda i: self._labels.get(i.id, "").lower() == want,
                     lambda i: i.browser.lower() == want or i.browser.lower().replace("google ", "") == want,
                     lambda i: want in ("chrome", "google chrome", "chromium") and i.browser.lower() in ("google chrome", "chromium")):
            found = [i for i in insts if test(i)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise BridgeError(f"{len(found)} browsers match {browser!r}: {describe()}. Use a label or id.")
        raise BridgeError(f"no connected browser matches {browser!r}. Connected: {describe()}")

    # ---- calls (only from agent/tools/browser.py) ----------------------------------------------
    def call(self, cmd: str, args: Optional[dict] = None, timeout: float = 30.0, browser: Optional[str] = None) -> Any:
        inst = self.pick(browser)
        mid = next(self._ids)
        ev = threading.Event()
        self._events[mid] = ev
        try:
            with inst.wlock:
                inst.conn.sendall((json.dumps({"id": mid, "cmd": cmd, "args": args or {}}) + "\n").encode())
            if not ev.wait(timeout):
                raise BridgeError(f"the browser did not answer {cmd!r} within {timeout:.0f} s")
            reply = self._pending.pop(mid, None)
            if reply is None:
                raise BridgeError("the browser disconnected")
            if not reply.get("ok"):
                raise BridgeError(reply.get("error") or f"{cmd} failed")
            return reply.get("result")
        finally:
            self._events.pop(mid, None)
            self._pending.pop(mid, None)


_bridge: Optional[Bridge] = None


def get_bridge() -> Bridge:
    """The process-wide bridge. Not listening until `start()` is called (by the server)."""
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    return _bridge
