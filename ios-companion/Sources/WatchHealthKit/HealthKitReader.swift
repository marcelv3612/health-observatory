#if canImport(HealthKit) && os(iOS)
@preconcurrency import HealthKit
import UIKit
import Foundation
import WatchSyncCore

public struct HealthSourceOption: Identifiable, Hashable, Sendable {
    public let bundleID: String
    public let name: String
    public var id: String { bundleID }
}

public struct DeviceProtectedDataGate: ProtectedDataGate {
    public init() {}
    public func isAvailable() async -> Bool {
        await MainActor.run { UIApplication.shared.isProtectedDataAvailable }
    }
}

/// Source-only adapter: iOS SDK build and real-device QA remain required.
/// No network, upload endpoint, credential or authorization-status read is present.
@MainActor public final class HealthKitReader: HealthReader {
    private let healthStore = HKHealthStore()
    private var observers: [HKObserverQuery] = []
    public init() {}
    public var isAvailable: Bool { HKHealthStore.isHealthDataAvailable() }

    public static func sampleType(for metric: WatchMetric) -> HKSampleType? {
        switch metric {
        case .sleepAnalysis: HKObjectType.categoryType(forIdentifier: .sleepAnalysis)
        case .heartRate: HKObjectType.quantityType(forIdentifier: .heartRate)
        case .restingHeartRate: HKObjectType.quantityType(forIdentifier: .restingHeartRate)
        case .hrvSDNN: HKObjectType.quantityType(forIdentifier: .heartRateVariabilitySDNN)
        case .stepCount: HKObjectType.quantityType(forIdentifier: .stepCount)
        case .activeEnergy: HKObjectType.quantityType(forIdentifier: .activeEnergyBurned)
        case .appleExerciseTime: HKObjectType.quantityType(forIdentifier: .appleExerciseTime)
        case .respiratoryRate: HKObjectType.quantityType(forIdentifier: .respiratoryRate)
        case .oxygenSaturation: HKObjectType.quantityType(forIdentifier: .oxygenSaturation)
        case .sleepingWristTemperature:
            if #available(iOS 16.0, *) { HKObjectType.quantityType(forIdentifier: .appleSleepingWristTemperature) }
            else { nil }
        case .vo2Max: HKObjectType.quantityType(forIdentifier: .vo2Max)
        }
    }

    /// ONLY invoke from the explicit user button after choosing the minimal read types.
    /// Completion means the authorization workflow finished, never that reads were granted.
    public func requestSelectedReadWorkflow(metrics: Set<WatchMetric>) async throws {
        guard isAvailable, !metrics.isEmpty, UIApplication.shared.isProtectedDataAvailable else {
            throw CaptureError.protectedDataUnavailable
        }
        let types = Set<HKObjectType>(metrics.compactMap { Self.sampleType(for: $0) })
        guard types.count == metrics.count else { throw CaptureError.readerUnavailable }
        try await healthStore.requestAuthorization(toShare: Set<HKSampleType>(), read: types)
    }

