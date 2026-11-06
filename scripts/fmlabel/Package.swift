// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "fmlabel",
    platforms: [
        .macOS("27.0")
    ],
    targets: [
        .executableTarget(name: "fmlabel")
    ]
)
