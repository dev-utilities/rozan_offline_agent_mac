// Test helper: synthetic mouse events in screen points (top-left origin).
//   swift scripts/mouse.swift click X Y | rclick X Y | drag X1 Y1 X2 Y2 | move X Y
import CoreGraphics
import Foundation

let a = CommandLine.arguments.dropFirst().map { $0 }
func pt(_ i: Int) -> CGPoint { CGPoint(x: Double(a[i])!, y: Double(a[i + 1])!) }
func post(_ t: CGEventType, _ p: CGPoint, _ b: CGMouseButton = .left) {
    CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: p, mouseButton: b)?.post(tap: .cghidEventTap)
    usleep(60_000)
}
switch a.first {
case "click": let p = pt(1); post(.mouseMoved, p); post(.leftMouseDown, p); post(.leftMouseUp, p)
case "rclick": let p = pt(1); post(.mouseMoved, p); post(.rightMouseDown, p, .right); post(.rightMouseUp, p, .right)
case "move": post(.mouseMoved, pt(1))
case "drag":
    let s = pt(1), e = pt(3)
    post(.mouseMoved, s); post(.leftMouseDown, s)
    for i in 1...12 {
        let f = Double(i) / 12
        post(.leftMouseDragged, CGPoint(x: s.x + (e.x - s.x) * f, y: s.y + (e.y - s.y) * f))
    }
    post(.leftMouseUp, e)
default: print("usage: click|rclick|move|drag")
}
