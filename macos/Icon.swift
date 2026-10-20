import AppKit
let directory = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let p = CGFloat(pixels)
        NSColor(calibratedRed: 0.25, green: 0.20, blue: 0.50, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: p * 0.06, y: p * 0.06, width: p * 0.88, height: p * 0.88),
                     xRadius: p * 0.20, yRadius: p * 0.20).fill()
        NSColor(calibratedRed: 0.57, green: 0.48, blue: 0.96, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: p * 0.19, y: p * 0.24, width: p * 0.62, height: p * 0.49),
                     xRadius: p * 0.065, yRadius: p * 0.065).fill()
        NSColor(calibratedRed: 0.12, green: 0.10, blue: 0.24, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: p * 0.215, y: p * 0.265, width: p * 0.57, height: p * 0.44),
                     xRadius: p * 0.045, yRadius: p * 0.045).fill()
        NSColor.white.setStroke()
        let path = NSBezierPath()
        path.lineWidth = p * 0.04
        path.lineCapStyle = .round
        path.lineJoinStyle = .round
        path.move(to: NSPoint(x: p * 0.31, y: p * 0.57))
        path.line(to: NSPoint(x: p * 0.42, y: p * 0.48))
        path.line(to: NSPoint(x: p * 0.31, y: p * 0.39))
        path.move(to: NSPoint(x: p * 0.51, y: p * 0.39))
        path.line(to: NSPoint(x: p * 0.67, y: p * 0.39))
        path.stroke()
        image.unlockFocus()
        let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let filename = "icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(filename))
    }
}
