import AppKit

/// Icons bundled in Contents/Resources (Lucide, ISC license), drawn as template images so they take any tint.
@MainActor
func bundledIcon(_ name: String, size: CGFloat) -> NSImage? {
    guard let url = Bundle.main.url(forResource: name, withExtension: "svg"),
          let img = NSImage(contentsOf: url) else { return nil }
    img.size = NSSize(width: size, height: size)
    img.isTemplate = true
    return img
}

/// A small round button with a bundled icon, used in the hover bar.
final class RoundButton: NSView {
    var onClick: (() -> Void)?
    private let glyph = NSImageView()
    private let hoverTint: NSColor

    init(diameter: CGFloat, icon: String, fallback: String, label: String, hoverTint: NSColor) {
        self.hoverTint = hoverTint
        super.init(frame: NSRect(x: 0, y: 0, width: diameter, height: diameter))
        wantsLayer = true
        layer?.cornerRadius = diameter / 2
        glyph.image = bundledIcon(icon, size: 14) ?? NSImage(systemSymbolName: fallback, accessibilityDescription: label)
        glyph.frame = bounds.insetBy(dx: (diameter - 14) / 2, dy: (diameter - 14) / 2)
        addSubview(glyph)
        toolTip = label
        setAccessibilityRole(.button)
        setAccessibilityLabel(label)
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
        restyle(hover: false)
    }
    required init?(coder: NSCoder) { fatalError() }

    func setIcon(_ icon: String, fallback: String, label: String) {
        glyph.image = bundledIcon(icon, size: 14) ?? NSImage(systemSymbolName: fallback, accessibilityDescription: label)
        toolTip = label
        setAccessibilityLabel(label)
    }

    private func restyle(hover: Bool) {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = (hover ? hoverTint : NSColor.controlBackgroundColor).cgColor
            layer?.borderColor = NSColor.separatorColor.cgColor
        }
        layer?.borderWidth = hover ? 0 : 0.5
        glyph.contentTintColor = hover ? .white : .labelColor
    }

    override func mouseEntered(with event: NSEvent) { restyle(hover: true) }
    override func mouseExited(with event: NSEvent) { restyle(hover: false) }
    override func mouseUp(with event: NSEvent) {
        if bounds.contains(convert(event.locationInWindow, from: nil)) { onClick?() }
    }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func viewDidChangeEffectiveAppearance() { restyle(hover: false) }
    override func accessibilityPerformPress() -> Bool { onClick?(); return true }   // VoiceOver / keyboard access
}

/// The two quick actions shown while the pointer is over the bubble: dictate and quit.
final class HoverBar: NSPanel {
    let mic: RoundButton
    let quit: RoundButton
    var onHover: ((Bool) -> Void)?
    static let size = NSSize(width: 64, height: 30)

    init() {
        mic = RoundButton(diameter: 26, icon: "mic", fallback: "mic", label: "Dictate (⌃⌥D)", hoverTint: .controlAccentColor)
        quit = RoundButton(diameter: 26, icon: "x", fallback: "xmark", label: "Quit Rozan", hoverTint: .systemRed)
        super.init(contentRect: NSRect(origin: .zero, size: Self.size), styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered, defer: false)
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        hidesOnDeactivate = false
        let host = TrackingView(frame: NSRect(origin: .zero, size: Self.size))
        host.onHover = { [weak self] inside in self?.onHover?(inside) }
        mic.setFrameOrigin(NSPoint(x: 4, y: 2))
        quit.setFrameOrigin(NSPoint(x: 34, y: 2))
        host.addSubview(mic)
        host.addSubview(quit)
        contentView = host
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    /// Centered above the bubble; below it when the bubble sits at the top of the screen.
    func place(near bubble: NSRect) {
        let screen = (NSScreen.screens.first { $0.frame.intersects(bubble) } ?? NSScreen.main)?.visibleFrame ?? bubble
        var y = bubble.maxY + 2
        if y + Self.size.height > screen.maxY { y = bubble.minY - Self.size.height - 2 }
        let x = min(max(bubble.midX - Self.size.width / 2, screen.minX), screen.maxX - Self.size.width)
        setFrameOrigin(NSPoint(x: x, y: y))
    }
}

/// Reports when the pointer enters or leaves its bounds.
final class TrackingView: NSView {
    var onHover: ((Bool) -> Void)?
    override init(frame: NSRect) {
        super.init(frame: frame)
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }
    required init?(coder: NSCoder) { fatalError() }
    override func mouseEntered(with event: NSEvent) { onHover?(true) }
    override func mouseExited(with event: NSEvent) { onHover?(false) }
}
