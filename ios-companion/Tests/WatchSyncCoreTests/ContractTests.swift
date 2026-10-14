import Foundation
import Testing
@testable import WatchSyncCore

@Test func allMetricSamplesUseExactUnitsAndLowercaseOriginalUUIDs() throws {
    for (index, metric) in WatchMetric.allCases.enumerated() {
        let sample = try syntheticSample(metric, id: index + 10, value: metric == .oxygenSaturation ? 0.97 : 12)
        try sample.validate(sourceBundle: syntheticBundle)
        #expect(sample.unit == metric.unit)
        #expect(sample.id == syntheticUUID(index + 10).uuidString.lowercased())
        let scope = try syntheticScope(metric)
        let batch = try syntheticBatch(scope: scope, samples: [sample])
        try batch.validate()
        let decoded = try JSONDecoder().decode(WatchBatch.self, from: batch.encoded())
        #expect(decoded == batch)
        #expect(batch.streamID == syntheticUUID(1).uuidString.lowercased())
    }
}

@Test func missingTimezoneUsesUTCReferenceAndRecordedDSTKeepsOffsets() throws {
    let missing = try syntheticSample()
    #expect(missing.timezone == "UTC")
    #expect(missing.timezoneProvenance == .utcReference)
    let recordedUTC = try WatchSample(id: syntheticUUID(11), type: .heartRate, start: syntheticNow, end: syntheticNow,
                                      recordedTimezone: "UTC", source: syntheticSource, value: .quantity(60))
    #expect(recordedUTC.timezoneProvenance == .recorded)
    let sample = try WatchSample(id: syntheticUUID(10), type: .sleepAnalysis,
                                 start: WireDate.parse("2031-03-30T00:30:00Z")!,
                                 end: WireDate.parse("2031-03-30T02:30:00Z")!,
                                 recordedTimezone: "Europe/Prague", source: syntheticSource, value: .stage("deep"))
    #expect(sample.start.hasSuffix("+01:00"))
    #expect(sample.end.hasSuffix("+02:00"))
    #expect(sample.timezoneProvenance == .recorded)
    try sample.validate(sourceBundle: syntheticBundle)
}

@Test func invalidTimezoneOffsetsOrProvenanceAreRejected() throws {
    #expect(throws: CaptureError.invalidContract) {
        try WatchSample(id: syntheticUUID(2), type: .heartRate, start: syntheticNow, end: syntheticNow,
                        recordedTimezone: "Unknown/Invented", source: syntheticSource, value: .quantity(60))
    }
    var sample = try syntheticSample()
    sample.timezone = "Europe/Prague"
    #expect(throws: CaptureError.invalidContract) { try sample.validate(sourceBundle: syntheticBundle) }
    sample = try syntheticSample(); sample.start = "2031-03-11T11:00:00+01:00"
    #expect(throws: CaptureError.invalidContract) { try sample.validate(sourceBundle: syntheticBundle) }
}

@Test func wrongUnitSourcePrivateDeviceIDAndNonfiniteValuesAreRejected() throws {
    var sample = try syntheticSample(); sample.unit = "RMSSD"
    #expect(throws: CaptureError.invalidContract) { try sample.validate(sourceBundle: syntheticBundle) }
    sample = try syntheticSample(); sample.source.bundleID = "com.example.other"
    #expect(throws: CaptureError.invalidContract) { try sample.validate(sourceBundle: syntheticBundle) }
    sample = try syntheticSample(); sample.source.device["local_identifier"] = "private-device"
    #expect(throws: CaptureError.invalidContract) { try sample.validate(sourceBundle: syntheticBundle) }
    for invalid in [Double.nan, Double.infinity, -1] {
        #expect(throws: CaptureError.invalidContract) { try syntheticSample(value: invalid) }
    }
    #expect(throws: CaptureError.invalidContract) { try syntheticSample(.oxygenSaturation, value: 97) }
    #expect(throws: CaptureError.invalidContract) { try syntheticSample(.stepCount, value: 2.5) }
}

