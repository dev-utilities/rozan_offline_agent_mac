import Foundation

/// Appends a line to ~/.personal_agent/shell.log (the app's own diagnostics; no content of requests).
func shellLog(_ msg: String) {
    let url = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".personal_agent/shell.log")
    let line = ISO8601DateFormatter().string(from: Date()) + " " + msg + "\n"
    if let h = try? FileHandle(forWritingTo: url) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
    } else {
        try? line.data(using: .utf8)!.write(to: url)
    }
}
