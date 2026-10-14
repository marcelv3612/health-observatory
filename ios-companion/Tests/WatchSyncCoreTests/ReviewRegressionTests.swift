import Foundation
import Testing
@testable import WatchSyncCore

@Test func backupExclusionUsesCompiledPortableFoundationAPIForDirectoryAndFile() throws {
    let package = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let directory = package.appendingPathComponent(".cache/backup-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let file = directory.appendingPathComponent("synthetic.pending")
    try Data("invented value".utf8).write(to: file)
    for location in [directory, file] {
        try BackupExclusion.excludeFromBackup(location)
        #expect(FileManager.default.fileExists(atPath: location.path))
        // This Mac host reports false even on fresh uncached URLs after a successful
        // setter. Exercise the portable API without claiming iOS backup semantics.
        let fresh = URL(fileURLWithPath: location.path)
        _ = try fresh.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup
    }
    #expect(try Data(contentsOf: file) == Data("invented value".utf8))
    #expect(throws: (any Error).self) {
        try BackupExclusion.excludeFromBackup(directory.appendingPathComponent("nonexistent"))
    }
}

@Test func strictWireDatesRejectImpossibleCalendarTimeOffsetAndEncodedYear() throws {
    let invalid = [
        "2031-02-29T00:00:00Z", "2031-04-31T00:00:00Z", "2100-02-29T00:00:00Z",
        "2031-00-01T00:00:00Z", "2031-13-01T00:00:00Z", "2031-01-00T00:00:00Z",
        "2031-01-01T24:00:00Z", "2031-01-01T00:60:00Z", "2031-01-01T00:00:60Z",
        "2031-01-01T00:00:00+14:01", "2031-01-01T00:00:00+13:60", "2031-01-01T00:00:00-15:00",
        "1999-12-31T23:59:59Z", "2101-01-01T00:00:00Z", "٢٠٣١-01-01T00:00:00Z"
    ]
    for text in invalid { #expect(WireDate.parse(text) == nil) }
    for text in ["2000-02-29T12:34:56Z", "2032-02-29T12:34:56.123456+01:00",
                 "2000-01-01T00:00:00+14:00", "2100-12-31T23:59:59-12:00", "2031-01-01T00:00:00-13:45"] {
        #expect(WireDate.parse(text) != nil)
    }
    #expect(WireDate.parse("2032-02-29T12:34:56.123456+01:00") == WireDate.parse("2032-02-29T11:34:56.123456Z"))
    let earliest = WireDate.parse("2000-01-01T00:00:00+14:00")!
    let sample = try WatchSample(id: syntheticUUID(2), type: .heartRate, start: earliest, end: earliest,
                                 recordedTimezone: "Pacific/Kiritimati", source: syntheticSource, value: .quantity(60))
    try sample.validate(sourceBundle: syntheticBundle)
}

@Test func invalidSampleAndGeneratedAtCannotAdvanceLocalCheckpoint() async throws {
    let io = MemorySnapshot(), store = try syntheticStore(io: io)
    try await store.register(scopes: [syntheticScope()]); let before = io.data()
    for text in ["2031-02-29T00:00:00Z", "2031-03-11T24:00:00Z"] {
        var batch = try syntheticBatch()
        batch.types[0].samples[0].start = text; batch.types[0].samples[0].end = text
        await #expect(throws: CaptureError.invalidContract) { try await store.commit(batch) }
        #expect(io.data() == before)
        #expect(try await store.checkpoints().first?.anchor == nil)
    }
    for text in ["2031-02-29T00:00:00Z", "2031-03-11T24:00:00Z", "2101-01-01T00:00:00Z"] {
        var payload = try JSONSerialization.jsonObject(with: syntheticBatch().encoded()) as! [String: Any]
        payload["generated_at"] = text
        let batch = try JSONDecoder().decode(WatchBatch.self, from: JSONSerialization.data(withJSONObject: payload))
        await #expect(throws: CaptureError.invalidContract) { try await store.commit(batch) }
        #expect(io.data() == before)
        #expect(try await store.pendingBatches().isEmpty)
    }
}

@Test @MainActor func concurrentResumesReserveBeforeAvailabilityAwaitAndStartExactlyOnce() async {
    let control = CaptureSessionControl(), availability = SuspendedAvailability()
    let state = ResumeState()
    let tasks = (0..<8).map { _ in
        Task { @MainActor in
            guard let token = control.reservePriorOptInResume(persistedOptIn: state.persistedOptIn) else { return }
            defer { control.release(token) }
            state.reservations += 1
            let available = await availability.check()
            guard available, state.persistedOptIn, control.isCurrent(token, requiresOptIn: true) else { return }
            state.starts += 1
        }
    }
    for _ in 0..<100 { await Task.yield() }
    #expect(state.reservations == 1)
    #expect(control.busy)
    await availability.release()
    for task in tasks { await task.value }
    #expect(state.starts == 1)
    #expect(!control.busy)
}

@Test @MainActor func pauseDuringAvailabilityInvalidatesResumeAndCannotBeUndone() async {
    let control = CaptureSessionControl(), availability = SuspendedAvailability(), state = ResumeState()
    let task = Task { @MainActor in
        guard let token = control.reservePriorOptInResume(persistedOptIn: state.persistedOptIn) else { return }
        defer { control.release(token) }
        state.reservations += 1
        let available = await availability.check()
        guard available, state.persistedOptIn, control.isCurrent(token, requiresOptIn: true) else { return }
        state.starts += 1
    }
    for _ in 0..<100 { await Task.yield() }
    #expect(state.reservations == 1)
    state.persistedOptIn = false; control.pause()
    #expect(control.reservePriorOptInResume(persistedOptIn: state.persistedOptIn) == nil)
    await availability.release(); await task.value
    #expect(state.starts == 0)
    #expect(!control.optedIn)
    #expect(!control.busy)
}

@Test @MainActor func pauseInvalidatesEveryLaterSuspensionAndStaleReleaseCannotUnlockNewOperation() {
    let control = CaptureSessionControl()
    for _ in 0..<4 {
        let token = control.reserveUserAction()!
        #expect(control.activateOptIn(token))
        #expect(control.isCurrent(token, requiresOptIn: true))
        control.pause()
        #expect(!control.isCurrent(token, requiresOptIn: true))
        #expect(!control.activateOptIn(token))
        #expect(control.reserveUserAction() == nil)
        control.release(token)
        let fresh = control.reserveUserAction()!
        control.release(token)
        #expect(control.isCurrent(fresh))
        control.release(fresh)
    }
}

private actor SuspendedAvailability {
    private var continuation: CheckedContinuation<Bool, Never>?
    private var released = false
    func check() async -> Bool {
        await withCheckedContinuation {
            if released { $0.resume(returning: true) } else { continuation = $0 }
        }
    }
    func release() {
        released = true; continuation?.resume(returning: true); continuation = nil
    }
}
@MainActor private final class ResumeState {
    var persistedOptIn = true
    var reservations = 0
    var starts = 0
}
