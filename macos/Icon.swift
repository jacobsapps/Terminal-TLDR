import AppKit

// Package the selected Tab Bookmark artwork at every native macOS icon size.
let directory = URL(fileURLWithPath: CommandLine.arguments[1])
guard let artwork = NSImage(contentsOfFile: "macos/AppIcon.png") else {
    fatalError("Missing selected Tab Bookmark artwork")
}
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                                      bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                      isPlanar: false, colorSpaceName: .deviceRGB,
                                      bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        NSGraphicsContext.current?.imageInterpolation = .high
        let p = CGFloat(pixels)
        let bounds = NSRect(x: p * 0.05, y: p * 0.05, width: p * 0.90, height: p * 0.90)
        NSBezierPath(roundedRect: bounds, xRadius: p * 0.20, yRadius: p * 0.20).addClip()
        artwork.draw(in: bounds, from: .zero, operation: .copy, fraction: 1)
        NSGraphicsContext.restoreGraphicsState()
        let filename = "icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(filename))
    }
}
