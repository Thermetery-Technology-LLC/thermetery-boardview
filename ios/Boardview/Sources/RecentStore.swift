import Foundation
import os

/// The "Open recent" list — mirrors android `RecentStore.kt` (contract §4
/// listRecent / openRecent / removeRecent / clearRecent; desktop parity with
/// viewer.py `_remember_board_opened` / `_get_recent` / `_clear_recent_persisted`).
///
/// Per entry, in order of preference:
///
///  1. A **security-scoped bookmark** to the original document or folder —
///     made while the picker's (open-in-place) or an in-place "Open with"
///     security scope is open. Reopening resolves it and reads the source.
///  2. An **app-private copy** under Application Support/recent/<id>/ when no
///     bookmark is possible — an "Open with" hand-off arrives as an Inbox
///     copy that the shell deletes after staging. Copies are bounded by
///     `copyBudgetBytes` in total (plus the `limit`-entry cap); the oldest
///     copy-backed entries are evicted first.
///
/// Entries are recorded only after a successful parse. The list lives in
/// Application Support/recent.json; it and the copies are excluded from
/// backup, like the remembered keys (KeyVault).
///
/// Not thread-safe: ViewController calls it only on its serial `recentQueue`.
final class RecentStore {
    static let limit = 10                               // viewer.py _RECENT_LIMIT
    static let copyBudgetBytes: Int64 = 256 * 1024 * 1024
    static let kindFile = "file"
    static let kindFolder = "folder"

    private static let log = Logger(subsystem: "com.thermetery.boardview", category: "BoardviewRecent")

    struct Entry: Codable {
        var id: String
        var name: String
        /// `kindFile` or `kindFolder` (eM-Test .asc set).
        var kind: String
        /// Security-scoped bookmark to the original, if one could be made.
        var bookmark: Data?
        /// Path of the original when it was recorded (dedupe only).
        var sourcePath: String?
        /// Name of the app-private copy under recent/<id>/, or nil.
        var copy: String?
        /// open_board meta.format ("tvw", "fz", "asc", …).
        var format: String
        /// Bytes of the parsed board (folder: sum of members). Dedupe key.
        var size: Int64
        /// Milliseconds since 1970, like Android's System.currentTimeMillis().
        var openedAt: Int64
    }

    /// Where a board being parsed came from; carried by ParseAttempt and
    /// turned into an Entry on a successful parse.
    struct Origin {
        let kind: String
        /// Bookmark made while the security scope was open (nil: none).
        var bookmark: Data?
        /// Original path for dedupe (nil for transient Inbox copies).
        var sourcePath: String?
        /// Non-nil when this is a reopen from the recent list.
        var recentId: String?
        /// True when the session file was staged from the entry's own copy.
        var fromCopy: Bool

        init(kind: String, bookmark: Data? = nil, sourcePath: String? = nil,
             recentId: String? = nil, fromCopy: Bool = false) {
            self.kind = kind
            self.bookmark = bookmark
            self.sourcePath = sourcePath
            self.recentId = recentId
            self.fromCopy = fromCopy
        }
    }

    private struct ListFile: Codable {
        var version: Int
        var items: [Entry]
    }

    private let fm = FileManager.default

    private var baseDir: URL {
        fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    }
    private var listURL: URL { baseDir.appendingPathComponent("recent.json") }
    private var copiesRoot: URL { baseDir.appendingPathComponent("recent", isDirectory: true) }

    // MARK: - Reading

    func list() -> [Entry] { load() }

    func get(_ id: String) -> Entry? { load().first { $0.id == id } }

    /// The entry's app-private copy, if it still exists.
    func copyURL(_ e: Entry) -> URL? {
        guard let name = e.copy else { return nil }
        let url = copiesRoot.appendingPathComponent(e.id, isDirectory: true)
            .appendingPathComponent(name)
        return fm.fileExists(atPath: url.path) ? url : nil
    }

    /// Resolves a bookmark; nil when the original can no longer be found.
    func resolve(_ bookmark: Data) -> (url: URL, stale: Bool)? {
        var stale = false
        guard let url = try? URL(resolvingBookmarkData: bookmark, options: [],
                                 relativeTo: nil, bookmarkDataIsStale: &stale) else {
            return nil
        }
        return (url, stale)
    }

    /// Best-effort "can this entry be reopened right now?" for the list UI.
    func isAvailable(_ e: Entry) -> Bool {
        if let bm = e.bookmark, let resolved = resolve(bm) {
            let scoped = resolved.url.startAccessingSecurityScopedResource()
            defer { if scoped { resolved.url.stopAccessingSecurityScopedResource() } }
            if fm.fileExists(atPath: resolved.url.path) { return true }
        }
        return copyURL(e) != nil
    }

    /// `{"items":[{id,name,kind,format,openedAt,available}]}` for bv.onRecent.
    func json() -> String {
        let items: [[String: Any]] = list().map { e in
            ["id": e.id, "name": e.name, "kind": e.kind, "format": e.format,
             "openedAt": e.openedAt, "available": isAvailable(e)]
        }
        let data = (try? JSONSerialization.data(withJSONObject: ["items": items])) ?? Data()
        return String(data: data, encoding: .utf8) ?? "{\"items\":[]}"
    }

    // MARK: - Writing

