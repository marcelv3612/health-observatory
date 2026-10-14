import Foundation

/// Synchronous consent generation shared by the coordinator and durable store.
/// Invalidation and a permitted synchronous save are mutually exclusive: when
/// opt-out returns, an older generation cannot begin or continue a later save.
public final class CaptureCommitFence: @unchecked Sendable {
    private let lock = NSLock()
    private var enabled = false
    private var generation = UUID()
    public init() {}

    public func setLocalOptIn(_ value: Bool) {
        lock.withLock {
            if !value || value != enabled { generation = UUID() }
            enabled = value
        }
    }

    public func acquirePermit() -> CaptureCommitPermit? {
        lock.withLock {
            guard enabled else { return nil }
            return CaptureCommitPermit(fence: self, generation: generation)
        }
    }

    fileprivate func withPermit<T>(_ expected: UUID, _ operation: () throws -> T) throws -> T {
        try lock.withLock {
            guard enabled, generation == expected else { throw CaptureError.cancelled }
            // No await can occur in this synchronous closure. The caller keeps
            // the fence through checkpoint/outbox mutation and durable replace.
            return try operation()
        }
    }
}

/// Opaque, non-persistent permit; it is never a credential or wire field.
public struct CaptureCommitPermit: Sendable {
    fileprivate let fence: CaptureCommitFence
    fileprivate let generation: UUID

    func check() throws { try fence.withPermit(generation) {} }
    func withValidConsent<T>(_ operation: () throws -> T) throws -> T {
        try fence.withPermit(generation, operation)
    }
}
