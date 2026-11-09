# HANDOFF — Rozan (offline agent for macOS)

Last updated: 2026-11-09 (renamed to **Rozan**; repo `dev-utilities/rozan_offline_agent_mac`). Earlier: 2026-10-14 on the Mac host.
Internal ids are unchanged on purpose: bundle id `com.personal-agent.shell` (macOS permissions are tied to it),
data folder `~/.personal_agent`, Python package `agent`, native host `com.personal_agent.bridge`.
Previous update: 2026-10-13 from a Linux container (core written, never executed).

Requirements live in `REQUIREMENTS.md`. Owner questions live in `QUESTIONS.md`; answers are copied into section 9 of this file.

## 1. What this project is

A personal assistant that runs **on the owner's Mac, as the owner**, with the permissions macOS asks for.
It may touch files, the shell, mail and the owner's real browser with whatever accounts are signed in.
There is no limit on what it may do **as long as it is safe**: every destructive step passes a data-flow
check and an approval card, and prompts and content go only to **local Ollama models**, never to a
third-party AI. The core design goal is that prompt injection in email/web/file content **cannot become
an action**. Architecture follows CaMeL (Google DeepMind, SaTML 2026): privileged planner + quarantined
reader + capability-tagged data flow enforced in Python. Read `README.md` for the security model; it is
the contract every change must keep.

## 2. Decisions already made (do not re-litigate)

