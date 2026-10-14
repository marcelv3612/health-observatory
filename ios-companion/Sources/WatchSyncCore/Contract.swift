import Foundation

public enum CaptureError: String, Error, Codable, Sendable {
    case invalidContract = "invalid_contract"
    case scopeConflict = "scope_conflict"
    case staleAnchor = "stale_anchor"
    case replayConflict = "replay_conflict"
    case resourceLimit = "resource_limit"
    case reconciliationRequired = "reconciliation_required"
    case protectedDataUnavailable = "protected_data_unavailable"
    case readVisibilityUnknown = "read_visibility_unknown"
    case readerUnavailable = "reader_unavailable"
    case localStorageError = "local_storage_error"
    case storageConflict = "storage_conflict"
    case cancelled
}

public enum WatchMetric: String, Codable, CaseIterable, Sendable {
    case sleepAnalysis = "sleep_analysis", heartRate = "heart_rate"
    case restingHeartRate = "resting_heart_rate", hrvSDNN = "hrv_sdnn"
    case stepCount = "step_count", activeEnergy = "active_energy"
    case appleExerciseTime = "apple_exercise_time", respiratoryRate = "respiratory_rate"
    case oxygenSaturation = "oxygen_saturation"
    case sleepingWristTemperature = "sleeping_wrist_temperature", vo2Max = "vo2_max"

    public var unit: String {
        switch self {
        case .sleepAnalysis: "category"
        case .heartRate, .restingHeartRate, .respiratoryRate: "count/min"
        case .hrvSDNN: "ms"
        case .stepCount: "count"
        case .activeEnergy: "kcal"
        case .appleExerciseTime: "min"
        case .oxygenSaturation: "fraction"
        case .sleepingWristTemperature: "degC"
        case .vo2Max: "mL/kg/min"
        }
    }

    public var label: String {
        switch self {
        case .sleepAnalysis: "Sleep stages"
        case .heartRate: "Heart rate"
        case .restingHeartRate: "Resting heart rate"
        case .hrvSDNN: "HRV (SDNN)"
        case .stepCount: "Steps"
        case .activeEnergy: "Active energy"
        case .appleExerciseTime: "Exercise minutes"
        case .respiratoryRate: "Respiratory rate"
        case .oxygenSaturation: "Oxygen saturation (if available)"
        case .sleepingWristTemperature: "Sleeping wrist temperature (absolute, if available)"
        case .vo2Max: "VO₂ max estimate (if available)"
        }
    }
}

public enum DatasetMode: String, Codable, Sendable { case synthetic, device }
public enum TimezoneProvenance: String, Codable, Sendable {
    case recorded, utcReference = "utc_reference"
}
public enum ReadState: String, Codable, Sendable { case unknown, empty, nonempty, error }
public enum QueryKind: String, Codable, Sendable { case baseline, incremental }

public enum SampleValue: Codable, Equatable, Sendable {
    case quantity(Double), stage(String)
    public init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let text = try? container.decode(String.self) { self = .stage(text) }
        else { self = .quantity(try container.decode(Double.self)) }
    }
    public func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .quantity(let value): try container.encode(value)
        case .stage(let value): try container.encode(value)
        }
    }
}

public struct SampleSource: Codable, Equatable, Sendable {
    public var bundleID: String
    public var name: String
    public var version: String
    public var device: [String: String]
    public init(bundleID: String, name: String, version: String = "", device: [String: String] = [:]) {
        self.bundleID = bundleID; self.name = name; self.version = version; self.device = device
    }
    enum CodingKeys: String, CodingKey { case bundleID = "bundle_id", name, version, device }
}

public struct WatchSample: Codable, Equatable, Sendable {
    public var id: String
    public var type: WatchMetric
    public var start: String
    public var end: String
    public var timezone: String
    public var endTimezone: String
    public var timezoneProvenance: TimezoneProvenance
    public var source: SampleSource
    public var unit: String
    public var value: SampleValue

