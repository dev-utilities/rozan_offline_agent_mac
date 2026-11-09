// Rozan's icon: light shining through a small window (rozan). Drawn from shapes, no artwork files.
// Two variants: "light" (pale blue tile, blue window; the default: Dock, Finder, Chrome extension) and
// "dark" (navy tile, glowing window; the floating bubble uses it when macOS is in dark mode).
//   swift scripts/make_icon.swift iconset <out.iconset>      app icon sizes, light (then: iconutil -c icns)
//   swift scripts/make_icon.swift extension <dir>            icon16/32/48/128.png for the Chrome extension, light
//   swift scripts/make_icon.swift bubble <dir>               bubble-light.png / bubble-dark.png (round, 2x)
//   swift scripts/make_icon.swift preview <dir>              icon-light.png / icon-dark.png at 1024 px
import AppKit

func rgb(_ r: Int, _ g: Int, _ b: Int, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat(r) / 255, green: CGFloat(g) / 255, blue: CGFloat(b) / 255, alpha: a)
}

enum Variant { case light, dark }
enum Shape { case tile, square, circle }   // tile: macOS rounded square with margin; square: full bleed; circle: bubble

func draw(_ s: CGFloat, _ v: Variant, _ shape: Shape) {
    guard let ctx = NSGraphicsContext.current?.cgContext else { return }
    let inset = shape == .tile ? s * 0.1 : 0
    let box = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let u = box.width / 100
    let c = NSPoint(x: box.midX, y: box.midY)
    let dark = v == .dark
    let outline: NSBezierPath = shape == .circle ? NSBezierPath(ovalIn: box)
        : NSBezierPath(roundedRect: box, xRadius: box.width * 0.225, yRadius: box.width * 0.225)
    if shape == .tile {
        NSGraphicsContext.current?.saveGraphicsState()
        let sh = NSShadow(); sh.shadowBlurRadius = s * 0.02; sh.shadowOffset = NSSize(width: 0, height: -s * 0.01)
        sh.shadowColor = NSColor.black.withAlphaComponent(dark ? 0.35 : 0.22); sh.set()
        (dark ? rgb(6, 18, 38) : rgb(236, 244, 253)).setFill(); outline.fill()
        NSGraphicsContext.current?.restoreGraphicsState()
    }
    NSGraphicsContext.current?.saveGraphicsState()
    outline.addClip()
    let bgColors = dark ? [rgb(16, 44, 86), rgb(7, 20, 42), rgb(3, 10, 24)] : [rgb(255, 255, 255), rgb(236, 244, 253), rgb(214, 230, 248)]
    NSGradient(colors: bgColors, atLocations: [0, 0.55, 1], colorSpace: .sRGB)!
        .draw(fromCenter: c, radius: 0, toCenter: c, radius: box.width * 0.75, options: [.drawsAfterEndingLocation])
    // the window
    let fw = (shape == .circle ? 44 : 50) * u
    let frame = NSRect(x: c.x - fw / 2, y: c.y - fw / 2, width: fw, height: fw)
    let framePath = NSBezierPath(roundedRect: frame, xRadius: 7 * u, yRadius: 7 * u)
    NSGradient(colors: [rgb(60, 150, 230, 0.10), rgb(30, 90, 170, 0.04)])!.draw(in: framePath, angle: -90)
    framePath.lineWidth = (shape == .circle ? 4.2 : 3.4) * u
    (dark ? rgb(96, 160, 230, 0.8) : rgb(28, 78, 150, 0.9)).setStroke(); framePath.stroke()
    if !dark {                                            // panes only in the light variant
        let mull = NSBezierPath()
        mull.move(to: NSPoint(x: c.x, y: frame.minY + 2 * u)); mull.line(to: NSPoint(x: c.x, y: frame.maxY - 2 * u))
        mull.move(to: NSPoint(x: frame.minX + 2 * u, y: c.y)); mull.line(to: NSPoint(x: frame.maxX - 2 * u, y: c.y))
        mull.lineWidth = 1.5 * u
        rgb(28, 78, 150, 0.35).setStroke(); mull.stroke()
    }
    // beams
    ctx.setBlendMode(dark ? .plusLighter : .multiply)
    let beams: [(angle: CGFloat, length: CGFloat, width: CGFloat, alpha: CGFloat)] = [
        (90, 64, 16, 0.5), (270, 64, 16, 0.5), (57, 70, 9, 0.55), (123, 70, 9, 0.55), (237, 70, 9, 0.55), (303, 70, 9, 0.55),
        (90, 70, 3, 0.95), (270, 70, 3, 0.95), (57, 60, 2.2, 0.8), (123, 60, 2.2, 0.8), (237, 60, 2.2, 0.8), (303, 60, 2.2, 0.8)]
    for b in beams {
        let rad = b.angle * .pi / 180
        let dir = NSPoint(x: cos(rad), y: sin(rad)), nrm = NSPoint(x: -sin(rad), y: cos(rad))
        let end = NSPoint(x: c.x + dir.x * b.length * u, y: c.y + dir.y * b.length * u)
        let half = b.width * u / 2
        let p = NSBezierPath()
        p.move(to: NSPoint(x: c.x + nrm.x * 0.6 * u, y: c.y + nrm.y * 0.6 * u))
        p.line(to: NSPoint(x: end.x + nrm.x * half, y: end.y + nrm.y * half))
        p.line(to: NSPoint(x: end.x - nrm.x * half, y: end.y - nrm.y * half))
        p.line(to: NSPoint(x: c.x - nrm.x * 0.6 * u, y: c.y - nrm.y * 0.6 * u))
        p.close()
        NSGraphicsContext.current?.saveGraphicsState()
        p.addClip()
        let cols = dark
            ? [rgb(240, 253, 255, b.alpha), rgb(90, 215, 255, b.alpha * 0.55), rgb(40, 150, 255, b.alpha * 0.12), rgb(40, 150, 255, 0)]
            : [rgb(120, 200, 255, b.alpha), rgb(70, 160, 240, b.alpha * 0.6), rgb(70, 160, 240, b.alpha * 0.15), rgb(70, 160, 240, 0)]
        NSGradient(colors: cols, atLocations: [0, 0.25, 0.65, 1], colorSpace: .sRGB)!.draw(from: c, to: end, options: [])
        NSGraphicsContext.current?.restoreGraphicsState()
    }
    ctx.setBlendMode(dark ? .plusLighter : .normal)
    // the light: halo, then a white core
    NSGradient(colors: [rgb(110, 225, 255, 0.6), rgb(40, 170, 255, 0.22), rgb(20, 120, 255, 0)], atLocations: [0, 0.35, 1],
               colorSpace: .sRGB)!.draw(fromCenter: c, radius: 0, toCenter: c, radius: (dark ? 42 : 34) * u, options: [])
    NSGradient(colors: [rgb(255, 255, 255, 1), rgb(200, 250, 255, 0.95), rgb(90, 225, 255, 0)], atLocations: [0, 0.3, 1],
               colorSpace: .sRGB)!.draw(fromCenter: c, radius: 0, toCenter: c, radius: 11 * u, options: [])
    ctx.setBlendMode(.normal)
    NSGraphicsContext.current?.restoreGraphicsState()
    if shape == .circle && !dark {                        // a hairline so the light bubble shows on white windows
        outline.lineWidth = s * 0.012; rgb(28, 78, 150, 0.25).setStroke(); outline.stroke()
    }
}

func png(_ px: Int, _ v: Variant, _ shape: Shape) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4,
                               hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    draw(CGFloat(px), v, shape)
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let mode = CommandLine.arguments[1], out = URL(fileURLWithPath: CommandLine.arguments[2])
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
func write(_ d: Data, _ name: String) { try! d.write(to: out.appendingPathComponent(name)) }
switch mode {
case "iconset":
    for (name, px) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128), ("128x128@2x", 256),
                       ("256x256", 256), ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)] {
        write(png(px, .light, .tile), "icon_\(name).png")
    }
case "extension":
    for px in [16, 32, 48, 128] { write(png(px, .light, px >= 48 ? .tile : .square), "icon\(px).png") }
case "bubble":
    write(png(104, .light, .circle), "bubble-light.png"); write(png(104, .dark, .circle), "bubble-dark.png")
case "preview":
    write(png(1024, .light, .tile), "icon-light.png"); write(png(1024, .dark, .tile), "icon-dark.png")
    write(png(208, .light, .circle), "bubble-light.png"); write(png(208, .dark, .circle), "bubble-dark.png")
default:
    fatalError("mode: iconset | extension | bubble | preview")
}
