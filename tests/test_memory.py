"""Encrypted memory, and normal / isolated / incognito chats."""
import json
import time

import pytest

from agent.fakes import CLEANUP_PLAN, FakeLLM, build_fake_registry
from agent.server import AgentService, create_app
from agent.vault import Vault, VaultError
from fastapi.testclient import TestClient

H = {"Authorization": "Bearer t"}


def test_vault_roundtrip_wrong_passphrase_and_ciphertext_at_rest(tmp_path):
    path = tmp_path / "memory.enc"
    v = Vault(path)
    v.setup("correct horse")
    v.add_preference("Prefer archive over delete")
    assert b"archive" not in path.read_bytes()             # nothing readable at rest
    v.lock()
    with pytest.raises(VaultError, match="locked"):
        v.preferences()
    with pytest.raises(VaultError, match="wrong passphrase"):
        Vault(path).unlock("wrong one!")
    v2 = Vault(path)
    v2.unlock("correct horse")
    assert [p["text"] for p in v2.preferences()] == ["Prefer archive over delete"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_vault_auto_locks(tmp_path):
    v = Vault(tmp_path / "m.enc", idle_s=0.05)
    v.setup("passphrase1")
    time.sleep(0.1)
    assert not v.unlocked


def make(policy, tmp_path, planner_replies):
    planner = FakeLLM(planner_replies)
    svc = AgentService(policy, lambda printer, show: build_fake_registry(policy, show=show), planner, FakeLLM([]),
                       tmp_path / "audit.jsonl", store_dir=tmp_path / "conversations")
    svc.vault = Vault(tmp_path / "memory.enc")
    return TestClient(create_app(svc, "t"), base_url="http://127.0.0.1"), svc, planner


def test_preferences_reach_the_planner_only_in_normal_unlocked_chats(policy, tmp_path):
    client, svc, planner = make(policy, tmp_path, [json.dumps(CLEANUP_PLAN)] * 4)
    with client:
        assert client.post("/memory/setup", json={"passphrase": "longenough"}, headers=H).json()["unlocked"]
        client.post("/memory/preferences", json={"text": "Never trash email from Mum"}, headers=H)
        for mode, expect in (("normal", True), ("isolated", False), ("incognito", False)):
            cid = client.post("/conversations", json={"mode": mode}, headers=H).json()["id"]
            client.post("/chat", json={"message": "trash marketing", "conversation_id": cid}, headers=H)
            assert ("Never trash email from Mum" in planner.calls[-1][1]) is expect, mode
        client.post("/memory/lock", headers=H)
        cid = client.post("/conversations", json={"mode": "normal"}, headers=H).json()["id"]
        turn = client.post("/chat", json={"message": "trash marketing", "conversation_id": cid}, headers=H).json()["turn"]
        assert "Mum" not in planner.calls[-1][1] and any("locked" in n for n in turn["notes"])


def test_incognito_chats_are_never_saved_and_expire(policy, tmp_path):
    client, svc, planner = make(policy, tmp_path, [json.dumps(CLEANUP_PLAN)] * 2)
    with client:
        normal = client.post("/conversations", json={"mode": "normal"}, headers=H).json()["id"]
        secret = client.post("/conversations", json={"mode": "incognito"}, headers=H).json()["id"]
        client.post("/chat", json={"message": "private request", "conversation_id": secret}, headers=H)
        files = [p.stem for p in (tmp_path / "conversations").glob("*.json")]
        assert normal in files and secret not in files
        svc.incognito_ttl_s = 0
        svc.housekeeping()
        assert client.get(f"/conversations/{secret}", headers=H).status_code == 404
        assert client.get(f"/conversations/{normal}", headers=H).status_code == 200
