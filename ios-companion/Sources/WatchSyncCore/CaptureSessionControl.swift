import Foundation

public struct CaptureOperationToken: Equatable, Sendable {
    fileprivate let id: UUID
    fileprivate let generation: UInt64
}

/// Reserve synchronously BEFORE the first await in a MainActor UI operation.
/// Pause invalidates in-flight tokens without allowing a second operation to overlap.
@MainActor public final class CaptureSessionControl {
    private var generation: UInt64 = 0
    private var active: CaptureOperationToken?
    public private(set) var optedIn = false
    public var busy: Bool { active != nil }
    public init() {}

    public func reserveUserAction(requiresOptIn: Bool = false) -> CaptureOperationToken? {
        guard !busy, !requiresOptIn || optedIn else { return nil }
        let token = CaptureOperationToken(id: UUID(), generation: generation)
        active = token; return token
    }

    public func reservePriorOptInResume(persistedOptIn: Bool) -> CaptureOperationToken? {
        guard persistedOptIn, !busy else { return nil }
        optedIn = true
        return reserveUserAction(requiresOptIn: true)
    }

    @discardableResult public func activateOptIn(_ token: CaptureOperationToken) -> Bool {
        guard isCurrent(token) else { return false }
        optedIn = true; return true
    }

    public func isCurrent(_ token: CaptureOperationToken, requiresOptIn: Bool = false) -> Bool {
        active == token && generation == token.generation && (!requiresOptIn || optedIn)
    }

    public func pause() {
        optedIn = false; generation &+= 1
    }

    public func release(_ token: CaptureOperationToken) {
        // A paused operation still owns its reservation until it unwinds. A stale
        // release cannot clear a later operation's distinct reservation.
        if active?.id == token.id { active = nil }
    }
}
