// Rozan's icon: a small window (rozan) in a warm arch, light streaming through it. Drawn from shapes.
//   swift scripts/make_icon.swift iconset <out.iconset>      app icon sizes (then: iconutil -c icns)
//   swift scripts/make_icon.swift extension <dir>            icon16/32/48/128.png for the Chrome extension
//   swift scripts/make_icon.swift preview <file.png>          one 1024 px image
import AppKit

func rgb(_ r: Int, _ g: Int, _ b: Int, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat(r) / 255, green: CGFloat(g) / 255, blue: CGFloat(b) / 255, alpha: a)
}

/// Draws the art into a square of side `s`. `tile` adds the macOS rounded-square background.
func draw(_ s: CGFloat, tile: Bool) {
    let inset = tile ? s * 0.1 : 0
    let box = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let u = box.width / 100                               // design units
    func R(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSRect {   // y from the top, in units
        NSRect(x: box.minX + x * u, y: box.maxY - (y + h) * u, width: w * u, height: h * u)
    }
    if tile {
        let bg = NSBezierPath(roundedRect: box, xRadius: box.width * 0.225, yRadius: box.width * 0.225)
        NSGraphicsContext.current?.saveGraphicsState()
        let sh = NSShadow(); sh.shadowBlurRadius = s * 0.02; sh.shadowOffset = NSSize(width: 0, height: -s * 0.01)
        sh.shadowColor = NSColor.black.withAlphaComponent(0.28); sh.set()
        rgb(255, 248, 238).setFill(); bg.fill()
        NSGraphicsContext.current?.restoreGraphicsState()
        NSGradient(colors: [rgb(255, 251, 245), rgb(253, 236, 214)])!.draw(in: bg, angle: -90)
        bg.addClip()
    }
    // the arch (wall with a rounded top)
    let arch = R(24, 14, 52, 74)
    let archPath = NSBezierPath()
    let r = arch.width / 2
    archPath.move(to: NSPoint(x: arch.minX, y: arch.minY))
    archPath.line(to: NSPoint(x: arch.minX, y: arch.maxY - r))
    archPath.appendArc(withCenter: NSPoint(x: arch.midX, y: arch.maxY - r), radius: r, startAngle: 180, endAngle: 0, clockwise: true)
    archPath.line(to: NSPoint(x: arch.maxX, y: arch.minY))
    archPath.close()
    NSGradient(colors: [rgb(240, 182, 140), rgb(205, 125, 82)])!.draw(in: archPath, angle: -90)
    // glow around the window
    let win = R(42.5, 27, 15, 24)
    NSGraphicsContext.current?.saveGraphicsState()
    archPath.addClip()
    NSGradient(colors: [rgb(255, 244, 214, 0.95), rgb(255, 214, 150, 0)])!
        .draw(fromCenter: NSPoint(x: win.midX, y: win.midY), radius: 0,
              toCenter: NSPoint(x: win.midX, y: win.midY), radius: 30 * u, options: [])
    NSGraphicsContext.current?.restoreGraphicsState()
    // light rays fanning down from the window to the ground
    let ground = arch.minY - (tile ? 4 * u : 0)
    for (fromX, toX, w) in [(-0.30, -1.25, 9.0), (-0.08, -0.35, 7.0), (0.12, 0.45, 7.0), (0.32, 1.30, 9.0)] as [(CGFloat, CGFloat, CGFloat)] {
        let top = NSPoint(x: win.midX + fromX * win.width * 1.4, y: win.minY)
        let bx = win.midX + toX * 17 * u
        let ray = NSBezierPath()
        ray.move(to: NSPoint(x: top.x - u * 1.2, y: top.y))
        ray.line(to: NSPoint(x: top.x + u * 1.2, y: top.y))
        ray.line(to: NSPoint(x: bx + w * u / 2, y: ground))
        ray.line(to: NSPoint(x: bx - w * u / 2, y: ground))
        ray.close()
        NSGradient(colors: [rgb(255, 214, 120, 0.95), rgb(255, 180, 90, 0.15)])!.draw(in: ray, angle: -90)
    }
    // the window: bright opening with a frame
    let opening = NSBezierPath(roundedRect: win, xRadius: u * 1.2, yRadius: u * 1.2)
    NSGradient(colors: [rgb(255, 255, 255), rgb(255, 246, 222)])!.draw(in: opening, angle: -90)
    rgb(176, 98, 58).setStroke()
    opening.lineWidth = max(1, 2.2 * u)
    opening.stroke()
}

func png(_ px: Int, tile: Bool) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4,
                               hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    draw(CGFloat(px), tile: tile)
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let mode = CommandLine.arguments[1], out = URL(fileURLWithPath: CommandLine.arguments[2])
switch mode {
case "iconset":
    try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
    for (name, px) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128), ("128x128@2x", 256),
                       ("256x256", 256), ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)] {
        try! png(px, tile: true).write(to: out.appendingPathComponent("icon_\(name).png"))
    }
case "extension":
    try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
    for px in [16, 32, 48, 128] { try! png(px, tile: px >= 48).write(to: out.appendingPathComponent("icon\(px).png")) }
case "preview":
    try! png(1024, tile: true).write(to: out)
default:
    fatalError("mode: iconset | extension | preview")
}