| Topic | Decision | Why |
|---|---|---|
| Where it runs | On the host Mac, launched by the Swift shell, as the owner. **No container.** The `docker/` packaging was removed on 2026-10-14 | the shell, the real browser and macOS folder permissions all assume the core runs as the user |
| Core language | Python (`agent/`) | best local-model tooling; nothing about the core is language-bound |
| UI shell | Native Swift (SwiftUI + AppKit `NSPanel`) | floating always-on-top bubble across Spaces + TCC permissions are far easier native; Electron/Bun rejected |
| Modal / full app content | Web UI (HTML/JS) served by the Python core, shown in `WKWebView` | so ~90% of the UI is testable headlessly with Playwright; Swift shell stays thin |
| Platform | macOS first; Windows later = rewrite only the shell | |
| Models | **`qwen3.5:4b` for both planner and reader** (one model, ~3.5 GB while working, unloaded when idle; thinking off, 8K context). Safety comes from separate inputs, not separate weights. Benchmarked 2026-10-23 on this 18 GB M3 Pro, see section 9. Gemma 4 26B via TurboFieldfare (`~/repos/turbo-fieldfare`, ~2.3 GB) is the runner-up | fits next to the owner's daily apps; fast; no refusals |
| "Offline" means | prompts, mail, pages and files reach only Ollama on this Mac (`network.ollama_url` must be loopback unless `allow_remote_ollama`). The agent itself may use any service on the owner's behalf; `Policy.external_services()` lists them for the settings screen | owner's clarification 2026-10-14: no data to third-party AI, otherwise no limits |
| Browser | **Drive the owner's real browsers by desktop automation, in the owner's own sessions.** Chrome (any profile, incognito) first; other installed browsers through the generic GUI layer. Page reading: DOM text via Apple Events JavaScript for content **plus** a screenshot for layout and click targets; Accessibility tree as fallback. Any site allowed, with a one-time confirmation per new domain and a never-visit list. The Playwright tool is an interim stand-in | owner answers A1–A4, 2026-10-22 |
| GUI automation | One automation layer for all apps: AppleScript/Shortcuts where an app supports them, then the Accessibility API, then synthetic mouse/keyboard (Quartz events or `pyautogui`) on coordinates from a screenshot. Every screen read is untrusted (`screen` source) | owner answers C1/C2 |
| Guard layers | (1) deterministic policy (data-flow + hard no-go list), cannot be overridden by any model; (2) an independent local **monitor** model that reviews each proposed action against the request and can only **add** a consent prompt, never remove one; (3) the approval card. Downloads/installs never automatic: show the source site, check code signature/notarization, then ask | owner answers B1/B2/C1 |
| Hard no-go list | payments and purchases, password/2FA/security-setting changes, posting publicly, permanent deletes (Trash only), messaging people on a blocked list, plus: sharing files/links outside the account, changing mail forwarding/filters, granting OAuth/app access, editing system settings or login items, disabling security software, running downloaded code | owner answer B2 + research |
| Knowledge base | user preferences/memory stored **encrypted** with a key derived from the owner's passphrase; a dialog asks for it when needed; auto-locks after inactivity. Chats can be created **isolated** (no shared memory); **incognito** chats are permanently deleted after a set inactivity period | owner answers C1/F2 |
| Mail | IMAP/SMTP for bulk sort (labels, archive, mark read, trash) via a Google **app password** (no Google review needed; requires 2-Step Verification; Workspace admins can block it); the browser for viewing and anything else | owner answer A5 |
| Existing agents | OpenClaw rejected (CVEs, marketplace malware, LLM-decides-security). Hermes considered but lacks planner/reader split | |
| Security line | `gmail.send`, `shell.run`, `browser.open`, `fs.write`, `browser.click/type` accept only `[user, planner]` sources in `policy.yaml` | that single rule is what bounds injection damage; it stays however wide access becomes |
| File access | whole home folder, read and write; deny list (`denied_paths`, `denied_globs`) wins over any root and is checked after symlink resolution; writes ask for approval and back up any file they overwrite to `~/.personal_agent/backups/` | "no limitation as long as it is safe" |
| Destructive operations | prefer reversible: Trash over delete, backup before overwrite, approval card for every irreversible tool; owner may say "approve all <tool> this run" | QUESTIONS B1–B4 refine the list |
| Service shape | FastAPI + uvicorn on `127.0.0.1`, bearer token in a 600-mode file, one execution worker thread, events over one websocket | token keeps other local processes/web pages out |
| Web UI | plain HTML/CSS/JS in `ui/`, no build step, CSP `script-src 'self'`, untrusted text only via `textContent`; one page serves the modal (narrow) and the full app window (wide, sidebar) | the page renders email content; it must never render it as markup |
| Chat history | one JSON file per conversation in `~/.personal_agent/conversations/` (dir 700, files 600); the planner and scope model also see the last 6 turns of the same chat, but only the owner's own words and the planner's own replies, never tool output | follow-ups ("can you do this?") must work; tool output stays untrusted (2026-11-09) |
| When plans run | plans whose tools are all read/show (approval false or new_domain, not high risk) run at once; anything else shows "Review before running" with each step and exact command, and risky steps still ask again at run time | owner, 2026-11-09 |
| Plain questions | a plan that only shows planner-written text is shown as a reply (no Run button) and logged as `answer` in the audit | owner, 2026-11-09 |
| Desktop control | `desktop.*` tools through the Accessibility API (`helpers/desktop.swift` → `bin/desktop`), not pixel clicks; press/type/key always ask; targets only from the owner/planner or an `ask.choose` pick; never-control list `desktop.blocked_apps` (terminals, password managers, System Settings, the agent itself) | owner: "it should be able to control the desktop", 2026-11-09 |
| Pause | `POST /pause` refuses new plans/runs **and cancels the running plan** | "Pause agent" must mean nothing happens from now on (confirm: QUESTIONS, if the owner disagrees) |

## 3. Current state

2026-11-09: **Rozan** beta. `pytest`: 126 pass (core, policy, API, approver, scope, bridge, watcher, memory,
desktop guard rails, Playwright UI). Requirement status per item: `REQUIREMENTS.md`.

