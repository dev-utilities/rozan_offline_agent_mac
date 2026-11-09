"""MVP safety pieces: first-visit approval, never-visit list, Trash, read-only shell, hash-chained audit log."""
import json

import pytest

from agent.approval import allow_all, deny_all
from agent.audit import Audit, verify
from agent.domains import KnownDomains, site
from agent.planner import Plan
from agent.policy import PolicyViolation
from agent.tools import Registry, ToolSpec
from agent.tools import fs as fs_tool
from agent.tools import shell as shell_tool


def open_plan(url):
    return Plan.model_validate({"goal": "x", "steps": [{"id": "o", "tool": "browser.open", "args": {"url": url}}]})


@pytest.fixture
def browser_exec(policy, audit, tmp_path):
    from agent.executor import Executor
    from agent.quarantine import QuarantinedReader
    from tests.conftest import FakeLLM
    calls, asked = [], []
    reg = Registry()
    reg.add(ToolSpec("browser.open", lambda url: calls.append(url) or {"opened": url}, "", {"url": ""},
                     source="browser", required=["url"]))
    policy.browser_blocked_hosts = ["paypal.com"]
    domains = KnownDomains(tmp_path / "known.json")

    def make(approver):
        def recording(tool, args):
            asked.append(args)
            return approver(tool, args)
        return Executor(policy, reg, QuarantinedReader(FakeLLM([])), recording, audit,
                        printer=lambda *a, **k: None, domains=domains)
    return make, calls, asked, domains


def test_first_visit_asks_once_per_site(browser_exec):
    make, calls, asked, domains = browser_exec
    make(allow_all).run(open_plan("https://mail.google.com/x"))
    make(allow_all).run(open_plan("https://docs.google.com/y"))      # same site: no second question
    assert len(asked) == 1 and asked[0]["first_visit_to"] == "mail.google.com"
    assert calls == ["https://mail.google.com/x", "https://docs.google.com/y"]
    assert domains.known("calendar.google.com") and site("a.b.example.org") == "example.org"


def test_denied_first_visit_opens_nothing_and_is_not_remembered(browser_exec):
    make, calls, asked, domains = browser_exec
    make(deny_all).run(open_plan("https://example.org/"))
    assert calls == [] and not domains.known("example.org")


def test_never_visit_list_is_refused_without_asking(browser_exec):
    make, calls, asked, domains = browser_exec
    with pytest.raises(PolicyViolation, match="never-visit"):
        make(allow_all).run(open_plan("https://www.paypal.com/send"))
    assert calls == [] and asked == []


def test_move_to_trash_never_deletes(policy, tmp_path, monkeypatch):
    monkeypatch.setattr(fs_tool.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".Trash").mkdir()
    reg = Registry()
    fs_tool.register(reg, policy)
    f = tmp_path / "write" / "old.txt"
    f.write_text("keep me")
    out = reg.get("fs.move_to_trash").fn(path=str(f))
    assert not f.exists() and (tmp_path / ".Trash" / "old.txt").read_text() == "keep me"
    assert out["now_at"].endswith(".Trash/old.txt")
    with pytest.raises(ValueError):
        reg.get("fs.move_to_trash").fn(path=str(tmp_path / "write"))     # never a whole root


def test_shell_runs_only_allowlisted_programs(policy):
    policy.shell_allowed_programs = ["echo"]
    reg = Registry()
    shell_tool.register(reg, policy)
    assert reg.get("shell.run").fn(command="echo hi")["stdout"].strip() == "hi"
    with pytest.raises(PolicyViolation):
        reg.get("shell.run").fn(command="touch /tmp/x")


def test_audit_log_is_hash_chained_and_tampering_shows(tmp_path):
    path = tmp_path / "audit.jsonl"
    a = Audit(path)
    for i in range(3):
        a.log("tool.call", step=f"s{i}", tool="show")
    Audit(path).log("plan.end")                          # a new run continues the same chain
    assert verify(path) == (True, 4, "")
    lines = path.read_text().splitlines()
    rec = json.loads(lines[1]); rec["tool"] = "gmail.send"
    path.write_text("\n".join([lines[0], json.dumps(rec)] + lines[2:]) + "\n")
    assert verify(path)[0] is False
    path.write_text("\n".join([lines[0]] + lines[2:]) + "\n")   # a record removed
    ok, _, problem = verify(path)
    assert not ok and "removed" in problem