    /// Record a successful open. `parsed` is the session copy under
    /// caches/boards/ (a file, or an .asc directory); it is copied into
    /// recent/<id>/ only when there is no bookmark. Returns the stored
    /// entry, or nil when it could not be kept.
    @discardableResult
    func record(origin: Origin, name: String, parsed: URL, format: String) -> Entry? {
        var entries = load()
        let size = sizeOf(parsed)

        var existingIndex: Int?
        if let rid = origin.recentId {
            existingIndex = entries.firstIndex { $0.id == rid }
        }
        if existingIndex == nil, let path = origin.sourcePath {
            existingIndex = entries.firstIndex { $0.sourcePath == path }
        }
        if existingIndex == nil {
            // Same board reached by another route (picker vs. "Open with"):
            // the rows only show name + format, so two would look identical.
            existingIndex = entries.firstIndex {
                $0.kind == origin.kind && $0.name == name && $0.size == size
            }
        }
        let existing = existingIndex.map { entries[$0] }
        let id = existing?.id ?? String(UUID().uuidString.lowercased().prefix(12))

        var bookmark = origin.bookmark
        var sourcePath = origin.sourcePath
        if bookmark == nil, let old = existing, let oldBm = old.bookmark, resolve(oldBm) != nil {
            // This route had no lasting access but the old bookmark still works.
            bookmark = oldBm
            sourcePath = old.sourcePath
        }

        var copy: String?
        if bookmark == nil {
            if origin.fromCopy, let old = existing, let reuse = copyURL(old) {
                copy = reuse.lastPathComponent
            } else if size > Self.copyBudgetBytes {
                Self.log.info("Not keeping \(name, privacy: .public) in recent: over the copy budget")
                return existing
            } else if let made = makeCopy(id: id, name: name, parsed: parsed) {
                copy = made
            } else {
                return existing
            }
        }

        let entry = Entry(
            id: id, name: name, kind: origin.kind, bookmark: bookmark,
            sourcePath: sourcePath, copy: copy,
            format: format.isEmpty ? (existing?.format ?? "") : format,
            size: size, openedAt: Int64(Date().timeIntervalSince1970 * 1000))
        if let idx = existingIndex {
            entries.remove(at: idx)
            if existing?.copy != nil && copy == nil {
                try? fm.removeItem(at: copiesRoot.appendingPathComponent(id, isDirectory: true))
            }
        }
        entries.insert(entry, at: 0)

        // Count cap, then copy budget (never evicting the entry just added).
        while entries.count > Self.limit {
            drop(&entries, at: entries.count - 1)
        }
        while copiesBytes(entries) > Self.copyBudgetBytes {
            guard let victim = entries.indices.dropFirst().last(where: { entries[$0].copy != nil }) else {
                break
            }
            drop(&entries, at: victim)
        }
        save(entries)
        return entry
    }

    func remove(_ id: String) {
        var entries = load()
        guard let idx = entries.firstIndex(where: { $0.id == id }) else { return }
        drop(&entries, at: idx)
        save(entries)
    }

    /// "Clear recent": forget every entry and delete every copy.
    func clear() {
        try? fm.removeItem(at: copiesRoot)
        save([])
    }

    // MARK: - Helpers

    private func drop(_ entries: inout [Entry], at index: Int) {
        let e = entries.remove(at: index)
        try? fm.removeItem(at: copiesRoot.appendingPathComponent(e.id, isDirectory: true))
    }

    private func makeCopy(id: String, name: String, parsed: URL) -> String? {
        let dir = copiesRoot.appendingPathComponent(id, isDirectory: true)
        let fileName = name.replacingOccurrences(
            of: "[\\\\/:*?\"<>|]", with: "_", options: .regularExpression)
        let safeName = fileName.isEmpty ? "board" : fileName
        do {
            try? fm.removeItem(at: dir)
            try fm.createDirectory(at: dir, withIntermediateDirectories: true)
            excludeFromBackup(copiesRoot)
            try fm.copyItem(at: parsed, to: dir.appendingPathComponent(safeName))
            return safeName
        } catch {
            Self.log.warning("Could not keep a recent copy of \(name, privacy: .public): \(error.localizedDescription, privacy: .public)")
            try? fm.removeItem(at: dir)
            return nil
        }
    }

    private func sizeOf(_ url: URL) -> Int64 {
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: url.path, isDirectory: &isDir) else { return 0 }
        if !isDir.boolValue {
            return Int64((try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0)
        }
        var total: Int64 = 0
        if let walker = fm.enumerator(at: url, includingPropertiesForKeys: [.isRegularFileKey, .fileSizeKey]) {
            for case let child as URL in walker {
                let v = try? child.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
                if v?.isRegularFile == true { total += Int64(v?.fileSize ?? 0) }
            }
        }
        return total
    }

    private func copiesBytes(_ entries: [Entry]) -> Int64 {
        entries.reduce(Int64(0)) { sum, e in sum + (copyURL(e).map { sizeOf($0) } ?? 0) }
    }

    private func excludeFromBackup(_ url: URL) {
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var u = url
        try? u.setResourceValues(values)
    }

    private func load() -> [Entry] {
        guard let data = try? Data(contentsOf: listURL) else { return [] }
        guard let file = try? JSONDecoder().decode(ListFile.self, from: data) else {
            Self.log.warning("recent.json unreadable; starting empty")
            return []
        }
        return Array(file.items.prefix(Self.limit))
    }

    private func save(_ entries: [Entry]) {
        do {
            try fm.createDirectory(at: baseDir, withIntermediateDirectories: true)
            let data = try JSONEncoder().encode(ListFile(version: 1, items: entries))
            try data.write(to: listURL, options: .atomic)
            excludeFromBackup(listURL)
        } catch {
            Self.log.warning("Could not save recent.json: \(error.localizedDescription, privacy: .public)")
        }
    }
}
