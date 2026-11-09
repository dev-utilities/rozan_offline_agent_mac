"""System tools give plain summaries; long output is answered in a sentence with details kept."""
import json

from agent.planner import Plan
from agent.tools import Registry, ToolSpec
from agent.tools import system as system_tool


def test_system_tools_return_summaries(policy):
    reg = Registry()
    system_tool.register(reg, policy)
    m = reg.get("system.memory").fn()
    assert "free" in m["summary"] and m["total_gb"] > 0
    assert "free of" in reg.get("system.disk").fn()["summary"]
    assert reg.get("system.top_apps").fn(limit=3)["apps"]
    assert "summary" in reg.get("system.battery").fn()


def test_summary_tools_show_their_summary(make_executor, fake_registry):
    fake_registry.add(ToolSpec("system.memory", lambda: {"summary": "7.4 GB free", "free_gb": 7.4}, "", {}, source="system"))
    ex = make_executor([])
    ex.policy.tools["system.memory"] = ex.policy.tools["fs.list"].__class__("system.memory", frozenset({"user", "planner"}))
    ex.request = "how much memory is left?"
    ex.run(Plan.model_validate({"steps": [{"id": "m", "tool": "system.memory", "args": {}},
                                          {"id": "s", "tool": "show", "args": {"text": {"$ref": "m"}}}]}))
    assert fake_registry.shown[-1]["answer"] == "7.4 GB free"


def test_long_output_gets_a_short_answer_from_the_reader(make_executor, fake_registry):
    long_text = "Pages free: 5430.\n" * 60
    ex = make_executor([{"answer": "About 85 MB is completely free."}])
    ex.request = "how much memory is left?"
    ex.run(Plan.model_validate({"steps": [{"id": "s", "tool": "show", "args": {"text": long_text}}]}))
    shown = fake_registry.shown[-1]
    assert shown["answer"] == "About 85 MB is completely free." and shown["details"] == long_text


def test_reader_failure_falls_back_to_the_raw_text(make_executor, fake_registry):
    ex = make_executor(["not json", "still not json"])
    ex.request = "x"
    ex.run(Plan.model_validate({"steps": [{"id": "s", "tool": "show", "args": {"text": "y" * 500}}]}))
    assert fake_registry.shown[-1] == "y" * 500


def test_add_keyboard_picks_the_plain_layout_and_reports(monkeypatch):
    from agent.tools import system as sysmod
    calls = []
    found = [{"id": "translit", "name": "Urdu – Transliteration", "enabled": False},
             {"id": "com.apple.keylayout.Urdu", "name": "Urdu", "enabled": False}]

    def fake(*args):
        calls.append(args)
        return found if args[0] == "find" else {}
    monkeypatch.setattr(sysmod, "_keyboards", fake)
    reg = __import__("agent.tools", fromlist=["Registry"]).Registry()
    sysmod.register(reg, None)
    out = reg.get("system.add_keyboard").fn(language="Urdu")
    assert ("enable", "com.apple.keylayout.Urdu") in calls and "Added the Urdu keyboard" in out["summary"]


def test_remove_keyboard_never_removes_the_last_one(monkeypatch):
    from agent.tools import system as sysmod
    monkeypatch.setattr(sysmod, "_keyboards", lambda *a: [{"id": "us", "name": "U.S."}] if a[0] == "list" else {})
    reg = __import__("agent.tools", fromlist=["Registry"]).Registry()
    sysmod.register(reg, None)
    assert "Not removed" in reg.get("system.remove_keyboard").fn(language="U.S.")["summary"]
