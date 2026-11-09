"""AsyncApprover: blocks the executor thread until answered; timeout and close deny."""
import threading
import time

import pytest

from agent.approval import AsyncApprover


def wait_for(cond, timeout=2.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.005)
    assert cond()


def test_roundtrip_and_approve_all():
    events = []
    ap = AsyncApprover(events.append, timeout_s=2)
    results = []

    def worker():
        results.append(ap("gmail.trash", {"id": "1"}))
        results.append(ap("gmail.trash", {"id": "2"}))
        results.append(ap("gmail.send", {"to": "x"}))

    t = threading.Thread(target=worker)
    t.start()
    wait_for(lambda: len(events) >= 1)
    req = events[0]
    assert req["type"] == "approval.request" and req["tool"] == "gmail.trash" and req["args"] == {"id": "1"}
    assert ap.pending()[0]["approval_id"] == req["approval_id"]
    assert ap.resolve(req["approval_id"], "all")
    # the second gmail.trash needs no request; gmail.send does
    wait_for(lambda: len(events) >= 3)
    req2 = events[2]
    assert req2["type"] == "approval.request" and req2["tool"] == "gmail.send"
    assert ap.resolve(req2["approval_id"], "no")
    t.join(2)
    assert results == [True, True, False]
    assert [e["type"] for e in events] == ["approval.request", "approval.resolved", "approval.request", "approval.resolved"]
    assert events[1]["decision"] == "all" and events[1]["by"] == "user"
    assert ap.pending() == []


def test_timeout_denies():
    events = []
    ap = AsyncApprover(events.append, timeout_s=0.1)
    assert ap("shell.run", {"command": "ls"}) is False
    assert events[-1] == {**events[-1], "type": "approval.resolved", "decision": "no", "by": "timeout"}


def test_close_denies_pending_and_future_requests():
    events = []
    ap = AsyncApprover(events.append, timeout_s=5)
    results = []
    t = threading.Thread(target=lambda: results.append(ap("fs.write", {"path": "p"})))
    t.start()
    wait_for(lambda: len(events) >= 1)
    ap.close()
    t.join(2)
    assert results == [False]
    assert events[-1]["by"] == "cancel"
    assert ap("fs.write", {"path": "q"}) is False
    assert len(events) == 2  # no request was published after close


def test_resolve_validates():
    ap = AsyncApprover(lambda e: None)
    with pytest.raises(ValueError):
        ap.resolve("x", "maybe")
    assert ap.resolve("unknown", "yes") is False