    private func sources(for type: HKSampleType) async throws -> Set<HKSource> {
        let ticket = QueryTicket<VisibleSources>(healthStore: healthStore)
        let result = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
            let query = HKSourceQuery(sampleType: type, samplePredicate: nil) { _, sources, error in
                if let error { ticket.finish(.failure(Self.safeError(error))); return }
                ticket.finish(.success(VisibleSources(sources: sources ?? [])))
            }
            ticket.start(continuation, query: query)
            }
        } onCancel: { ticket.finish(.failure(.cancelled)) }
        return result.sources
    }

    /// Show source choices only after an explicit selected-read workflow action.
    /// These are source names/bundles, never sample values or device identifiers.
    public func visibleSourceOptions(metrics: Set<WatchMetric>) async throws -> [HealthSourceOption] {
        guard UIApplication.shared.isProtectedDataAvailable else { throw CaptureError.protectedDataUnavailable }
        var options: [String: HealthSourceOption] = [:]
        for metric in metrics {
            guard let type = Self.sampleType(for: metric) else { continue }
            for source in try await sources(for: type) {
                options[source.bundleIdentifier] = HealthSourceOption(bundleID: source.bundleIdentifier, name: source.name)
            }
        }
        return options.values.sorted { $0.bundleID < $1.bundleID }
    }

    private nonisolated static func safeError(_ error: any Error) -> CaptureError {
        let cocoa = error as NSError
        if cocoa.domain == HKErrorDomain && cocoa.code == HKError.Code.errorDatabaseInaccessible.rawValue {
            return .protectedDataUnavailable
        }
        return .readerUnavailable
    }

    public func read(scope: QueryScope, anchor: String?, limit: Int) async throws -> ReaderPage {
        guard isAvailable, UIApplication.shared.isProtectedDataAvailable else {
            throw CaptureError.protectedDataUnavailable
        }
        try scope.validate()
        guard (1...1_000).contains(limit), let type = Self.sampleType(for: scope.metric) else {
            throw CaptureError.invalidContract
        }
        let visibleSources = try await sources(for: type)
        let exactSources = Set(visibleSources.filter { $0.bundleIdentifier == scope.sourceBundleID })
        guard !exactSources.isEmpty else {
            // Source invisibility can mean no data or denied reads. Neither can be inferred.
            return ReaderPage(nextAnchor: anchor, visibility: .unknown)
        }
        let originalAnchor: HKQueryAnchor?
        if let anchor {
            guard let archive = Data(base64Encoded: anchor),
                  let restored = try NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: archive) else {
                throw CaptureError.invalidContract
            }
            originalAnchor = restored
        } else { originalAnchor = nil }
        let predicate = NSCompoundPredicate(andPredicateWithSubpredicates: [
            HKQuery.predicateForObjects(from: exactSources),
            HKQuery.predicateForSamples(withStart: scope.startDate, end: nil, options: .strictStartDate)
        ])
        guard UIApplication.shared.isProtectedDataAvailable else { throw CaptureError.protectedDataUnavailable }
        let ticket = QueryTicket<ReaderPage>(healthStore: healthStore)
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
            let query = HKAnchoredObjectQuery(type: type, predicate: predicate, anchor: originalAnchor, limit: limit) {
                _, samples, deletions, nextAnchor, error in
                if let error { ticket.finish(.failure(Self.safeError(error))); return }
                do {
                    guard let nextAnchor else {
                        guard (samples ?? []).isEmpty, (deletions ?? []).isEmpty else { throw CaptureError.readerUnavailable }
                        ticket.finish(.success(ReaderPage(nextAnchor: anchor, visibility: .unknown))); return
                    }
                    let mapped = try (samples ?? []).map { try Self.map($0, scope: scope) }
                    let deleted = (deletions ?? []).map { $0.uuid.uuidString.lowercased() }
                    guard mapped.count + deleted.count <= 2_000 else { throw CaptureError.resourceLimit }
                    let encodedAnchor = try NSKeyedArchiver.archivedData(withRootObject: nextAnchor, requiringSecureCoding: true).base64EncodedString()
                    guard encodedAnchor.utf8.count <= 2_048 else { throw CaptureError.resourceLimit }
                    ticket.finish(.success(ReaderPage(samples: mapped, deletedIDs: deleted, nextAnchor: encodedAnchor,
                                                             visibility: mapped.isEmpty ? .empty : .nonempty,
                                                             hasMore: mapped.count + deleted.count >= limit)))
                } catch { ticket.finish(.failure((error as? CaptureError) ?? CaptureError.readerUnavailable)) }
            }
            ticket.start(continuation, query: query)
            }
        } onCancel: { ticket.finish(.failure(.cancelled)) }
    }

    private nonisolated static func map(_ sample: HKSample, scope: QueryScope) throws -> WatchSample {
        guard sample.sourceRevision.source.bundleIdentifier == scope.sourceBundleID,
              sample.startDate >= scope.startDate else { throw CaptureError.scopeConflict }
        let revision = sample.sourceRevision
        var device: [String: String] = [:]
        if let value = sample.device?.manufacturer { device["manufacturer"] = value }
        if let value = sample.device?.model { device["model"] = value }
        if let value = sample.device?.hardwareVersion { device["hardware_version"] = value }
        if let value = sample.device?.softwareVersion { device["software_version"] = value }
        let source = SampleSource(bundleID: revision.source.bundleIdentifier, name: revision.source.name,
                                  version: revision.version ?? "", device: device)
        let recordedTimezone: String?
        if let metadata = sample.metadata?[HKMetadataKeyTimeZone] {
            guard let text = metadata as? String,
                  (text == "UTC" || TimeZone.knownTimeZoneIdentifiers.contains(text)) else { throw CaptureError.invalidContract }
            recordedTimezone = text
        } else { recordedTimezone = nil }
        let value: SampleValue
        if scope.metric == .sleepAnalysis {
            guard let category = sample as? HKCategorySample else { throw CaptureError.invalidContract }
            switch category.value {
            case HKCategoryValueSleepAnalysis.inBed.rawValue: value = .stage("inbed")
            case HKCategoryValueSleepAnalysis.awake.rawValue: value = .stage("awake")
            case HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue: value = .stage("unspecified")
            case HKCategoryValueSleepAnalysis.asleepCore.rawValue: value = .stage("core")
            case HKCategoryValueSleepAnalysis.asleepDeep.rawValue: value = .stage("deep")
            case HKCategoryValueSleepAnalysis.asleepREM.rawValue: value = .stage("rem")
            default: throw CaptureError.invalidContract
            }
        } else {
            guard let quantity = sample as? HKQuantitySample else { throw CaptureError.invalidContract }
            let unit: HKUnit
            switch scope.metric {
            case .heartRate, .restingHeartRate, .respiratoryRate: unit = HKUnit.count().unitDivided(by: .minute())
            case .hrvSDNN: unit = HKUnit.secondUnit(with: .milli)
            case .stepCount: unit = .count()
            case .activeEnergy: unit = .kilocalorie()
            case .appleExerciseTime: unit = .minute()
            case .oxygenSaturation: unit = .percent() // HealthKit's dimensionless percent unit yields a fraction.
            case .sleepingWristTemperature: unit = .degreeCelsius()
            case .vo2Max:
                unit = HKUnit.literUnit(with: .milli).unitDivided(by: HKUnit.gramUnit(with: .kilo)).unitDivided(by: .minute())
            case .sleepAnalysis: throw CaptureError.invalidContract
            }
            guard quantity.quantity.is(compatibleWith: unit) else { throw CaptureError.invalidContract }
            value = .quantity(quantity.quantity.doubleValue(for: unit))
        }
        return try WatchSample(id: sample.uuid, type: scope.metric, start: sample.startDate, end: sample.endDate,
                               recordedTimezone: recordedTimezone, source: source, value: value)
    }

    /// Install after restoring a prior explicit local opt-in. Best effort, never wake-exact.
    /// Each callback completes after bounded LOCAL handling, independent of any recipient.
    public func startObserving(scopes: [QueryScope], coordinator: CaptureCoordinator,
                               shouldContinue: @MainActor @Sendable () -> Bool = { true }) async throws {
        guard shouldContinue() else { throw CaptureError.cancelled }
        stopObserving()
        for metric in Set(scopes.map(\.metric)) {
            guard shouldContinue() else { stopObserving(); throw CaptureError.cancelled }
            guard let type = Self.sampleType(for: metric) else { throw CaptureError.readerUnavailable }
            let observer = HKObserverQuery(sampleType: type, predicate: nil) { _, completion, error in
                let callback = HealthKitObserverCompletion(completion)
                if error != nil {
                    callback.finish()
                    Task { await coordinator.noteReaderFailure(.readerUnavailable) }
                    return
                }
                Task {
                    await ObserverNotification.handle(coordinator: coordinator, metric: metric,
                                                      completion: { callback.finish() })
                }
            }
            observers.append(observer); healthStore.execute(observer)
            try await healthStore.enableBackgroundDelivery(for: type, frequency: .immediate)
            guard shouldContinue() else { stopObserving(); throw CaptureError.cancelled }
        }
    }
    public func stopObserving() {
        for observer in observers { healthStore.stop(observer) }
        observers.removeAll()
    }
}

