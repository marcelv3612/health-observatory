import Foundation
import Testing
@testable import WatchSyncCore

@Test func noLocalOptInPerformsNoReaderOrSnapshotAccess() async throws {
    let io = MemorySnapshot(), reader = SyntheticReader([]), gate = TestGate(), store = try syntheticStore(io: io, gate: gate)
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic)
    #expect(await coordinator.catchUp() == .optInRequired)
    #expect(await reader.count() == 0)
    #expect(io.reads() == 0)
}

@Test func lockedNotificationQueuesDataFreeRetryAndAlwaysCompletes() async throws {
    let io = MemorySnapshot(), reader = SyntheticReader([]), gate = TestGate(false), store = try syntheticStore(io: io, gate: gate)
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic)
    await coordinator.setLocalOptIn(true)
    let completion = CompletionCounter()
    await ObserverNotification.handle(coordinator: coordinator, metric: .sleepAnalysis, completion: { completion.mark() })
    #expect(completion.count() == 1)
    #expect(await reader.count() == 0)
    #expect(io.reads() == 0)
    #expect(await coordinator.retryIntent().error == .protectedDataUnavailable)
    #expect(await coordinator.retryIntent().needed)
}

@Test func unknownAndNilAnchorNeverCommitOrImplyReadGrant() async throws {
    for page in [ReaderPage(nextAnchor: nil, visibility: .unknown), ReaderPage(nextAnchor: nil, visibility: .empty)] {
        let reader = SyntheticReader([.page(page)]), store = try syntheticStore(), gate = TestGate()
        try await store.register(scopes: [syntheticScope()])
        let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic)
        await coordinator.setLocalOptIn(true)
        #expect(await coordinator.catchUp() == .deferred(.readVisibilityUnknown))
        #expect(try await store.checkpoints().first?.anchor == nil)
        #expect(try await store.pendingBatches().isEmpty)
        #expect(try await store.status().lastSuccessfulLocalCapture == nil)
        #expect(try await store.status().readVisibility == "unknown")
    }
}

@Test func lockingDuringReadPreservesCheckpointUntilUnlockCatchUp() async throws {
    let gate = TestGate(), io = MemorySnapshot(), store = try syntheticStore(io: io, gate: gate)
    try await store.register(scopes: [syntheticScope()])
    let page = ReaderPage(samples: [try syntheticSample()], nextAnchor: "a1", visibility: .nonempty)
    let reader = SyntheticReader([.lockThenPage(gate, page), .page(page)])
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { syntheticNow })
    await coordinator.setLocalOptIn(true); let before = io.data()
    #expect(await coordinator.catchUp() == .deferred(.protectedDataUnavailable))
    #expect(io.data() == before)
    gate.set(true)
    #expect(await coordinator.catchUp() == .captured(pages: 1))
    #expect(try await store.checkpoints().first?.anchor == "a1")
    #expect(await reader.anchors() == [nil, nil])
    #expect(!(await coordinator.retryIntent().needed))
}

@Test func databaseInaccessibleFailureKeepsExactAnchorAndGenericRetry() async throws {
    let reader = SyntheticReader([.fail(.protectedDataUnavailable)]), store = try syntheticStore(), gate = TestGate()
    try await store.register(scopes: [syntheticScope()]); _ = try await store.commit(syntheticBatch())
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic)
    await coordinator.setLocalOptIn(true)
    #expect(await coordinator.catchUp() == .deferred(.protectedDataUnavailable))
    #expect(try await store.checkpoints().first?.anchor == "anchor-1")
    #expect(try await store.pendingBatches().count == 1)
    #expect(await reader.anchors() == ["anchor-1"])
}

@Test func pagedReadUsesCommittedAnchorsAndFixedPredicateAcrossDays() async throws {
    let clock = TestClock(), scope = try syntheticScope(), store = try syntheticStore(clock: clock), gate = TestGate()
    try await store.register(scopes: [scope])
    let reader = SyntheticReader([
        .page(ReaderPage(samples: [try syntheticSample()], nextAnchor: "a1", visibility: .nonempty, hasMore: true)),
        .page(ReaderPage(nextAnchor: "a2", visibility: .empty))
    ])
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { clock.now() })
    await coordinator.setLocalOptIn(true)
    #expect(await coordinator.catchUp(maxPagesPerScope: 1) == .captured(pages: 1))
    #expect(await coordinator.retryIntent().needed)
    clock.advance(2 * 86_400)
    #expect(await coordinator.catchUp() == .captured(pages: 1))
    #expect(await reader.anchors() == [nil, "a1"])
    #expect(await reader.scopes() == [scope, scope])
    #expect(try await store.checkpoints().first?.anchor == "a2")
}

@Test func emptyQueryAndMeasuredZeroStayDistinctWithUnknownCoverage() async throws {
    let store = try syntheticStore(), gate = TestGate(), scope = try syntheticScope(.stepCount)
    try await store.register(scopes: [scope])
    let reader = SyntheticReader([
        .page(ReaderPage(nextAnchor: "a1", visibility: .empty)),
        .page(ReaderPage(samples: [try syntheticSample(.stepCount, value: 0)], nextAnchor: "a2", visibility: .nonempty))
    ])
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { syntheticNow })
    await coordinator.setLocalOptIn(true)
    #expect(await coordinator.catchUp() == .captured(pages: 1))
    #expect(try await store.checkpoints().first?.lastReadState == .empty)
    #expect(try await store.status().readVisibility == "unknown")
    #expect(await coordinator.catchUp() == .captured(pages: 1))
    #expect(try await store.pendingBatches().last?.types.first?.samples.first?.value == .quantity(0))
}

