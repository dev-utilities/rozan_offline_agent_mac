import AppKit
import WebKit

/// Builds a web view on the core's UI with the session token injected before the page script runs.
@MainActor
func makeWebView(session: Core.Session, handler: WKScriptMessageHandler) -> WKWebView {
    let config = WKWebViewConfiguration()
    let token = session.token.replacingOccurrences(of: "\\", with: "").replacingOccurrences(of: "\"", with: "")
    let script = WKUserScript(source: "window.__AGENT_TOKEN__ = \"\(token)\";",
                              injectionTime: .atDocumentStart, forMainFrameOnly: true)
    config.userContentController.addUserScript(script)
    config.userContentController.add(handler, name: "shell")
    config.websiteDataStore = .nonPersistent()
    let web = WKWebView(frame: .zero, configuration: config)
    web.uiDelegate = handler as? WKUIDelegate   // without this, confirm() and alert() silently do nothing
    web.setValue(false, forKey: "drawsBackground")
    if let url = URL(string: "http://127.0.0.1:\(session.port)/") { web.load(URLRequest(url: url)) }
    return web
}

/// The modal next to the bubble. It is a panel that can take keyboard focus (for typing) and closes on Esc.
final class ModalPanel: NSPanel {
    var onEscape: (() -> Void)?
    override var canBecomeKey: Bool { true }
    override func cancelOperation(_ sender: Any?) { onEscape?() }
}

@MainActor
final class Windows: NSObject, WKScriptMessageHandler, NSWindowDelegate {
    static let modalSize = NSSize(width: 400, height: 560)
    private(set) var modal: ModalPanel?
    private var modalWeb: WKWebView?
    private var full: NSWindow?
    var session: Core.Session?
    var onModalShown: (() -> Void)?
    var onDictate: (() -> Void)?
    /// Where dictated text goes: the window whose mic button was pressed; the modal otherwise.
    var dictationTarget: WKWebView?
    var dictatingInModal: Bool { dictationTarget == nil || dictationTarget === modalWeb }

    var modalVisible: Bool { modal?.isVisible ?? false }

    func reset() {
        modal?.orderOut(nil); modal = nil; modalWeb = nil
        full?.close(); full = nil
    }

    // MARK: modal
    func toggleModal(near bubble: NSRect) {
        if modalVisible { hideModal() } else { showModal(near: bubble) }
    }

    func showModal(near bubble: NSRect) {
        guard let session else { return }
        if modal == nil {
            let panel = ModalPanel(contentRect: NSRect(origin: .zero, size: Self.modalSize),
                                   styleMask: [.titled, .closable, .resizable, .nonactivatingPanel],
                                   backing: .buffered, defer: false)
            panel.title = "Rozan"
            panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
            panel.standardWindowButton(.zoomButton)?.isHidden = true
            panel.isFloatingPanel = true
            panel.level = .floating
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            panel.hidesOnDeactivate = false
            panel.isReleasedWhenClosed = false
            panel.minSize = NSSize(width: 340, height: 360)
            panel.onEscape = { [weak self] in self?.hideModal() }
            panel.delegate = self
            let web = makeWebView(session: session, handler: self)
            panel.contentView = web
            modal = panel
            modalWeb = web
        }
        guard let modal else { return }
        modal.setFrameOrigin(Self.anchor(size: modal.frame.size, bubble: bubble))
        NSApp.activate()
        modal.makeKeyAndOrderFront(nil)
        onModalShown?()
    }

    func hideModal() { modal?.orderOut(nil) }

    /// Place the modal beside the bubble: right of it if there is room, else left; flip vertically near edges.
    static func anchor(size: NSSize, bubble: NSRect) -> NSPoint {
        let screen = (NSScreen.screens.first { $0.frame.intersects(bubble) } ?? NSScreen.main)?.visibleFrame
            ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let gap: CGFloat = 10
        var x = bubble.maxX + gap
        if x + size.width > screen.maxX { x = bubble.minX - gap - size.width }
        var y = bubble.maxY - size.height
        if y < screen.minY { y = bubble.minY }
        x = min(max(x, screen.minX), screen.maxX - size.width)
        y = min(max(y, screen.minY), screen.maxY - size.height)
        return NSPoint(x: x, y: y)
    }

    // MARK: full app
    func openFullApp() {
        guard let session else { return }
        hideModal()
        if full == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            w.title = "Rozan"
            w.minSize = NSSize(width: 760, height: 480)
            w.isReleasedWhenClosed = false
            w.contentView = makeWebView(session: session, handler: self)
            w.center()
            w.setFrameAutosaveName("PersonalAgentFullApp")
            full = w
        }
        NSApp.activate()
        full?.makeKeyAndOrderFront(nil)
    }

    func reloadModal() { modalWeb?.reload() }

    /// Show dictation in the chat box: `final` false = live text while listening, true = the result
    /// (the owner still presses Send). Retries until the page has loaded its input box.
    func setDictation(_ text: String, final: Bool, attempt: Int = 0) {
        guard let data = try? JSONSerialization.data(withJSONObject: [text, final]),
              let arr = String(data: data, encoding: .utf8) else { return }
        let js = "(() => { const i = document.querySelector('#input'); if (!i) return false; const [t, fin] = \(arr); " +
                 "if (i.dataset.before === undefined) i.dataset.before = i.value; " +
                 "i.value = (i.dataset.before ? i.dataset.before + ' ' : '') + t; " +
                 "i.placeholder = fin ? 'Ask anything…' : 'Listening… press ⌃⌥D to stop'; " +
                 "if (fin) delete i.dataset.before; i.dispatchEvent(new Event('input')); i.focus(); return true; })()"
        (dictationTarget ?? modalWeb)?.evaluateJavaScript(js) { [weak self] result, _ in
            if (result as? Bool) != true && attempt < 20 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    self?.setDictation(text, final: final, attempt: attempt + 1)
                }
            }
        }
    }

    // MARK: messages from the page: {action: "openFullApp"}
    nonisolated func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        let action = (message.body as? [String: Any])?["action"] as? String
        let web = message.webView
        Task { @MainActor in
            switch action {
            case "openFullApp": self.openFullApp()
            case "dictate":
                self.dictationTarget = web            // text goes back to the window whose mic was pressed
                self.onDictate?()
            default: break
            }
        }
    }
}

/// The page's confirm() and alert() as native sheets on the window that holds the page.
extension Windows: WKUIDelegate {
    private func sheet(_ text: String, on web: WKWebView, buttons: [String], done: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = text
        for b in buttons { a.addButton(withTitle: b) }
        if let w = web.window {
            a.beginSheetModal(for: w) { done($0 == .alertFirstButtonReturn) }
        } else {
            done(a.runModal() == .alertFirstButtonReturn)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        sheet(message, on: webView, buttons: ["OK", "Cancel"], done: completionHandler)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        sheet(message, on: webView, buttons: ["OK"]) { _ in completionHandler() }
    }
}
