import Foundation

/// Kept outside iOS guards so the exact Foundation API is compiled and tested on Mac.
public enum BackupExclusion {
    public static func excludeFromBackup(_ location: URL) throws {
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var location = location
        try location.setResourceValues(values)
    }
}
