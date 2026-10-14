// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "SleepStudioCompanion",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "WatchSyncCore", targets: ["WatchSyncCore"]),
        .library(name: "WatchHealthKit", targets: ["WatchHealthKit"]),
        .executable(name: "synthetic-watch-fixture", targets: ["SyntheticFixture"])
    ],
    dependencies: [],
    targets: [
        .target(name: "WatchSyncCore"),
        .target(name: "WatchHealthKit", dependencies: ["WatchSyncCore"]),
        .executableTarget(name: "SyntheticFixture", dependencies: ["WatchSyncCore"]),
        .testTarget(name: "WatchSyncCoreTests", dependencies: ["WatchSyncCore"])
    ]
)