    public init(id: UUID, type: WatchMetric, start: Date, end: Date,
                recordedTimezone: String? = nil, source: SampleSource, value: SampleValue) throws {
        // The phone's current timezone is never a substitute for original sample metadata.
        let zone: TimeZone
        if let recordedTimezone {
            guard (recordedTimezone == "UTC" || TimeZone.knownTimeZoneIdentifiers.contains(recordedTimezone)),
                  let parsed = TimeZone(identifier: recordedTimezone) else { throw CaptureError.invalidContract }
            zone = parsed; timezone = recordedTimezone; timezoneProvenance = .recorded
        } else {
            zone = TimeZone(secondsFromGMT: 0)!; timezone = "UTC"; timezoneProvenance = .utcReference
        }
        self.id = id.uuidString.lowercased(); self.type = type
        self.start = WireDate.format(start, timezone: zone); self.end = WireDate.format(end, timezone: zone)
        endTimezone = timezone; self.source = source; unit = type.unit; self.value = value
        try validate(sourceBundle: source.bundleID)
    }
    enum CodingKeys: String, CodingKey {
        case id, type, start, end, timezone, source, unit, value
        case endTimezone = "end_timezone", timezoneProvenance = "timezone_provenance"
    }

    public func validate(sourceBundle: String) throws {
        try Contract.uuid(id); try Contract.bundle(sourceBundle)
        guard source.bundleID == sourceBundle, unit == type.unit else { throw CaptureError.invalidContract }
        try Contract.text(source.name, max: 200); try Contract.text(source.version, max: 100, empty: true)
        let deviceKeys: Set<String> = ["manufacturer", "model", "hardware_version", "software_version"]
        guard Set(source.device.keys).isSubset(of: deviceKeys) else { throw CaptureError.invalidContract }
        for value in source.device.values { try Contract.text(value, max: 100, empty: true) }
        guard let startDate = WireDate.parse(start), let endDate = WireDate.parse(end) else {
            throw CaptureError.invalidContract
        }
        let elapsed = endDate.timeIntervalSince(startDate)
        guard elapsed >= 0, elapsed <= 36 * 3600 else { throw CaptureError.invalidContract }
        if [.sleepAnalysis, .stepCount, .activeEnergy, .appleExerciseTime].contains(type), elapsed == 0 {
            throw CaptureError.invalidContract
        }
        try WireDate.validateOffset(start, zone: timezone)
        try WireDate.validateOffset(end, zone: endTimezone)
        if timezoneProvenance == .utcReference,
           (timezone != "UTC" || endTimezone != "UTC" || !start.hasSuffix("Z") || !end.hasSuffix("Z")) {
            throw CaptureError.invalidContract
        }
        switch (type, value) {
        case (.sleepAnalysis, .stage(let stage)):
            guard ["inbed", "awake", "unspecified", "core", "deep", "rem"].contains(stage) else {
                throw CaptureError.invalidContract
            }
        case (.sleepAnalysis, _), (_, .stage): throw CaptureError.invalidContract
        case (_, .quantity(let number)):
            let min = type == .sleepingWristTemperature ? -273.15 : 0
            let max = type == .sleepingWristTemperature ? 100 : type == .oxygenSaturation ? 1 : 1e9
            guard number.isFinite, number >= min, number <= max,
                  type != .stepCount || number.rounded() == number else { throw CaptureError.invalidContract }
        }
    }
}

/// Immutable predicate: original stream, bundle, type and absolute initial start boundary.
/// This boundary never rolls forward; retention is independent of the HealthKit query.
public struct QueryScope: Codable, Equatable, Sendable {
    public let streamID: String
    public let sourceBundleID: String
    public let metric: WatchMetric
    public let startDate: Date
    public init(streamID: UUID, sourceBundleID: String, metric: WatchMetric, startDate: Date) throws {
        self.streamID = streamID.uuidString.lowercased(); self.sourceBundleID = sourceBundleID
        self.metric = metric; self.startDate = startDate; try validate()
    }
    public var key: String { "\(streamID)|\(sourceBundleID)|\(metric.rawValue)" }
    public func validate() throws {
        try Contract.uuid(streamID); try Contract.bundle(sourceBundleID)
        guard startDate.timeIntervalSince1970.isFinite,
              (2000...2100).contains(Calendar(identifier: .gregorian).component(.year, from: startDate)) else {
            throw CaptureError.invalidContract
        }
    }
}