@Test func canonicalUUIDUnsupportedLabAndDuplicateOperationsAreRejected() throws {
    var batch = try syntheticBatch(); batch.batchID = batch.batchID.uppercased().replacingOccurrences(of: "10000000", with: "ABCDEF00")
    #expect(throws: CaptureError.invalidContract) { try batch.validate() }
    let payload = String(data: try syntheticBatch().encoded(), encoding: .utf8)!.replacingOccurrences(of: "resting_heart_rate", with: "vitamin_d")
    #expect(throws: (any Error).self) { try JSONDecoder().decode(WatchBatch.self, from: Data(payload.utf8)) }
    batch = try syntheticBatch(); batch.types[0].deletedIDs = [batch.types[0].samples[0].id]
    #expect(throws: CaptureError.invalidContract) { try batch.validate() }
}

@Test func unknownFailedAndNilAnchorReadsCannotAdvance() throws {
    var batch = try syntheticBatch(samples: []); batch.types[0].nextAnchor = nil
    #expect(throws: CaptureError.invalidContract) { try batch.validate() }
    batch.types[0].readState = .unknown; try batch.validate()
    batch.types[0].nextAnchor = "invented"
    #expect(throws: CaptureError.invalidContract) { try batch.validate() }
    batch.types[0].readState = .error; batch.types[0].nextAnchor = nil; batch.types[0].error = "generic_read_failure"
    try batch.validate()
    batch.types[0].error = "private/path"
    #expect(throws: CaptureError.invalidContract) { try batch.validate() }
}

@Test func individualSampleLimitsAndIntervalsAreEnforced() throws {
    #expect(throws: CaptureError.invalidContract) {
        try syntheticSample(.sleepAnalysis, start: syntheticNow, end: syntheticNow)
    }
    #expect(throws: CaptureError.invalidContract) {
        try syntheticSample(start: syntheticNow, end: syntheticNow.addingTimeInterval(-1))
    }
    #expect(throws: CaptureError.invalidContract) {
        try syntheticSample(start: syntheticNow, end: syntheticNow.addingTimeInterval(36 * 3600 + 1))
    }
    let samples = try (0...2_000).map { try syntheticSample(id: $0 + 100) }
    let batch = try syntheticBatch(samples: samples)
    #expect(throws: CaptureError.resourceLimit) { try batch.validate() }
}

@Test func nullableWireAnchorsAreRequiredJSONKeysAndOriginalLetterUUIDsAreLowercased() throws {
    let id = UUID(uuidString: "ABCDEF00-ABCD-4ABC-8ABC-ABCDEFABCDEF")!
    let scope = try QueryScope(streamID: id, sourceBundleID: syntheticBundle, metric: .heartRate,
                               startDate: syntheticNow.addingTimeInterval(-86_400))
    let sample = try WatchSample(id: id, type: .heartRate, start: syntheticNow, end: syntheticNow,
                                 source: syntheticSource, value: .quantity(60))
    let batch = WatchBatch(batchID: id, mode: .synthetic, scope: scope, generatedAt: syntheticNow,
                           types: [QueryDelta(type: .heartRate, previousAnchor: nil, nextAnchor: "a1", readState: .nonempty, samples: [sample])])
    let object = try JSONSerialization.jsonObject(with: batch.encoded()) as! [String: Any]
    let query = (object["types"] as! [[String: Any]])[0]
    #expect(query["previous_anchor"] is NSNull)
    #expect(query["next_anchor"] as? String == "a1")
    #expect(sample.id == "abcdef00-abcd-4abc-8abc-abcdefabcdef")
    #expect(batch.streamID == sample.id)
    #expect(batch.batchID == sample.id)
    let unknown = QueryDelta(type: .heartRate, previousAnchor: nil, nextAnchor: nil, readState: .unknown)
    let unknownJSON = try JSONSerialization.jsonObject(with: WireJSON.encode(unknown)) as! [String: Any]
    #expect(unknownJSON["previous_anchor"] is NSNull)
    #expect(unknownJSON["next_anchor"] is NSNull)
}