- **Mac app** (`macos/`, SwiftPM; build `scripts/build_mac_app.sh [--install]` → `macos/build/Rozan.app`, also
  compiles `helpers/*.swift` into `bin/`): gradient bubble with the Lucide sparkle (52 pt, floating, all Spaces,
  drag, per-display position, state ring/pulse/red dot/dim); **hover bar** with mic (dictate) and × (quit, asks
  first); modal anchored to the bubble; full-app window; menu bar item; right-click menu; ⌃⌥Space, ⌃⌥D. Shows
  macOS's Accessibility prompt the first time a desktop step needs it (`/status.permission_needed`). The page's
  `confirm()`/`alert()` are native sheets.
- **Web UI** (`ui/`): modern layout (Lucide icons in `ui/icons.js`, ISC), ⋯ menu, mic button, suggestion chips,
  replies as chat bubbles with safe Markdown (escaped first), quiet plan line for auto-run plans, "Review before
  running" card with readable steps for risky ones, untrusted output still labelled.
- **Core**: planner/reader split, provenance policy, task scope (now with chat context), direct answers, auto-run
  of read-only plans, fast/careful models with memory checks (swap room = swap file space + disk above 10 GB, plus
  macOS pressure level), standby + watchdog, watcher, encrypted memory, chat modes, look-then-ask, audit log.
  Planner output fixes in code: guessed browser ids dropped, fixed "done!" text replaced by the tool's own
  `summary`, a missing final `show` added.
- **Tools added 2026-11-09**: `system.keyboards/add_keyboard/remove_keyboard` (`helpers/input_sources.swift`);
  `desktop.apps/controls/read/press/type/key` (`helpers/desktop.swift`, wakes Chromium accessibility trees);
  `setup.chrome_extension` (loads `./extension` via chrome://extensions → Load unpacked; keys go system-wide only
  while Chrome or the macOS folder picker is frontmost, checked per key).
- **Verified live 2026-11-09**: extension installed by the agent itself and connected; "how's weather today" →
  wttr.in in the owner's Chrome → answer; keyboard tool end to end (on an already-enabled layout); desktop.controls
  on Finder.
- **Browser**: Rozan Bridge extension (`extension/`), any Chromium browser, per-instance ids.
- **Icons (owner-approved 2026-11-09)**: `scripts/make_icon.swift` draws them from shapes. Variant D (light) for the
  Dock/Finder icon and the extension; the bubble follows the system theme (C navy in dark mode, D in light mode).
