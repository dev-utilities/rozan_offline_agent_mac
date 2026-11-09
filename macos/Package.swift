// swift-tools-version:5.9
// The Personal Agent shell: floating bubble, modal and full-app window around the Python core.
// Build the .app with scripts/build_mac_app.sh.
import PackageDescription

let package = Package(
    name: "PersonalAgent",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "PersonalAgent",
            path: "Sources/PersonalAgent",
            linkerSettings: [.linkedFramework("WebKit"), .linkedFramework("Carbon"),
                             .linkedFramework("UserNotifications"),
                             .linkedFramework("Speech"), .linkedFramework("AVFoundation"),
                             .linkedFramework("ServiceManagement")]
        ),
    ]
)
