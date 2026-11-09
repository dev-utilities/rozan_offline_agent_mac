// Desktop control through the macOS Accessibility API (no pixel clicking).
//   desktop apps                                 running apps with windows, as JSON
//   desktop controls <app>                       buttons, fields, menus... of the app's front window, as JSON
//   desktop read <app>                           visible text of the app's front window, as JSON
//   desktop press <app> <label>                  press the one control with that label
//   desktop type <app> <label> <text>            put text into the one field with that label ("" = focused field)
//   desktop key <app> <combo>                    e.g. "return", "cmd+f", "cmd+shift+n"
// <app> is an app name or bundle id. Runs as a child of the agent app, so it uses that app's
// Accessibility permission. Exit 3 = permission missing.
import AppKit
import ApplicationServices

func fail(_ msg: String, _ code: Int32 = 1) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!); exit(code)
}
func emit(_ obj: Any) {
    print(String(data: try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]), encoding: .utf8)!)
}
func attr(_ e: AXUIElement, _ a: String) -> AnyObject? {
    var v: AnyObject?; return AXUIElementCopyAttributeValue(e, a as CFString, &v) == .success ? v : nil
}
func str(_ e: AXUIElement, _ a: String) -> String {
    (attr(e, a) as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

guard CommandLine.arguments.dropFirst().first == "apps" || AXIsProcessTrusted() else {
    fail("Rozan needs Accessibility permission: System Settings → Privacy & Security → Accessibility → turn on Rozan.", 3)
}

let args = Array(CommandLine.arguments.dropFirst())
let apps = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }

func findApp(_ q: String) -> NSRunningApplication {
    let l = q.lowercased()
    if let a = apps.first(where: { $0.bundleIdentifier?.lowercased() == l || $0.localizedName?.lowercased() == l }) { return a }
    let hits = apps.filter { ($0.localizedName ?? "").lowercased().contains(l) }
    if hits.count == 1 { return hits[0] }
    fail(hits.isEmpty ? "no running app called \(q)" : "several apps match \(q): " + hits.compactMap(\.localizedName).joined(separator: ", "))
}

/// Chromium-based apps (Chrome, Teams, Slack, VS Code...) only build the accessibility tree of their web
/// content when asked; without this their pages look empty.
func wakeAccessibility(_ app: NSRunningApplication) {
    let ax = AXUIElementCreateApplication(app.processIdentifier)
    var cur: AnyObject?
    if AXUIElementCopyAttributeValue(ax, "AXManualAccessibility" as CFString, &cur) == .success, (cur as? Bool) == true { return }
    // Chromium/Electron honour AXManualAccessibility; Chrome itself listens for AXEnhancedUserInterface
    // (what VoiceOver sets). Either can report an error and still switch the tree on, so set both.
    AXUIElementSetAttributeValue(ax, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    AXUIElementSetAttributeValue(ax, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
    usleep(1_000_000)                      // let the app build the tree
}

func frontWindow(_ app: NSRunningApplication) -> AXUIElement {
    wakeAccessibility(app)
    let ax = AXUIElementCreateApplication(app.processIdentifier)
    if let w = attr(ax, kAXFocusedWindowAttribute) { return w as! AXUIElement }
    if let ws = attr(ax, kAXWindowsAttribute) as? [AXUIElement], let w = ws.first { return w }
    fail("\(app.localizedName ?? "the app") has no open window")
}

let ACTIONABLE: Set<String> = ["AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXTextField",
                               "AXTextArea", "AXComboBox", "AXSearchField", "AXLink", "AXMenuItem", "AXTab", "AXCell", "AXRow", "AXSlider"]
let KIND: [String: String] = ["AXButton": "button", "AXCheckBox": "checkbox", "AXRadioButton": "radio", "AXPopUpButton": "menu",
                              "AXMenuButton": "menu", "AXTextField": "field", "AXTextArea": "field", "AXComboBox": "field",
                              "AXSearchField": "field", "AXLink": "link", "AXMenuItem": "menu item", "AXTab": "tab",
                              "AXCell": "item", "AXRow": "item", "AXSlider": "slider"]

struct Control { let el: AXUIElement; let kind: String; let label: String }

func label(_ e: AXUIElement) -> String {
    for a in [kAXTitleAttribute, kAXDescriptionAttribute, "AXPlaceholderValue", kAXHelpAttribute] {
        let s = str(e, a); if !s.isEmpty { return String(s.prefix(80)) }
    }
    if let t = attr(e, kAXTitleUIElementAttribute) { let s = str(t as! AXUIElement, kAXValueAttribute); if !s.isEmpty { return String(s.prefix(80)) } }
    // rows/cells: their first static text
    if let kids = attr(e, kAXChildrenAttribute) as? [AXUIElement] {
        for k in kids.prefix(6) where str(k, kAXRoleAttribute) == "AXStaticText" {
            let s = str(k, kAXValueAttribute); if !s.isEmpty { return String(s.prefix(80)) }
        }
    }
    return ""
}

func walk(_ e: AXUIElement, depth: Int, into out: inout [Control], texts: inout [String]) {
    if depth > 30 || out.count > 400 { return }
    let role = str(e, kAXRoleAttribute)
    if ACTIONABLE.contains(role) {
        let l = label(e)
        if !l.isEmpty { out.append(Control(el: e, kind: KIND[role] ?? role, label: l)) }
    } else if role == "AXStaticText" {
        let s = str(e, kAXValueAttribute); if !s.isEmpty && texts.count < 300 { texts.append(String(s.prefix(300))) }
    }
    for k in (attr(e, kAXChildrenAttribute) as? [AXUIElement]) ?? [] { walk(k, depth: depth + 1, into: &out, texts: &texts) }
}

func controls(_ app: NSRunningApplication) -> ([Control], [String]) {
    var out: [Control] = [], texts: [String] = []
    walk(frontWindow(app), depth: 0, into: &out, texts: &texts)
    return (out, texts)
}

func one(_ app: NSRunningApplication, _ wanted: String, kinds: Set<String>? = nil) -> Control {
    let (all, _) = controls(app)
    let pool = all.filter { kinds == nil || kinds!.contains($0.kind) }
    let exact = pool.filter { $0.label == wanted }
    if exact.count == 1 { return exact[0] }
    if exact.count > 1 { fail("\(exact.count) controls are labelled \"\(wanted)\"; not guessing which one") }
    let ci = pool.filter { $0.label.lowercased() == wanted.lowercased() }
    if ci.count == 1 { return ci[0] }
    fail("no single control labelled \"\(wanted)\" in \(app.localizedName ?? "the app")'s window")
}

func keyCode(_ k: String) -> CGKeyCode? {
    let map: [String: CGKeyCode] = ["return": 36, "enter": 76, "tab": 48, "space": 49, "delete": 51, "escape": 53, "esc": 53,
        "left": 123, "right": 124, "down": 125, "up": 126, "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
        "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
        "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40,
        "n": 45, "m": 46, ",": 43, ".": 47, "/": 44]
    return map[k]
}

func activate(_ app: NSRunningApplication) {
    app.activate()
    for _ in 0..<20 where NSWorkspace.shared.frontmostApplication?.processIdentifier != app.processIdentifier { usleep(50_000) }
    if NSWorkspace.shared.frontmostApplication?.processIdentifier != app.processIdentifier {
        fail("could not bring \(app.localizedName ?? "the app") to the front; nothing was typed")
    }
}

switch (args.first ?? "", args.count) {
case ("apps", 1):
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier
    emit(apps.map { ["name": $0.localizedName ?? "", "bundle_id": $0.bundleIdentifier ?? "", "frontmost": $0.processIdentifier == front] })
case ("controls", 2):
    let app = findApp(args[1])
    let (cs, _) = controls(app)
    emit(["app": app.localizedName ?? "", "window": str(frontWindow(app), kAXTitleAttribute),
          "controls": cs.prefix(150).map { ["kind": $0.kind, "label": $0.label] }])
case ("read", 2):
    let app = findApp(args[1])
    let (_, texts) = controls(app)
    emit(["app": app.localizedName ?? "", "window": str(frontWindow(app), kAXTitleAttribute), "text": texts.joined(separator: "\n")])
case ("press", 3):
    let app = findApp(args[1])
    let c = one(app, args[2])
    let r = AXUIElementPerformAction(c.el, kAXPressAction as CFString)
    if r != .success { fail("pressing \"\(c.label)\" failed (\(r.rawValue))") }
    emit(["app": app.localizedName ?? "", "pressed": c.label, "kind": c.kind])
case ("type", 4):
    let app = findApp(args[1])
    let target: AXUIElement
    if args[2].isEmpty {
        guard let f = attr(AXUIElementCreateApplication(app.processIdentifier), kAXFocusedUIElementAttribute) else { fail("no focused field") }
        target = f as! AXUIElement
    } else {
        target = one(app, args[2], kinds: ["field"]).el
    }
    AXUIElementSetAttributeValue(target, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    let r = AXUIElementSetAttributeValue(target, kAXValueAttribute as CFString, args[3] as CFString)
    if r != .success { fail("could not type into that field (\(r.rawValue))") }
    emit(["app": app.localizedName ?? "", "typed_into": args[2].isEmpty ? "focused field" : args[2], "characters": args[3].count])
case ("key", 3):
    let app = findApp(args[1])
    let parts = args[2].lowercased().split(separator: "+").map(String.init)
    guard let last = parts.last, let code = keyCode(last) else { fail("unknown key \(args[2])") }
    var flags: CGEventFlags = []
    for m in parts.dropLast() {
        switch m { case "cmd", "command": flags.insert(.maskCommand); case "shift": flags.insert(.maskShift)
        case "alt", "option", "opt": flags.insert(.maskAlternate); case "ctrl", "control": flags.insert(.maskControl)
        default: fail("unknown modifier \(m)") }
    }
    activate(app)                                 // keys go only to the app that was asked for
    for down in [true, false] {
        let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down); e?.flags = flags
        e?.postToPid(app.processIdentifier)
    }
    emit(["app": app.localizedName ?? "", "pressed_keys": args[2]])
case ("chrome-load-unpacked", 2):
    chromeLoadUnpacked(args[1])
default:
    fail("usage: desktop apps | controls <app> | read <app> | press <app> <label> | type <app> <label> <text> | key <app> <combo>", 64)
}


// MARK: install the agent's own extension the way a person would (Developer mode → Load unpacked → folder)
func waitFor<T>(_ seconds: Double, _ probe: () -> T?) -> T? {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end { if let v = probe() { return v }; usleep(250_000) }
    return nil
}

func typeString(_ text: String, to pid: pid_t) {
    for ch in text.utf16 {
        for down in [true, false] {
            let e = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down)
            var c = ch; e?.keyboardSetUnicodeString(stringLength: 1, unicodeString: &c)
            e?.postToPid(pid)
        }
        usleep(8_000)
    }
}

func allControls(_ app: NSRunningApplication) -> [Control] {
    wakeAccessibility(app)
    var out: [Control] = [], texts: [String] = []
    let ax = AXUIElementCreateApplication(app.processIdentifier)
    for w in (attr(ax, kAXWindowsAttribute) as? [AXUIElement]) ?? [] { walk(w, depth: 0, into: &out, texts: &texts) }
    return out
}

func chromeLoadUnpacked(_ folder: String) -> Never {
    guard FileManager.default.fileExists(atPath: folder + "/manifest.json") else { fail("no extension at \(folder)") }
    let launcher = Process()
    launcher.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    launcher.arguments = ["-a", "Google Chrome", "chrome://extensions/"]
    try? launcher.run(); launcher.waitUntilExit()
    guard let chrome = waitFor(10, { NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "com.google.Chrome" } })
    else { fail("Google Chrome did not start") }
    activate(chrome)
    // 1. Developer mode on
    guard let dev = waitFor(25, { allControls(chrome).first { $0.label.lowercased() == "developer mode" } })
    else { fail("could not find the Developer mode switch on chrome://extensions (is that tab in front?)") }
    if ((attr(dev.el, kAXValueAttribute) as? NSNumber)?.intValue ?? 0) == 0 {
        AXUIElementPerformAction(dev.el, kAXPressAction as CFString)
    }
    // 2. Load unpacked
    guard let load = waitFor(8, { allControls(chrome).first { $0.label.lowercased() == "load unpacked" } })
    else { fail("Developer mode is on but the Load unpacked button did not appear") }
    AXUIElementPerformAction(load.el, kAXPressAction as CFString)
    // 3. The folder picker runs in a separate macOS process, so keys sent to Chrome never reach it. Send
    //    them system-wide, but only while Chrome or the picker is frontmost (checked before every key).
    usleep(1_800_000)
    let pickerOK: Set<String> = ["com.google.Chrome", "com.apple.appkit.xpc.openAndSavePanelService"]
    func guardedKey(_ code: CGKeyCode, flags: CGEventFlags = [], unicode: UInt16? = nil) {
        guard let front = NSWorkspace.shared.frontmostApplication?.bundleIdentifier, pickerOK.contains(front) else {
            fail("stopped: the folder picker is no longer in front, so nothing more was typed")
        }
        for down in [true, false] {
            let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)
            e?.flags = flags
            if var u = unicode { e?.keyboardSetUnicodeString(stringLength: 1, unicodeString: &u) }
            e?.post(tap: .cghidEventTap)
        }
        usleep(25_000)
    }
    guardedKey(5, flags: [.maskCommand, .maskShift])            // ⇧⌘G: Go to Folder
    usleep(900_000)
    // put the path into the focused field (wherever that process is) via Accessibility; type it if that fails
    let sys = AXUIElementCreateSystemWide()
    var typed = false
    if let f = attr(sys, kAXFocusedUIElementAttribute) {
        typed = AXUIElementSetAttributeValue(f as! AXUIElement, kAXValueAttribute as CFString, folder as CFString) == .success
    }
    if !typed { for ch in folder.utf16 { guardedKey(0, unicode: ch) } }
    usleep(500_000)
    guardedKey(36)                                               // Return: go there
    usleep(1_200_000)
    guard let select = waitFor(6, { allControls(chrome).first { ["select", "open", "upload"].contains($0.label.lowercased()) } })
    else { fail("the folder picker did not show a Select button") }
    AXUIElementPerformAction(select.el, kAXPressAction as CFString)
    // 4. Check
    let ok = waitFor(10, { () -> Bool? in
        var c: [Control] = [], t: [String] = []
        walk(frontWindow(chrome), depth: 0, into: &c, texts: &t)
        return t.contains { $0.contains("Rozan Bridge") } ? true : nil
    }) ?? false
    emit(["installed": ok, "folder": folder])
    exit(ok ? 0 : 1)
}
