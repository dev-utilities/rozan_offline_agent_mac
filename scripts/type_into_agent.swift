// Test helper: types text into the Rozan app ONLY if it is the frontmost app; otherwise refuses.
//   swift scripts/type_into_agent.swift "text" [--enter]
import AppKit

let args = CommandLine.arguments.dropFirst()
let text = args.first ?? ""
let front = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "none"
guard front == "com.personal-agent.shell" else {
    FileHandle.standardError.write("refusing to type: frontmost app is \(front), not Rozan\n".data(using: .utf8)!)
    exit(2)
}
func key(_ down: Bool, _ code: CGKeyCode = 0, _ s: String? = nil) {
    let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)
    if let s { let u = Array(s.utf16); e?.keyboardSetUnicodeString(stringLength: u.count, unicodeString: u) }
    e?.post(tap: .cghidEventTap)
    usleep(8_000)
}
for ch in text { key(true, 0, String(ch)); key(false, 0, String(ch)) }
if args.contains("--enter") {
    // check again right before Enter
    guard NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.personal-agent.shell" else { exit(3) }
    key(true, 36); key(false, 36)
}
