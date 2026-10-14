import Foundation
import Testing
@testable import WatchSyncCore

@Test func atomicCaptureHasCheckpointReceiptAndOnlyOneRawOutbox() async throws {
    let io = MemorySnapshot(), store = try syntheticStore(io: io)
    try await store.register(scopes: [syntheticScope()])
    let receipt = try await store.commit(syntheticBatch())
    #expect(!receipt.replayed)
    let checkpoints = try await store.checkpoints()
    #expect(checkpoints.first?.anchor == "anchor-1")
    #expect(try await store.pendingBatches().count == 1)
    let object = try JSONSerialization.jsonObject(with: io.data()!) as! [String: Any]
    #expect(Set(object.keys).isDisjoint(with: ["samples", "records", "rawMirror"]))
    #expect((object["receipts"] as! [String: Any]).count == 1)
    #expect((object["identities"] as! [String: Any]).count == 1)
    let reopened = try syntheticStore(io: io)
    #expect(try await reopened.checkpoints().first?.anchor == "anchor-1")
    #expect(try await reopened.status().lastSuccessfulLocalCapture == syntheticNow)
    #expect(try await reopened.status().destinationEnabled == false)
}

@Test func replayConcurrentCommitAndChangedReplayAreSafe() async throws {
    let store = try syntheticStore(); try await store.register(scopes: [syntheticScope()])
    let batch = try syntheticBatch()
    let receipts = try await withThrowingTaskGroup(of: CommitReceipt.self) { group in
        for _ in 0..<8 { group.addTask { try await store.commit(batch) } }
        var values: [CommitReceipt] = []; for try await value in group { values.append(value) }; return values
    }
    #expect(receipts.filter { !$0.replayed }.count == 1)
    #expect(try await store.pendingBatches().count == 1)
    var changed = batch; changed.types[0].samples[0].value = .quantity(70)
    await #expect(throws: CaptureError.replayConflict) { try await store.commit(changed) }
}

@Test func preRenameFailurePreservesAnchorAndUncertainPostRenameRecoversByReceipt() async throws {
    for failure in [MemorySnapshot.Failure.beforeReplace, .afterReplace] {
        let io = MemorySnapshot(), store = try syntheticStore(io: io)
        try await store.register(scopes: [syntheticScope()])
        io.setFailure(failure)
        await #expect(throws: CaptureError.localStorageError) { try await store.commit(syntheticBatch()) }
        let reopened = try syntheticStore(io: io)
        let recovered = try await reopened.commit(syntheticBatch())
        #expect(recovered.replayed == (failure == .afterReplace))
        #expect(try await reopened.pendingBatches().count == 1)
        #expect(try await reopened.checkpoints().first?.anchor == "anchor-1")
    }
}

@Test func laterQueryConflictRollsBackEntireSnapshotAndImmutableScopeCannotRoll() async throws {
    let io = MemorySnapshot(), store = try syntheticStore(io: io)
    let scope = try syntheticScope(), other = try syntheticScope(.stepCount)
    try await store.register(scopes: [scope, other])
    _ = try await store.commit(syntheticBatch())
    let before = io.data()
    let first = QueryDelta(type: .stepCount, previousAnchor: nil, nextAnchor: "step-1", readState: .nonempty,
                           samples: [try syntheticSample(.stepCount, id: 4, value: 100)])
    let second = QueryDelta(type: .restingHeartRate, previousAnchor: "stale", nextAnchor: "a2", readState: .empty)
    let bad = WatchBatch(batchID: syntheticUUID(6), mode: .synthetic, scope: scope, generatedAt: syntheticNow, types: [first, second])
    await #expect(throws: CaptureError.staleAnchor) { try await store.commit(bad) }
    #expect(io.data() == before)
    let rolled = try syntheticScope(start: scope.startDate.addingTimeInterval(86_400))
    await #expect(throws: CaptureError.scopeConflict) { try await store.register(scopes: [rolled]) }
}

