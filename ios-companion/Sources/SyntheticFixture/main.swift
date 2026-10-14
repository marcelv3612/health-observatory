import Foundation
import WatchSyncCore

// This executable has no HealthKit dependency and emits ONLY these invented records.
// It is not a device exporter; no source file, argument or environment variable can
// replace this fixture with private records.
let start = WireDate.parse("2031-03-11T06:00:00Z")!
let end = WireDate.parse("2031-03-11T06:05:00Z")!
let stream = UUID(uuidString: "10000000-0000-4000-8000-000000000001")!
let source = SampleSource(bundleID: "com.example.sleepstudio.synthetic", name: "Invented Watch", version: "0",
                          device: ["manufacturer": "Synthetic", "model": "Fixture"])
let scope = try QueryScope(streamID: stream, sourceBundleID: source.bundleID, metric: .restingHeartRate,
                           startDate: WireDate.parse("2031-03-04T00:00:00Z")!)
let sample = try WatchSample(id: UUID(uuidString: "10000000-0000-4000-8000-000000000002")!, type: .restingHeartRate,
                             start: start, end: end, source: source, value: .quantity(62))
let batch = WatchBatch(batchID: UUID(uuidString: "10000000-0000-4000-8000-000000000003")!, mode: .synthetic,
                       scope: scope, generatedAt: WireDate.parse("2031-03-11T07:00:00Z")!,
                       types: [QueryDelta(type: .restingHeartRate, previousAnchor: nil,
                                          nextAnchor: "synthetic-anchor-1", readState: .nonempty, samples: [sample])])
try batch.validate()
FileHandle.standardOutput.write(try batch.encoded())
FileHandle.standardOutput.write(Data([10]))
