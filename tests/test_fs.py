"""Filesystem tools: roots, deny list, and the backup taken before an overwrite."""
import pytest

from agent.policy import PolicyViolation
from agent.tools import fs as fs_tool
from agent.tools import Registry


@pytest.fixture
def fs(policy, tmp_path, monkeypatch):
    monkeypatch.setattr(fs_tool, "BACKUP_DIR", tmp_path / "backups")
    reg = Registry()
    fs_tool.register(reg, policy)
    return reg


def test_write_backs_up_an_existing_file(fs, tmp_path):
    target = tmp_path / "write" / "notes.txt"
    first = fs.get("fs.write").fn(path=str(target), content="v1")
    assert first["backup"] is None and target.read_text() == "v1"
    second = fs.get("fs.write").fn(path=str(target), content="v2")
    assert target.read_text() == "v2"
    backup = tmp_path / "backups"
    copies = list(backup.glob("*notes.txt"))
    assert len(copies) == 1 and copies[0].read_text() == "v1" and second["backup"] == str(copies[0])


def test_write_outside_roots_or_in_denied_paths_is_refused(fs, policy, tmp_path):
    with pytest.raises(PolicyViolation):
        fs.get("fs.write").fn(path=str(tmp_path / "read" / "x.txt"), content="no")   # read-only root
    policy.denied_globs = [".env"]
    with pytest.raises(PolicyViolation):
        fs.get("fs.write").fn(path=str(tmp_path / "write" / ".env"), content="SECRET=1")


def test_read_and_list_stay_inside_roots(fs, tmp_path):
    (tmp_path / "read" / "a.txt").write_text("hello")
    assert fs.get("fs.read").fn(path=str(tmp_path / "read" / "a.txt")) == "hello"
    assert [e["name"] for e in fs.get("fs.list").fn(path=str(tmp_path / "read"))] == ["a.txt"]
    with pytest.raises(PolicyViolation):
        fs.get("fs.read").fn(path="/etc/passwd")
