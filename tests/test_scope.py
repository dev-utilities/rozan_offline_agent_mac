"""Task scope and careful-model escalation."""
import json
from contextlib import contextmanager

import pytest

from agent.fakes import CLEANUP_PLAN, FakeLLM, build_fake_registry
from agent.models import LowMemory, ModelManager
from agent.planner import Plan
from agent.policy import PolicyViolation
from agent.scope import Scoper, ScopeError, TaskScope, out_of_scope
from agent.server import AgentService

SEND_PLAN = {"goal": "x", "steps": [{"id": "s", "tool": "gmail.send",
                                     "args": {"to": "a@example.com", "subject": "hi", "body": "hi"}}]}
LABEL_ONLY = {"capabilities": ["mail.read", "mail.organize"], "open_ended": False}
TRASH_SCOPE = {"capabilities": ["mail.read", "mail.trash"], "open_ended": False}
SEND_SCOPE = {"capabilities": ["mail.send"], "open_ended": False}


class FakeStrong:
    name = "careful-fake"
    need_gb = 4.0

    def __init__(self, replies):
        self.llm = FakeLLM(replies)
        self.started = self.stopped = 0

    def start(self):
        self.started += 1
        return self.llm

    def stop(self):
        self.stopped += 1


def service(policy, tmp_path, fast_replies, scope_reply, strong=None, free=10.0, reader_replies=()):
    fast = FakeLLM(fast_replies)
    events = []
    models = ModelManager(fast, "fast-fake", strong, free_gb=lambda: free, unload_fast=lambda: events.append("unload"))
    svc = AgentService(policy, lambda printer, show: build_fake_registry(policy, show=show), fast,
                       FakeLLM(list(reader_replies)), tmp_path / "audit.jsonl", planner_model="fast-fake",
                       scoper=Scoper(FakeLLM([scope_reply])), models=models)
    svc.events = events
    return svc


def test_scope_maps_capabilities_to_tools_and_flags_extras():
    scope = TaskScope(**LABEL_ONLY)
    assert {"gmail.list", "gmail.get", "show"} <= scope.allowed_tools()
    assert "gmail.send" not in scope.allowed_tools() and "gmail.trash" not in scope.allowed_tools()
    assert out_of_scope(Plan.model_validate(CLEANUP_PLAN), scope) == ["gmail.trash"]
    assert out_of_scope(Plan.model_validate(CLEANUP_PLAN), TaskScope(**TRASH_SCOPE)) == []


def test_scoper_rejects_unknown_capabilities():
    with pytest.raises(ScopeError):
        Scoper(FakeLLM([{"capabilities": ["root.everything"]}] * 2)).scope("x")


def test_out_of_scope_plan_is_never_registered(policy, tmp_path):
    # label request; both models (wrongly) plan a trash -> refused, nothing to execute
    svc = service(policy, tmp_path, [json.dumps(CLEANUP_PLAN)], LABEL_ONLY, FakeStrong([json.dumps(CLEANUP_PLAN)]))
    svc.new_conversation()
    cid = next(iter(svc.conversations))
    turn = svc.plan_request(cid, "label my bank emails")
    assert turn.plan is None and "gmail.trash" in turn.error
    assert turn.escalated and svc.plans == {}


def test_high_risk_plan_goes_to_the_careful_model_with_one_model_in_memory(policy, tmp_path):
    strong = FakeStrong([json.dumps(SEND_PLAN)])
    svc = service(policy, tmp_path, [json.dumps(SEND_PLAN)], SEND_SCOPE, strong)
    cid = svc.new_conversation().id
    turn = svc.plan_request(cid, "email a@example.com saying hi")
    assert turn.plan is not None and turn.planned_by == "careful-fake"
    assert any("gmail.send" in r for r in turn.escalated)
    assert svc.events == ["unload"] and strong.started == strong.stopped == 1


