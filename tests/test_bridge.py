"""Chrome bridge: socket round trip, native-host framing, and the browser tools' safety checks."""
import io
import json
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agent.bridge import Bridge, BridgeError
from agent.policy import PolicyViolation
from agent.tools import Registry
from bridge import native_host

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bridge():
    d = Path(tempfile.mkdtemp(prefix="pa", dir="/tmp"))      # short path: Unix sockets have a 104-char limit
    b = Bridge(d / "b.sock", labels_path=d / "labels.json").start()
    yield b
    b.stop()


def fake_extension(path, handler, instance="inst-chrome-1", browser="Google Chrome"):
    """Plays the native host + extension: answers each command with handler(cmd, args)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(path))
    s.sendall((json.dumps({"hello": "0.2.0", "instance": instance, "browser": browser}) + "\n").encode())

    def loop():
        for line in s.makefile("rb"):
            msg = json.loads(line)
            try:
                reply = {"id": msg["id"], "ok": True, "result": handler(msg["cmd"], msg["args"])}
            except Exception as e:
                reply = {"id": msg["id"], "ok": False, "error": str(e)}
            s.sendall((json.dumps(reply) + "\n").encode())
    threading.Thread(target=loop, daemon=True).start()
    return s


def wait_connected(b, n=1):
    for _ in range(100):
        if len(b.browsers()) >= n:
            return
        time.sleep(0.01)
    raise AssertionError("not connected")


def test_round_trip_errors_and_disconnect(bridge):
    with pytest.raises(BridgeError, match="no browser is connected"):
        bridge.call("ping")
    s = fake_extension(bridge.path, lambda cmd, args: {"pong": True} if cmd == "ping" else (_ for _ in ()).throw(RuntimeError("nope")))
    wait_connected(bridge)
    assert bridge.call("ping") == {"pong": True} and bridge.extension_version == "0.2.0"
    with pytest.raises(BridgeError, match="nope"):
        bridge.call("other")
    s.shutdown(socket.SHUT_RDWR)
    s.close()
    for _ in range(100):
        if not bridge.connected():
            break
        time.sleep(0.01)
    assert not bridge.connected()


def test_socket_is_private(bridge):
    assert oct(bridge.path.stat().st_mode & 0o777) == "0o600"


def test_native_host_framing():
    msg = {"id": 3, "ok": True, "result": "é"}
    out = io.BytesIO()
    native_host.write_chrome(out, msg, threading.Lock())
    out.seek(0)
    assert native_host.read_chrome(out) == msg
    assert native_host.read_chrome(io.BytesIO(b"")) is None
    (n,) = struct.unpack("<I", out.getvalue()[:4])
    assert n == len(out.getvalue()) - 4


@pytest.fixture
def tools(bridge, policy, monkeypatch):
    from agent.tools import browser as browser_tool
    monkeypatch.setattr(browser_tool, "get_bridge", lambda: bridge)
    policy.browser_blocked_hosts = ["paypal.com"]
    reg = Registry()
    browser_tool.register(reg, policy)
    return reg


def test_click_checks_the_target_first_and_never_follows_a_blocked_link(bridge, tools):
    calls = []

    def ext(cmd, args):
        calls.append(cmd)
        if cmd == "resolve":
            return {"tag": "a", "label": args["target"], "href": "https://www.paypal.com/pay" if args["target"] == "Pay" else "",
                    "matches": 1, "page": "https://mail.google.com/"}
        return {"clicked": True}
    fake_extension(bridge.path, ext)
    wait_connected(bridge)
    out = tools.get("browser.click").fn(target="Archive")
    assert calls == ["resolve", "click"] and out["clicked"]["label"] == "Archive"
    calls.clear()
    with pytest.raises(PolicyViolation, match="never-visit"):
        tools.get("browser.click").fn(target="Pay")
    assert calls == ["resolve"]                              # resolved, checked, not clicked


def test_click_and_type_need_the_extension(tools):
    from agent.bridge import BridgeError
    with pytest.raises(BridgeError):
        tools.get("browser.click").fn(target="Archive")
    with pytest.raises(BridgeError):
        tools.get("browser.screenshot").fn()


def test_open_goes_through_the_extension_and_refuses_blocked_hosts(bridge, tools):
    fake_extension(bridge.path, lambda cmd, args: {"tabId": 1, "url": args.get("url"), "title": "t"})
    wait_connected(bridge)
    assert tools.get("browser.open").fn(url="https://mail.google.com/")["url"] == "https://mail.google.com/"
    with pytest.raises(PolicyViolation):
        tools.get("browser.open").fn(url="https://paypal.com/")


def test_several_browsers_must_be_named(bridge):
    fake_extension(bridge.path, lambda c, a: {"who": "work"}, instance="aaaa1111-work", browser="Google Chrome")
    fake_extension(bridge.path, lambda c, a: {"who": "brave"}, instance="bbbb2222-brave", browser="Brave")
    wait_connected(bridge, 2)
    with pytest.raises(BridgeError, match="several browsers"):
        bridge.call("ping")
    assert bridge.call("ping", browser="Brave") == {"who": "brave"}
    assert bridge.call("ping", browser="chrome") == {"who": "work"}           # "Google " prefix optional
    assert bridge.call("ping", browser="bbbb2222") == {"who": "brave"}        # id prefix
    bridge.set_label("aaaa1111-work", "Work Chrome")
    assert bridge.call("ping", browser="work chrome") == {"who": "work"}
    assert {b["label"] for b in bridge.browsers()} == {"Work Chrome", ""}
    with pytest.raises(BridgeError, match="no connected browser"):
        bridge.call("ping", browser="Safari")


def test_two_profiles_of_the_same_browser_need_a_label(bridge):
    fake_extension(bridge.path, lambda c, a: 1, instance="cccc3333", browser="Google Chrome")
    fake_extension(bridge.path, lambda c, a: 2, instance="dddd4444", browser="Google Chrome")
    wait_connected(bridge, 2)
    with pytest.raises(BridgeError, match="2 browsers match"):
        bridge.call("ping", browser="Google Chrome")
    bridge.set_label("dddd4444", "Personal")
    assert bridge.call("ping", browser="Personal") == 2


def test_only_the_browser_tools_talk_to_the_extension():
    """Every extension interaction must go through agent/tools/browser.py (policy, approval, audit)."""
    offenders = []
    for path in (ROOT / "agent").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ("agent/tools/browser.py", "agent/bridge.py"):
            continue
        text = path.read_text()
        if ".call(" in text and "bridge" in text and "get_bridge().call" in text.replace(" ", ""):
            offenders.append(rel)
        if "bridge.call(" in text or "_bridge.call(" in text:
            offenders.append(rel)
    assert offenders == []
