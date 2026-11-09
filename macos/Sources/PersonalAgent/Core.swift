import Foundation

/// Starts the Python core, finds its port and token, restarts it if it dies, and talks to its API.
@MainActor
final class Core {
    struct Session { let port: Int; let token: String }

    let projectRoot: URL
    private var process: Process?
    private(set) var session: Session?
    private var restarts = 0
    private var stopping = false
    var onReady: ((Session) -> Void)?
    var onDown: ((String) -> Void)?

    private var home: URL {
        if let h = ProcessInfo.processInfo.environment["AGENT_HOME"] { return URL(fileURLWithPath: h) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".personal_agent")
    }

    init(projectRoot: URL) { self.projectRoot = projectRoot }

    func start() {
        stopping = false
        let python = Paths.python
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            onDown?("Python environment not found at \(python.path). Open Rozan → Setup & Requirements.")
            return
        }
        try? FileManager.default.removeItem(at: home.appendingPathComponent("session.json"))
        let p = Process()
        p.executableURL = python
        p.arguments = ["-m", "agent.server", "--port", "0"]
        p.currentDirectoryURL = projectRoot
        var env = ProcessInfo.processInfo.environment
        env["PYTHONDONTWRITEBYTECODE"] = "1"          // never write inside the (signed) app bundle
        p.environment = env
        let log = home.appendingPathComponent("core.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        if let h = try? FileHandle(forWritingTo: log) { p.standardOutput = h; p.standardError = h }
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in self?.coreExited(code: proc.terminationStatus) }
        }
        do { try p.run() } catch { onDown?("Could not start the core: \(error.localizedDescription)"); return }
        process = p
        waitForSession(deadline: Date().addingTimeInterval(60))
    }

    private func waitForSession(deadline: Date) {
        Task { @MainActor in
            while Date() < deadline {
                if let s = readSession(), await healthy(port: s.port) {
                    session = s
                    restarts = 0
                    onReady?(s)
                    return
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
            onDown?("The core did not start in time. See ~/.personal_agent/core.log")
        }
    }

    private func readSession() -> Session? {
        guard let data = try? Data(contentsOf: home.appendingPathComponent("session.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let port = obj["port"] as? Int,
              let token = try? String(contentsOf: home.appendingPathComponent("session.token"), encoding: .utf8)
        else { return nil }
        return Session(port: port, token: token.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private func healthy(port: Int) async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        var req = URLRequest(url: url); req.timeoutInterval = 2
        return ((try? await URLSession.shared.data(for: req))?.1 as? HTTPURLResponse)?.statusCode == 200
    }

    private func coreExited(code: Int32) {
        process = nil
        session = nil
        if stopping { return }
        onDown?("The core stopped (exit \(code)). Restarting…")
        restarts += 1
        let delay = min(30.0, pow(2.0, Double(restarts)))
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            if !stopping { start() }
        }
    }

    func stop() {
        stopping = true
        guard let p = process else { return }
        p.terminate()                                   // SIGTERM: the core cleans up its session files
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        process = nil
    }

    // MARK: API
    func request(_ method: String, _ path: String, body: [String: Any]? = nil) async -> [String: Any]? {
        guard let s = session, let url = URL(string: "http://127.0.0.1:\(s.port)\(path)") else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = 5
        req.setValue("Bearer \(s.token)", forHTTPHeaderField: "Authorization")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        guard let (data, _) = try? await URLSession.shared.data(for: req) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }
}