- **Distribution**: `scripts/make_dmg.sh` → `dist/Rozan-<version>-arm64.dmg` (self-contained app: code in
  `Contents/Resources/core`, never written to; `PYTHONDONTWRITEBYTECODE=1`). First launch shows **Rozan Setup**
  (`Setup.swift`): Ollama (ollama.com zip), Python 3.11+ via uv (no admin), Rozan's venv in
  `~/Library/Application Support/Rozan/venv` from `requirements.txt`, model pull, Chrome bridge (launcher in
  Application Support, runs Rozan's own Python). Verified 2026-11-09 by running the app from the mounted read-only DMG.
  `.github/workflows/release.yml` builds and attaches the DMG for `v*` tags (ad-hoc signed; not notarized: users
  click Open Anyway once). Mac App Store is not an option (sandbox forbids Accessibility control, subprocesses,
  installing other software).
- **Standby popup**: on auto-standby the core snapshots the top 5 apps by memory (processes grouped by `.app`;
  Rozan's core and Ollama named) into `/status.standby_info` and the banner; the app shows Resume / Stay on Standby /
  Open Activity Monitor. Verified live 2026-11-09.

Not done yet: model comparison of qwen3.5:9b / gemma4:12b
(the guard aborted it: ~14 GB already in swap; reboot first); connectors (section 5 C2); Touch ID unlock; daily log
reviewer; saved-action authoring from the UI.

## 4. Requirements

All owner requirements, with status, live in `REQUIREMENTS.md`. Update that file whenever a requirement
changes; this handoff only tracks decisions, state and next steps.

## 5. Plan — next steps in order

### A. Python core as a service — DONE

All under the bearer token except `/health` and the static files:

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness only (`ok`, `version`, `pid`); no token needed, use it to wait for startup |
| `GET /status[?deep=1]` | `state` (idle/planning/running/needs_approval/error/paused), `paused`, `last_error`, current run + pending approvals, models; `deep` probes Ollama |
| `POST /chat {message, conversation_id?}` | planner → `validate_plan` → turn `{plan_id, plan, error}`; 409 while paused |
| `POST /plan/{id}/run` | 202 + run summary; 409 while another run is active or paused |
| `WS /events` | first client message `{"token": ...}` (or `Authorization` header); then `state`, `run.start`, `audit`, `show`, `log`, `approval.request`, `approval.resolved`, `run.end` |
| `POST /approval/{id} {decision: yes/no/all}` | 404 once answered or timed out |
| `POST /run/{id}/cancel`, `GET /run/{id}` | cancel stops at the next step and denies the pending approval; GET returns status + buffered events |
| `POST /pause`, `POST /resume`, `POST /state/ack` | pause cancels the current run; ack clears the sticky error |
| `GET /audit?limit=&run=` | tail of `~/.personal_agent/audit.jsonl` |
| `GET /settings` | read-only policy, services, model names, paths |
| `GET /permissions`, `POST /permissions {name, status, detail?}`, `POST /permissions/probe` | status + explanations; the shell reports native ones; probe touches the folders (may prompt) |
| `GET/POST /conversations`, `GET/DELETE /conversations/{id}` | persisted conversations with turns and runs |
| `GET /examples`, `POST /examples/{name}/load` | `examples/*.json` as saved actions |

Files the shell needs: `~/.personal_agent/session.token` (mode 600, new token every start) and
`~/.personal_agent/session.json` (`{port, pid, started, token_file, fake}`); both removed on exit, including
on SIGTERM. `--port 0` picks a free port. `AGENT_HOME` overrides the directory. Requests with a non-loopback
`Host` header get 403; all responses carry a CSP and `Cache-Control: no-store`.

How the page gets the token, in order: `window.__AGENT_TOKEN__` (for the Swift shell: a `WKUserScript`
at **document start**, before `app.js` runs), else `#token=...` once in the URL (stored in
`sessionStorage`, then stripped), else the token screen asks for a paste.

### A2. Browser = the owner's default browser (blocked on QUESTIONS A1–A5)

Replace `agent/tools/browser.py` with a tool that drives the real browser; the policy rules do not change:
- `browser.open(url)` — opens the URL in the default browser (`open -u` / AppleScript `open location`).
  URL still only from `[user, planner]`. The host allowlist becomes a per-domain first-visit confirmation
  plus a never-visit list (A3).
- `browser.read()` — current tab's URL, title and text. Preferred source: Apple Events JavaScript
  (`document.body.innerText`) via `osascript` for Chrome/Safari (needs the one-time "Allow JavaScript from
  Apple Events" setting); fallback: the Accessibility tree; last resort: screenshot + `qwen2.5vl`.
  Result tagged `browser` (untrusted) exactly as today.
- `browser.click(selector|text)`, `browser.type(selector, text)` — JavaScript or AX actions; approval stays on.
- Permissions: Automation (the browser), Accessibility, Screen Recording only for the screenshot fallback.
  Add them to `PERMISSIONS` in `agent/server.py` with the explanation shown when first asked.
- Keep `source="browser"` so `policy.yaml` needs no change beyond deleting `allowed_hosts`.
- Tests: fake the `osascript` layer; keep `test_untrusted_text_cannot_reach_browser_even_if_planner_tries`.
- Mail: keep IMAP for bulk operations unless A5 says otherwise; the browser path shows the mailbox.

### A3. Task scope + careful model — DONE (2026-10-23)

- `agent/scope.py`: a scope step reads **only the user's request** and picks capabilities from a fixed list
  (`mail.read`, `mail.organize`, `mail.trash`, `mail.send`, `files.read`, `files.write`, `shell`,
  `browser.read`, `browser.act`). Code adds implied reads (acting on mail implies reading it). A plan using
  any tool outside the scope is never registered, and the executor refuses it again at run time.
- `agent/models.py`: `ModelManager` keeps one model in memory. Code (not a model) escalates to the careful
  model when the request is open-ended, the plan uses a `risk: high` tool (`policy.yaml`), the fast plan is
  out of scope, or the fast plan is invalid. Before starting it: unload the fast model, check
  `agent/memory.free_gb()` against `models.strong.min_free_gb`; if short, say so (keep the fast plan with a
  warning, or refuse if there is none). Plans written by the careful model that read untrusted content are
  also **read** by it at run time; low memory then stops the run with a clear message.
- Careful model = Gemma 4 26B via TurboFieldfare, started on demand and stopped after (`policy.yaml → models`).
- The plan card shows "Allowed to: …", which model planned it and why, and any warning.
- Real-model check: the scope step answers in about 1 s with qwen3.5:4b and got 8 of 8 test requests right
  after the implied-reads rule.

### A4. Narrow tools and hard-coded logs (next; owner idea 2026-10-23)

Principle: many small, named tools instead of a few powerful ones, so the scope check, the policy and the
audit log all speak in exact terms. `shell.run` and raw GUI clicks are last resorts.
1. **MVP tool set.** Add `gmail.label`, `gmail.archive`, `gmail.mark_read` (IMAP), `fs.move_to_trash`,
   `app.open`, `browser.open/read` on the real browser. Consider removing `shell.run` from the default policy
   or limiting it to an allowlist of read-only commands (`df`, `du`, `ls`, `uptime`).
2. **Logs are written by code, never by a model.** The executor already records tool, arguments, sources and
   approvals. Add a hash chain (each record carries the hash of the previous one) so deletion or editing is
   detectable, and keep the log outside anything the agent can write (`~/.personal_agent` is already denied).
3. **No free-text "comment" from the model.** A model-written comment ("delete button on email") is a claim
   and could lie. Instead code captures **evidence** at action time: for a click, the Accessibility label and
   role of the element under the cursor, the app and window title, and a small screenshot crop; for a file
   write, path, size and a diff summary. The planner's step `id` stays as a label only.
4. **Low-level GUI tools** (`move_cursor`, `left_click`, `type_text`) lose meaning in a log. Use them only as a
   fallback, always with the evidence above, and promote repeated patterns into named tools.
5. **Daily review.** A model with no memory and no tools reads the day's log as untrusted data (logs contain
   email subjects and page text, which could carry an injection aimed at the reviewer) and returns only a
   typed list of flagged record ids with a category. It cannot delete anything. Retention is a fixed rule
   (e.g. 30 days, flagged runs kept until the owner dismisses them). Review is detection after the fact;
   prevention stays with the scope check, policy and approvals.
6. **Tool roadmap from logs.** A weekly report counts fallback uses (`shell.run`, raw clicks) by app/command,
   so the next narrow tools are chosen from real usage.

### B. Swift shell (host only: Xcode, then launch + computer-use testing)
1. `macos/PersonalAgent.xcodeproj` (SwiftUI app, `LSUIElement=true`; menu bar item with the same menu as the bubble).
2. Bubble per section 4.3: `NSPanel`, borderless, non-activating, `level = .floating`,
   `collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]`, 52 pt circle, drag anywhere, position
   per display in `UserDefaults`, clamp on screen-parameter changes, template icon, state ring/badge/dot driven
   by `WS /events` `state` messages (fall back to polling `GET /status` every 2 s if the socket drops).
   An approval must never wait silently: pulse the bubble, and post a `UNUserNotification` when the modal is
   closed (ask for Notifications permission the first time, with the explanation from `/permissions`).
3. Modal: second `NSPanel` with `WKWebView` on `http://127.0.0.1:<port>/`, width ~400 pt, anchored to the
   bubble and flipped when within 420 pt of a screen edge; token via `WKUserScript`
   (`window.__AGENT_TOKEN__ = "..."`, `.atDocumentStart`, main frame only); a `WKScriptMessageHandler`
   named `shell` that handles `{action: "openFullApp"}`. Esc and a second click close it. Opening it calls
   `POST /state/ack`.
4. Full app: `NSWindow` (resizable, ≥ 720 pt wide) with another `WKWebView` on the same URL.
5. Process management: launch `.venv/bin/python -m agent.server --port 0` on start, read `session.json` for
   the port, poll `GET /health`, restart on crash (back off), SIGTERM on quit.
6. Permissions, lazily with the explanations from `GET /permissions`: Automation when the browser tool is
   first used, Accessibility only when a hotkey is enabled or AX reading is needed, Notifications the first
   time an approval would wait unseen, Full Disk Access via a guidance page only if B3 widens into
   `~/Library`; report results with `POST /permissions`. Hardened runtime + entitlements.
7. Launch at login via `SMAppService`.

### C2. Connectors (planned, 2026-11-08)

1. Add an MCP client to the core (`agent/connectors.py`): start each configured local MCP server (stdio), list its
   tools, and register each as a `ToolSpec` named `<connector>.<tool>` with source `connector.<connector>`.
2. A connector tool runs only if `policy.yaml → tools` lists it (unlisted = refused, as today). Reads:
   `allow_sources: [user, planner]`, no approval. Writes: approval, `risk: high`.
3. Credentials: macOS Keychain (`security` CLI or the Swift app), fetched at call time by `agent/broker.py`.
4. Scope: each connector adds `<connector>.read` / `<connector>.write` capabilities in `agent/scope.py`.
5. A cloud-hosted connector is listed by `Policy.external_services()`; data still goes only to local models.
6. First candidates: Google Calendar, Microsoft Graph (Outlook/Teams), Slack, Notion, GitHub.

### C. First real run (host)
1. Pull the models (QUESTIONS D1); create `~/.personal_agent/secrets.json` (Gmail app password), `chmod 600`.
2. `python main.py --dry-run "trash marketing mail older than 2 months"` → check plan quality. If the
   planner emits invalid JSON often, tune `agent/planner.py` SYSTEM prompt or try a different model.
   `GET /status?deep=1` tells you whether Ollama is reachable and the models are pulled.
3. `python main.py --plan-file examples/cleanup.json` with approval prompts → confirms Gmail IMAP path.
4. Then through the service: `python -m agent.server --open`, load `cleanup` from the picker, Execute,
   approve in the page.
5. Only then run end-to-end through the bubble UI.

### D. Later / nice to have
- Memory for the planner (user preferences only, never untrusted content) — QUESTIONS F2.
- Global hotkey, voice input, screenshot tool (its output is UNTRUSTED and gets its own source label).
- Settings become editable (a `PUT /settings` that rewrites `policy.yaml` and reloads the registry).
- Saved actions authored from the UI (save a reviewed plan under `examples/`) — QUESTIONS F3.
- Automation of other apps (Finder, Mail.app, Messages, Calendar…) — QUESTIONS C1/C2.
- Windows shell.

## 6. How to test from Claude Desktop (host)

- Core + UI without models or accounts: `.venv/bin/python -m agent.server --fake --open` prints a URL
  with `#token=...`; open it in the browser pane. Try "trash my marketing mail" (approval flow,
  untrusted output) and "open the link in my email" (policy refusal → error state). Widen the pane past
  720 px to see the full-app layout. The fake inbox is in `agent/fakes.py`.
- Tests: `.venv/bin/python -m pytest` (the UI tests start their own server on a free port and need the
  Chromium that `playwright install chromium` fetched; they skip if it is missing).
- Shell (section B): `xcodebuild -project macos/PersonalAgent.xcodeproj -scheme PersonalAgent build`, then
  open the built app; use computer use to verify bubble drag, modal open, approval card flow.
- Browser-pane note: screenshots there can lag one action behind, and navigating to the same URL with only a
  different `#fragment` does not reload the page; verify state with `read_page`/`find`.
- Optional isolation: a Tart/UTM macOS VM on the same Mac for granting permissions without touching the
  real account; drive it over SSH.

## 7. Rules for any change

- Every new tool gets a `source` label and an entry in `policy.yaml`; unlisted tools are refused.
- Nothing from `gmail.*`, `browser`, `fs.read`, `shell.run`, or `quarantine` may reach send/shell/browser-open/write.
- The planner must never receive tool results, untrusted content, or stored conversation turns. Keep it one-shot.
- No model call may go anywhere but the configured local Ollama. Any new code path that opens a network
  connection on the owner's behalf must be listed by `Policy.external_services()`.
- Destructive tools: approval on, prefer the reversible form (Trash, backup), log everything.
- Secrets never enter a prompt; use `agent/broker.py`. The server never puts the token in a URL it logs.
- The web UI puts tool/reader/audit text into the DOM only through `textContent`; keep the CSP without
  `'unsafe-inline'`.
- Every API route except `/health` and static files depends on `require_token`.
- Keep `pytest` green; add a test for each new policy rule and each new endpoint.
- **Before loading any model**, check free memory (`vm_stat`, `memory_pressure`) and swap (`sysctl vm.swapusage`);
  load one model at a time, unload it after (`keep_alive: 0`), never run model jobs unattended without the
  guards in `scripts/eval_models.py`. On 2026-10-22 an unguarded benchmark swapped this 18 GB Mac to a crawl
  for ~10 hours (a reasoning model at 16K context, 7.4 GB resident, with the owner's apps open).
- When the owner answers `QUESTIONS.md`: copy the answers into section 9 below (dated), update
  `REQUIREMENTS.md`, act on them, and rewrite `QUESTIONS.md` with only what is still open.

## 8. Open questions

See `QUESTIONS.md`. Answers go to section 9.

## 9. Owner answers (recorded)

**2026-10-14** (in chat):
- The agent runs on the host Mac and asks the owner for the permissions it needs. No container; Docker
  packaging removed.
- It must be secure enough to handle destructive operations through careful checks; beyond that there
  should be no limitation on what it can do.
- "Offline" means the owner's prompts and information must not be passed to third-party AI/agents. It does
  not mean no network.
- The agent should open the owner's default browser (desktop automation, not Playwright), open the Gmail
  account when asked to check and sort emails, and likewise any other accounts.
- A questions file for the owner is wanted; answers are to be recorded in the project plan (this section)
  and the questions file refreshed as needed.

**2026-10-22** (QUESTIONS.md, first round):
- A1 Browser: mostly Chrome, including incognito and other profiles; any installed browser may be driven through GUI control.
- A2 Page reading: owner leans to screenshots for exact layout; fine with DOM-first. Decision: both — DOM text for content, screenshot for layout and click targets.
- A3 Sites: any site, one-time confirmation per new domain, never-visit list.
- A4 Accounts: real browser and its sessions are fine given the guardrails (new-site confirm, data stays local, layered checks before destructive actions).
- A5 Mail: "sort" = labels, archive, mark read, trash. IMAP fine if it does not need Google approval (it does not; it needs an app password).
- B1 Approvals: maximum power, but a second watcher should be able to stop it and force consent before risky actions.
- B2 No-go: payments, password/security changes, posting publicly, deleting without Trash, messaging certain people; add more from research.
- B3 Files: whole home only. B4 Deletes: Trash only.
- C1 Apps: all GUI apps, with privacy guarded and no destructive action without telling the owner. Examples: message a person on Teams (ask which account if not found), find and install software (never download without showing the site first). Wants the knowledge base encrypted with an owner key asked for by a dialog.
- C2 Mechanism: AppleScript/Shortcuts, Accessibility, plus a reputable GUI-control library.
- D1 RAM: checked, 18 GB. D2 vision model: Claude's call.
- E1 icon, E2 full-screen behaviour: Claude's call. E3 shortcut: wants suggestions. E4 picker: confirmed. E5 approve-all: keep.
- F1 Voice: yes. F2 Memory: yes, with an option to create isolated chats, and incognito chats permanently deleted after inactivity. F3: unclear, re-asked.

**2026-10-23** (in chat): the benchmark of 2026-10-22 made the Mac unusable. Rule from the owner: check free
memory and similar before running anything. Recorded in section 7.

Partial benchmark results (`eval/results.jsonl`, this agent's own planner/reader prompts):
- `llama3` (8B): 2 of 8 planner cases produced a valid plan; as reader it marked a personal email deletable and
  was flipped by the injected email. Not suitable.
- `deepseek-r1:8b`: a reasoning model; 4 of 5 planner cases took minutes to hours while the Mac was swapping,
  1 timed out, 1 server error. Not suitable.
- `gpt-oss:20b` was never reached; at ~20 GB with headroom it does not fit this Mac at all.

**2026-10-23** benchmark (`scripts/eval_models.py`, guarded; `eval/results.jsonl`):

| Model | Memory while working | Valid plans | Reader (3 cases) | Time per answer |
|---|---|---|---|---|
| qwen3.5:4b (Ollama, think off) | ~3.5 GB | 7 of 8 | 2 of 3 (personal mail marked deletable; now blocked in code) | 4–18 s |
| Gemma 4 26B-A4B via TurboFieldfare | ~2.3 GB | 5 of 8 (2 broken JSON, 1 bad reference) | 3 of 3 | 45–80 s |
| llama3 8B | ~5 GB | 2 of 8 | 1 of 3, fooled by injection | 2–25 s |
| deepseek-r1:8b | 7.4 GB at 16K | unusable (reasoning model, swapped) | — | minutes–hours |
| qwen3.5:9b, gpt-oss:20b | ~7.5 GB / ~20 GB | not run: do not fit alongside daily apps | — | — |

No model refused the medical or YouTube requests. Ollama's Metal backend had been left broken by the 2026-10-22
crash; restarting Ollama.app fixed it.

**2026-10-24** (QUESTIONS.md, second round):
- G1: stop and ask the owner on surprises; only the owner's reply may lead to a new plan. Agreed.
- G2: watcher model agreed, slowness accepted. Add an on/off switch for it (e.g. on when away from the screen)
  and an emergency stop button; the watcher uses the same stop.

**2026-10-24** (voice notes, third round):
- I2 Gmail: any account. Preferred: open the owner's browser, open Gmail and do the work there, visibly. App
  password via IMAP is acceptable later if better; the agent would then ask for account details at run time.
  Live Gmail test deferred.
- J3 Messages: every outgoing message asks first. Security can be loosened in later updates.
- K1 Sensitive sites: phase 1 block list is fine. Later, asking before sensitive acts is acceptable since no
  data leaves the Mac.
- Browser: asked whether a Chrome extension (as Claude in Chrome uses) would be better than Apple Events.

**2026-11-05** (chat): owner asked for a standby button usable any time, and automatic standby when memory use
reaches 90–95%. Built: `POST /standby` (also the Pause/Standby buttons), a watchdog checking every 5 s
(`policy.yaml → memory`: 92% used or < 0.5 GB swap free), models unloaded on standby, manual resume (a resume
while memory is still full goes straight back to standby). 101 tests pass.

**2026-11-08** (chat): "complete the tasks and finish it; plan to add connectors later". Done this round: look-then-ask
tested live, Mac app (bubble, modal, full app, menu bar, hotkeys, notifications, login item), watcher with on/off,
encrypted memory, isolated/incognito chats, voice input, careful-model swap check, Ollama auto-restart. Connectors
planned in section 5 C2 and `REQUIREMENTS.md` section 9.