@Test func correctionsAndRestingHeartRateDeletionAreOrderedOriginalUUIDOperations() async throws {
    let store = try syntheticStore(); try await store.register(scopes: [syntheticScope()])
    _ = try await store.commit(syntheticBatch())
    _ = try await store.commit(syntheticBatch(id: 4, previous: "anchor-1", next: "a2", samples: [syntheticSample(value: 70)]))
    _ = try await store.commit(syntheticBatch(id: 5, previous: "a2", next: "a3", samples: [], deleted: [syntheticUUID(2).uuidString.lowercased()]))
    let pending = try await store.pendingBatches()
    #expect(pending.count == 3)
    #expect(pending[0].types[0].samples[0].id == pending[1].types[0].samples[0].id)
    #expect(pending[2].types[0].deletedIDs == [syntheticUUID(2).uuidString.lowercased()])
    #expect(try await store.checkpoints().first?.anchor == "a3")
}

@Test func sampleUUIDAndDeletionProvenanceCannotChangeScope() async throws {
    let store = try syntheticStore(), scope = try syntheticScope(), other = try syntheticScope(.stepCount)
    try await store.register(scopes: [scope, other]); _ = try await store.commit(syntheticBatch())
    _ = try await store.commit(syntheticBatch(id: 8, scope: other, next: "steps-1", samples: []))
    let id = syntheticUUID(2).uuidString.lowercased()
    await #expect(throws: CaptureError.scopeConflict) {
        try await store.commit(syntheticBatch(id: 9, scope: other, previous: "steps-1", samples: [], deleted: [id]))
    }
    await #expect(throws: CaptureError.scopeConflict) {
        try await store.commit(syntheticBatch(id: 10, scope: other, previous: "steps-1", samples: [syntheticSample(.stepCount, value: 3)]))
    }
}

@Test func sampleByteBatchReceiptAndIdentityBackpressurePreserveCheckpoint() async throws {
    for restriction in 0..<5 {
        var limits = CaptureLimits()
        switch restriction {
        case 0: limits.pendingOperations = 1
        case 1: limits.pendingBytes = 1_000
        case 2: limits.pendingBatches = 1
        case 3: limits.receipts = 1
        default: limits.identities = 1
        }
        let store = try syntheticStore(limits: limits); try await store.register(scopes: [syntheticScope()])
        _ = try await store.commit(syntheticBatch())
        await #expect(throws: CaptureError.resourceLimit) {
            try await store.commit(syntheticBatch(id: 4, previous: "anchor-1", next: "a2", samples: [syntheticSample(id: 10)]))
        }
        #expect(try await store.checkpoints().first?.anchor == "anchor-1")
        #expect(try await store.pendingBatches().count == 1)
    }
}

@Test func retentionPhysicallyPurgesRawValuesRetainsAnchorsAndStopsCapture() async throws {
    for access in 0..<4 {
        let io = MemorySnapshot(), clock = TestClock(), store = try syntheticStore(io: io, clock: clock)
        let original = try syntheticBatch(samples: [syntheticSample(value: 4321.234567)])
        try await store.register(scopes: [syntheticScope()]); _ = try await store.commit(original)
        #expect(String(data: io.data()!, encoding: .utf8)!.contains("4321.234567"))
        clock.advance(91 * 86_400)
        switch access {
        case 0: _ = try await store.status()
        case 1: _ = try await store.pendingBatches()
        case 2: _ = try await store.checkpoints()
        default: _ = try await syntheticStore(io: io, clock: clock).status()
        }
        #expect(!String(data: io.data()!, encoding: .utf8)!.contains("4321.234567"))
        #expect(try await store.status().deliveryGap)
        #expect(try await store.status().expiredBatches == 1)
        #expect(try await store.pendingBatches().isEmpty)
        #expect(try await store.checkpoints().first?.anchor == "anchor-1")
        #expect(try await store.commit(original).replayed)
        await #expect(throws: CaptureError.reconciliationRequired) {
            try await store.commit(syntheticBatch(id: 4, previous: "anchor-1", next: "a2", samples: [], at: clock.now()))
        }
    }
}

@Test func verifiedSystemTemporaryAliasWorksButUserDirectorySymlinkDoesNot() async throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("sleepstudio-synthetic-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let io = AtomicSnapshotFile(directory: root.appendingPathComponent("capture"))
    let store = try syntheticStore(io: io); try await store.register(scopes: [syntheticScope()])
    _ = try await store.commit(syntheticBatch())
    #expect(try await store.checkpoints().first?.anchor == "anchor-1")
    let alias = root.appendingPathComponent("supplied-alias")
    try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: io.directory)
    #expect(throws: CaptureError.localStorageError) { try AtomicSnapshotFile(directory: alias).read() }
}

