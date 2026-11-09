import AppKit

/// The agent's state as the bubble shows it (from the core's GET /status).
enum AgentState: String {
    case idle, planning, running, needsApproval = "needs_approval", error, paused, offline
}

/// A borderless, always-on-top panel on every Space holding the round bubble.
final class BubblePanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// The 52 pt circle: glyph, animated ring while working, pulsing badge for approvals, red dot on error.
final class BubbleView: NSView {
    var state: AgentState = .offline { didSet { if oldValue != state { applyState() } } }
    var pendingCount = 0 { didSet { badge.stringValue = pendingCount > 1 ? "\(pendingCount)" : "!"; } }
    var listening = false { didSet { setGlyph() } }
    var onClick: (() -> Void)?
    var onRightClick: ((NSEvent) -> Void)?
    var onMoved: ((NSPoint) -> Void)?
    var onHover: ((Bool) -> Void)?
    var onDragStart: (() -> Void)?

    private let circle = CAGradientLayer()
    private let ring = CAShapeLayer()
    private let glyph = NSImageView()
    private let badge = NSTextField(labelWithString: "!")
    private let dot = CALayer()
    private var dragStart: NSPoint?
    private var windowStart: NSPoint?
    private var dragged = false

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        let inset: CGFloat = 4
        let d = frame.width - inset * 2
        circle.frame = CGRect(x: inset, y: inset, width: d, height: d)
        circle.cornerRadius = d / 2
        circle.startPoint = CGPoint(x: 0, y: 1)
        circle.endPoint = CGPoint(x: 1, y: 0)
        circle.shadowOpacity = 0.25
        circle.shadowRadius = 4
        circle.shadowOffset = CGSize(width: 0, height: -1)
        layer?.addSublayer(circle)

        ring.path = CGPath(ellipseIn: circle.frame.insetBy(dx: -2, dy: -2), transform: nil)
        ring.fillColor = nil
        ring.lineWidth = 3
        ring.lineCap = .round
        ring.strokeEnd = 0.3
        ring.frame = bounds
        layer?.addSublayer(ring)

        glyph.frame = circle.frame.insetBy(dx: 12, dy: 12)
        glyph.imageScaling = .scaleProportionallyUpOrDown
        addSubview(glyph)
        setGlyph()
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))

        dot.frame = CGRect(x: frame.width - 16, y: frame.height - 16, width: 11, height: 11)
        dot.cornerRadius = 5.5
        dot.backgroundColor = NSColor.systemRed.cgColor
        dot.borderWidth = 1.5
        layer?.addSublayer(dot)

        badge.font = .boldSystemFont(ofSize: 11)
        badge.alignment = .center
        badge.textColor = .white
        badge.wantsLayer = true
        badge.layer?.backgroundColor = NSColor.systemOrange.cgColor
        badge.layer?.cornerRadius = 8
        badge.frame = NSRect(x: frame.width - 20, y: frame.height - 18, width: 18, height: 16)
        addSubview(badge)

        setAccessibilityRole(.button)
        setAccessibilityLabel("Rozan")
        applyState()
    }

    required init?(coder: NSCoder) { fatalError() }

    override func viewDidChangeEffectiveAppearance() { applyState() }

    private func setGlyph() {
        let name = listening ? "mic" : "rozan"
        glyph.image = bundledIcon(name, size: 24)
            ?? NSImage(systemSymbolName: listening ? "mic.fill" : "sparkle", accessibilityDescription: nil)
        setAccessibilityLabel(listening ? "Rozan, listening" : "Rozan")
        applyState()
    }

    private func applyState() {
        // Warm amber → terracotta (Rozan's colours); grey while on standby or starting; red while listening.
        let colors: [NSColor] = listening ? [NSColor(srgbRed: 0.94, green: 0.27, blue: 0.27, alpha: 1),
                                             NSColor(srgbRed: 0.86, green: 0.15, blue: 0.47, alpha: 1)]
            : (state == .paused || state == .offline) ? [NSColor(white: 0.55, alpha: 1), NSColor(white: 0.42, alpha: 1)]
            : [NSColor(srgbRed: 0.95, green: 0.66, blue: 0.42, alpha: 1), NSColor(srgbRed: 0.78, green: 0.44, blue: 0.27, alpha: 1)]
        circle.colors = colors.map(\.cgColor)
        circle.borderWidth = 0
        effectiveAppearance.performAsCurrentDrawingAppearance {
            ring.strokeColor = NSColor.controlAccentColor.cgColor
            dot.borderColor = NSColor.windowBackgroundColor.cgColor
        }
        glyph.contentTintColor = .white
        alphaValue = state == .paused ? 0.6 : 1
        ring.isHidden = !(state == .planning || state == .running)
        dot.isHidden = state != .error && state != .offline
        dot.backgroundColor = (state == .offline ? NSColor.systemGray : NSColor.systemRed).cgColor
        badge.isHidden = state != .needsApproval

        ring.removeAllAnimations()
        if !ring.isHidden {
            let spin = CABasicAnimation(keyPath: "transform.rotation.z")
            spin.fromValue = 0; spin.toValue = -2 * Double.pi
            spin.duration = 1.2; spin.repeatCount = .infinity
            ring.anchorPoint = CGPoint(x: 0.5, y: 0.5)
            ring.position = CGPoint(x: bounds.midX, y: bounds.midY)
            ring.bounds = bounds
            ring.add(spin, forKey: "spin")
        }
        circle.removeAnimation(forKey: "pulse")
        if state == .needsApproval {
            // An approval must never wait silently: pulse until it is answered.
            let pulse = CABasicAnimation(keyPath: "shadowColor")
            pulse.fromValue = NSColor.systemOrange.cgColor
            pulse.toValue = NSColor.clear.cgColor
            pulse.duration = 0.8; pulse.autoreverses = true; pulse.repeatCount = .infinity
            circle.shadowRadius = 10; circle.shadowOpacity = 0.9
            circle.add(pulse, forKey: "pulse")
        } else {
            circle.shadowRadius = 4; circle.shadowOpacity = 0.25; circle.shadowColor = NSColor.black.cgColor
        }
        toolTip = "Rozan — " + [
            .idle: "idle", .planning: "planning…", .running: "working…", .needsApproval: "needs your approval",
            .error: "something went wrong", .paused: "on standby", .offline: "starting…",
        ][state, default: ""]
    }

    // MARK: mouse: click toggles the modal, drag moves the bubble, right-click opens the menu
    override func mouseEntered(with event: NSEvent) { onHover?(true) }
    override func mouseExited(with event: NSEvent) { onHover?(false) }

    override func mouseDown(with event: NSEvent) {
        onDragStart?()
        dragStart = NSEvent.mouseLocation
        windowStart = window?.frame.origin
        dragged = false
    }

    override func mouseDragged(with event: NSEvent) {
        guard let s = dragStart, let w = windowStart, let window else { return }
        let now = NSEvent.mouseLocation
        if abs(now.x - s.x) + abs(now.y - s.y) > 3 { dragged = true }
        window.setFrameOrigin(NSPoint(x: w.x + now.x - s.x, y: w.y + now.y - s.y))
    }

    override func mouseUp(with event: NSEvent) {
        if dragged, let window { onMoved?(window.frame.origin) } else { onClick?() }
        dragStart = nil
    }

    override func rightMouseDown(with event: NSEvent) { onRightClick?(event) }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}