def test_low_memory_keeps_the_fast_plan_with_a_warning(policy, tmp_path):
    strong = FakeStrong([])
    svc = service(policy, tmp_path, [json.dumps(SEND_PLAN)], SEND_SCOPE, strong, free=1.5)
    cid = svc.new_conversation().id
    turn = svc.plan_request(cid, "email a@example.com saying hi")
    assert turn.plan is not None and turn.planned_by == "fast-fake"
    assert any("Not enough free memory" in n for n in turn.notes)
    assert strong.started == 0


def test_low_memory_with_no_usable_fast_plan_is_an_error(policy, tmp_path):
    svc = service(policy, tmp_path, ["not json", "not json"], SEND_SCOPE, FakeStrong([]), free=1.0)
    cid = svc.new_conversation().id
    turn = svc.plan_request(cid, "email a@example.com saying hi")
    assert turn.plan is None and "Not enough free memory" in turn.error


def test_simple_in_scope_task_stays_on_the_fast_model(policy, tmp_path):
    strong = FakeStrong([])
    svc = service(policy, tmp_path, [json.dumps(CLEANUP_PLAN)], TRASH_SCOPE, strong)
    cid = svc.new_conversation().id
    turn = svc.plan_request(cid, "trash marketing mail")
    assert turn.plan is not None and turn.planned_by == "fast-fake" and turn.escalated == []
    assert strong.started == 0 and svc.plan_meta[turn.plan_id]["allowed"] >= {"gmail.trash"}


def test_executor_refuses_tools_outside_the_scope_at_run_time(make_executor, fake_registry):
    from agent.approval import allow_all
    ex = make_executor([])
    ex.allowed_tools = TaskScope(**LABEL_ONLY).allowed_tools()
    with pytest.raises(PolicyViolation, match="outside what this task"):
        ex.run(Plan.model_validate(SEND_PLAN))
    assert fake_registry.calls == []


def test_careful_reader_runs_with_the_strong_model_and_low_memory_stops_the_run(policy, tmp_path):
    import time
    verdicts = [{"should_delete": True, "category": "marketing", "reason": "promo"},
                {"should_delete": False, "category": "personal", "reason": "mum"}]
    strong = FakeStrong([json.dumps(CLEANUP_PLAN)] + verdicts)
    svc = service(policy, tmp_path, ["bad", "bad"], {**TRASH_SCOPE, "open_ended": True}, strong)
    svc.attach(__import__("asyncio").new_event_loop())
    svc.approval_timeout_s = 0.1                         # approvals time out (deny); we only check who read
    cid = svc.new_conversation().id
    turn = svc.plan_request(cid, "clean up whatever")
    assert turn.careful_reader and turn.planned_by == "careful-fake"
    run = svc.start_run(turn.plan_id)
    for _ in range(100):
        if run.status != "running":
            break
        time.sleep(0.05)
    assert run.status == "done" and strong.started == 2   # once to plan, once to read
    svc.models.free_gb = lambda: 0.5
    run2 = svc.start_run(turn.plan_id)
    for _ in range(100):
        if run2.status != "running":
            break
        time.sleep(0.05)
    assert run2.status == "error" and "Not enough free memory" in run2.error


def test_acting_implies_reading_but_not_the_reverse():
    assert {"gmail.list", "gmail.get", "gmail.trash"} <= TaskScope(capabilities=["mail.trash"]).allowed_tools()
    assert "gmail.trash" not in TaskScope(capabilities=["mail.read"]).allowed_tools()
    assert TaskScope(capabilities=["files.write"]).describe() == ["list and read files", "create or change files"]


def test_guessed_browser_argument_is_dropped_unless_the_user_named_it():
    plan = Plan.model_validate({"steps": [{"id": "r", "tool": "browser.read", "args": {"browser": "Chrome"}}]})
    AgentService._drop_guessed_browser(plan, "summarise the page")
    assert "browser" not in plan.steps[0].args
    plan = Plan.model_validate({"steps": [{"id": "r", "tool": "browser.read", "args": {"browser": "Brave"}}]})
    AgentService._drop_guessed_browser(plan, "read the page in Brave")
    assert plan.steps[0].args["browser"] == "Brave"
