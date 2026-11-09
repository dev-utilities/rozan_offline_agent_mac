import AppKit
import ApplicationServices
import Carbon.HIToolbox
import ServiceManagement
import UserNotifications

@main
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)          // Dock icon + app menu (owner's choice, 2026-11-09)
        app.run()
    }

    private var core: Core!
    private let windows = Windows()
    private var panel: BubblePanel!
    private var bubble: BubbleView!
    private var statusItem: NSStatusItem!
    private var pollTask: Task<Void, Never>?
    private var lastState: AgentState = .offline
    private var notifiedApproval = false
    private var paused = false
    private var watcherOn = false
    private var hotKey: EventHotKeyRef?
    private var voiceKey: EventHotKeyRef?
    private let voice = Voice()
    private let size: CGFloat = 52

    // Clicking the Dock icon opens the full app.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        windows.openFullApp()
        return true
    }

    /// Standard menus: the Edit menu is what makes ⌘C / ⌘V / ⌘A work in the chat box.
    private func makeMainMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            holder.submenu = m
            main.addItem(holder)
        }
        func item(_ t: String, _ a: Selector?, _ k: String = "", _ mods: NSEvent.ModifierFlags = [.command], target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        sub("Rozan", [
            item("About Rozan", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Open Full App", #selector(menuFullApp), "o", target: self),
            item("New Chat", #selector(menuNewChat), "n", target: self),
            item("Setup & Requirements…", #selector(menuSetup), "", target: self),
            .separator(),
            item("Hide Rozan", #selector(NSApplication.hide(_:)), "h"),
            item("Quit Rozan", #selector(menuQuit), "q", target: self),
        ])
        sub("Edit", [
            item("Undo", Selector(("undo:")), "z"), item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"), item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"), item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        sub("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        makeMainMenu()
        core = Core(projectRoot: Paths.root)
        core.onReady = { [weak self] s in self?.coreReady(s) }
        core.onDown = { [weak self] msg in self?.coreDown(msg) }
        windows.onModalShown = { [weak self] in
            Task { _ = await self?.core.request("POST", "/state/ack") }   // opening the modal clears the error dot
            self?.notifiedApproval = false
        }
        makeBubble()
        makeStatusItem()
        voice.onState = { [weak self] on in
            guard let self else { return }
            self.bubble.listening = on
            self.hoverBar.mic.setIcon(on ? "square" : "mic", fallback: on ? "stop.fill" : "mic",
                                      label: on ? "Stop dictation" : "Dictate (⌃⌥D)")
            if on {
                if self.windows.dictatingInModal { self.windows.showModal(near: self.panel.frame) }
                self.windows.setDictation("", final: false)
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.windows.dictationTarget = nil }
            }
        }
        windows.onDictate = { [weak self] in self?.voice.toggle() }
        voice.onPartial = { [weak self] text in self?.windows.setDictation(text, final: false) }
        voice.onText = { [weak self] text in self?.windows.setDictation(text, final: true) }
        voice.onError = { [weak self] msg in shellLog("voice error: \(msg)"); self?.showAlert(msg) }
        registerHotKey()
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
                                               name: NSApplication.didChangeScreenParametersNotification, object: nil)
        // Local automation hooks (Raycast, Shortcuts, scripts): show/hide the modal or open the full app.
        // They only change what is on screen; they cannot run anything.
        let dnc = DistributedNotificationCenter.default()
        dnc.addObserver(self, selector: #selector(remoteToggle), name: .init("com.personal-agent.toggle"), object: nil)
        dnc.addObserver(self, selector: #selector(remoteFullApp), name: .init("com.personal-agent.fullapp"), object: nil)
        dnc.addObserver(self, selector: #selector(remoteDictate), name: .init("com.personal-agent.dictate"), object: nil)
        // First launch (or something went missing): the Setup window checks and installs what Rozan needs.
        Task {
            if await setup.requirementsMet() { core.start() } else { showSetup() }
        }
    }

    private let setup = SetupWindow()
    private var coreStarted = false
    private func showSetup() {
        setup.show { [weak self] in
            guard let self else { return }
            if self.core.session == nil { self.core.start() }
        }
    }
    @objc private func menuSetup() { showSetup() }

    func applicationWillTerminate(_ note: Notification) {
        pollTask?.cancel()
        core.stop()
    }

    // MARK: core lifecycle
    private func coreReady(_ s: Core.Session) {
        windows.reset()
        windows.session = s
        startPolling()
    }

    private func coreDown(_ msg: String) {
        setState(.offline, pending: 0)
        bubble.toolTip = "Rozan — \(msg)"
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.poll()
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    private func poll() async {
        guard let status = await core.request("GET", "/status") else { setState(.offline, pending: 0); return }
        let state = AgentState(rawValue: status["state"] as? String ?? "") ?? .idle
        paused = status["paused"] as? Bool ?? false
        watcherOn = status["watcher"] as? Bool ?? false
        setState(state, pending: status["pending_approvals"] as? Int ?? 0)
        // Auto-standby: say why once, with the apps that were using the most memory at that moment.
        if paused, let info = status["standby_info"] as? [String: Any], let ts = info["ts"] as? Double, ts > shownStandbyTs {
            shownStandbyTs = ts
            DispatchQueue.main.async { [weak self] in self?.explainStandby(info) }
        }
        // Permissions are asked for lazily: the first time a desktop step needed Accessibility.
        if status["permission_needed"] as? String == "accessibility" && !askedAccessibility && !AXIsProcessTrusted() {
            askedAccessibility = true
            let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(opts)     // macOS shows its own prompt that opens the right pane
        }
    }
    private var askedAccessibility = false
    private var shownStandbyTs: Double = Date().timeIntervalSince1970 - 5   // not for a standby from before launch

    private func explainStandby(_ info: [String: Any]) {
        let apps = (info["apps"] as? [[String: Any]]) ?? []
        let lines = apps.map { a in
            String(format: "•  %@ — %.1f GB", (a["name"] as? String) ?? "?", (a["memory_gb"] as? Double) ?? 0)
        }
        let a = NSAlert()
        a.icon = NSApp.applicationIconImage
        let pct = (info["used_percent"] as? Double).map { String(format: " (memory %.0f%% used)", $0) } ?? ""
        a.messageText = "Rozan went on standby\(pct)"
        a.informativeText = "\((info["reason"] as? String) ?? "Memory is nearly full").\n\n"
            + (lines.isEmpty ? "" : "Using the most memory just before standby:\n" + lines.joined(separator: "\n") + "\n\n")
            + "Quit an app you don't need, then resume. Nothing is planned or run until you do."
        a.addButton(withTitle: "Resume")
        a.addButton(withTitle: "Stay on Standby")
        a.addButton(withTitle: "Open Activity Monitor")
        NSApp.activate()
        switch a.runModal() {
        case .alertFirstButtonReturn: Task { _ = await core.request("POST", "/resume"); await poll() }
        case .alertThirdButtonReturn: NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/Utilities/Activity Monitor.app"))
        default: break
        }
    }

    private func setState(_ state: AgentState, pending: Int) {
        bubble.state = state
        bubble.pendingCount = pending
        statusItem.button?.image = NSImage(systemSymbolName: state == .needsApproval ? "sparkle.magnifyingglass" : "sparkle",
                                           accessibilityDescription: "Rozan")
        if state == .needsApproval && !windows.modalVisible && !notifiedApproval {
            notifiedApproval = true
            notifyApproval()
        }
        if state != .needsApproval { notifiedApproval = false }
        lastState = state
    }

    // MARK: bubble
    private func makeBubble() {
        panel = BubblePanel(contentRect: NSRect(x: 0, y: 0, width: size, height: size),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.hidesOnDeactivate = false
        bubble = BubbleView(frame: NSRect(x: 0, y: 0, width: size, height: size))
        bubble.onClick = { [weak self] in self?.toggleModal() }
        bubble.onRightClick = { [weak self] event in
            guard let self else { return }
            NSMenu.popUpContextMenu(self.makeMenu(), with: event, for: self.bubble)
        }
        bubble.onMoved = { [weak self] origin in self?.savePosition(origin) }
        bubble.onHover = { [weak self] inside in self?.hover(bubble: inside) }
        bubble.onDragStart = { [weak self] in self?.hoverBar.orderOut(nil) }
        hoverBar.onHover = { [weak self] inside in self?.hover(bar: inside) }
        hoverBar.mic.onClick = { [weak self] in self?.windows.dictationTarget = nil; self?.voice.toggle() }
        hoverBar.quit.onClick = { [weak self] in self?.confirmQuit() }
        panel.contentView = bubble
        panel.setFrameOrigin(restorePosition())
        panel.orderFrontRegardless()
    }

    private func toggleModal() { windows.toggleModal(near: panel.frame) }

    // MARK: hover bar: mic + quit, shown while the pointer is over the bubble or the bar
    private lazy var hoverBar = HoverBar()
    private var overBubble = false, overBar = false

    private func hover(bubble inside: Bool? = nil, bar: Bool? = nil) {
        if let inside { overBubble = inside }
        if let bar { overBar = bar }
        if overBubble || overBar {
            hoverBar.place(near: panel.frame)
            hoverBar.orderFrontRegardless()
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in
                guard let self, !self.overBubble, !self.overBar else { return }
                self.hoverBar.orderOut(nil)
            }
        }
    }

    private func confirmQuit() {
        hoverBar.orderOut(nil)
        let a = NSAlert()
        a.messageText = "Quit Rozan?"
        a.informativeText = "The bubble and the agent stop. Nothing runs until you open the app again."
        a.addButton(withTitle: "Quit")
        a.addButton(withTitle: "Cancel")
        a.buttons.first?.hasDestructiveAction = true
        NSApp.activate()
        if a.runModal() == .alertFirstButtonReturn { NSApp.terminate(nil) }
    }

    // Position is remembered per display and clamped back on screen when displays change.
    private func displayKey(for screen: NSScreen?) -> String {
        let id = screen?.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber
        return "bubble.origin.\(id?.stringValue ?? "main")"
    }

    private func screenFor(_ origin: NSPoint) -> NSScreen? {
        let center = NSPoint(x: origin.x + size / 2, y: origin.y + size / 2)
        return NSScreen.screens.first { $0.frame.contains(center) } ?? NSScreen.main
    }

    private func savePosition(_ origin: NSPoint) {
        let p = clamped(origin)                     // clamp first, so it is saved for a real display
        panel.setFrameOrigin(p)
        let key = displayKey(for: screenFor(p))
        UserDefaults.standard.set([Double(p.x), Double(p.y)], forKey: key)
        UserDefaults.standard.set(key, forKey: "bubble.lastDisplay")
    }

    private func restorePosition() -> NSPoint {
        let saved = { (key: String) -> NSPoint? in
            guard let xy = (UserDefaults.standard.array(forKey: key) as? [NSNumber])?.map({ CGFloat($0.doubleValue) }),
                  xy.count == 2 else { return nil }
            return NSPoint(x: xy[0], y: xy[1])
        }
        // the display it was last on, if connected; else any connected display it was ever placed on
        let keys = NSScreen.screens.map { displayKey(for: $0) }
        if let last = UserDefaults.standard.string(forKey: "bubble.lastDisplay"), keys.contains(last), let p = saved(last) {
            return clamped(p)
        }
        for k in keys { if let p = saved(k) { return clamped(p) } }
        let f = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
        return NSPoint(x: f.maxX - size - 24, y: f.minY + 120)
    }

    private func clamped(_ p: NSPoint) -> NSPoint {
        let center = NSPoint(x: p.x + size / 2, y: p.y + size / 2)
        let f = (NSScreen.screens.first { $0.frame.contains(center) } ?? NSScreen.main ?? NSScreen.screens[0]).visibleFrame
        return NSPoint(x: min(max(p.x, f.minX), f.maxX - size), y: min(max(p.y, f.minY), f.maxY - size))
    }

    @objc private func screensChanged() { panel.setFrameOrigin(clamped(restorePosition())) }

    // MARK: menus (bubble right-click and menu bar share one menu)
    private func makeStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "sparkle", accessibilityDescription: "Rozan")
        statusItem.button?.image?.isTemplate = true
        statusItem.menu = makeMenu()
        statusItem.menu?.delegate = self
    }

    private func makeMenu() -> NSMenu {
        let m = NSMenu()
        m.addItem(withTitle: windows.modalVisible ? "Hide Rozan" : "Show Rozan",
                  action: #selector(menuToggle), keyEquivalent: "").target = self
        m.addItem(withTitle: "New Chat", action: #selector(menuNewChat), keyEquivalent: "").target = self
        m.addItem(withTitle: "Open Full App", action: #selector(menuFullApp), keyEquivalent: "").target = self
        m.addItem(.separator())
        m.addItem(withTitle: paused ? "Resume Agent" : "Standby (pause agent)", action: #selector(menuPause),
                  keyEquivalent: "").target = self
        m.addItem(withTitle: voice.listening ? "Stop Dictation" : "Dictate (⌃⌥D)", action: #selector(menuDictate),
                  keyEquivalent: "").target = self
        let watch = m.addItem(withTitle: "Watcher (extra check on each action)", action: #selector(menuWatcher), keyEquivalent: "")
        watch.target = self
        watch.state = watcherOn ? .on : .off
        m.addItem(withTitle: "Emergency Stop", action: #selector(menuStop), keyEquivalent: "").target = self
        m.addItem(.separator())
        let login = m.addItem(withTitle: "Open at Login", action: #selector(menuLogin), keyEquivalent: "")
        login.target = self
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        m.addItem(withTitle: "Quit Rozan", action: #selector(menuQuit), keyEquivalent: "q").target = self
        return m
    }

    @objc private func menuToggle() { toggleModal() }
    @objc private func remoteToggle() { toggleModal() }
    @objc private func remoteFullApp() { windows.openFullApp() }
    @objc private func remoteDictate() { windows.dictationTarget = nil; voice.toggle() }
    @objc private func menuFullApp() { windows.openFullApp() }
    @objc private func menuNewChat() {
        Task {
            _ = await core.request("POST", "/conversations")
            windows.reloadModal()
            windows.showModal(near: panel.frame)
        }
    }
    @objc private func menuPause() {
        Task { _ = await core.request("POST", paused ? "/resume" : "/standby"); await poll() }
    }
    @objc private func menuDictate() { voice.toggle() }

    private func showAlert(_ msg: String) {
        let a = NSAlert()
        a.messageText = "Rozan"
        a.informativeText = msg
        NSApp.activate()
        a.runModal()
    }

    @objc private func menuWatcher() {
        Task { _ = await core.request("POST", "/watcher", body: ["enabled": !watcherOn]); await poll() }
    }
    @objc private func menuStop() { Task { _ = await core.request("POST", "/stop"); await poll() } }
    @objc private func menuLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else { try SMAppService.mainApp.register() }
        } catch {
            showAlert("Could not change Open at Login: \(error.localizedDescription). "
                      + "Install the app first: scripts/build_mac_app.sh --install")
        }
    }
    @objc private func menuQuit() { NSApp.terminate(nil) }

    // MARK: approvals never wait silently: a notification when the modal is closed
    private func notifyApproval() {
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            let send = {
                let content = UNMutableNotificationContent()
                content.title = "Rozan needs your approval"
                content.body = "Click the bubble to review the waiting action."
                center.add(UNNotificationRequest(identifier: "approval", content: content, trigger: nil))
            }
            switch settings.authorizationStatus {
            case .notDetermined:
                // Asked only the first time it is needed, and the reason is in the notification itself.
                center.requestAuthorization(options: [.alert, .sound]) { ok, _ in
                    Task { @MainActor in
                        _ = await self.core.request("POST", "/permissions",
                                                    body: ["name": "notifications", "status": ok ? "granted" : "denied"])
                    }
                    if ok { send() }
                }
            case .authorized, .provisional:
                send()
            default:
                break
            }
        }
    }

    // MARK: ⌃⌥Space shows and hides the modal (Carbon hot key: no Accessibility permission needed)
    private func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, ctx in
            guard let ctx, let event else { return noErr }
            var hk = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hk)
            let me = Unmanaged<AppDelegate>.fromOpaque(ctx).takeUnretainedValue()
            let which = hk.id
            shellLog("hotkey \(which)")
            Task { @MainActor in if which == 2 { me.voice.toggle() } else { me.toggleModal() } }
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil)
        let sig = OSType(0x50414754)   // 'PAGT'
        RegisterEventHotKey(UInt32(kVK_Space), UInt32(controlKey | optionKey), EventHotKeyID(signature: sig, id: 1),
                            GetApplicationEventTarget(), 0, &hotKey)
        RegisterEventHotKey(UInt32(kVK_ANSI_D), UInt32(controlKey | optionKey), EventHotKeyID(signature: sig, id: 2),
                            GetApplicationEventTarget(), 0, &voiceKey)
    }
}

extension AppDelegate: NSMenuDelegate {
    nonisolated func menuNeedsUpdate(_ menu: NSMenu) {
        MainActor.assumeIsolated {
            menu.removeAllItems()
            for item in makeMenu().items { item.menu?.removeItem(item); menu.addItem(item) }
        }
    }
}
