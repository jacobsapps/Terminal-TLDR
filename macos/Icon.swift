import AppKit

// The source is the transparent mark. Add a Dock tile here, not in the artwork,
// so the in-app logo can use the same mark without a padded white background.
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
        let tile = NSBezierPath(roundedRect: bounds, xRadius: p * 0.20, yRadius: p * 0.20)
        NSColor(srgbRed: 1, green: 0.98, blue: 0.94, alpha: 1).setFill()
        tile.fill()
        tile.addClip()
        // The source has only ~3% side padding. Do not add another inner inset.
        artwork.draw(in: bounds, from: .zero, operation: .sourceOver, fraction: 1)
        NSGraphicsContext.restoreGraphicsState()
        let filename = "icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(filename))
    }
}
