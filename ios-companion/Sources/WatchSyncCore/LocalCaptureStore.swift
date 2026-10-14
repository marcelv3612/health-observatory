import Foundation
import CryptoKit

public protocol ProtectedDataGate: Sendable { func isAvailable() async -> Bool }
public struct AlwaysAvailableGate: ProtectedDataGate {
    public init() {}
    public func isAvailable() async -> Bool { true }
}

public struct CaptureLimits: Codable, Equatable, Sendable {
    public var pendingBatches = 512
    public var pendingOperations = 50_000
    public var pendingBytes = 16 * 1_024 * 1_024
    public var receipts = 4_096
    public var identities = 100_000
    public var scopes = 64
    public var retentionDays = 90
    public init() {}
    func validate() throws {
        guard (1...512).contains(pendingBatches), (1...50_000).contains(pendingOperations),
              (1...16 * 1_024 * 1_024).contains(pendingBytes), (1...4_096).contains(receipts),
              (1...100_000).contains(identities), (1...64).contains(scopes),
              (1...90).contains(retentionDays) else { throw CaptureError.invalidContract }
    }
}

public struct Checkpoint: Codable, Equatable, Sendable {
    public let scope: QueryScope
    public var anchor: String?
    public var lastLocalCapture: Date?
    public var lastReadState: ReadState = .unknown
}
public struct CaptureStatus: Equatable, Sendable {
    public let mode: DatasetMode
    public let lastSuccessfulLocalCapture: Date?
    public let pendingBatches: Int
    public let pendingOperations: Int
    public let destinationEnabled: Bool = false
    public let deliveryGap: Bool
    public let expiredBatches: Int
    public let lastError: CaptureError?
    public let readVisibility: String = "unknown"
}
public struct CommitReceipt: Equatable, Sendable {
    public let batchID: String
    public let replayed: Bool
    public let capturedAt: Date
}

private struct StoredReceipt: Codable, Sendable {
    var digest: String
    var capturedAt: Date
}
private struct PendingBatch: Codable, Sendable {
    var batch: WatchBatch
    var capturedAt: Date
}
private struct Snapshot: Codable, Sendable {
    var schema = 1
    var mode: DatasetMode
    var limits: CaptureLimits
    var revision = 0
    var checkpoints: [String: Checkpoint] = [:]
    var receipts: [String: StoredReceipt] = [:]
    var identities: [String: String] = [:] // UUID -> immutable scope; no sample values.
    var outbox: [PendingBatch] = []
    var lastSuccessfulLocalCapture: Date?
    var lastError: CaptureError?
    var deliveryGap = false
    var expiredBatches = 0
}

