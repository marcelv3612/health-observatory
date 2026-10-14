import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Implementations must compare-and-replace atomically and never append raw records to logs.
public protocol SnapshotIO: Sendable {
    func read() throws -> Data?
    func replace(_ data: Data, expected: Data?) throws
}

/// One protected snapshot contains checkpoints, minimal receipts and the only raw outbox.
/// The fsync'ed temporary file is renamed in the same directory; there is no raw journal.
public struct AtomicSnapshotFile: SnapshotIO, Sendable {
    public let directory: URL
    public let maximumBytes: Int
    public init(directory: URL, maximumBytes: Int = 32 * 1_024 * 1_024) {
        var path = directory.standardizedFileURL.path
        // macOS and iOS expose these root-owned system aliases. Resolve only their
        // verified exact targets; arbitrary supplied/ancestor symlinks stay forbidden.
        for (alias, target) in [("/var", "/private/var"), ("/tmp", "/private/tmp")] {
            var info = stat()
            let linkTarget = try? FileManager.default.destinationOfSymbolicLink(atPath: alias)
            if path.hasPrefix(alias + "/"), lstat(alias, &info) == 0,
               (info.st_mode & S_IFMT) == S_IFLNK, info.st_uid == 0,
               (linkTarget == target || linkTarget == String(target.dropFirst())) {
                path = target + path.dropFirst(alias.count)
            }
        }
        self.directory = URL(fileURLWithPath: path); self.maximumBytes = maximumBytes
    }
    private var path: String { directory.appendingPathComponent("capture.json").path }
    private var temporary: String { directory.appendingPathComponent("capture.pending").path }

    private func ensureDirectory() throws {
        // Application Support's container ancestry must exist; create only our final directory.
        let parent = directory.deletingLastPathComponent()
        var current = parent
        while current.path != "/" {
            var info = stat()
            guard lstat(current.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR else {
                throw CaptureError.localStorageError
            }
            current.deleteLastPathComponent()
        }
        if mkdir(directory.path, 0o700) != 0, errno != EEXIST { throw CaptureError.localStorageError }
        var info = stat()
        guard lstat(directory.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR,
              info.st_uid == getuid(), (info.st_mode & 0o077) == 0 else { throw CaptureError.localStorageError }
        #if os(iOS)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: directory.path)
        try BackupExclusion.excludeFromBackup(directory)
        #endif
    }

    private func withLock<T>(_ operation: () throws -> T) throws -> T {
        try ensureDirectory()
        let lockPath = directory.appendingPathComponent("capture.lock").path
        let fd = open(lockPath, O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw CaptureError.localStorageError }
        defer { _ = close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid(),
              (info.st_mode & 0o077) == 0, info.st_nlink == 1,
              flock(fd, LOCK_EX) == 0 else { throw CaptureError.localStorageError }
        defer { _ = flock(fd, LOCK_UN) }
        return try operation()
    }

    private func readUnlocked() throws -> Data? {
        let fd = open(path, O_RDONLY | O_NOFOLLOW)
        if fd < 0, errno == ENOENT { return nil }
        guard fd >= 0 else { throw CaptureError.localStorageError }
        defer { _ = close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid(),
              (info.st_mode & 0o077) == 0, info.st_nlink == 1, info.st_size >= 0,
              info.st_size <= maximumBytes else { throw CaptureError.localStorageError }
        var data = Data(count: Int(info.st_size))
        let expectedCount = data.count
        let count = data.withUnsafeMutableBytes { buffer in
            var consumed = 0
            while consumed < expectedCount {
                let result = systemRead(fd, buffer.baseAddress!.advanced(by: consumed), expectedCount - consumed)
                if result < 0, errno == EINTR { continue }
                if result <= 0 { return -1 }
                consumed += result
            }
            return consumed
        }
        guard count == expectedCount else { throw CaptureError.localStorageError }
        return data
    }

    public func read() throws -> Data? {
        try withLock {
            // A crash before rename can leave only this one bounded, protected temporary file.
            if unlink(temporary) != 0, errno != ENOENT { throw CaptureError.localStorageError }
            return try readUnlocked()
        }
    }

    public func replace(_ data: Data, expected: Data?) throws {
        guard data.count <= maximumBytes else { throw CaptureError.resourceLimit }
        try withLock {
            guard try readUnlocked() == expected else { throw CaptureError.storageConflict }
            if unlink(temporary) != 0, errno != ENOENT { throw CaptureError.localStorageError }
            let fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
            guard fd >= 0 else { throw CaptureError.localStorageError }
            var renamed = false
            defer { _ = close(fd); if !renamed { _ = unlink(temporary) } }
            #if os(iOS)
            // Protection is set BEFORE writing any health value.
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: temporary)
            try BackupExclusion.excludeFromBackup(URL(fileURLWithPath: temporary))
            #endif
            try data.withUnsafeBytes { buffer in
                var written = 0
                while written < data.count {
                    let result = systemWrite(fd, buffer.baseAddress!.advanced(by: written), data.count - written)
                    if result < 0, errno == EINTR { continue }
                    guard result > 0 else { throw CaptureError.localStorageError }
                    written += result
                }
            }
            guard fsync(fd) == 0, rename(temporary, path) == 0 else { throw CaptureError.localStorageError }
            renamed = true
            let directoryFD = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
            guard directoryFD >= 0 else { throw CaptureError.localStorageError }
            defer { _ = close(directoryFD) }
            guard fsync(directoryFD) == 0 else { throw CaptureError.localStorageError }
        }
    }
}

private func systemRead(_ fd: Int32, _ buffer: UnsafeMutableRawPointer, _ count: Int) -> Int {
    #if canImport(Darwin)
    Darwin.read(fd, buffer, count)
    #else
    Glibc.read(fd, buffer, count)
    #endif
}
private func systemWrite(_ fd: Int32, _ buffer: UnsafeRawPointer, _ count: Int) -> Int {
    #if canImport(Darwin)
    Darwin.write(fd, buffer, count)
    #else
    Glibc.write(fd, buffer, count)
    #endif
}