@Test func expiredOutboxBlocksBeforeReaderAndCallbacksStillComplete() async throws {
    let clock = TestClock(), store = try syntheticStore(clock: clock), gate = TestGate(), reader = SyntheticReader([])
    try await store.register(scopes: [syntheticScope()]); _ = try await store.commit(syntheticBatch())
    clock.advance(91 * 86_400)
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { clock.now() })
    await coordinator.setLocalOptIn(true)
    let completion = CompletionCounter()
    await ObserverNotification.handle(coordinator: coordinator, metric: .restingHeartRate, completion: { completion.mark() })
    #expect(await reader.count() == 0)
    #expect(completion.count() == 1)
    #expect(await coordinator.retryIntent().error == .reconciliationRequired)
}

@Test func malformedOldSampleIsValidatedBeforeMinimizationAndAnchorsDoNotAdvance() async throws {
    let scope = try syntheticScope(start: syntheticNow.addingTimeInterval(-90 * 86_400)), store = try syntheticStore(), gate = TestGate()
    try await store.register(scopes: [scope])
    var invalid = try syntheticSample(); invalid.source.bundleID = "com.example.wrong"
    let reader = SyntheticReader([.page(ReaderPage(samples: [invalid], nextAnchor: "a1", visibility: .nonempty))])
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { syntheticNow })
    await coordinator.setLocalOptIn(true)
    #expect(await coordinator.catchUp() == .deferred(.invalidContract))
    #expect(try await store.checkpoints().first?.anchor == nil)
}

@Test func operationBeforeFixedStartOrNonprogressingPageCannotAdvance() async throws {
    let scope = try syntheticScope(), store = try syntheticStore(), gate = TestGate()
    try await store.register(scopes: [scope])
    let outside = try syntheticSample(start: scope.startDate.addingTimeInterval(-300), end: scope.startDate)
    let reader = SyntheticReader([.page(ReaderPage(samples: [outside], nextAnchor: "a1", visibility: .nonempty))])
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic)
    await coordinator.setLocalOptIn(true)
    #expect(await coordinator.catchUp() == .deferred(.scopeConflict))
    _ = try await store.commit(syntheticBatch())
    let badReader = SyntheticReader([.page(ReaderPage(nextAnchor: "anchor-1", visibility: .empty, hasMore: true))])
    let other = CaptureCoordinator(reader: badReader, gate: gate, store: store, mode: .synthetic)
    await other.setLocalOptIn(true)
    #expect(await other.catchUp() == .deferred(.staleAnchor))
    #expect(try await store.checkpoints().first?.anchor == "anchor-1")
}

@Test func boundedRetryIntentCapsAttemptsAndContainsOnlyGenericControlState() async throws {
    let gate = TestGate(false), reader = SyntheticReader([]), store = try syntheticStore(gate: gate)
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { syntheticNow })
    await coordinator.setLocalOptIn(true)
    for _ in 0..<12 { _ = await coordinator.catchUp() }
    let retry = await coordinator.retryIntent()
    #expect(retry.attempts == 8)
    #expect(retry.nextAttempt == syntheticNow.addingTimeInterval(7_680))
    #expect(await reader.count() == 0)
}

private final class CompletionCounter: @unchecked Sendable {
    private let lock = NSLock(); private var value = 0
    func mark() { lock.withLock { value += 1 } }
    func count() -> Int { lock.withLock { value } }
}

@Test func observerDeadlineCompletesOnceDespiteNoncancellingReaderAndLateResultCannotCommit() async throws {
    let reader = SuspendedSyntheticReader(), gate = TestGate(), store = try syntheticStore()
    try await store.register(scopes: [syntheticScope()])
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { syntheticNow })
    await coordinator.setLocalOptIn(true)
    let completion = CompletionCounter()
    await ObserverNotification.handle(coordinator: coordinator, metric: .restingHeartRate, deadline: .milliseconds(100),
                                      completion: { completion.mark() })
    #expect(completion.count() == 1)
    #expect(try await store.checkpoints().first?.anchor == nil)
    await reader.release(ReaderPage(samples: [try syntheticSample()], nextAnchor: "late-anchor", visibility: .nonempty))
    for _ in 0..<100 { await Task.yield() }
    #expect(completion.count() == 1)
    #expect(try await store.pendingBatches().isEmpty)
    #expect(try await store.checkpoints().first?.anchor == nil)
}

@Test func observerHandlesOnlyOneSavedScopePerNotification() async throws {
    let store = try syntheticStore(), gate = TestGate()
    try await store.register(scopes: [syntheticScope(stream: 1), syntheticScope(stream: 7)])
    let reader = SyntheticReader([.page(ReaderPage(nextAnchor: "a1", visibility: .empty))])
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { syntheticNow })
    await coordinator.setLocalOptIn(true)
    await ObserverNotification.handle(coordinator: coordinator, metric: .restingHeartRate, completion: {})
    #expect(await reader.count() == 1)
    #expect(await coordinator.retryIntent().needed)
}

private actor SuspendedSyntheticReader: HealthReader {
    private var continuation: CheckedContinuation<ReaderPage, Never>?
    private var earlyRelease: ReaderPage?
    func read(scope: QueryScope, anchor: String?, limit: Int) async throws -> ReaderPage {
        await withCheckedContinuation {
            if let earlyRelease { $0.resume(returning: earlyRelease) }
            else { continuation = $0 }
        }
    }
    func release(_ value: ReaderPage) {
        if let continuation { self.continuation = nil; continuation.resume(returning: value) }
        else { earlyRelease = value }
    }
}
