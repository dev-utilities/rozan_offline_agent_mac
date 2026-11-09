# Requirements

The owner's requirements, kept current. Status: **done** · **partial** · **todo** · **open** (waiting on an
answer in `QUESTIONS.md`). Last updated: 2026-11-08.

## 1. Privacy
- Prompts, emails, pages and files go only to local models on this Mac; never to a third-party AI. — done
- The agent may use online services on the owner's behalf (Gmail, websites); each is listed in settings. — done
- Memory/knowledge base encrypted with an owner key; asked for when needed; auto-locks. — done (passphrase, AES-GCM,
  15 min auto-lock; Touch ID unlock todo)
- Chats can be isolated from shared memory; incognito chats are permanently deleted after inactivity. — done
  (incognito: never saved, deleted after 60 min idle)

## 2. Platform
- Runs on the owner's Mac, as the owner; no container. — done
- Asks for each macOS permission only when first needed, with a plain explanation; status shown in settings. — partial
- Never crashes or swaps the Mac: check free memory before loading any model; one model in memory at a time;
  if memory is short, say so instead of running. — done
- Standby button: stops the task, unloads models, pauses. Auto-standby when memory use reaches 92% or swap is
  almost full (`policy.yaml → memory`); the owner resumes by hand. — done

## 3. Models
- Fast model for everyday work: `qwen3.5:4b` (~3.5 GB while working, unloaded when idle). — done
- Careful model for risky or open-ended work: Gemma 4 26B via TurboFieldfare (~2.3 GB), started on demand. — done
- Code, not a model, decides when to use the careful model. — done
- Official model weights only; no community "uncensored" weights. — done
- Few refusals for legitimate personal requests (medical questions, downloads). — done (none refused in tests)

## 4. Safety
- Injected text in emails, pages or files can never cause an action (planner never sees it; policy enforces). — done
- Each task gets a scope from the owner's words only; any action outside it is refused. — done
- Approval card before every irreversible action; "approve all <tool> this run" offered. — done
- Every outgoing message asks each time, showing recipient and text; no "approve all" (may loosen later). — todo
- Hard no-go list: payments, password/2FA/security changes, public posts, permanent deletes, messaging blocked
  people, sharing outside the account, mail forwarding/filter changes, granting app access, system settings or
  login items, disabling security software, running downloaded code. — todo
- Deletes go to Trash only; overwritten files are backed up first. — done
- Downloads/installs never automatic: show the source site, check signature and notarization, then ask. — todo (J2 open)
- Watcher: a second local model reviews each proposed action and can only add a consent prompt or stop. — done
- Watcher on/off switch (e.g. on when the owner is away). — done (menu bar, Settings)
- Emergency stop button that halts the agent and pauses it; the watcher will use the same stop. — done (a running tool finishes its current step first)
- When something unexpected happens on screen, stop and ask the owner; only the owner's reply may lead to a
  new plan. Also needed for unfamiliar pages, where the planner cannot know control labels. — partial
  (look-then-ask built and tested live in an isolated Chromium)

## 5. Logs
- Every action logged by code, never by a model; tamper-evident (hash chain). — done
- No model-written comments; code captures evidence (element label, app, window, screenshot crop). — todo
- Daily review by a memory-less model that only flags entries; it cannot delete logs; fixed retention. — todo
- Weekly report of fallback tool use to decide which narrow tools to add next. — todo

## 6. Tools
- Many narrow, named tools; `run_shell` and raw clicks are last resorts. — partial (shell limited to read-only programs)
- MVP: Gmail list/read/label/archive/mark-read/trash/send, files read/write/move-to-Trash, open app,
  real-browser open/read. — done (live Gmail untested: I2 open)
- Mail work in the owner's visible browser (Gmail web) by default; IMAP with an app password asked for at
  run time is an optional faster engine for bulk jobs. — partial (IMAP tools built; Gmail-in-browser todo)
- Real browser: Chrome first (profiles, incognito), any installed browser via GUI control; page text for content
  plus screenshot for layout. — partial (Chrome extension built: open, read, click/type by label, screenshot;
  tested in an isolated Chromium; multi-browser by instance id; Firefox/Safari todo)
- Any website allowed, with a one-time confirmation per new domain and a never-visit list. — done
- All GUI apps (Teams, Slack, WhatsApp, Finder…): AppleScript/Shortcuts first, then Accessibility, then mouse
  and keyboard. Example: message a person on Teams, asking which account if not found. — partial (desktop.* tools
  through Accessibility: list apps, read window, controls, press/type/keys with approval; never-control list)
- Keyboards / input languages (e.g. add Urdu). — done (system.keyboards, add_keyboard, remove_keyboard)
- The agent installs its own browser extension. — done (setup.chrome_extension, verified 2026-11-09)
- Voice input. — done (⌃⌥D, Apple on-device recognition only)

## 7. Interface
- Floating bubble: 48–56 pt circle, always on top, on every Space, draggable, position per display, clamped on
  screen. — done (macos/, scripts/build_mac_app.sh)
- Bubble states: idle, thinking ring, needs-approval pulse (never silent), error red dot, paused. — done (+ notification
  when an approval waits and the modal is closed)
- Click opens the modal; click again or Esc closes; right-click menu: New chat, Open full app, Pause, Quit;
  no Dock icon; menu bar fallback. — done (+ Open at Login)
- Modal next to the bubble, flipping at screen edges: conversation, input, plan card, inline approvals,
  untrusted output styled differently, New chat, Open full app. — done
- Plain questions get a direct reply; read-only plans run at once; risky plans show each step and its exact
  command before Run; risky steps ask again while running. — done (2026-11-09)
- Follow-up questions understand the earlier chat. — done (owner words + planner replies only)
- Hover on the bubble: mic (dictate) and × (quit with confirmation). — done
- Modern look: icons from a free library, ⋯ menu, chat bubbles. — done (Lucide, ISC)
- Picker: previous chats, saved actions, audit log, pause. — done
- Full app window: chat history sidebar, conversation, audit log viewer, settings (folders, services, models,
  permissions). — done (web UI)
- Plan card shows what the task may do, which model planned it, and any warning. — done
- Keyboard shortcut to toggle the modal. — done (⌃⌥Space; no Accessibility permission needed)
- Bubble icon/colour: Lucide sparkle on an indigo→violet gradient; red while listening; grey on standby. — done

## 8. Data access
- Whole home folder, read and write; deny list for keys, keychains, `.env`, the agent's own files; nothing
  outside home. — done
- Chat history stored locally under `~/.personal_agent/conversations/`. — done
- Saved actions: reusable tasks in the picker. — partial (J1 open: "save as action" button)

## 9. Connectors (planned)
- Connectors to other services (calendar, Outlook/Teams, Slack, Notion, GitHub, …) as local MCP servers or narrow
  native tools. — todo
- Each connector tool must be listed in `policy.yaml` (unlisted = refused); reads are untrusted (`connector.<name>`
  source), writes need approval and count as high-risk. — todo
- Credentials only in the macOS Keychain, fetched at call time; never in a prompt. — todo
- Data from connectors goes only to the local models; a cloud-hosted connector is listed in settings like Gmail. — todo
- Each connector adds scope capabilities (`<name>.read`, `<name>.write`) so a task only gets what it asked for. — todo
