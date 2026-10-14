import Foundation

public struct ReaderPage: Sendable {
    public var samples: [WatchSample]
    public var deletedIDs: [String]
    public var nextAnchor: String?
    public var visibility: ReadState
    public var hasMore: Bool
    public init(samples: [WatchSample] = [], deletedIDs: [String] = [], nextAnchor: String?,
                visibility: ReadState, hasMore: Bool = false) {
        self.samples = samples; self.deletedIDs = deletedIDs; self.nextAnchor = nextAnchor
        self.visibility = visibility; self.hasMore = hasMore
    }
}
public protocol HealthReader: Sendable {
    /// The adapter must use the exact saved scope predicate and original anchor.
    func read(scope: QueryScope, anchor: String?, limit: Int) async throws -> ReaderPage
}
public enum CatchUpResult: Equatable, Sendable {
    case captured(pages: Int), deferred(CaptureError), busy, optInRequired
}
public struct RetryIntent: Equatable, Sendable {
    public var needed: Bool = false
    public var attempts = 0
    public var nextAttempt: Date?
    public var error: CaptureError?
}

/// Serial, bounded catch-up. Permission workflow completion is never a read-grant signal.
/// Observers await at most one page per scope and call completion after local handling;
/// neither this actor nor the adapter waits for a destination or performs networking.
public actor CaptureCoordinator {
    private let reader: any HealthReader
    private let gate: any ProtectedDataGate
    private let store: LocalCaptureStore
    private let mode: DatasetMode
    private let clock: @Sendable () -> Date
    private let commitFence = CaptureCommitFence()
    private var optedIn = false
    private var running = false
    private var retry = RetryIntent()
    public init(reader: any HealthReader, gate: any ProtectedDataGate, store: LocalCaptureStore,
                mode: DatasetMode, clock: @escaping @Sendable () -> Date = { Date() }) {
        self.reader = reader; self.gate = gate; self.store = store; self.mode = mode; self.clock = clock
    }
    /// Call only after the user's explicit read-selection action, or restore a prior local opt-in.
    /// This controls querying; it does not assert HealthKit granted any read type.
    public func setLocalOptIn(_ value: Bool) {
        // Invalidates in-flight generations, or waits for a synchronous permitted
        // save already in progress to finish before opt-out returns.
        commitFence.setLocalOptIn(value); optedIn = value
    }
    public func retryIntent() -> RetryIntent { retry }
    public func noteReaderFailure(_ code: CaptureError) async { _ = await deferRead(code) }

    private func deferRead(_ code: CaptureError) async -> CatchUpResult {
        retry.needed = true; retry.error = code; retry.attempts = min(8, retry.attempts + 1)
        retry.nextAttempt = clock().addingTimeInterval(min(86_400, 60 * pow(2, Double(retry.attempts - 1))))
        // No protected snapshot access while locked. The data-free retry intent is in memory;
        // the iOS shell persists only a Boolean catch-up-needed control flag separately.
        if await gate.isAvailable() { try? await store.recordFailure(code) }
        return .deferred(code)
    }

    public func catchUp(maxPagesPerScope: Int = 4, metrics: Set<WatchMetric>? = nil, scopeLimit: Int? = nil) async -> CatchUpResult {
        guard optedIn else { return .optInRequired }
        guard !running else { return .busy }
        guard (1...16).contains(maxPagesPerScope) else { return await deferRead(.resourceLimit) }
        if let scopeLimit, !(1...64).contains(scopeLimit) { return await deferRead(.resourceLimit) }
        guard let permit = commitFence.acquirePermit() else { return .optInRequired }
        running = true; defer { running = false }
        guard await gate.isAvailable() else { return await deferRead(.protectedDataUnavailable) }
        var pages = 0, incomplete = false
        do {
            try permit.check()
            guard !(try await store.status()).deliveryGap else { throw CaptureError.reconciliationRequired }
            try permit.check()
            let matching = try await store.checkpoints().filter { metrics == nil || metrics!.contains($0.scope.metric) }
            try permit.check()
            let checkpoints = Array(matching.prefix(scopeLimit ?? matching.count))
            incomplete = matching.count > checkpoints.count
            guard !checkpoints.isEmpty else { return await deferRead(.readVisibilityUnknown) }
            for checkpoint in checkpoints {
                var previous = checkpoint.anchor
                for pageIndex in 0..<maxPagesPerScope {
                    try Task.checkCancellation()
                    try permit.check()
                    guard optedIn, await gate.isAvailable() else { throw CaptureError.protectedDataUnavailable }
                    try permit.check()
                    let page = try await reader.read(scope: checkpoint.scope, anchor: previous, limit: 1_000)
                    try Task.checkCancellation()
                    try permit.check()
                    // A lock or opt-out during the asynchronous read cannot advance checkpoints.
                    guard optedIn, await gate.isAvailable() else { throw CaptureError.protectedDataUnavailable }
                    try permit.check()
                    guard [.empty, .nonempty].contains(page.visibility), page.nextAnchor != nil else {
                        throw CaptureError.readVisibilityUnknown
                    }
                    guard (page.visibility == .nonempty) == !page.samples.isEmpty else { throw CaptureError.invalidContract }
                    if page.hasMore, page.nextAnchor == previous { throw CaptureError.staleAnchor }
                    // Validate ALL original operations before retention minimization, including
                    // duplicate/cross-type/source UUIDs and samples outside the fixed predicate.
                    let original = QueryDelta(type: checkpoint.scope.metric, previousAnchor: previous,
                                              nextAnchor: page.nextAnchor, readState: page.visibility,
                                              samples: page.samples, deletedIDs: page.deletedIDs)
                    try WatchBatch(mode: mode, scope: checkpoint.scope, generatedAt: clock(), types: [original]).validate()
                    guard page.samples.allSatisfy({
                        (WireDate.parse($0.start) ?? .distantPast) >= checkpoint.scope.startDate
                    }) else { throw CaptureError.scopeConflict }
                    let cutoff = await store.retentionCutoff()
                    try permit.check()
                    let samples = try page.samples.filter { sample in
                        guard let date = WireDate.parse(sample.end) else { throw CaptureError.invalidContract }
                        return date >= cutoff
                    }
                    let query = QueryDelta(type: checkpoint.scope.metric, previousAnchor: previous,
                                           nextAnchor: page.nextAnchor, readState: samples.isEmpty ? .empty : .nonempty,
                                           samples: samples, deletedIDs: page.deletedIDs)
                    let batch = WatchBatch(mode: mode, scope: checkpoint.scope, generatedAt: clock(), types: [query])
                    try Task.checkCancellation()
                    _ = try await store.commit(batch, permit: permit)
                    try permit.check()
                    previous = page.nextAnchor; pages += 1
                    if !page.hasMore { break }
                    if pageIndex == maxPagesPerScope - 1 { incomplete = true }
                }
            }
            if incomplete {
                retry.needed = true; retry.error = nil; retry.nextAttempt = clock().addingTimeInterval(60)
            } else { retry = RetryIntent() }
            return .captured(pages: pages)
        } catch is CancellationError { return await deferRead(.cancelled) }
        catch { return await deferRead((error as? CaptureError) ?? .readerUnavailable) }
    }
}