public struct QueryDelta: Codable, Equatable, Sendable {
    public var type: WatchMetric
    public var kind: QueryKind
    public var previousAnchor: String?
    public var nextAnchor: String?
    public var readState: ReadState
    public var samples: [WatchSample]
    public var deletedIDs: [String]
    public var error: String?
    public init(type: WatchMetric, previousAnchor: String?, nextAnchor: String?, readState: ReadState,
                samples: [WatchSample] = [], deletedIDs: [String] = [], error: String? = nil) {
        self.type = type; kind = previousAnchor == nil ? .baseline : .incremental
        self.previousAnchor = previousAnchor; self.nextAnchor = nextAnchor
        self.readState = readState; self.samples = samples; self.deletedIDs = deletedIDs; self.error = error
    }
    enum CodingKeys: String, CodingKey {
        case type, kind, samples, error
        case previousAnchor = "previous_anchor", nextAnchor = "next_anchor"
        case readState = "read_state", deletedIDs = "deleted_ids"
    }
    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(type, forKey: .type); try container.encode(kind, forKey: .kind)
        if let previousAnchor { try container.encode(previousAnchor, forKey: .previousAnchor) }
        else { try container.encodeNil(forKey: .previousAnchor) }
        if let nextAnchor { try container.encode(nextAnchor, forKey: .nextAnchor) }
        else { try container.encodeNil(forKey: .nextAnchor) }
        try container.encode(readState, forKey: .readState)
        try container.encode(samples, forKey: .samples); try container.encode(deletedIDs, forKey: .deletedIDs)
        try container.encodeIfPresent(error, forKey: .error)
    }
}

public struct WatchBatch: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public var batchID: String
    public let mode: DatasetMode
    public let streamID: String
    public let sourceBundleID: String
    public let generatedAt: String
    public var types: [QueryDelta]
    public init(batchID: UUID = UUID(), mode: DatasetMode, scope: QueryScope, generatedAt: Date, types: [QueryDelta]) {
        schemaVersion = 1; self.batchID = batchID.uuidString.lowercased(); self.mode = mode
        streamID = scope.streamID; sourceBundleID = scope.sourceBundleID
        self.generatedAt = WireDate.format(generatedAt); self.types = types
    }
    enum CodingKeys: String, CodingKey {
        case mode, types
        case schemaVersion = "schema_version", batchID = "batch_id", streamID = "stream_id"
        case sourceBundleID = "source_bundle_id", generatedAt = "generated_at"
    }
    public func encoded() throws -> Data { try WireJSON.encode(self) }
    public func validate() throws {
        guard schemaVersion == 1, !types.isEmpty, types.count <= WatchMetric.allCases.count,
              WireDate.parse(generatedAt) != nil else { throw CaptureError.invalidContract }
        try Contract.uuid(batchID); try Contract.uuid(streamID); try Contract.bundle(sourceBundleID)
        var metrics = Set<WatchMetric>(), ids = Set<String>(), count = 0
        for query in types {
            guard metrics.insert(query.type).inserted else { throw CaptureError.invalidContract }
            try Contract.anchor(query.previousAnchor); try Contract.anchor(query.nextAnchor)
            if query.kind == .baseline, query.previousAnchor != nil { throw CaptureError.invalidContract }
            switch query.readState {
            case .unknown, .error:
                guard query.samples.isEmpty, query.deletedIDs.isEmpty, query.previousAnchor == query.nextAnchor,
                      (query.readState == .error) == (query.error != nil) else { throw CaptureError.invalidContract }
            case .empty, .nonempty:
                guard query.nextAnchor != nil, query.error == nil,
                      (query.readState == .nonempty) == !query.samples.isEmpty,
                      query.kind != .baseline || query.deletedIDs.isEmpty else { throw CaptureError.invalidContract }
            }
            if let error = query.error {
                guard error.range(of: "^[a-z0-9_]{1,64}$", options: .regularExpression) != nil else {
                    throw CaptureError.invalidContract
                }
            }
            for sample in query.samples {
                guard sample.type == query.type, ids.insert(sample.id).inserted else { throw CaptureError.invalidContract }
                try sample.validate(sourceBundle: sourceBundleID)
            }
            for id in query.deletedIDs {
                try Contract.uuid(id); guard ids.insert(id).inserted else { throw CaptureError.invalidContract }
            }
            count += query.samples.count + query.deletedIDs.count
        }
        guard count <= 2_000, try encoded().count <= 1_048_576 else { throw CaptureError.resourceLimit }
    }
}