/// Actor serialization plus compare-and-replace prevents stale writers and replay races.
/// There is NO network client, recipient, credential, raw mirror, or acknowledgement API.
public actor LocalCaptureStore {
    private let io: any SnapshotIO
    private let gate: any ProtectedDataGate
    private let mode: DatasetMode
    private let limits: CaptureLimits
    private let clock: @Sendable () -> Date
    private var current: Snapshot?
    private var currentBytes: Data?

    public init(io: any SnapshotIO, mode: DatasetMode, gate: any ProtectedDataGate,
                limits: CaptureLimits = CaptureLimits(), clock: @escaping @Sendable () -> Date = { Date() }) throws {
        try limits.validate()
        self.io = io; self.mode = mode; self.gate = gate; self.limits = limits; self.clock = clock
    }

    private func loadAndPrune() async throws -> Snapshot {
        guard await gate.isAvailable() else { throw CaptureError.protectedDataUnavailable }
        do {
            let bytes = try io.read()
            if current == nil || bytes != currentBytes {
                let state = try bytes.map { try JSONDecoder().decode(Snapshot.self, from: $0) }
                    ?? Snapshot(mode: mode, limits: limits)
                try validateSnapshot(state)
                current = state; currentBytes = bytes
            }
            var state = current!
            let now = clock(), cutoff = now.addingTimeInterval(-Double(limits.retentionDays) * 86_400)
            guard now.timeIntervalSince1970.isFinite else { throw CaptureError.invalidContract }
            let before = state.outbox.count
            state.outbox.removeAll { pending in
                pending.capturedAt < cutoff || pending.batch.types.contains { query in
                    query.samples.contains { sample in
                        guard let end = WireDate.parse(sample.end) else { return true }
                        return end < cutoff
                    }
                }
            }
            if before != state.outbox.count {
                state.expiredBatches += before - state.outbox.count
                // Checkpoints survive expiry, but delivery would have a missing chain.
                // Any future transport must explicitly resolve this gap before proceeding.
                state.deliveryGap = true; try save(state)
            }
            return current!
        } catch {
            current = nil; currentBytes = nil
            throw (error as? CaptureError) ?? CaptureError.localStorageError
        }
    }

    private func validateSnapshot(_ state: Snapshot) throws {
        guard state.schema == 1, state.mode == mode, state.limits == limits, state.revision >= 0,
              state.expiredBatches >= 0, state.checkpoints.count <= limits.scopes,
              state.receipts.count <= limits.receipts, state.identities.count <= limits.identities else {
            throw CaptureError.localStorageError
        }
        for (key, checkpoint) in state.checkpoints {
            try checkpoint.scope.validate(); try Contract.anchor(checkpoint.anchor)
            guard key == checkpoint.scope.key else { throw CaptureError.localStorageError }
        }
        for (id, receipt) in state.receipts {
            try Contract.uuid(id)
            guard receipt.digest.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
                  receipt.capturedAt.timeIntervalSince1970.isFinite else {
                throw CaptureError.localStorageError
            }
        }
        for (id, key) in state.identities {
            try Contract.uuid(id); guard state.checkpoints[key] != nil else { throw CaptureError.localStorageError }
        }
        var ids = Set<String>(), chain: [String: String] = [:]
        for pending in state.outbox {
            try pending.batch.validate()
            let digest = SHA256.hash(data: try pending.batch.encoded()).map { String(format: "%02x", $0) }.joined()
            guard pending.batch.mode == mode, ids.insert(pending.batch.batchID).inserted,
                  state.receipts[pending.batch.batchID]?.digest == digest else {
                throw CaptureError.localStorageError
            }
            for query in pending.batch.types {
                let key = "\(pending.batch.streamID)|\(pending.batch.sourceBundleID)|\(query.type.rawValue)"
                guard state.checkpoints[key] != nil, [.empty, .nonempty].contains(query.readState),
                      let next = query.nextAnchor else { throw CaptureError.localStorageError }
                for id in query.samples.map(\.id) + query.deletedIDs {
                    guard state.identities[id] == key else { throw CaptureError.localStorageError }
                }
                if !state.deliveryGap, query.previousAnchor != chain[key] { throw CaptureError.localStorageError }
                chain[key] = next
            }
        }
        if !state.deliveryGap {
            for (key, checkpoint) in state.checkpoints {
                guard checkpoint.anchor == chain[key] else { throw CaptureError.localStorageError }
            }
        }
        try checkBackpressure(state)
    }

    private func save(_ value: Snapshot) throws {
        var next = value; next.revision += 1
        let bytes = try WireJSON.encode(next)
        do { try io.replace(bytes, expected: currentBytes) }
        catch {
            // A failure after rename has an uncertain result. Reload, then use the
            // durable receipt to determine replay rather than inventing rollback.
            current = nil; currentBytes = nil
            throw (error as? CaptureError) ?? CaptureError.localStorageError
        }
        current = next; currentBytes = bytes
    }

    private func checkBackpressure(_ state: Snapshot) throws {
        let operations = state.outbox.reduce(0) { sum, pending in
            sum + pending.batch.types.reduce(0) { $0 + $1.samples.count + $1.deletedIDs.count }
        }
        let bytes = try state.outbox.reduce(0) { try $0 + $1.batch.encoded().count }
        guard state.outbox.count <= limits.pendingBatches, operations <= limits.pendingOperations,
              bytes <= limits.pendingBytes else { throw CaptureError.resourceLimit }
    }

    public func register(scopes: [QueryScope]) async throws {
        var state = try await loadAndPrune()
        for scope in scopes {
            try scope.validate()
            if let existing = state.checkpoints[scope.key] {
                guard existing.scope == scope else { throw CaptureError.scopeConflict }
            } else {
                guard !state.checkpoints.values.contains(where: {
                    $0.scope.streamID == scope.streamID && ($0.scope.sourceBundleID != scope.sourceBundleID || $0.scope.startDate != scope.startDate)
                }) else { throw CaptureError.scopeConflict }
                state.checkpoints[scope.key] = Checkpoint(scope: scope, anchor: nil, lastLocalCapture: nil)
            }
        }
        guard state.checkpoints.count <= limits.scopes else { throw CaptureError.resourceLimit }
        if state.checkpoints != current!.checkpoints { try save(state) }
    }

    public func checkpoints() async throws -> [Checkpoint] {
        try await loadAndPrune().checkpoints.values.sorted { $0.scope.key < $1.scope.key }
    }

    public func commit(_ batch: WatchBatch, permit: CaptureCommitPermit? = nil) async throws -> CommitReceipt {
        let state = try await loadAndPrune()
        if let permit {
            // Consent can change while the protected-data gate is awaited. Check
            // the original generation AFTER that await and retain its lock through
            // every synchronous mutation and save; an actor Boolean is insufficient.
            return try permit.withValidConsent { try commitLoaded(batch, state: state) }
        }
        // Standalone synthetic harness commits have no coordinator consent lifecycle.
        // Every coordinator capture always supplies its original operation permit.
        guard mode == .synthetic else { throw CaptureError.cancelled }
        return try commitLoaded(batch, state: state)
    }

    private func commitLoaded(_ batch: WatchBatch, state original: Snapshot) throws -> CommitReceipt {
        var state = original
        try Task.checkCancellation()
        try batch.validate()
        guard batch.mode == mode else { throw CaptureError.scopeConflict }
        let digest = SHA256.hash(data: try batch.encoded()).map { String(format: "%02x", $0) }.joined()
        if let receipt = state.receipts[batch.batchID] {
            guard digest == receipt.digest else { throw CaptureError.replayConflict }
            return CommitReceipt(batchID: batch.batchID, replayed: true, capturedAt: receipt.capturedAt)
        }
        guard !state.deliveryGap else { throw CaptureError.reconciliationRequired }
        guard state.receipts.count < limits.receipts else { throw CaptureError.resourceLimit }
        let now = clock(), cutoff = now.addingTimeInterval(-Double(limits.retentionDays) * 86_400)
        for query in batch.types {
            let key = "\(batch.streamID)|\(batch.sourceBundleID)|\(query.type.rawValue)"
            guard var checkpoint = state.checkpoints[key] else { throw CaptureError.scopeConflict }
            guard checkpoint.anchor == query.previousAnchor,
                  (query.kind == .baseline) == (checkpoint.anchor == nil) else { throw CaptureError.staleAnchor }
            guard [.empty, .nonempty].contains(query.readState) else { throw CaptureError.readVisibilityUnknown }
            for sample in query.samples {
                guard let start = WireDate.parse(sample.start), let end = WireDate.parse(sample.end),
                      start >= checkpoint.scope.startDate, end >= cutoff,
                      state.identities[sample.id] == nil || state.identities[sample.id] == key else {
                    throw CaptureError.scopeConflict
                }
                state.identities[sample.id] = key
            }
            for id in query.deletedIDs {
                guard state.identities[id] == nil || state.identities[id] == key else { throw CaptureError.scopeConflict }
                state.identities[id] = key
            }
            checkpoint.anchor = query.nextAnchor; checkpoint.lastLocalCapture = now
            checkpoint.lastReadState = query.readState; state.checkpoints[key] = checkpoint
        }
        guard state.identities.count <= limits.identities else { throw CaptureError.resourceLimit }
        state.outbox.append(PendingBatch(batch: batch, capturedAt: now))
        try checkBackpressure(state)
        state.receipts[batch.batchID] = StoredReceipt(digest: digest, capturedAt: now)
        if state.lastSuccessfulLocalCapture == nil || state.lastSuccessfulLocalCapture! < now {
            state.lastSuccessfulLocalCapture = now
        }
        state.lastError = nil
        try Task.checkCancellation()
        try save(state)
        return CommitReceipt(batchID: batch.batchID, replayed: false, capturedAt: now)
    }

    public func recordFailure(_ code: CaptureError) async throws {
        var state = try await loadAndPrune(); state.lastError = code; try save(state)
    }

    public func pendingBatches() async throws -> [WatchBatch] { try await loadAndPrune().outbox.map(\.batch) }

    public func status() async throws -> CaptureStatus {
        let state = try await loadAndPrune()
        return CaptureStatus(mode: mode, lastSuccessfulLocalCapture: state.lastSuccessfulLocalCapture,
                             pendingBatches: state.outbox.count,
                             pendingOperations: state.outbox.reduce(0) { count, pending in
                                count + pending.batch.types.reduce(0) { $0 + $1.samples.count + $1.deletedIDs.count }
                             }, deliveryGap: state.deliveryGap, expiredBatches: state.expiredBatches, lastError: state.lastError)
    }

    public func retentionCutoff() -> Date { clock().addingTimeInterval(-Double(limits.retentionDays) * 86_400) }
}
