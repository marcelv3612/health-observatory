import Foundation
import Dispatch
import Testing
@testable import WatchSyncCore

@Test func pauseDuringActualStoreCommitGateCannotAdvanceCheckpointReceiptOrOutbox() async throws {
    let gate = CommitSuspensionGate(), io = MemorySnapshot(), store = try syntheticStore(io: io, gate: gate)
    try await store.register(scopes: [syntheticScope()])
    let reader = SyntheticReader([.page(ReaderPage(samples: [try syntheticSample()], nextAnchor: "a1", visibility: .nonempty))])
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { syntheticNow })
    await coordinator.setLocalOptIn(true)
    // After setup, catch-up's sixth availability check is commit.loadAndPrune's
    // awaited store gate: status/checkpoint/pre/post-read checks have already passed.
    await gate.arm(onCheck: 6)
    let capture = Task { await coordinator.catchUp() }
    await gate.waitUntilSuspended()
    #expect(await reader.count() == 1)
    await coordinator.setLocalOptIn(false)
    await gate.release()
    #expect(await capture.value == .deferred(.cancelled))
    #expect(try await store.checkpoints().first?.anchor == nil)
    #expect(try await store.pendingBatches().isEmpty)
    #expect(try await store.status().lastSuccessfulLocalCapture == nil)
    let snapshot = try JSONSerialization.jsonObject(with: io.data()!) as! [String: Any]
    #expect((snapshot["receipts"] as! [String: Any]).isEmpty)
    #expect((snapshot["identities"] as! [String: Any]).isEmpty)
    #expect(await coordinator.catchUp() == .optInRequired)
}

@Test func pauseThenResumeRejectsOldInFlightReaderGenerationAndFreshCaptureCanProceed() async throws {
    let reader = ConsentSuspendedReader(page: ReaderPage(samples: [try syntheticSample()], nextAnchor: "a1", visibility: .nonempty))
    let gate = TestGate(), store = try syntheticStore()
    try await store.register(scopes: [syntheticScope()])
    let coordinator = CaptureCoordinator(reader: reader, gate: gate, store: store, mode: .synthetic, clock: { syntheticNow })
    await coordinator.setLocalOptIn(true)
    let oldCapture = Task { await coordinator.catchUp() }
    await reader.waitUntilSuspended()
    await coordinator.setLocalOptIn(false)
    await coordinator.setLocalOptIn(true)
    await reader.release()
    #expect(await oldCapture.value == .deferred(.cancelled))
    #expect(try await store.checkpoints().first?.anchor == nil)
    #expect(try await store.pendingBatches().isEmpty)
    #expect(await coordinator.catchUp() == .captured(pages: 1))
    #expect(try await store.checkpoints().first?.anchor == "a1")
    #expect(try await store.pendingBatches().count == 1)
}

@Test func invalidatedPermitAndUnfencedDeviceStoreCannotCommit() async throws {
    let io = MemorySnapshot(), store = try syntheticStore(io: io)
    try await store.register(scopes: [syntheticScope()])
    let fence = CaptureCommitFence()
    #expect(fence.acquirePermit() == nil)
    fence.setLocalOptIn(true)
    let permit = fence.acquirePermit()!
    fence.setLocalOptIn(false); fence.setLocalOptIn(true)
    let before = io.data()
    await #expect(throws: CaptureError.cancelled) { try await store.commit(syntheticBatch(), permit: permit) }
    #expect(io.data() == before)
    let deviceStore = try LocalCaptureStore(io: MemorySnapshot(), mode: .device, gate: AlwaysAvailableGate(), clock: { syntheticNow })
    let scope = try syntheticScope()
    try await deviceStore.register(scopes: [scope])
    let batch = WatchBatch(mode: .device, scope: scope, generatedAt: syntheticNow, types: try syntheticBatch().types)
    await #expect(throws: CaptureError.cancelled) { try await deviceStore.commit(batch) }
    #expect(try await deviceStore.checkpoints().first?.anchor == nil)
    #expect(try await deviceStore.pendingBatches().isEmpty)
}

@Test func optOutWaitsForAnAlreadyEnteredSynchronousSaveAndOldPermitCannotSaveAgain() async throws {
    let fence = CaptureCommitFence(), entered = AsyncConsentSignal(), pauseStarted = AsyncConsentSignal()
    let release = DispatchSemaphore(value: 0), completed = ConsentCompletionState()
    fence.setLocalOptIn(true)
    let permit = fence.acquirePermit()!
    let save = Task.detached {
        try permit.withValidConsent {
            Task { await entered.signal() }
            guard release.wait(timeout: .now() + 5) == .success else { throw CaptureError.localStorageError }
            completed.saveFinished()
        }
    }
    await entered.wait()
    let pause = Task.detached {
        await pauseStarted.signal()
        fence.setLocalOptIn(false)
        completed.pauseFinished()
    }
    await pauseStarted.wait()
    #expect(!completed.pauseReturned())
    release.signal()
    try await save.value; await pause.value
    #expect(completed.saveCompleted())
    #expect(completed.pauseReturned())
    #expect(!completed.pausePrecededSave())
    #expect(throws: CaptureError.cancelled) { try permit.withValidConsent { completed.saveFinished() } }
}

private actor CommitSuspensionGate: ProtectedDataGate {
    private var check = 0
    private var target: Int?
    private var suspended = false
    private var gateContinuation: CheckedContinuation<Bool, Never>?
    private var waiters: [CheckedContinuation<Void, Never>] = []
    func arm(onCheck: Int) { check = 0; target = onCheck }
    func isAvailable() async -> Bool {
        check += 1
        guard check == target else { return true }
        suspended = true; target = nil
        for waiter in waiters { waiter.resume() }; waiters.removeAll()
        return await withCheckedContinuation { gateContinuation = $0 }
    }
    func waitUntilSuspended() async {
        if suspended { return }
        await withCheckedContinuation { waiters.append($0) }
    }
    func release() { gateContinuation?.resume(returning: true); gateContinuation = nil }
}

private actor ConsentSuspendedReader: HealthReader {
    private let page: ReaderPage
    private var firstRead = true
    private var suspended = false
    private var continuation: CheckedContinuation<ReaderPage, Never>?
    private var waiters: [CheckedContinuation<Void, Never>] = []
    init(page: ReaderPage) { self.page = page }
    func read(scope: QueryScope, anchor: String?, limit: Int) async throws -> ReaderPage {
        guard firstRead else { return page }
        firstRead = false; suspended = true
        for waiter in waiters { waiter.resume() }; waiters.removeAll()
        return await withCheckedContinuation { continuation = $0 }
    }
    func waitUntilSuspended() async {
        if suspended { return }
        await withCheckedContinuation { waiters.append($0) }
    }
    func release() { continuation?.resume(returning: page); continuation = nil }
}

private actor AsyncConsentSignal {
    private var signalled = false
    private var waiters: [CheckedContinuation<Void, Never>] = []
    func signal() {
        signalled = true; for waiter in waiters { waiter.resume() }; waiters.removeAll()
    }
    func wait() async {
        if signalled { return }
        await withCheckedContinuation { waiters.append($0) }
    }
}
private final class ConsentCompletionState: @unchecked Sendable {
    private let lock = NSLock()
    private var saved = false, paused = false, pausedFirst = false
    func saveFinished() { lock.withLock { saved = true } }
    func pauseFinished() { lock.withLock { paused = true; pausedFirst = !saved } }
    func saveCompleted() -> Bool { lock.withLock { saved } }
    func pauseReturned() -> Bool { lock.withLock { paused } }
    func pausePrecededSave() -> Bool { lock.withLock { pausedFirst } }
}