public enum WireJSON {
    public static func encode<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(value)
    }
}

public enum WireDate {
    public static func format(_ date: Date, timezone: TimeZone = TimeZone(secondsFromGMT: 0)!) -> String {
        let formatter = ISO8601DateFormatter(); formatter.timeZone = timezone
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }
    public static func parse(_ text: String) -> Date? {
        // ISO8601DateFormatter can normalize impossible dates and times. Validate
        // every original encoded-clock component before constructing the instant.
        guard text.range(of: "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$", options: .regularExpression) != nil else { return nil }
        let bytes = Array(text.utf8)
        func number(_ range: Range<Int>) -> Int {
            range.reduce(0) { $0 * 10 + Int(bytes[$1] - 48) }
        }
        let year = number(0..<4), month = number(5..<7), day = number(8..<10)
        let hour = number(11..<13), minute = number(14..<16), second = number(17..<19)
        guard (2000...2100).contains(year), (1...12).contains(month), (1...31).contains(day),
              hour < 24, minute < 60, second < 60 else { return nil }
        let zoneStart: Int, offset: Int
        if bytes.last == 90 { zoneStart = bytes.count - 1; offset = 0 }
        else {
            zoneStart = bytes.count - 6
            let zoneHour = number((zoneStart + 1)..<(zoneStart + 3))
            let zoneMinute = number((zoneStart + 4)..<(zoneStart + 6))
            guard zoneHour <= 14, zoneMinute < 60 else { return nil }
            offset = (bytes[zoneStart] == 45 ? -1 : 1) * (zoneHour * 3600 + zoneMinute * 60)
            guard abs(offset) <= 14 * 3600 else { return nil }
        }
        let nanosecond: Int
        if zoneStart > 19 {
            let digits = (zoneStart - 20)
            nanosecond = number(20..<zoneStart) * (0..<(9 - digits)).reduce(1) { value, _ in value * 10 }
        } else { nanosecond = 0 }
        guard let timezone = TimeZone(secondsFromGMT: offset) else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone; calendar.locale = Locale(identifier: "en_US_POSIX")
        var components = DateComponents()
        components.timeZone = timezone; components.year = year; components.month = month; components.day = day
        components.hour = hour; components.minute = minute; components.second = second; components.nanosecond = nanosecond
        guard components.isValidDate(in: calendar) else { return nil }
        return calendar.date(from: components)
    }
    static func validateOffset(_ text: String, zone: String) throws {
        guard (zone == "UTC" || TimeZone.knownTimeZoneIdentifiers.contains(zone)),
              let timeZone = TimeZone(identifier: zone), let date = parse(text) else { throw CaptureError.invalidContract }
        let supplied: Int
        if text.hasSuffix("Z") { supplied = 0 }
        else {
            let offset = String(text.suffix(6)); let parts = offset.dropFirst().split(separator: ":")
            guard parts.count == 2, let hours = Int(parts[0]), let minutes = Int(parts[1]),
                  hours <= 14, minutes < 60 else { throw CaptureError.invalidContract }
            supplied = (offset.first == "-" ? -1 : 1) * (hours * 3600 + minutes * 60)
        }
        guard abs(supplied) <= 14 * 3600, timeZone.secondsFromGMT(for: date) == supplied else {
            throw CaptureError.invalidContract
        }
    }
}

enum Contract {
    static func text(_ value: String, max: Int, empty: Bool = false) throws {
        guard (empty || !value.isEmpty), value.utf8.count <= max,
              !value.unicodeScalars.contains(where: { $0.value < 32 }) else { throw CaptureError.invalidContract }
    }
    static func uuid(_ value: String) throws {
        guard let parsed = UUID(uuidString: value), parsed.uuidString.lowercased() == value,
              value != "00000000-0000-0000-0000-000000000000" else { throw CaptureError.invalidContract }
    }
    static func bundle(_ value: String) throws {
        guard value.contains("."), value.range(of: "^[A-Za-z0-9][A-Za-z0-9.-]{0,199}$", options: .regularExpression) != nil else {
            throw CaptureError.invalidContract
        }
    }
    static func anchor(_ value: String?) throws {
        if let value { try text(value, max: 2048) }
    }
}
