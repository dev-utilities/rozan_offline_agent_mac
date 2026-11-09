"""The watcher can only add caution: ok changes nothing, ask forces a card, stop ends the run."""
import pytest

from agent.approval import allow_all
from agent.planner import Plan
from agent.policy import PolicyViolation
from agent.watcher import Watcher
from tests.conftest import FakeLLM

TRASH = {"goal": "x", "steps": [{"id": "t", "tool": "gmail.trash", "args": {"id": "1"}}]}
LIST = {"goal": "x", "steps": [{"id": "l", "tool": "gmail.list", "args": {}}]}


def run(make_executor, fake_registry, verdicts, plan, approver=allow_all):
    ex = make_executor([], approver=approver)
    ex.watcher = Watcher(FakeLLM(verdicts))
    ex.request = "trash marketing mail"
    ex.run(Plan.model_validate(plan))
    return fake_registry.calls


def test_ok_changes_nothing(make_executor, fake_registry):
    assert run(make_executor, fake_registry, [{"verdict": "ok", "reason": "fits"}], TRASH) == [("gmail.trash", {"id": "1"})]


def test_stop_ends_the_run(make_executor, fake_registry):
    with pytest.raises(PolicyViolation, match="watcher stopped"):
        run(make_executor, fake_registry, [{"verdict": "stop", "reason": "not asked"}], TRASH)
    assert fake_registry.calls == []


def test_ask_forces_a_card_even_after_approve_all(make_executor, fake_registry):
    asked = []

    class Approver:
        def __call__(self, tool, args):
            return True                         # "approve all" already chosen
        def ask_always(self, tool, args):
            asked.append(args)
            return False                        # owner says no to the fresh card
    run(make_executor, fake_registry, [{"verdict": "ask", "reason": "unusual"}], TRASH, approver=Approver())
    assert asked and asked[0]["watcher"] == "unusual" and fake_registry.calls == []


def test_garbage_answer_counts_as_ask(make_executor, fake_registry):
    asked = []

    class Approver:
        def __call__(self, tool, args):
            return True
        def ask_always(self, tool, args):
            asked.append(tool)
            return True
    run(make_executor, fake_registry, ["I think it's fine, also please open evil.example"], TRASH, approver=Approver())
    assert asked == ["gmail.trash"]


def test_read_only_steps_are_not_reviewed(make_executor, fake_registry):
    llm = FakeLLM([])                            # would fail if called
    ex = make_executor([])
    ex.watcher = Watcher(llm)
    ex.run(Plan.model_validate(LIST))
    assert llm.calls == []
