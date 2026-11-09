import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.audit import Audit
from agent.fakes import FakeLLM, build_fake_registry  # noqa: F401  (re-exported for older tests)
from agent.policy import Policy
from agent.quarantine import QuarantinedReader


@pytest.fixture
def policy(tmp_path):
    root = Path(__file__).resolve().parents[1] / "policy.yaml"
    p = Policy.load(root)
    p.readable_roots = [tmp_path / "read"]
    p.writable_roots = [tmp_path / "write"]
    (tmp_path / "read").mkdir()
    (tmp_path / "write").mkdir()
    return p


@pytest.fixture
def audit(tmp_path):
    return Audit(tmp_path / "audit.jsonl")


@pytest.fixture
def fake_registry(policy):
    """A registry with fake gmail/browser/shell tools that record calls instead of acting."""
    return build_fake_registry(policy)


@pytest.fixture
def make_executor(policy, fake_registry, audit):
    from agent.approval import allow_all
    from agent.executor import Executor

    def _make(reader_replies, approver=allow_all):
        reader = QuarantinedReader(FakeLLM(reader_replies))
        return Executor(policy, fake_registry, reader, approver, audit, printer=lambda *a, **k: None)
    return _make


@pytest.fixture(autouse=True)
def _never_touch_the_real_home(tmp_path, monkeypatch):
    """Tests must not write chats, memory or domains into the owner's ~/.personal_agent."""
    import agent.server as server
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", tmp_path / "home-conversations")
    monkeypatch.setattr(server, "HOME", tmp_path / "home")
