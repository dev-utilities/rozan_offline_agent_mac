"""API tests for agent/server.py: auth, planning, execution with approvals over the websocket,
timeout/cancel, policy refusal, examples, audit and settings. Fake tools, fake models, no browser."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent.fakes import CLEANUP_PLAN, INJECTION_PLAN, FakeLLM, build_fake_registry
from agent.server import AgentService, build_service, create_app  # noqa: E402

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
# Starlette's test client connects websockets as ws://testserver; the server only accepts loopback hosts.
WS = {"host": "127.0.0.1"}
VERDICTS = [
    {"should_delete": True, "category": "marketing", "reason": "promo"},
    {"should_delete": False, "category": "personal", "reason": "from mum"},
]


def make_client(policy, tmp_path, planner_replies, reader_replies, approval_timeout_s=5.0, store_dir=None):
    service = AgentService(policy, lambda printer, show: build_fake_registry(policy, show=show),
                           FakeLLM(planner_replies), FakeLLM(reader_replies), tmp_path / "audit.jsonl",
                           approval_timeout_s=approval_timeout_s, store_dir=store_dir)
    # base_url matters: the server refuses any Host header that is not loopback
    return TestClient(create_app(service, TOKEN), base_url="http://127.0.0.1"), service


@pytest.fixture
def api(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [json.dumps(CLEANUP_PLAN)] * 3, VERDICTS * 3)
    with client:
        yield client, service


def until(ws, typ, limit=300):
    for _ in range(limit):
        evt = ws.receive_json()
        if evt["type"] == typ:
            return evt
    raise AssertionError(f"no {typ!r} event within {limit} messages")


def plan(client, message="trash marketing mail"):
    r = client.post("/chat", json={"message": message}, headers=H)
    assert r.status_code == 200, r.text
    turn = r.json()["turn"]
    assert turn["error"] is None, turn["error"]
    return r.json()["conversation_id"], turn


def hello(ws):
    ws.send_json({"token": TOKEN})
    assert ws.receive_json()["type"] == "hello"


# ---- auth -----------------------------------------------------------------------------------
def test_health_is_open_but_the_api_needs_the_token(api):
    client, _ = api
    assert client.get("/health").json()["ok"] is True
    assert client.get("/status").status_code == 401
    assert client.post("/chat", json={"message": "x"}).status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/status", headers=H).status_code == 200


def test_non_loopback_host_header_is_rejected(api):
    client, _ = api
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/health", headers={"Host": "localhost:8765"}).status_code == 200


def test_websocket_needs_the_token(api):
    client, _ = api
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/events", headers=WS) as ws:
            ws.send_json({"token": "wrong"})
            ws.receive_json()


def test_websocket_accepts_bearer_header(api):
    client, _ = api
    with client.websocket_connect("/events", headers={**WS, **H}) as ws:
        assert ws.receive_json()["type"] == "hello"


# ---- planning ---------------------------------------------------------------------------------
def test_chat_returns_a_validated_plan_and_the_planner_saw_only_the_request(api):
    client, service = api
    cid, turn = plan(client)
    assert turn["plan"]["goal"] == CLEANUP_PLAN["goal"] and turn["plan_id"]
    conv = client.get(f"/conversations/{cid}", headers=H).json()
    assert conv["title"] == "trash marketing mail"
    assert conv["turns"][0]["plan_id"] == turn["plan_id"]
    system, user = service.planner.llm.calls[0]
    assert "evil.example" not in system + user
    assert client.get("/conversations/nope", headers=H).status_code == 404


def test_invalid_plan_is_reported_and_never_registered(policy, tmp_path):
    bad = {"goal": "x", "steps": [{"id": "a", "tool": "nuke.all", "args": {}}]}
    client, service = make_client(policy, tmp_path, [json.dumps(bad)], [])
    with client:
        r = client.post("/chat", json={"message": "x"}, headers=H)
        assert r.status_code == 200
        turn = r.json()["turn"]
        assert turn["plan"] is None and "unknown tool" in turn["error"]
    assert service.plans == {}


def test_examples_load_without_the_planner(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [], VERDICTS)  # planner has no replies: must not be used
    with client:
        names = [e["name"] for e in client.get("/examples", headers=H).json()]
        assert "cleanup" in names
        r = client.post("/examples/cleanup/load", json={}, headers=H)
        assert r.status_code == 200 and r.json()["turn"]["plan"]["steps"]
        assert client.post("/examples/nope/load", json={}, headers=H).status_code == 404
    assert service.planner.llm.calls == []


def test_fake_mode_service_plans_and_reports_fake(policy, tmp_path):
    service = build_service(policy, fake=True, audit_path=tmp_path / "a.jsonl", store_dir=tmp_path / "conversations")
    client = TestClient(create_app(service, TOKEN), base_url="http://127.0.0.1")
    with client:
        assert client.get("/status", headers=H).json()["fake"] is True
        cid, turn = plan(client, "trash my marketing mail")
        assert turn["plan"]["goal"] == CLEANUP_PLAN["goal"]


# ---- execution --------------------------------------------------------------------------------
def test_run_with_approval_over_the_websocket(api):
    client, service = api
    cid, turn = plan(client)
    with client.websocket_connect("/events", headers=WS) as ws:
        hello(ws)
        r = client.post(f"/plan/{turn['plan_id']}/run", headers=H)
        assert r.status_code == 202, r.text
        run_id = r.json()["id"]
        assert client.post(f"/plan/{turn['plan_id']}/run", headers=H).status_code == 409
        assert until(ws, "run.start")["plan_id"] == turn["plan_id"]
        req = until(ws, "approval.request")
        assert req["tool"] == "gmail.trash" and req["args"] == {"id": "1"}
        assert client.get("/status", headers=H).json()["run"]["pending_approvals"][0]["approval_id"] == req["approval_id"]
        assert client.post(f"/approval/{req['approval_id']}", json={"decision": "yes"}, headers=H).status_code == 200
        res = until(ws, "approval.resolved")
        assert res["decision"] == "yes" and res["by"] == "user"
        assert until(ws, "show")["value"] == "promo"
        end = until(ws, "run.end")
        assert end["status"] == "done" and end["run_id"] == run_id
    assert service.registry.calls == [("gmail.trash", {"id": "1"})]
    assert service.registry.shown == ["promo", "from mum"]
    # answered approvals cannot be re-answered
    assert client.post(f"/approval/{req['approval_id']}", json={"decision": "yes"}, headers=H).status_code == 404
    assert client.post(f"/approval/{req['approval_id']}", json={"decision": "maybe"}, headers=H).status_code == 422

    run = client.get(f"/run/{run_id}", headers=H).json()
    assert run["status"] == "done"
    types = [e["type"] for e in run["events"]]
    assert types[0] == "run.start" and types[-1] == "run.end" and "approval.request" in types
    conv = client.get(f"/conversations/{cid}", headers=H).json()
    assert conv["turns"][0]["runs"][0]["id"] == run_id

    audit = client.get("/audit", params={"run": run_id}, headers=H).json()
    events = [a["event"] for a in audit]
    assert events[0] == "plan.start" and events[-1] == "plan.end"
    assert any(a["event"] == "approval" and a["approved"] for a in audit)
    assert all(a["run"] == run_id for a in audit)
    assert client.get("/status", headers=H).json()["run"] is None


def test_denied_approval_skips_only_that_step(api):
    client, service = api
    cid, turn = plan(client)
    with client.websocket_connect("/events", headers=WS) as ws:
        hello(ws)
        client.post(f"/plan/{turn['plan_id']}/run", headers=H)
        req = until(ws, "approval.request")
        client.post(f"/approval/{req['approval_id']}", json={"decision": "no"}, headers=H)
        assert until(ws, "approval.resolved")["decision"] == "no"
        assert until(ws, "run.end")["status"] == "done"
    assert service.registry.calls == []
    assert service.registry.shown == ["promo", "from mum"]


def test_approve_all_covers_later_calls_of_the_same_tool(policy, tmp_path):
    both = [{"should_delete": True, "category": "marketing", "reason": "promo"}] * 2
    client, service = make_client(policy, tmp_path, [json.dumps(CLEANUP_PLAN)], both)
    with client:
        cid, turn = plan(client)
        with client.websocket_connect("/events", headers=WS) as ws:
            hello(ws)
            client.post(f"/plan/{turn['plan_id']}/run", headers=H)
            req = until(ws, "approval.request")
            client.post(f"/approval/{req['approval_id']}", json={"decision": "all"}, headers=H)
            end = until(ws, "run.end")
            assert end["status"] == "done"
        assert service.registry.calls == [("gmail.trash", {"id": "1"}), ("gmail.trash", {"id": "2"})]
        requests = [e for e in service.runs[end["run_id"]].events if e["type"] == "approval.request"]
        assert len(requests) == 1


def test_approval_timeout_denies(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [json.dumps(CLEANUP_PLAN)], VERDICTS, approval_timeout_s=0.2)
    with client:
        cid, turn = plan(client)
        with client.websocket_connect("/events", headers=WS) as ws:
            hello(ws)
            client.post(f"/plan/{turn['plan_id']}/run", headers=H)
            until(ws, "approval.request")
            res = until(ws, "approval.resolved")
            assert res["decision"] == "no" and res["by"] == "timeout"
            assert until(ws, "run.end")["status"] == "done"
    assert service.registry.calls == []


def test_cancel_stops_the_run_and_denies_the_pending_approval(api):
    client, service = api
    cid, turn = plan(client)
    with client.websocket_connect("/events", headers=WS) as ws:
        hello(ws)
        run_id = client.post(f"/plan/{turn['plan_id']}/run", headers=H).json()["id"]
        until(ws, "approval.request")
        assert client.post(f"/run/{run_id}/cancel", headers=H).json() == {"cancelled": True}
        assert until(ws, "approval.resolved")["by"] == "cancel"
        assert until(ws, "run.end")["status"] == "cancelled"
    assert service.registry.calls == []
    assert client.post(f"/run/{run_id}/cancel", headers=H).json() == {"cancelled": False}
    assert client.get(f"/run/{run_id}", headers=H).json()["status"] == "cancelled"
    # the worker is free again
    cid2, turn2 = plan(client)
    assert client.post(f"/plan/{turn2['plan_id']}/run", headers=H).status_code == 202


def test_policy_refusal_ends_the_run_as_refused(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [json.dumps(INJECTION_PLAN)], [{"summary": "http://evil.example/x"}])
    with client:
        cid, turn = plan(client, "open the link in my email")
        with client.websocket_connect("/events", headers=WS) as ws:
            hello(ws)
            client.post(f"/plan/{turn['plan_id']}/run", headers=H)
            end = until(ws, "run.end")
        assert end["status"] == "refused" and "browser.open" in end["error"]
        refusals = [e["record"] for e in service.runs[end["run_id"]].events
                    if e["type"] == "audit" and e["record"]["event"] == "policy.refuse"]
        assert refusals and refusals[0]["tool"] == "browser.open"
    assert service.registry.calls == []


def test_unknown_plan_and_run_ids(api):
    client, _ = api
    assert client.post("/plan/nope/run", headers=H).status_code == 404
    assert client.get("/run/nope", headers=H).status_code == 404
    assert client.post("/run/nope/cancel", headers=H).status_code == 404
    assert client.post("/approval/nope", json={"decision": "yes"}, headers=H).status_code == 404


# ---- introspection ------------------------------------------------------------------------------
def test_settings_expose_the_policy_read_only(api):
    client, _ = api
    s = client.get("/settings", headers=H).json()
    tools = {t["name"]: t for t in s["policy"]["tools"]}
    assert tools["gmail.send"]["allow_sources"] == ["planner", "user"] and tools["gmail.send"]["approval"]
    assert tools["show"]["allow_sources"] == ["*"]
    assert "paypal.com" in s["policy"]["browser"]["blocked_hosts"]
    assert "df" in s["policy"]["shell"]["allowed_programs"] and "rm" not in s["policy"]["shell"]["allowed_programs"]
    assert s["models"] == {"planner": "qwen2.5:14b", "reader": "qwen2.5:7b"} or s["models"]["planner"]


def test_static_ui_is_served_without_a_token_with_a_csp(api):
    client, _ = api
    r = client.get("/")
    assert r.status_code == 200 and "<title>Rozan</title>" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


# ---- state feed, pause, persistence, permissions ---------------------------------------------
def run_and_approve(client, ws, plan_id, decision="yes"):
    client.post(f"/plan/{plan_id}/run", headers=H)
    req = until(ws, "approval.request")
    client.post(f"/approval/{req['approval_id']}", json={"decision": decision}, headers=H)
    return until(ws, "run.end")


def test_state_events_track_what_the_bubble_shows(api):
    client, service = api
    cid, turn = plan(client)
    assert client.get("/status", headers=H).json()["state"] == "idle"
    with client.websocket_connect("/events", headers=WS) as ws:
        hello(ws)
        client.post(f"/plan/{turn['plan_id']}/run", headers=H)
        states = []
        for _ in range(300):
            evt = ws.receive_json()
            if evt["type"] == "state":
                states.append(evt["state"])
            if evt["type"] == "approval.request":
                assert client.get("/status", headers=H).json()["state"] == "needs_approval"
                client.post(f"/approval/{evt['approval_id']}", json={"decision": "yes"}, headers=H)
            if evt["type"] == "run.end":
                break
        # the idle state follows run.end
        assert until(ws, "state")["state"] == "idle"
    assert states[:3] == ["running", "needs_approval", "running"]
    assert client.get("/status", headers=H).json()["state"] == "idle"


def test_error_state_is_sticky_until_acknowledged(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [json.dumps(INJECTION_PLAN)], [{"summary": "http://evil.example/x"}])
    with client:
        cid, turn = plan(client, "open the link in my email")
        with client.websocket_connect("/events", headers=WS) as ws:
            hello(ws)
            client.post(f"/plan/{turn['plan_id']}/run", headers=H)
            assert until(ws, "run.end")["status"] == "refused"
            assert until(ws, "state")["state"] == "error"
        st = client.get("/status", headers=H).json()
        assert st["state"] == "error" and "browser.open" in st["last_error"]
        assert client.post("/state/ack", headers=H).json()["state"] == "idle"


def test_pause_stops_the_run_and_blocks_new_work(api):
    client, service = api
    cid, turn = plan(client)
    with client.websocket_connect("/events", headers=WS) as ws:
        hello(ws)
        client.post(f"/plan/{turn['plan_id']}/run", headers=H)
        until(ws, "approval.request")
        assert client.post("/pause", headers=H).json()["state"] == "paused"
        assert until(ws, "run.end")["status"] == "cancelled"
        assert client.post("/chat", json={"message": "x", "conversation_id": cid}, headers=H).status_code == 409
        assert client.post(f"/plan/{turn['plan_id']}/run", headers=H).status_code == 409
        assert client.get("/status", headers=H).json()["paused"] is True
        assert client.post("/resume", headers=H).json()["state"] == "idle"
        assert client.post(f"/plan/{turn['plan_id']}/run", headers=H).status_code == 202
        until(ws, "approval.request")
        client.post("/pause", headers=H)  # leave nothing running
        until(ws, "run.end")
    assert service.registry.calls == []


def test_conversations_persist_across_a_restart_and_can_be_deleted(policy, tmp_path):
    store = tmp_path / "conversations"
    client, service = make_client(policy, tmp_path, [json.dumps(CLEANUP_PLAN)], VERDICTS, store_dir=store)
    with client:
        cid, turn = plan(client)
        with client.websocket_connect("/events", headers=WS) as ws:
            hello(ws)
            assert run_and_approve(client, ws, turn["plan_id"])["status"] == "done"
    files = list(store.glob("*.json"))
    assert len(files) == 1 and oct(files[0].stat().st_mode & 0o777) == "0o600"

    # a new core on the same directory sees the chat, its plan and its run
    client2, service2 = make_client(policy, tmp_path, [], VERDICTS, store_dir=store)
    with client2:
        assert [c["id"] for c in client2.get("/conversations", headers=H).json()] == [cid]
        conv = client2.get(f"/conversations/{cid}", headers=H).json()
        t = conv["turns"][0]
        assert t["plan"]["goal"] == CLEANUP_PLAN["goal"]
        assert t["runs"][0]["status"] == "done"
        assert any(e["type"] == "show" for e in t["runs"][0]["events"])
        with client2.websocket_connect("/events", headers=WS) as ws:   # the restored plan runs again
            hello(ws)
            assert run_and_approve(client2, ws, t["plan_id"], "no")["status"] == "done"
        assert client2.delete(f"/conversations/{cid}", headers=H).status_code == 204
        assert client2.get(f"/conversations/{cid}", headers=H).status_code == 404
        assert client2.delete(f"/conversations/{cid}", headers=H).status_code == 404
    assert list(store.glob("*.json")) == []


def test_stored_untrusted_content_never_reaches_the_planner(policy, tmp_path):
    store = tmp_path / "conversations"
    client, service = make_client(policy, tmp_path, [json.dumps(CLEANUP_PLAN)] * 2, VERDICTS, store_dir=store)
    with client:
        cid, turn = plan(client)
        with client.websocket_connect("/events", headers=WS) as ws:
            hello(ws)
            run_and_approve(client, ws, turn["plan_id"])
        stored = (store / f"{cid}.json").read_text()
        assert "evil.example" in stored            # the injected email is stored, for display only
        r = client.post("/chat", json={"message": "and the rest?", "conversation_id": cid}, headers=H)
        assert r.status_code == 200
        system, user = service.planner.llm.calls[-1]
        assert "evil.example" not in system + user and "50% off" not in system + user
        assert user.strip().endswith("and the rest?")


def test_permissions_have_explanations_and_accept_shell_reports(api):
    client, _ = api
    perms = client.get("/permissions", headers=H).json()
    by_name = {p["name"]: p for p in perms}
    assert {"full_disk_access", "accessibility", "screen_recording", "notifications"} <= set(by_name)
    assert all(p["why"] and p["how"] and p["status"] in ("granted", "denied", "not_requested", "unknown") for p in perms)
    assert by_name["accessibility"]["status"] == "unknown"
    r = client.post("/permissions", json={"name": "accessibility", "status": "granted"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "granted"
    assert client.post("/permissions", json={"name": "nope", "status": "granted"}, headers=H).status_code == 404
    assert client.post("/permissions", json={"name": "accessibility", "status": "maybe"}, headers=H).status_code == 422
    folders = [p for p in client.post("/permissions/probe", headers=H).json() if p["name"].startswith("folder:")]
    assert folders and all(p["status"] == "granted" for p in folders)   # the test roots are tmp dirs


def test_settings_list_every_external_service(api):
    client, _ = api
    s = client.get("/settings", headers=H).json()
    services = {x["service"]: x for x in s["external_services"]}
    assert set(services) == {"ollama", "gmail", "browser"}
    assert services["ollama"]["local"] is True
    assert all(x["endpoints"] and x["note"] for x in services.values())
    assert s["paths"]["conversations"] is None   # this fixture runs without a store


# ---- standby and the memory watchdog ---------------------------------------------------------
def test_memory_watchdog_puts_the_agent_on_standby_and_stops_the_run(api):
    client, service = api
    unloaded = []

    class Models:
        strong_backend = None
        strong_name = None
        def unload_fast(self):
            unloaded.append(True)
    service.models = Models()
    service.memory_reader = lambda: {"used_percent": 70.0, "swap_free_gb": 3.0}
    assert service.check_memory() is None and not service.paused
    cid, turn = plan(client)
    with client.websocket_connect("/events", headers=WS) as ws:
        hello(ws)
        client.post(f"/plan/{turn['plan_id']}/run", headers=H)
        until(ws, "approval.request")
        service.memory_reader = lambda: {"used_percent": 95.0, "swap_free_gb": 3.0}
        assert "95%" in service.check_memory()
        assert until(ws, "run.end")["status"] == "cancelled"
    st = client.get("/status", headers=H).json()
    assert st["state"] == "paused" and "Auto-standby" in st["standby_reason"] and st["memory"]["used_percent"] == 95.0
    for _ in range(100):
        if unloaded:
            break
        time.sleep(0.01)
    assert unloaded and service.registry.calls == []
    # resuming while memory is still full goes straight back to standby, with the reason
    assert client.post("/resume", headers=H).json()["paused"] is True
    service.memory_reader = lambda: {"used_percent": 60.0, "swap_free_gb": 3.0}
    assert client.post("/resume", headers=H).json()["state"] == "idle"


def test_swap_almost_full_also_triggers_standby(api):
    client, service = api
    service.memory_reader = lambda: {"used_percent": 50.0, "swap_free_gb": 0.2}
    assert "swap" in service.check_memory() and service.paused


def test_standby_button_and_emergency_stop_give_reasons(api):
    client, service = api
    assert client.post("/standby", headers=H).json()["standby_reason"] == "Paused by you"
    client.post("/resume", headers=H)
    assert client.post("/stop", headers=H).json()["standby_reason"] == "Emergency stop"


def test_plan_needing_an_unavailable_tool_is_explained_not_run(api):
    client, service = api
    cid, turn = plan(client)
    service.registry._tools.pop("gmail.list")           # e.g. no Gmail app password on this Mac
    view = client.get(f"/conversations/{cid}", headers=H).json()
    assert "gmail.list" in view["turns"][0]["unavailable"] and "app password" in view["turns"][0]["unavailable"]
    r = client.post(f"/plan/{turn['plan_id']}/run", headers=H)
    assert r.status_code == 409 and "can't run here" in r.json()["detail"]


def test_browser_plan_is_unavailable_until_the_extension_connects(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [json.dumps(INJECTION_PLAN)], [{"summary": "x"}])
    service.browser_ready = lambda: False
    with client:
        cid, turn = plan(client, "open the link in my email")
        view = client.get(f"/conversations/{cid}", headers=H).json()
        assert "browser, which isn't connected" in view["turns"][-1]["unavailable"]
        assert "connected" in (turn["unavailable"] or "")
        assert client.post(f"/plan/{turn['plan_id']}/run", headers=H).status_code != 200
        service.browser_ready = lambda: True
        assert client.get(f"/conversations/{cid}", headers=H).json()["turns"][-1]["unavailable"] is None


ANSWER_PLAN = {"goal": "Explain Urdu input", "steps": [
    {"id": "a", "tool": "show", "args": {"text": "Add **Urdu** in System Settings → Keyboard → Input Sources."}}]}


def test_plain_question_gets_a_direct_answer_with_nothing_to_run(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [json.dumps(ANSWER_PLAN)], [])
    with client:
        cid, turn = plan(client, "how can i use urdu on this mac")
        assert turn["answer"].startswith("Add **Urdu**") and turn["plan_id"] is None
    assert any('"answer"' in line for line in (tmp_path / "audit.jsonl").read_text().splitlines())


def test_follow_up_sees_earlier_turns_but_never_tool_output(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [json.dumps(ANSWER_PLAN), json.dumps(INJECTION_PLAN),
                                                     json.dumps(ANSWER_PLAN)], [{"summary": "SECRET-PAGE-TEXT"}])
    with client:
        cid, _ = plan(client, "how can i use urdu")
        r = client.post("/chat", json={"message": "open the link in my email", "conversation_id": cid}, headers=H)
        assert r.status_code == 200
        r = client.post("/chat", json={"message": "can you do this?", "conversation_id": cid}, headers=H)
        assert r.status_code == 200
    last_prompt = service.planner.llm.calls[-1][1]
    assert "Owner: how can i use urdu" in last_prompt and "Add **Urdu**" in last_prompt
    assert "SECRET-PAGE-TEXT" not in last_prompt and "USER REQUEST: can you do this?" in last_prompt


def test_only_read_and_show_plans_run_without_a_press(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [json.dumps(INJECTION_PLAN), json.dumps(CLEANUP_PLAN)], [])
    with client:
        _, safe = plan(client, "open the link in my email")       # browser.open (asks on new sites) + show
        _, risky = plan(client, "trash my marketing mail")         # gmail.trash needs approval
    assert safe["auto_run"] is True and risky["auto_run"] is False


def test_fixed_success_text_is_replaced_by_the_tools_own_summary(policy, tmp_path):
    from agent.planner import Plan
    from agent.tools import ToolSpec
    _, service = make_client(policy, tmp_path, [], [])
    service.registry.add(ToolSpec("system.memory", lambda: {"summary": "x"}, "memory", {}, source="system",
                                  returns="{summary, ...numbers}"))
    plan = Plan.model_validate({"goal": "g", "steps": [
        {"id": "m", "tool": "system.memory", "args": {}},
        {"id": "s", "tool": "show", "args": {"text": "All done, memory is great!"}}]})
    service._show_real_results(plan)
    assert plan.steps[1].args["text"] == {"$ref": "m.summary"}


def test_auto_standby_snapshots_the_biggest_apps_first(policy, tmp_path):
    client, service = make_client(policy, tmp_path, [], [])
    service.memory_reader = lambda: {"used_percent": 95.0, "swap_free_gb": 30.0, "pressure": 1}
    service.top_apps_reader = lambda: [{"name": "Google Chrome", "memory_gb": 6.2},
                                       {"name": "Ollama (Rozan's AI model)", "memory_gb": 3.4}]
    with client:
        assert service.check_memory() and service.paused
        st = client.get("/status", headers=H).json()
    assert st["standby_info"]["apps"][0]["name"] == "Google Chrome"
    assert "Google Chrome 6.2 GB" in service.standby_reason and "Rozan's AI model" in service.standby_reason
