// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "TerminalTLDR",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "TerminalTLDR", targets: ["TerminalTLDR"])],
    targets: [.executableTarget(name: "TerminalTLDR")]
)
