# Questions for the owner

How this works: write your answer under each question, replacing the `Answer:` line (a sentence is
enough; "yes", "no" or "your call" are fine). The next Claude session copies answers into
`HANDOFF.md` section 9 ("Owner answers"), acts on them, and replaces this file with whatever it needs
to ask next. Answers so far are recorded there, and requirements in `REQUIREMENTS.md`.

Last refreshed: 2026-10-24 (after MVP round 1). Open questions: 6.

**Nothing blocks the next step. Answer these whenever convenient.**

## H. Encrypted knowledge base

### H1. Passphrase or macOS Keychain + Touch ID?
Why: a passphrase you type is what you described. Touch ID unlock is more convenient, and the key
still never sits on disk unprotected. Neither stops malware that already runs as you while the agent
is unlocked; both stop it from reading the files at rest.
Recommendation: Touch ID with a passphrase fallback; auto-lock after 15 minutes idle.
Answer:

### H2. Incognito chats: how long before permanent deletion? And should isolated chats be the default?
Recommendation: delete incognito chats after 1 hour idle; normal chats use shared memory by default.
Answer:

## I. Setup steps that need your OK

### I3. Keyboard shortcut to show and hide the modal?
You have Raycast installed, which usually takes ⌥Space, and Spotlight takes ⌘Space. Free options:
  (a) ⌃⌥Space  (b) ⌥⌘A  (c) ⌃⇧Space  (d) a double-tap of Right ⌘
Recommendation: (a) ⌃⌥Space.
Answer:

## J. Clarifications

### J1. (Re-asked F3) Saved actions: reusable tasks you can run again with one click.
Example: once you have reviewed and run "sort my marketing mail", you could press "Save as action", and
it appears in the picker as "Sort marketing mail" for next time, without the planner writing it again.
Do you want that button, or are saved actions not useful to you?
Answer:

### J2. Software installs: what counts as a trusted source?
Proposal: the agent may only download from the vendor's own site or the Mac App Store, shows you the
page first, checks that the app is signed and notarized by Apple before installing, and never installs
anything that fails the check. Homebrew counts as trusted too if you use it.
Answer:

## K. New

### K2. One-time Chrome setting, when you are ready to try the browser
In Chrome: View → Developer → "Allow JavaScript from Apple Events". Without it the agent can open pages but
not read them. macOS will also ask once to let the agent control Chrome.
Answer (done / not yet):