public enum ObserverNotification {
    /// Completion is always released after bounded local handling, including unknown reads,
    /// locked data, a full queue and a concurrent catch-up already in progress.
    public static func handle(coordinator: CaptureCoordinator, metric: WatchMetric,
                              deadline: Duration = .seconds(20),
                              completion: @escaping @Sendable () -> Void) async {
        let latch = ObserverCompletion(completion)
        await withCheckedContinuation { continuation in
            latch.setContinuation(continuation)
            let work = Task<Void, Never> {
                _ = await coordinator.catchUp(maxPagesPerScope: 1, metrics: [metric], scopeLimit: 1)
                _ = latch.finish()
            }
            let timeout = Task<Void, Never> {
                do { try await Task.sleep(for: deadline) } catch { return }
                if latch.finish() {
                    work.cancel()
                    await coordinator.noteReaderFailure(.readerUnavailable)
                }
            }
            latch.setTasks([work, timeout])
        }
    }
}

private final class ObserverCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private let completion: @Sendable () -> Void
    private var completed = false
    private var continuation: CheckedContinuation<Void, Never>?
    private var tasks: [Task<Void, Never>] = []
    init(_ completion: @escaping @Sendable () -> Void) { self.completion = completion }
    func setContinuation(_ value: CheckedContinuation<Void, Never>) { lock.withLock { continuation = value } }
    func setTasks(_ value: [Task<Void, Never>]) {
        lock.withLock {
            if completed { value.forEach { $0.cancel() } } else { tasks = value }
        }
    }
    @discardableResult func finish() -> Bool {
        let captured = lock.withLock { () -> (CheckedContinuation<Void, Never>?, [Task<Void, Never>])? in
            guard !completed else { return nil }
            completed = true
            let captured = (continuation, tasks); continuation = nil; tasks = []
            return captured
        }
        guard let captured else { return false }
        captured.1.forEach { $0.cancel() }
        completion(); captured.0?.resume()
        return true
    }
}