private struct VisibleSources: @unchecked Sendable { let sources: Set<HKSource> }

private final class HealthKitObserverCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private var completion: (() -> Void)?
    init(_ completion: @escaping () -> Void) { self.completion = completion }
    func finish() {
        let callback = lock.withLock { let callback = completion; completion = nil; return callback }
        callback?()
    }
}

/// Releases a query on cancellation/timeout; callbacks and cancellation may race.
/// Contains no log and never exposes an underlying NSError or sample value.
private final class QueryTicket<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private let healthStore: HKHealthStore
    private var terminal: Result<Value, CaptureError>?
    private var continuation: CheckedContinuation<Value, any Error>?
    private var query: HKQuery?
    private var timer: Task<Void, Never>?
    init(healthStore: HKHealthStore) { self.healthStore = healthStore }

    func start(_ continuation: CheckedContinuation<Value, any Error>, query: HKQuery) {
        let completed = lock.withLock {
            if let terminal { return terminal }
            self.continuation = continuation; self.query = query
            return nil as Result<Value, CaptureError>?
        }
        if let completed { continuation.resume(with: completed.mapError { $0 as any Error }); return }
        healthStore.execute(query)
        let timeout = Task {
            do { try await Task.sleep(for: .seconds(12)) }
            catch { return }
            self.finish(.failure(.readerUnavailable))
        }
        let alreadyFinished = lock.withLock {
            if terminal != nil { timeout.cancel(); return true }
            timer = timeout; return false
        }
        if alreadyFinished { healthStore.stop(query) }
    }
    func finish(_ result: Result<Value, CaptureError>) {
        let captured = lock.withLock { () -> (CheckedContinuation<Value, any Error>?, HKQuery?, Task<Void, Never>?)? in
            guard terminal == nil else { return nil }
            terminal = result
            let captured = (continuation, query, timer)
            continuation = nil; query = nil; timer = nil
            return captured
        }
        guard let captured else { return }
        captured.2?.cancel()
        if let query = captured.1 { healthStore.stop(query) }
        captured.0?.resume(with: result.mapError { $0 as any Error })
    }
}
#else
// Mac tests build the real core; HealthKit/UIKit source is deliberately unavailable.
public enum HealthKitSDKRequired {}
#endif
