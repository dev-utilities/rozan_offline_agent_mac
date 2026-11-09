# Rozan — a private, on-device AI agent for macOS

*Rozan* (روزن) is Urdu for a small window: it sees your Mac, and nothing leaves it.

Rozan is a desktop agent that can work with your files, apps, browser and mail **using only local
models** (Ollama on your Mac). No prompt, page, email or file is ever sent to a third-party AI. It is
built so that **prompt injection cannot turn into an action**, following the CaMeL design
(Google DeepMind): a planner that never sees untrusted content, a quarantined reader that cannot act,
and a policy enforced in code.

> Status: **beta**. It works, it is careful, and it is still learning. macOS only.

## What it does

- A floating bubble on every Space. Click it (or ⌃⌥Space) to chat; hover for 🎤 dictate and × quit.
- Plain questions get a direct answer. Follow-ups ("can you do this?") understand the earlier chat.
- Tasks become a plan. Plans that only read or show things run at once; anything risky
  (shell, send, click, type, trash, write) shows each step and its exact command first, and every risky
  step asks again while it runs.
- Tools: files (read, write with backup, move to Trash), read-only shell, Gmail (IMAP), your **real
  Chrome** through the Rozan Bridge extension (open, read, click/type by label), any app on screen through
  the macOS **Accessibility** API (read windows, press buttons, type into fields), Mac info (memory, disk,
  battery, busiest apps), keyboards / input languages.
- Safety: per-task scope from your words only, provenance tags on every value, approval cards, a
  never-control list of apps and never-visit list of sites, standby on low memory, emergency stop, a
  hash-chained audit log, encrypted memory, isolated and incognito chats.
- Voice input with Apple's on-device recognition only.

## How it stays safe

```
 you ──request──▶ PLANNER (local model) ──static JSON plan──▶ EXECUTOR (Python)
                  sees: your request, this chat's            │
                  earlier requests/replies, tool list        ├─ scope check (what this request may do)
                  never sees: email, web, files, screens     ├─ policy check per step (data-flow + control-flow taint)
                                                             ├─ approval card for anything risky
                                                             ├─ audit log
                                                             ▼
                              READER (quarantined model) ◀── untrusted content
                              no tools; returns ONE typed schema (bool/enum/capped string)
```

1. **The plan is fixed before anything untrusted is read.** Nothing an email or web page says reaches the planner.
2. **The reader cannot act.** It only fills small typed answers; anything else fails closed.
3. **Every value carries provenance.** `policy.yaml` says which sources may reach which tool, as arguments and as
   conditions. Text read from a page can be shown to you, but it can never become a shell command, a URL, a
   recipient, a click target or typed text, unless you picked it yourself from a list.
4. **Risky tools ask you first**, showing the exact arguments.
5. **Some things are never controlled**: terminals, password managers, System Settings, and Rozan itself
   (`desktop.blocked_apps`), and sites on `browser.blocked_hosts`.
6. **Secrets never enter a prompt.** Tools read credentials at call time from `~/.personal_agent/secrets.json`.
7. **Everything is logged** to `~/.personal_agent/audit.jsonl` (hash-chained; `GET /audit/verify`).

The worst case of a fully fooled reader is a *wrong verdict on the item it read*
(`tests/test_executor.py::test_injected_email_can_only_flip_its_own_verdict`).

## Requirements

- macOS 14+ on Apple silicon (built and tested on an 18 GB M3 Pro), Xcode command-line tools.
- [Ollama](https://ollama.com) with `qwen3.5:4b` (default planner and reader, ~3.5 GB while working).
- Python 3.11+, Google Chrome (for web tasks).

## Setup

```sh
brew install ollama && ollama pull qwen3.5:4b
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python scripts/install_chrome_bridge.py   # lets Chrome talk to Rozan (native messaging)
scripts/build_mac_app.sh --install                  # builds Rozan.app (+ native helpers in bin/)
open ~/Applications/Rozan.app
```

Then ask Rozan: *"install your chrome extension"*. It loads `extension/` in Chrome by itself
(it asks for Accessibility permission the first time it needs to see other apps).

Optional Gmail over IMAP (Google Account → Security → 2-Step Verification → App passwords):

```sh
mkdir -p ~/.personal_agent && cat > ~/.personal_agent/secrets.json <<'J'
{"gmail": {"user": "you@gmail.com", "app_password": "xxxx xxxx xxxx xxxx"}}
J
chmod 600 ~/.personal_agent/secrets.json
```

## Use

- Click the bubble or press ⌃⌥Space; ⌃⌥D to dictate. Right-click the bubble or use the menu bar icon for
  New chat, Open full app, Standby, Watcher, Emergency stop, Open at login, Quit.
- Full app: chats, audit log, settings (memory, watcher, browsers, permissions).
- Without the Mac app: `python -m agent.server --open` (real) or `--fake --open` (scripted models, no setup).

## Develop

```sh
.venv/bin/python -m pytest                          # core, policy, API, desktop guard rails, web UI (headless)
swift scripts/pa_signal.swift toggle|fullapp|dictate   # drive the running app from scripts
```

- New tool: a function in `agent/tools/`, a `ToolSpec` with a `source` label, an entry in `policy.yaml`
  (`allow_sources`, `approval`, `risk`), and a capability in `agent/scope.py`. Unlisted tools are refused.
- Design notes and decisions: `HANDOFF.md`. Requirements and status: `REQUIREMENTS.md`.

## Known limits

- Small local models still make mistakes; the validator, scope check and approvals catch the dangerous
  ones, not the silly ones. Bigger models (Qwen 3.5 9B, Gemma 4 12B) are an option on Macs with more free memory.
- The reader can be fooled into a wrong verdict on one item; keep approvals on for trash and send.
- Desktop control depends on each app's accessibility labels; some apps expose very little.

## Credits

Icons: [Lucide](https://lucide.dev) (ISC License). Architecture: CaMeL, "Defeating Prompt Injections by
Design" (Debenedetti et al., Google DeepMind).
