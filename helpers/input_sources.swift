// Keyboard input sources (macOS Text Input Sources API). A tiny helper the agent's
// system.keyboards / system.add_keyboard / system.remove_keyboard tools call.
//   input_sources list                 enabled keyboard sources, as JSON
//   input_sources find <language>      sources that can be enabled for a language name or code, as JSON
//   input_sources enable <id> | disable <id>
import Carbon
import Foundation

func prop<T>(_ s: TISInputSource, _ key: CFString) -> T? {
    guard let p = TISGetInputSourceProperty(s, key) else { return nil }
    return Unmanaged<AnyObject>.fromOpaque(p).takeUnretainedValue() as? T
}

func sources(all: Bool) -> [TISInputSource] {
    let filter = [kTISPropertyInputSourceCategory as String: kTISCategoryKeyboardInputSource as String] as CFDictionary
    return (TISCreateInputSourceList(filter, all)?.takeRetainedValue() as? [TISInputSource]) ?? []
}

func info(_ s: TISInputSource) -> [String: Any] {
    [
        "id": prop(s, kTISPropertyInputSourceID) as String? ?? "",
        "name": prop(s, kTISPropertyLocalizedName) as String? ?? "",
        "languages": Array((prop(s, kTISPropertyInputSourceLanguages) as [String]? ?? []).prefix(3)),
        "enabled": (prop(s, kTISPropertyInputSourceIsEnabled) as Bool?) ?? false,
        "selectable": (prop(s, kTISPropertyInputSourceIsSelectCapable) as Bool?) ?? false,
    ]
}

func emit(_ obj: Any) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

let args = Array(CommandLine.arguments.dropFirst())
switch (args.first, args.count) {
case ("list", 1):
    emit(sources(all: false).map(info).filter { ($0["selectable"] as? Bool) == true })
case ("find", 2):
    let q = args[1].lowercased()
    let code = Locale(identifier: "en").languageCode(for: q)
    let hits = sources(all: true).map(info).filter { s in
        let name = (s["name"] as? String ?? "").lowercased()
        let langs = (s["languages"] as? [String] ?? []).map { $0.lowercased() }
        return name.contains(q) || langs.first == q || (code != nil && langs.first == code)
    }
    emit(hits)
case ("enable", 2), ("disable", 2):
    guard let s = sources(all: true).first(where: { (prop($0, kTISPropertyInputSourceID) as String?) == args[1] }) else {
        FileHandle.standardError.write("no input source with id \(args[1])\n".data(using: .utf8)!); exit(2)
    }
    let status = args[0] == "enable" ? TISEnableInputSource(s) : TISDisableInputSource(s)
    if status != noErr { FileHandle.standardError.write("failed: OSStatus \(status)\n".data(using: .utf8)!); exit(1) }
    emit(info(s))
default:
    FileHandle.standardError.write("usage: input_sources list | find <language> | enable <id> | disable <id>\n".data(using: .utf8)!)
    exit(64)
}

extension Locale {
    /// "urdu" -> "ur", using the English names of all ISO language codes.
    func languageCode(for name: String) -> String? {
        if name.count <= 3 { return name }
        return Locale.LanguageCode.isoLanguageCodes.first {
            self.localizedString(forLanguageCode: $0.identifier)?.lowercased() == name
        }?.identifier
    }
}
