import AppKit
import SwiftUI

/// Where things live. Development build: the project folder (PAProjectRoot) and its .venv.
/// Downloaded app: the code inside Rozan.app (read-only) and a Python environment in Application Support.
enum Paths {
    static let devRoot: URL? = (Bundle.main.object(forInfoDictionaryKey: "PAProjectRoot") as? String
        ?? ProcessInfo.processInfo.environment["PA_PROJECT_ROOT"]).map { URL(fileURLWithPath: $0) }
    static var bundled: Bool { devRoot == nil }
    static var root: URL { devRoot ?? Bundle.main.resourceURL!.appendingPathComponent("core") }
    static let support = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Rozan")
    static var python: URL {
        bundled ? support.appendingPathComponent("venv/bin/python") : root.appendingPathComponent(".venv/bin/python")
    }
    static let model = "qwen3.5:4b"
}

/// One requirement: a shell check (exit 0 = ok) and the commands that install it. Every command is shown
/// to the owner before it runs, and runs only when they press Install.
struct SetupStep: Identifiable {
    let id: String
    let title: String
    let why: String
    let check: String
    let install: String
    var optional = false
}

enum StepState: Equatable { case unknown, checking, ok, missing, installing, failed(String) }

@MainActor
final class SetupModel: ObservableObject {
    @Published var states: [String: StepState] = [:]
    @Published var log = ""
    @Published var busy = false
    var onReady: (() -> Void)?

    let steps: [SetupStep]

    init() {
        let env = """
            export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/Applications/Ollama.app/Contents/Resources:$PATH"
            ROOT=\(Self.q(Paths.root.path)); PY=\(Self.q(Paths.python.path)); SUPPORT=\(Self.q(Paths.support.path))
            VENV="$SUPPORT/venv"; MODEL=\(Paths.model)
            """
        let ollamaUp = "curl -sf http://127.0.0.1:11434/api/version >/dev/null"
        steps = [
            SetupStep(id: "ollama", title: "Ollama", why: "Runs the AI model on this Mac. Nothing is sent to the cloud.",
                      check: env + "\n[ -d /Applications/Ollama.app ] || command -v ollama >/dev/null",
                      install: env + """

                        set -e
                        T=$(mktemp -d)
                        echo "Downloading Ollama from ollama.com…"
                        curl -fL --progress-bar -o "$T/Ollama.zip" https://ollama.com/download/Ollama-darwin.zip
                        ditto -xk "$T/Ollama.zip" /Applications
                        open -ga Ollama
                        for i in $(seq 1 40); do \(ollamaUp) && echo "Ollama is running." && exit 0; sleep 1; done
                        echo "Ollama was installed but did not start; open it from Applications." ; exit 1
                        """),
            SetupStep(id: "python", title: "Python 3.11 or newer", why: "Rozan's core is written in Python. Installed with uv, no admin password.",
                      check: env + "\n[ -x \"$PY\" ] || { command -v uv >/dev/null && uv python find '>=3.11' >/dev/null; }",
                      install: env + """

                        set -e
                        if ! command -v uv >/dev/null; then
                          echo "Installing uv (astral.sh)…"
                          curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
                        fi
                        uv python find '>=3.11' || uv python install 3.12
                        """),
            SetupStep(id: "env", title: "Rozan's Python packages", why: "A private environment for Rozan (FastAPI, pydantic…).",
                      check: env + "\n\"$PY\" -c 'import fastapi, uvicorn, pydantic, yaml, httpx, cryptography' 2>/dev/null",
                      install: env + """

                        set -e
                        mkdir -p "$SUPPORT"
                        if [ ! -x "$VENV/bin/python" ]; then uv venv --python "$(uv python find '>=3.11')" "$VENV"; fi
                        uv pip install --python "$VENV/bin/python" -r "$ROOT/requirements.txt"
                        """),
            SetupStep(id: "model", title: "AI model (\(Paths.model), about 3.4 GB)", why: "The local model Rozan thinks with.",
                      check: env + "\n(\(ollamaUp) || (open -ga Ollama; sleep 5)); curl -sf http://127.0.0.1:11434/api/tags | grep -q \"\\\"$MODEL\\\"\"",
                      install: env + """

                        set -e
                        \(ollamaUp) || { open -ga Ollama; sleep 5; }
                        ollama pull "$MODEL"
                        """),
            SetupStep(id: "bridge", title: "Chrome connection", why: "Lets Rozan use your Chrome. You can also skip this.",
                      check: env + """

                        H="$HOME/Library/Application Support/Google/Chrome"
                        M="$H/NativeMessagingHosts/com.personal_agent.bridge.json"
                        [ ! -d "$H" ] || { [ -f "$M" ] && L=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["path"])' "$M") && grep -qs "$PY" "$L"; }
                        """,
                      install: env + "\n\"$PY\" -I \"$ROOT/scripts/install_chrome_bridge.py\" --launcher-dir \"$SUPPORT\"",
                      optional: true),
        ]
        for s in steps { states[s.id] = .unknown }
    }

