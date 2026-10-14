import Foundation
import WatchSyncCore

let syntheticNow = WireDate.parse("2031-03-11T12:00:00Z")!
let syntheticBundle = "com.example.sleepstudio.synthetic"
let syntheticSource = SampleSource(bundleID: syntheticBundle, name: "Invented Watch", device: ["model": "Fixture"])
func syntheticUUID(_ index: Int) -> UUID {
    UUID(uuidString: String(format: "10000000-0000-4000-8000-%012d", index))!
}
func syntheticScope(_ metric: WatchMetric = .restingHeartRate, stream: Int = 1,
                    start: Date = syntheticNow.addingTimeInterval(-7 * 86_400)) throws -> QueryScope {
    try QueryScope(streamID: syntheticUUID(stream), sourceBundleID: syntheticBundle, metric: metric, startDate: start)
}
func syntheticSample(_ metric: WatchMetric = .restingHeartRate, id: Int = 2, value: Double = 62,
                     start: Date = syntheticNow.addingTimeInterval(-3_600), end: Date = syntheticNow.addingTimeInterval(-3_300)) throws -> WatchSample {
    try WatchSample(id: syntheticUUID(id), type: metric, start: start, end: end, source: syntheticSource,
                    value: metric == .sleepAnalysis ? .stage("core") : .quantity(value))
}
func syntheticBatch(id: Int = 3, scope: QueryScope? = nil, previous: String? = nil, next: String = "anchor-1",
                    samples: [WatchSample]? = nil, deleted: [String] = [], at: Date = syntheticNow) throws -> WatchBatch {
    let chosen = try scope ?? syntheticScope()
    let values = try samples ?? [syntheticSample(chosen.metric)]
    return WatchBatch(batchID: syntheticUUID(id), mode: .synthetic, scope: chosen, generatedAt: at,
                       types: [QueryDelta(type: chosen.metric, previousAnchor: previous, nextAnchor: next,
                                          readState: values.isEmpty ? .empty : .nonempty, samples: values, deletedIDs: deleted)])
}

final class TestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var date = syntheticNow
    func now() -> Date { lock.withLock { date } }
    func advance(_ seconds: TimeInterval) { lock.withLock { date.addTimeInterval(seconds) } }
}
final class TestGate: ProtectedDataGate, @unchecked Sendable {
    private let lock = NSLock()
    private var available: Bool
    init(_ available: Bool = true) { self.available = available }
    func set(_ value: Bool) { lock.withLock { available = value } }
    func isAvailable() async -> Bool { lock.withLock { available } }
}

final class MemorySnapshot: SnapshotIO, @unchecked Sendable {
    enum Failure: Sendable { case none, beforeReplace, afterReplace }
    private let lock = NSLock()
    private var bytes: Data?
    private var failure: Failure = .none
    private var readCounter = 0
    func setFailure(_ value: Failure) { lock.withLock { failure = value } }
    func data() -> Data? { lock.withLock { bytes } }
    func reads() -> Int { lock.withLock { readCounter } }
    func read() throws -> Data? { lock.withLock { readCounter += 1; return bytes } }
    func replace(_ data: Data, expected: Data?) throws {
        try lock.withLock {
            guard bytes == expected else { throw CaptureError.storageConflict }
            if failure == .beforeReplace { failure = .none; throw CaptureError.localStorageError }
            bytes = data
            if failure == .afterReplace { failure = .none; throw CaptureError.localStorageError }
        }
    }
    func corrupt(_ value: Data) { lock.withLock { bytes = value } }
}

actor SyntheticReader: HealthReader {
    enum Action: Sendable { case page(ReaderPage), fail(CaptureError), lockThenPage(TestGate, ReaderPage) }
    private var actions: [Action]
    private var calls: [(QueryScope, String?)] = []
    init(_ actions: [Action]) { self.actions = actions }
    func read(scope: QueryScope, anchor: String?, limit: Int) async throws -> ReaderPage {
        calls.append((scope, anchor))
        guard !actions.isEmpty else { throw CaptureError.readerUnavailable }
        switch actions.removeFirst() {
        case .page(let page): return page
        case .fail(let error): throw error
        case .lockThenPage(let gate, let page): gate.set(false); return page
        }
    }
    func count() -> Int { calls.count }
    func anchors() -> [String?] { calls.map { $0.1 } }
    func scopes() -> [QueryScope] { calls.map { $0.0 } }
}

func syntheticStore(io: any SnapshotIO = MemorySnapshot(), gate: any ProtectedDataGate = AlwaysAvailableGate(),
                    limits: CaptureLimits = CaptureLimits(), clock: TestClock = TestClock()) throws -> LocalCaptureStore {
    try LocalCaptureStore(io: io, mode: .synthetic, gate: gate, limits: limits, clock: { clock.now() })
}