@Test func corruptPendingDigestFailsClosedWithoutReturningValues() async throws {
    let io = MemorySnapshot(), store = try syntheticStore(io: io)
    try await store.register(scopes: [syntheticScope()]); _ = try await store.commit(syntheticBatch())
    let altered = String(data: io.data()!, encoding: .utf8)!.replacingOccurrences(of: "\"value\":62", with: "\"value\":63")
    io.corrupt(Data(altered.utf8))
    await #expect(throws: CaptureError.localStorageError) { try await store.pendingBatches() }
}

@Test func invalidOrOldInputNeverCommitsAndModeIsImmutable() async throws {
    let io = MemorySnapshot(), store = try syntheticStore(io: io)
    try await store.register(scopes: [syntheticScope()]); let before = io.data()
    let old = try syntheticSample(start: syntheticNow.addingTimeInterval(-91 * 86_400), end: syntheticNow.addingTimeInterval(-91 * 86_400 + 300))
    await #expect(throws: CaptureError.scopeConflict) { try await store.commit(syntheticBatch(samples: [old])) }
    #expect(io.data() == before)
    let device = WatchBatch(mode: .device, scope: try syntheticScope(), generatedAt: syntheticNow, types: try syntheticBatch().types)
    await #expect(throws: CaptureError.scopeConflict) { try await store.commit(device) }
    await #expect(throws: CaptureError.localStorageError) {
        try await LocalCaptureStore(io: io, mode: .device, gate: AlwaysAvailableGate()).status()
    }
}

@Test func lockedStoreNeverReadsSnapshotAndReopenCorruptionFailsClosed() async throws {
    let io = MemorySnapshot(), gate = TestGate(false), store = try syntheticStore(io: io, gate: gate)
    await #expect(throws: CaptureError.protectedDataUnavailable) { try await store.status() }
    await #expect(throws: CaptureError.protectedDataUnavailable) { try await store.pendingBatches() }
    #expect(io.reads() == 0)
    gate.set(true); io.corrupt(Data("invalid snapshot".utf8))
    await #expect(throws: CaptureError.localStorageError) { try await store.status() }
    #expect(io.data() == Data("invalid snapshot".utf8))
}

@Test func actualAtomicFileIsPrivateDurableAndRejectsSymlinkTargets() async throws {
    let packageRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let tempRoot = packageRoot.appendingPathComponent(".cache/test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: tempRoot, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: tempRoot) }
    let directory = tempRoot.appendingPathComponent("capture")
    let io = AtomicSnapshotFile(directory: directory)
    let store = try syntheticStore(io: io); try await store.register(scopes: [syntheticScope()])
    _ = try await store.commit(syntheticBatch())
    #expect(try await syntheticStore(io: io).checkpoints().first?.anchor == "anchor-1")
    let attributes = try FileManager.default.attributesOfItem(atPath: directory.appendingPathComponent("capture.json").path)
    #expect((attributes[.posixPermissions] as! NSNumber).intValue == 0o600)
    #expect(!FileManager.default.fileExists(atPath: directory.appendingPathComponent("capture.pending").path))
    let stale = directory.appendingPathComponent("capture.pending")
    try Data("invented-orphan-value".utf8).write(to: stale)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: stale.path)
    _ = try io.read()
    #expect(!FileManager.default.fileExists(atPath: stale.path))
    let alias = tempRoot.appendingPathComponent("alias")
    try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: directory)
    #expect(throws: CaptureError.localStorageError) { try AtomicSnapshotFile(directory: alias).read() }
    let snapshot = directory.appendingPathComponent("capture.json")
    let target = directory.appendingPathComponent("target.json")
    try FileManager.default.moveItem(at: snapshot, to: target)
    try FileManager.default.createSymbolicLink(at: snapshot, withDestinationURL: target)
    #expect(throws: CaptureError.localStorageError) { try io.read() }
}