    static func q(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

    var ready: Bool { steps.allSatisfy { $0.optional || states[$0.id] == .ok } }

    func refresh() async {
        for s in steps where states[s.id] != .installing {
            states[s.id] = .checking
            states[s.id] = await Self.run(s.check, log: nil) == 0 ? .ok : .missing
        }
    }

    func install(_ s: SetupStep) async {
        busy = true
        states[s.id] = .installing
        // show exactly what will run (without the PATH/variables preamble)
        let body = s.install.components(separatedBy: "\n").drop { !$0.isEmpty && !$0.hasPrefix("set -e") && $0 != "" }
        log += "\n▶ \(s.title)\n$ " + body.joined(separator: "\n$ ").trimmingCharacters(in: .whitespacesAndNewlines) + "\n"
        let code = await Self.run(s.install) { [weak self] text in self?.log += text }
        states[s.id] = code == 0 ? .ok : .failed("exit \(code)")
        if code == 0 { states[s.id] = await Self.run(s.check, log: nil) == 0 ? .ok : .failed("installed, but the check still fails") }
        busy = false
    }

    func installMissing() async {
        for s in steps where states[s.id] != .ok {
            await install(s)
            if case .failed = states[s.id], !s.optional { return }
        }
    }

    /// Runs a script with zsh; streams output to `log` when given.
    static func run(_ script: String, log: ((String) -> Void)?) async -> Int32 {
        await withCheckedContinuation { cont in
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/zsh")
            p.arguments = ["-c", script]
            let pipe = Pipe()
            p.standardOutput = pipe; p.standardError = pipe
            pipe.fileHandleForReading.readabilityHandler = { h in
                let d = h.availableData
                guard !d.isEmpty, let s = String(data: d, encoding: .utf8) else { return }
                Task { @MainActor in log?(s) }
            }
            p.terminationHandler = { proc in
                pipe.fileHandleForReading.readabilityHandler = nil
                cont.resume(returning: proc.terminationStatus)
            }
            do { try p.run() } catch { cont.resume(returning: 127) }
        }
    }
}

struct SetupView: View {
    @ObservedObject var model: SetupModel
    @State private var showLog = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 56, height: 56)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Set up Rozan").font(.title2.bold())
                    Text("Everything runs on this Mac. Install buttons run their commands in the background; Details shows each command and its output.")
                        .foregroundStyle(.secondary).font(.callout)
                }
            }
            VStack(spacing: 10) {
                ForEach(model.steps) { step in row(step) }
            }
            DisclosureGroup("Details", isExpanded: $showLog) {
                ScrollView {
                    Text(model.log.isEmpty ? "No output yet." : model.log)
                        .font(.system(.caption, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }.frame(height: 140)
            }
            HStack {
                Button("Check again") { Task { await model.refresh() } }.disabled(model.busy)
                Spacer()
                if model.ready {
                    Button("Start Rozan") { model.onReady?() }.keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
                } else {
                    Button("Install everything missing") { showLog = true; Task { await model.installMissing() } }
                        .keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent).disabled(model.busy)
                }
            }
        }
        .padding(22).frame(width: 560)
    }

    @ViewBuilder func row(_ step: SetupStep) -> some View {
        let state = model.states[step.id] ?? .unknown
        HStack(alignment: .top, spacing: 10) {
            Group {
                switch state {
                case .ok: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                case .checking, .installing: ProgressView().controlSize(.small)
                case .failed: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                default: Image(systemName: step.optional ? "circle.dashed" : "xmark.circle.fill")
                    .foregroundStyle(step.optional ? Color.secondary : Color.red)
                }
            }.frame(width: 18, height: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text(step.title + (step.optional ? " (optional)" : "")).font(.headline)
                Text(step.why).font(.callout).foregroundStyle(.secondary)
                if case .failed(let why) = state { Text("Failed: \(why). See Details.").font(.callout).foregroundStyle(.orange) }
            }
            Spacer()
            if state == .missing || { if case .failed = state { return true }; return false }() {
                Button(state == .missing ? "Install" : "Retry") { showLog = true; Task { await model.install(step) } }
                    .disabled(model.busy)
            } else if state == .ok {
                Text("Ready").font(.callout).foregroundStyle(.secondary)
            }
        }
        .padding(10).background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
    }
}

@MainActor
final class SetupWindow {
    private var window: NSWindow?
    let model = SetupModel()

    func show(onReady: @escaping () -> Void) {
        model.onReady = { [weak self] in self?.window?.close(); onReady() }
        if window == nil {
            let w = NSWindow(contentViewController: NSHostingController(rootView: SetupView(model: model)))
            w.title = "Rozan Setup"
            w.styleMask = [.titled, .closable]
            w.isReleasedWhenClosed = false
            w.center()
            window = w
        }
        NSApp.activate()
        window?.makeKeyAndOrderFront(nil)
        Task { await model.refresh() }
    }

    /// True when everything required is in place (used at launch to decide whether to show the window).
    func requirementsMet() async -> Bool {
        await model.refresh()
        return model.ready
    }
}
