"""Browser tests for the web UI (ui/) against a live server with scripted models and fake tools.

Needs `playwright install chromium`; skipped otherwise. Narrow viewport = the modal next to the
bubble; wide viewport = the full app window with the sidebar.
"""
import threading
import time
from pathlib import Path

import httpx
import pytest

pw = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from agent.fakes import ScriptedPlannerLLM, ScriptedReaderLLM, build_fake_registry  # noqa: E402
from agent.policy import Policy  # noqa: E402
from agent.server import AgentService, bind, create_app  # noqa: E402

expect = pw.expect
TOKEN = "ui-test-token"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    policy = Policy.load(ROOT / "policy.yaml")
    tmp = tmp_path_factory.mktemp("ui")
    service = AgentService(policy, lambda printer, show: build_fake_registry(policy, show=show),
                           ScriptedPlannerLLM(), ScriptedReaderLLM(), tmp / "audit.jsonl", fake=True,
                           store_dir=tmp / "conversations", approval_timeout_s=10,
                           planner_model="fake", reader_model="fake")
    sock = bind(0)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(service, TOKEN), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            httpx.get(url + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("server did not start")
    yield url, service
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser binary missing
            pytest.skip(f"chromium not available: {e}")
        yield b
        b.close()


def open_page(browser, live, width, height):
    url, service = live
    service.registry.calls.clear()
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{url}/#token={TOKEN}")
    page.locator("#conn.on").wait_for(timeout=5000)
    return ctx, page


@pytest.fixture
def page(browser, live):
    """The modal: narrow viewport, fresh chat."""
    ctx, page = open_page(browser, live, 420, 680)
    page.click("#btn-new")
    expect(page.locator("#empty")).to_be_visible()
    yield page
    ctx.close()


@pytest.fixture
def app(browser, live):
    """The full app window: wide viewport with the sidebar, fresh chat."""
    ctx, page = open_page(browser, live, 1000, 700)
    expect(page.locator("#sidebar")).to_be_visible()
    page.click("#side-new")
    expect(page.locator("#empty")).to_be_visible()
    yield page
    ctx.close()


def ask(page, text):
    page.fill("#input", text)
    page.press("#input", "Enter")
    card = page.locator(".card-plan").last
    card.wait_for(timeout=10000)
    return card


def run_and_approve(page, card, decision="Approve"):
    card.get_by_role("button", name="Run", exact=True).click()
    approval = page.locator(".card-approval").last
    approval.wait_for(timeout=10000)
    approval.get_by_role("button", name=decision, exact=True).click()
    expect(page.locator(".run").last.locator(".run-status")).to_have_text("Done", timeout=10000)
    return approval


# ---- modal -----------------------------------------------------------------------------------
def test_plan_execute_approve_and_untrusted_output(page, live):
    url, service = live
    card = ask(page, "trash my marketing mail")
    expect(card.locator(".plan-goal")).to_have_text("Trash marketing emails older than 60 days")
    expect(card.locator(".pre")).to_be_hidden()
    card.get_by_role("button", name="Show JSON").click()
    expect(card.locator(".pre")).to_contain_text('"gmail.trash"')
    expect(card.get_by_role("button", name="Cancel")).to_be_visible()

    card.get_by_role("button", name="Run", exact=True).click()
    expect(page.locator("#state-chip")).to_have_text("Needs approval", timeout=10000)
    approval = page.locator(".card-approval").first
    expect(approval).to_contain_text("gmail.trash")
    expect(approval.locator(".pre-args")).to_have_text("1")
    approval.get_by_role("button", name="Approve", exact=True).click()
    expect(approval).to_contain_text("Approved")

    run = page.locator(".run").first
    expect(run.locator(".run-status")).to_have_text("Done", timeout=10000)
    expect(page.locator("#state-chip")).to_have_text("Idle")
    outputs = page.locator(".card-untrusted")
    expect(outputs).to_have_count(2)
    expect(outputs.first).to_contain_text("evil.example")          # injected text shown only as data
    expect(outputs.first.locator(".badge")).to_contain_text("untrusted")
    expect(outputs.first.locator(".badge")).to_contain_text("gmail.get")
    expect(run).to_contain_text("skipped")                          # the personal email was not trashed
    expect(card.get_by_role("button", name="Run again")).to_be_enabled()
    assert service.registry.calls == [("gmail.trash", {"id": "1"})]


def test_deny_leaves_the_mail_alone(page, live):
    url, service = live
    card = ask(page, "trash my marketing mail")
    approval = run_and_approve(page, card, "Deny")
    expect(approval).to_contain_text("Denied")
    assert service.registry.calls == []


def test_policy_refusal_is_shown_and_sets_the_error_state(page, live):
    url, service = live
    # only opens and shows: runs on its own, no Run press (the policy still refuses the tainted URL)
    page.fill("#input", "open the link in my email")
    page.press("#input", "Enter")
    page.locator(".plan-mini").last.wait_for(timeout=10000)
    expect(page.locator(".card-plan")).to_have_count(0)
    run = page.locator(".run").first
    expect(run.locator(".run-status")).to_have_text("Stopped: policy refused a step", timeout=10000)
    expect(run.locator(".line-refuse")).to_contain_text("policy refused browser.open")
    expect(page.locator(".card-approval")).to_have_count(0)
    expect(page.locator("#state-chip")).to_have_text("Error")
    assert service.registry.calls == []
    httpx.post(f"{url}/state/ack", headers={"Authorization": f"Bearer {TOKEN}"})  # leave the state clean


def test_more_picker_lists_chats_and_saved_actions(page):
    ask(page, "trash my marketing mail")
    page.click("#btn-more")
    expect(page.locator("#more")).to_be_visible()
    expect(page.locator("#more-chats .list-item.active")).to_contain_text("trash my marketing mail")
    page.locator("#more-actions [data-example='cleanup']").click()
    card = page.locator(".card-plan").last
    card.wait_for(timeout=5000)
    expect(card.locator(".plan-goal")).to_contain_text("Trash marketing emails older than 60 days")
    expect(page.locator(".msg-example")).to_have_text("Example: cleanup")


def test_pause_blocks_the_composer_until_resumed(page):
    page.click("#btn-more")
    page.click("#more-pause")
    expect(page.locator("#paused-banner")).to_be_visible()
    expect(page.locator("#state-chip")).to_have_text("Standby")
    expect(page.locator("#paused-text")).to_contain_text("Paused by you")
    expect(page.locator("#input")).to_be_disabled()
    page.click("#btn-resume")
    expect(page.locator("#paused-banner")).to_be_hidden()
    expect(page.locator("#input")).to_be_enabled()


def test_settings_show_policy_services_and_permissions(page):
    page.click("#btn-settings")
    body = page.locator("#settings-body")
    expect(body).to_contain_text("Where your data goes")
    expect(body).to_contain_text("ollama")
    expect(body).to_contain_text("macOS permissions")
    expect(body).to_contain_text("Full Disk Access")
    expect(body).to_contain_text("gmail.send")
    expect(body).to_contain_text("planner, user")
    expect(body).to_contain_text("paypal.com")
    expect(body).to_contain_text("never")
    page.keyboard.press("Escape")
    expect(page.locator("#view-settings")).to_be_hidden()
    expect(page.locator("#view-chat")).to_be_visible()


def test_reload_restores_the_conversation_from_the_server(page):
    ask(page, "trash my marketing mail")
    page.reload()
    page.locator("#conn.on").wait_for(timeout=5000)
    expect(page.locator(".card-plan")).to_have_count(1)
    expect(page.locator(".msg-user")).to_have_text("trash my marketing mail")


def test_without_a_token_the_api_is_locked_and_the_ui_asks_for_one(browser, live):
    url, _ = live
    assert httpx.post(url + "/chat", json={"message": "x"}).status_code == 401
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(url + "/")
    expect(page.locator("#token-screen")).to_be_visible()
    ctx.close()


# ---- full app window ----------------------------------------------------------------------------
def test_full_app_sidebar_switches_chats_and_deletes_them(app):
    page = app
    title = "trash my marketing mail, sidebar test"       # unique: the server is shared by the whole module
    ask(page, title)
    expect(page.locator("#chat-list .chat-item.active")).to_contain_text(title)
    page.click("#side-new")
    expect(page.locator("#empty")).to_be_visible()
    page.locator("#chat-list .chat-item", has_text=title).click()
    expect(page.locator(".card-plan")).to_have_count(1)
    active = page.locator("#chat-list li", has=page.locator(".chat-item.active"))
    active.hover()
    active.locator(".chat-del").click()           # the confirm dialog is auto-accepted
    expect(page.locator("#empty")).to_be_visible()
    expect(page.locator("#chat-list .chat-item", has_text=title)).to_have_count(0)


def test_full_app_audit_log_view(app):
    page = app
    card = ask(page, "trash my marketing mail")
    run_and_approve(page, card)
    page.locator(".side-link[data-view='audit']").click()
    body = page.locator("#audit-body")
    expect(body).to_contain_text("tool.call")
    expect(body).to_contain_text("gmail.trash")
    page.fill("#audit-filter", "EmailTriage")                    # only the reader's records mention the schema
    expect(body.locator(".audit-row:not([data-event='quarantine'])")).to_have_count(0)
    expect(body.locator(".audit-row[data-event='quarantine']").first).to_be_visible()
    page.select_option("#audit-event", "approval")
    page.fill("#audit-filter", "")
    expect(body.locator(".audit-row").first).to_contain_text("approved")
    page.locator(".side-link[data-view='chat']").click()
    expect(page.locator("#view-chat")).to_be_visible()


def test_memory_setup_preferences_and_incognito_chat(app, live):
    url, service = live
    from agent.vault import Vault
    import tempfile
    from pathlib import Path
    service.vault = Vault(Path(tempfile.mkdtemp()) / "memory.enc")
    page = app
    page.locator(".side-link[data-view='settings']").click()
    box = page.locator("#memory-box")
    expect(box).to_contain_text("Set up memory")
    box.locator("input[type=password]").fill("longenough")
    box.get_by_role("button", name="Set up memory").click()
    expect(box).to_contain_text("Unlocked")
    box.locator("input[type=text]").fill("Prefer archive over delete")
    box.get_by_role("button", name="Remember").click()
    expect(box).to_contain_text("Prefer archive over delete")
    box.get_by_role("button", name="Lock now").click()
    expect(box).to_contain_text("Locked")
    page.locator("#sidebar [data-new-mode='incognito']").click()
    expect(page.locator("#mode-chip")).to_have_text("Incognito · not saved")
