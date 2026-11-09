"""Desktop control guard rails (the helper itself is faked: no real app is touched)."""
import pytest

from agent.policy import Policy, PolicyViolation
from agent.tools import Registry
from agent.tools import desktop as dmod

REAL = Policy.load("policy.yaml")
RUNNING = [{"name": "Notes", "bundle_id": "com.apple.Notes", "frontmost": False},
           {"name": "Terminal", "bundle_id": "com.apple.Terminal", "frontmost": False},
           {"name": "Rozan", "bundle_id": "com.personal-agent.shell", "frontmost": True}]


@pytest.fixture
def reg(monkeypatch):
    calls = []

    def fake(*args, **kw):
        calls.append(args)
        if args[0] == "apps":
            return RUNNING
        return {"app": args[1], "ok": True}
    monkeypatch.setattr(dmod, "_run", fake)
    r = Registry()
    dmod.register(r, REAL)
    r.calls = calls
    return r


def test_never_controls_terminal_or_the_agent_itself(reg):
    for app in ("Terminal", "Rozan", "com.personal-agent.shell"):
        with pytest.raises(PolicyViolation):
            reg.get("desktop.press").fn(app=app, target="Approve")
        with pytest.raises(PolicyViolation):
            reg.get("desktop.type").fn(app=app, text="rm -rf ~")
    assert not [c for c in reg.calls if c[0] in ("press", "type")]


def test_allowed_app_reaches_the_helper(reg):
    reg.get("desktop.press").fn(app="notes", target="New Note")
    assert ("press", "com.apple.Notes", "New Note") in reg.calls


def test_acting_needs_approval_and_trusted_targets():
    for tool in ("desktop.press", "desktop.type", "desktop.key"):
        assert REAL.tools[tool].approval is True and REAL.tools[tool].risk == "high"
        with pytest.raises(PolicyViolation):           # a label read off the screen cannot be the target
            REAL.check_flow(tool, frozenset({"desktop.controls"}))
        REAL.check_flow(tool, frozenset({"user", "planner"}))   # typed by the owner / picked via ask.choose
    for tool in ("desktop.apps", "desktop.controls", "desktop.read"):
        assert REAL.tools[tool].approval is False
